import json

from flask import Flask
import sniper.routes.api_memecoin_opportunity as opportunity_api

from sniper.routes.api_memecoin_opportunity import (
    _compact_snapshot,
    _stream_fingerprint,
    create_api_memecoin_opportunity_blueprint,
)
from sniper.research.memecoin_opportunity.paper_adapter import (
    ADAPTER_VERSION,
    FILL_MODEL_VERSION,
    PaperFillRequest,
    append_paper_fill_request,
)
from sniper.research.memecoin_opportunity.paper_lifecycle import activate_paper_lifecycle


def test_compact_snapshot_bounds_board_payload_without_losing_arm_summary():
    payload = {
        "history": [
            {"launch_id": f"h{index}", "age_since_update_s": float(50 - index),
             "facts": {"progress_bps": 1_000, "blob": "x" * 5_000}}
            for index in range(50)
        ],
        "forward_alerts": [{"state": "x" * 5_000} for _ in range(20)],
        "opportunities": [
            {
                "launch_id": str(index),
                "facts": {
                    "progress_bps": 2_000,
                    "effective_buyer_count": 5,
                    "unused": "x" * 5_000,
                },
                "clip_quotes": [
                    {
                        "clip_usd_micros": 25_000_000,
                        "status": "executable",
                        "is_remote": 1,
                        "round_trip_return_bps": -450,
                        "unused": "x" * 5_000,
                    }
                ],
            }
            for index in range(20)
        ],
        "insights": {
            "arm_comparison": {"pump-confirmation-v1": {"60000": {"samples": 62}}},
            "alert_evidence": [
                {"alert_id": str(index), "targets": [{"blob": "x" * 2_000}]}
                for index in range(50)
            ],
            "target_ladder": [{"blob": "x" * 2_000} for _ in range(50)],
            "near_misses": [{"blob": "x" * 2_000} for _ in range(20)],
        },
    }

    compact = _compact_snapshot(payload)

    # History is bounded and newest-stale first, not emptied: a launch leaves
    # `opportunities` after 15s while the worker rewrites it every ~34s, so an empty
    # history left the board with nothing to draw for most of every sweep.
    assert len(compact["history"]) == 24
    assert [row["launch_id"] for row in compact["history"][:3]] == ["h49", "h48", "h47"]
    assert compact["history"][0]["facts"] == {"progress_bps": 1_000}
    assert compact["forward_alerts"] == []
    assert compact["insights"]["arm_comparison"]["pump-confirmation-v1"]["60000"]["samples"] == 62
    assert len(compact["insights"]["alert_evidence"]) == 16
    assert "targets" not in compact["insights"]["alert_evidence"][0]
    assert compact["opportunities"][0]["facts"] == {
        "progress_bps": 2_000,
        "effective_buyer_count": 5,
    }
    assert "unused" not in compact["opportunities"][0]["clip_quotes"][0]
    assert len(json.dumps(compact)) < 150 * 1024
    assert len(payload["history"]) == 50
    assert "targets" in payload["insights"]["alert_evidence"][0]


def _app(db_path):
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_opportunity_blueprint({
        "db_path": str(db_path),
        "now_fn": lambda: "2026-07-26T12:00:00Z",
        "stale_after_s": 5,
    }))
    return app


def test_missing_database_is_explicitly_unavailable_and_never_created(tmp_path):
    db = tmp_path / "missing.db"
    response = _app(db).test_client().get("/api/memecoin/opportunity-radar")
    assert response.status_code == 200
    assert response.get_json()["status"] == "unavailable"
    assert response.get_json()["opportunities"] == []
    assert response.get_json()["insights"]["alert_evidence"] == []
    assert not db.exists()


def test_health_distinguishes_disabled_empty_database(tmp_path):
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    connect(db).close()
    response = _app(db).test_client().get("/api/memecoin/opportunity-radar/health")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "disabled"
    assert body["sources"] == []


def test_snapshot_exposes_radar_owned_realistic_entry_state(tmp_path):
    from sniper.research.memecoin_opportunity.projection import upsert_runtime_status
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    conn = connect(db)
    append_paper_fill_request(conn, PaperFillRequest(
        request_id="request-1", alert_id="alert-1", launch_id="launch-1",
        network_id="solana", venue_id="pumpfun_solana", token_id="mint-1",
        creator_id="creator-1", launch_mode="standard", state_fingerprint="state-1",
        rule_version="pump-confirmation-truth-v6", evaluator_version="alert-outcome-v4",
        adapter_version=ADAPTER_VERSION, fill_model_version=FILL_MODEL_VERSION,
        clip_usd_micros=25_000_000, decision_at="2026-07-26T12:00:00Z",
        decision_age_ms=20_000, intent_at="2026-07-26T12:00:00Z",
        expires_at="2026-07-26T12:00:30Z", local_qty_atomic=1_000,
    ))
    upsert_runtime_status(
        conn, source_key="opportunity:paper_adapter", status="healthy",
        heartbeat_at="2026-07-26T12:00:00Z", detail={},
    )
    conn.close()

    body = _app(db).test_client().get("/api/memecoin/opportunity-radar").get_json()

    assert body["paper_entry_test"] == {
        "state": "COLLECTING",
        "signals_tested": 1,
        "paper_buys": 0,
        "could_not_buy": 0,
        "awaiting_quote": 1,
        "unfilled_reasons": {},
        "latest_receipt_at": None,
    }
    assert all(row["source_key"] != "opportunity:paper_adapter"
               for row in body["source_health"])


def test_snapshot_exposes_only_the_forward_realistic_result_cohort(tmp_path):
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    conn = connect(db)
    activate_paper_lifecycle(conn, now_iso="2026-07-26T11:59:00Z")
    conn.close()

    body = _app(db).test_client().get("/api/memecoin/opportunity-radar").get_json()

    # Every cohort the ledger can hold must be keyed here. `radar-realistic-v3` was
    # missing, so activating it would have left v2's numbers sitting where the new
    # cohort belongs. An earlier version of this test asserted only two keys and so
    # could not catch the omission.
    assert set(body["paper_results"]) == {
        "radar-realistic-v2",
        "radar-realistic-v3",
        "radar-adaptive-v3",
    }
    # the unactivated realistic cohort reports itself as unactivated, not as absent
    assert body["paper_results"]["radar-realistic-v3"]["lifecycle_version"] == (
        "radar-realistic-v3"
    )
    assert body["paper_results"]["radar-realistic-v3"]["activated_at"] is None
    assert body["paper_results"]["radar-realistic-v3"]["valid_results"] == 0
    assert set(body["paper_results"]["radar-realistic-v3"]["by_venue"]) == {
        "pumpfun_solana", "pons_v1_4663",
    }
    assert body["paper_results"]["radar-realistic-v2"] == {
        "lifecycle_version": "radar-realistic-v2",
        "activated_at": "2026-07-26T11:59:00Z",
        "valid_results": 0,
        "target_first": 0,
        "stop_first": 0,
        "time_exits": 0,
        "censored": 0,
        "open": 0,
        "by_arm": {},
        "by_venue": {
            venue: {
                "valid_results": 0,
                "average_net_return_bps": None,
                "target_first": 0,
                "stop_first": 0,
                "time_exits": 0,
                "censored": 0,
                "open": 0,
            }
            for venue in (
                "pumpfun_solana",
                "pons_v1_4663",
            )
        },
        "execution_haircut_bps_per_leg": 122,
    }
    adaptive = body["paper_results"]["radar-adaptive-v3"]
    assert adaptive["lifecycle_version"] == "radar-adaptive-v3"
    assert adaptive["valid_results"] == 0
    assert "average_net_return_bps" not in adaptive
    assert set(adaptive["by_venue"]) == {
        "pumpfun_solana",
        "pons_v1_4663",
    }


def test_snapshot_marks_missing_or_degraded_entry_adapter_plainly(tmp_path):
    from sniper.research.memecoin_opportunity.projection import upsert_runtime_status
    from sniper.research.memecoin_opportunity.store import connect

    missing_db = tmp_path / "missing-status.db"
    connect(missing_db).close()
    missing = _app(missing_db).test_client().get(
        "/api/memecoin/opportunity-radar"
    ).get_json()
    assert missing["paper_entry_test"]["state"] == "NOT RUNNING"

    stale_db = tmp_path / "degraded-status.db"
    conn = connect(stale_db)
    upsert_runtime_status(
        conn, source_key="opportunity:paper_adapter", status="degraded",
        heartbeat_at="2026-07-26T12:00:00Z", detail={},
    )
    conn.close()
    stale = _app(stale_db).test_client().get(
        "/api/memecoin/opportunity-radar"
    ).get_json()
    assert stale["paper_entry_test"]["state"] == "STALE"


def test_sse_once_returns_snapshot_event_without_sixty_second_poll(tmp_path):
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    connect(db).close()
    response = _app(db).test_client().get("/api/memecoin/opportunity-radar/stream?once=1")
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    text = response.get_data(as_text=True)
    assert text.startswith("event: snapshot\n")
    data_line = next(line for line in text.splitlines() if line.startswith("data: "))
    assert json.loads(data_line[6:])["mode"] == "EXPLORATORY — EDGE NOT YET ESTABLISHED"


def test_launch_detail_404s_when_identity_is_absent(tmp_path):
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    connect(db).close()
    response = _app(db).test_client().get(
        "/api/memecoin/opportunity-radar/launch/4663/bags_robinhood_4663/0xabc"
    )
    assert response.status_code == 404


def test_stream_fingerprint_ignores_clock_only_age_changes():
    left = {
        "as_of": "2026-07-26T12:00:00Z",
        "opportunities": [{"launch_id": "a", "age_since_update_s": 1, "zone": "INNER"}],
        "source_health": [{"source_key": "source", "heartbeat_age_s": 1, "status": "healthy"}],
    }
    right = {
        "as_of": "2026-07-26T12:00:01Z",
        "opportunities": [{"launch_id": "a", "age_since_update_s": 2, "zone": "INNER"}],
        "source_health": [{"source_key": "source", "heartbeat_age_s": 2, "status": "healthy"}],
    }
    assert _stream_fingerprint(left) == _stream_fingerprint(right)
    assert len(_stream_fingerprint(left)) == 64


def test_stream_fingerprint_tracks_health_state_but_not_metric_churn():
    healthy = {
        "storage": {"status": "healthy", "hot_bytes": 10},
        "rpc": {"status": "healthy", "current_rps": 1.0, "queue_depth": 0},
    }
    counters_changed = {
        "storage": {"status": "healthy", "hot_bytes": 20},
        "rpc": {"status": "healthy", "current_rps": 0.5, "queue_depth": 7},
    }
    pressured = {
        "storage": {"status": "degraded", "blocker_code": "STORAGE_PRESSURE", "hot_bytes": 20},
        "rpc": {"status": "healthy", "current_rps": 0.5, "queue_depth": 7},
    }

    assert _stream_fingerprint(healthy) == _stream_fingerprint(counters_changed)
    assert _stream_fingerprint(healthy) != _stream_fingerprint(pressured)


def test_snapshot_separates_storage_and_rpc_health_from_market_sources(tmp_path):
    from sniper.research.memecoin_opportunity.projection import upsert_runtime_status
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    conn = connect(db)
    upsert_runtime_status(
        conn, source_key="pumpfun_solana:ws", status="healthy",
        heartbeat_at="2026-07-26T12:00:00Z", detail={},
    )
    upsert_runtime_status(
        conn, source_key="opportunity:storage", status="healthy",
        heartbeat_at="2026-07-26T12:00:00Z",
        detail={"hot_bytes": 42, "hot_hard_cap_bytes": 750},
    )
    upsert_runtime_status(
        conn, source_key="opportunity:rpc", status="healthy",
        heartbeat_at="2026-07-26T12:00:00Z",
        detail={"current_rps": 8.0, "rate_limit_events": 0},
    )
    conn.close()

    body = _app(db).test_client().get("/api/memecoin/opportunity-radar").get_json()
    assert [row["source_key"] for row in body["source_health"]] == ["pumpfun_solana:ws"]
    assert body["storage"]["hot_bytes"] == 42
    assert body["rpc"]["current_rps"] == 8.0
    assert body["insights"]["status"] == "COLLECTING — NO SCORED ALERTS YET"
    assert body["insights"]["alert_summary"]["alerts"] == 0
    assert body["insights"]["receipt"]["launches"] == 0
    assert body["stale"] is False


def test_snapshot_returns_one_result_row_per_scoreable_launchpad(tmp_path):
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    connect(db).close()

    body = _app(db).test_client().get("/api/memecoin/opportunity-radar").get_json()

    for lifecycle in body["paper_results"].values():
        # bags_robinhood_4663 retired 2026-08-19: no block for a retired venue
        assert set(lifecycle["by_venue"]) == {
            "pumpfun_solana",
            "pons_v1_4663",
        }
        assert all(
            result["valid_results"] == 0
            for result in lifecycle["by_venue"].values()
        )


def test_concurrent_pollers_share_one_bounded_snapshot(monkeypatch, tmp_path):
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    connect(db).close()
    calls = []

    def fake_snapshot(_conn, **_kwargs):
        calls.append("read")
        return {
            "status": "disabled",
            "mode": "EXPLORATORY — EDGE NOT YET ESTABLISHED",
            "stale": False,
            "opportunities": [],
            "history": [],
            "forward_alerts": [],
            "source_health": [],
            "summary": {},
        }

    monkeypatch.setattr(opportunity_api, "read_radar_snapshot", fake_snapshot)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_opportunity_blueprint({
        "db_path": str(db),
        "now_fn": lambda: "2026-07-26T12:00:00Z",
        "monotonic_fn": lambda: 100.0,
        "snapshot_cache_ttl_s": 1.0,
    }))
    client = app.test_client()

    assert client.get("/api/memecoin/opportunity-radar").status_code == 200
    assert client.get("/api/memecoin/opportunity-radar").status_code == 200
    assert calls == ["read"]


def test_route_stale_reason_describes_the_same_sources_as_the_stale_flag(tmp_path):
    """`stale` is narrowed to venue capture; `reason` must be narrowed with it."""
    from sniper.research.memecoin_opportunity.projection import upsert_runtime_status
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    conn = connect(db)
    upsert_runtime_status(
        conn, source_key="pumpfun_solana:ws", status="healthy",
        heartbeat_at="2026-07-26T12:00:00Z",
        detail={"transport_stale_after_s": 60},
    )
    # An internal worker past its bound must not become a venue-capture reason.
    upsert_runtime_status(
        conn, source_key="opportunity:bags_feed_recovery", status="healthy",
        heartbeat_at="2026-07-26T11:00:00Z", detail={},
    )
    conn.close()

    body = _app(db).test_client().get("/api/memecoin/opportunity-radar").get_json()

    assert body["stale"] is False
    assert body["stale_sources"] == []
    assert body["reason"] is None


def test_route_marks_one_stale_venue_as_degraded(tmp_path):
    from sniper.research.memecoin_opportunity.projection import upsert_runtime_status
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    conn = connect(db)
    upsert_runtime_status(
        conn, source_key="pons_v1_4663:ws", status="healthy",
        heartbeat_at="2026-07-26T06:00:00Z",
        detail={"transport_stale_after_s": 60},
    )
    upsert_runtime_status(
        conn, source_key="pumpfun_solana:ws", status="healthy",
        heartbeat_at="2026-07-26T12:00:00Z",
        detail={"transport_stale_after_s": 60},
    )
    conn.close()

    body = _app(db).test_client().get("/api/memecoin/opportunity-radar").get_json()

    assert body["status"] == "degraded"
    assert body["stale"] is False
    assert body["stale_sources"] == ["pons_v1_4663:ws"]
    assert body["reason"] == "failed paths: pons_v1_4663:ws"


def test_route_marks_all_current_required_paths_as_healthy(tmp_path):
    from sniper.research.memecoin_opportunity.projection import upsert_runtime_status
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    conn = connect(db)
    for source_key in (
        "pumpfun_solana:ws",
        "bags_robinhood_4663:ws",
        "pons_v1_4663:ws",
        "opportunity:runtime",
        "opportunity:quote:pumpfun_solana",
        "opportunity:quote:bags_robinhood_4663",
        "opportunity:quote:pons_v1_4663",
    ):
        upsert_runtime_status(
            conn,
            source_key=source_key,
            status="healthy",
            heartbeat_at="2026-07-26T12:00:00Z",
            detail={"transport_stale_after_s": 60},
        )
    conn.close()

    body = _app(db).test_client().get("/api/memecoin/opportunity-radar").get_json()

    assert body["status"] == "healthy"
    assert body["stale"] is False
    assert body["stale_sources"] == []


def test_a_retired_venues_frozen_quote_row_cannot_hold_the_status_degraded(tmp_path):
    """The retired row must be STALE for this test to mean anything.

    An earlier version gave the Bags quote a fresh heartbeat, so it passed against the
    old route logic too. Nothing refreshes a retired venue's `quote:<venue>` row after
    retirement, so a fresh one cannot occur in production: the real row froze at
    2026-08-19T13:25Z and held the dashboard at `degraded` permanently.
    """
    from sniper.research.memecoin_opportunity.projection import upsert_runtime_status
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    conn = connect(db)
    for source_key in (
        "pumpfun_solana:ws",
        "pons_v1_4663:ws",
        "opportunity:runtime",
        "opportunity:quote:pumpfun_solana",
        "opportunity:quote:pons_v1_4663",
    ):
        upsert_runtime_status(
            conn, source_key=source_key, status="healthy",
            heartbeat_at="2026-07-26T12:00:00Z",
            detail={"transport_stale_after_s": 60},
        )
    # the retired venue's quote row, FROZEN hours earlier and never refreshed again
    upsert_runtime_status(
        conn, source_key="opportunity:quote:bags_robinhood_4663", status="healthy",
        heartbeat_at="2026-07-26T06:00:00Z",
        detail={"transport_stale_after_s": 60},
    )
    conn.close()

    body = _app(db).test_client().get("/api/memecoin/opportunity-radar").get_json()

    assert body["status"] == "healthy", body.get("reason")
    assert body["stale"] is False
    assert not any("bags" in s for s in body["stale_sources"]), body["stale_sources"]
    assert "bags" not in str(body.get("reason") or "")
    # and it is absent from the displayed quote paths as well
    assert not any("bags" in k for k in (body.get("quote_paths") or {}))


def test_route_marks_a_stale_worker_as_stale(tmp_path):
    from sniper.research.memecoin_opportunity.projection import upsert_runtime_status
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    conn = connect(db)
    upsert_runtime_status(
        conn,
        source_key="pumpfun_solana:ws",
        status="healthy",
        heartbeat_at="2026-07-26T12:00:00Z",
        detail={"transport_stale_after_s": 60},
    )
    upsert_runtime_status(
        conn,
        source_key="opportunity:runtime",
        status="healthy",
        heartbeat_at="2026-07-26T11:00:00Z",
        detail={"transport_stale_after_s": 5},
    )
    conn.close()

    body = _app(db).test_client().get("/api/memecoin/opportunity-radar").get_json()

    assert body["status"] == "stale"
    assert body["stale"] is True
    assert body["stale_sources"] == ["opportunity:runtime"]


def test_route_omits_retired_bags_coverage_payload(tmp_path):
    from sniper.research.memecoin_opportunity.store import connect

    db = tmp_path / "op.db"
    connect(db).close()
    client = _app(db).test_client()

    snapshot = client.get("/api/memecoin/opportunity-radar").get_json()
    health = client.get("/api/memecoin/opportunity-radar/health").get_json()

    assert "bags_coverage" not in snapshot
    assert "bags_coverage" not in health

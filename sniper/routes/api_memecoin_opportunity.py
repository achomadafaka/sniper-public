"""Read-only API and SSE stream for the isolated opportunity Radar."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from copy import deepcopy

from flask import Blueprint, Response, jsonify, request, stream_with_context

from sniper.research.memecoin_opportunity.identity import normalize_identity
from sniper.research.memecoin_opportunity.insights import (
    connect_existing_insights,
    read_live_insights,
)
from sniper.research.memecoin_opportunity.projection import (
    paper_entry_test_summary,
    read_radar_snapshot,
)
from sniper.research.memecoin_opportunity.paper_lifecycle import (
    LIFECYCLE_VERSION as PAPER_LIFECYCLE_VERSION,
    realistic_result_summary,
)
from sniper.research.memecoin_opportunity.paper_lifecycle_v3 import (
    ADAPTIVE_ARMS,
    LIFECYCLE_V3,
    adaptive_result_summary,
)
from sniper.research.memecoin_opportunity.paper_lifecycle_realistic_v3 import (
    LIFECYCLE_VERSION as REALISTIC_V3_LIFECYCLE_VERSION,
)
from sniper.research.memecoin_opportunity.venue_policy import scoreable_venues
from sniper.research.memecoin_opportunity.store import (
    MissingOpportunityDatabase,
    OpportunitySchemaError,
    connect_existing,
)
from sniper.util.clock import real_utc_now_iso


_BOARD_FACT_FIELDS = (
    "progress_bps",
    "effective_buyer_count",
)
_BOARD_QUOTE_FIELDS = (
    "clip_usd_micros",
    "status",
    "is_remote",
    "round_trip_return_bps",
)


def _compact_row(source_item: dict[str, object]) -> dict[str, object]:
    item = dict(source_item)
    facts = item.get("facts") or {}
    item["facts"] = {key: facts[key] for key in _BOARD_FACT_FIELDS if key in facts}
    item["clip_quotes"] = [
        {key: quote[key] for key in _BOARD_QUOTE_FIELDS if key in quote}
        for quote in (item.get("clip_quotes") or [])
    ]
    return item


def _compact_snapshot(
    payload: dict[str, object], *, alert_limit: int = 16, history_limit: int = 24,
) -> dict[str, object]:
    """Keep the live board bounded; exact evidence remains on detail endpoints.

    `history` used to be emptied here. A launch leaves `opportunities` after
    `stale_after_s` (15s), while the worker rewrites a launch every ~34s, so the board
    held nothing to draw for most of every sweep: measured 2026-08-06, ten of fourteen
    polls returned zero rows against ~285 tracked launches. The freshness bound is
    correct and stays strict -- `would_fire` still requires a fresh row. What was wrong
    was throwing away every launch that was merely a few seconds old. The board now
    receives a bounded, newest-first slice to draw with reduced emphasis.
    """
    compact = {**payload, "forward_alerts": []}
    compact["opportunities"] = [
        _compact_row(item) for item in payload.get("opportunities", [])
    ]
    history = sorted(
        payload.get("history") or [],
        key=lambda item: float(item.get("age_since_update_s") or 0.0),
    )
    compact["history"] = [_compact_row(item) for item in history[:history_limit]]
    insights = dict(payload.get("insights") or {})
    evidence = []
    for row in (insights.get("alert_evidence") or [])[:alert_limit]:
        item = dict(row)
        item.pop("targets", None)
        evidence.append(item)
    insights["alert_evidence"] = evidence
    insights["target_ladder"] = []
    insights["near_misses"] = []
    compact["insights"] = insights
    return compact


# Derived from the authority registry, never hand-listed: a retired venue must vanish
# from the surface at the same moment it stops capturing, or the panel keeps showing a
# tile that can never hold a number.
RADAR_VENUES = tuple(sorted(venue.value for venue in scoreable_venues()))


def _empty_realistic_results(lifecycle_version: str) -> dict[str, object]:
    """One zeroed realistic-cohort block. Shared so the cohorts cannot drift apart."""
    return {
        "lifecycle_version": lifecycle_version,
        "activated_at": None,
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
            for venue in RADAR_VENUES
        },
    }


def _unavailable(reason: str) -> dict[str, object]:
    return {
        "status": "unavailable",
        "reason": reason,
        "mode": "EXPLORATORY — EDGE NOT YET ESTABLISHED",
        "stale": True,
        "opportunities": [],
        "history": [],
        "forward_alerts": [],
        "source_health": [],
        "storage": {},
        "rpc": {},
        "solana_quote": {},
        "quote_paths": {},
        "paper_entry_test": {
            "state": "NOT RUNNING",
            "signals_tested": 0,
            "paper_buys": 0,
            "could_not_buy": 0,
            "awaiting_quote": 0,
            "unfilled_reasons": {},
            "latest_receipt_at": None,
        },
        "paper_results": {
            PAPER_LIFECYCLE_VERSION: _empty_realistic_results(PAPER_LIFECYCLE_VERSION),
            REALISTIC_V3_LIFECYCLE_VERSION: _empty_realistic_results(
                REALISTIC_V3_LIFECYCLE_VERSION
            ),
            LIFECYCLE_V3: {
                "lifecycle_version": LIFECYCLE_V3,
                "activated_at": None,
                "valid_results": 0,
                "by_venue": {
                    venue: {
                        "valid_results": 0,
                        "average_net_return_bps": None,
                        "adaptive_exits": 0,
                        "trail_exits": 0,
                        "creator_exits": 0,
                        "stop_exits": 0,
                        "time_exits": 0,
                        "open": 0,
                    }
                    for venue in RADAR_VENUES
                },
                "by_arm": {
                    arm: {
                        "valid_results": 0,
                        "average_net_return_bps": None,
                        "adaptive_exits": 0,
                        "trail_exits": 0,
                        "creator_exits": 0,
                        "stop_exits": 0,
                        "time_exits": 0,
                        "open": 0,
                    }
                    for arm in ADAPTIVE_ARMS
                },
            },
        },
        "summary": {"active": 0, "archived": 0, "total": 0, "would_fire": 0, "zones": {}},
        "insights": {
            "status": "UNAVAILABLE",
            "degenerate": False,
            "receipt": {},
            "outcomes": {},
            "venues": [],
            "patterns": [],
            "alert_summary": {
                "alerts": 0,
                "scored": 0,
                "pending": 0,
                "unscored_legacy": 0,
                "profit_first": 0,
                "loss_first": 0,
                "other": 0,
            },
            "confirmation_microscope": {
                "status": "COLLECTING",
                "best_arm": None,
                "best_label": None,
                "arms": [],
                "paired_delay": {
                    "pairs": 0,
                    "immediate_better": 0,
                    "delayed_better": 0,
                    "ties": 0,
                    "mean_immediate_minus_delayed_bps": None,
                },
            },
            "alert_evidence": [],
            "target_ladder": [],
            "near_misses": [],
        },
    }


def _stream_fingerprint(payload: dict[str, object]) -> str:
    """Hash market truth, excluding clock-derived fields that change every poll."""
    stable = deepcopy(payload)
    stable.pop("as_of", None)
    for key in ("storage", "rpc", "solana_quote"):
        health = stable.get(key) or {}
        stable[key] = {
            field: health[field]
            for field in ("status", "blocker_code", "throttled", "coverage_status")
            if field in health
        }
    for item in stable.get("opportunities", []):
        item.pop("age_since_update_s", None)
    for item in stable.get("history", []):
        item.pop("age_since_update_s", None)
    for source in stable.get("source_health", []):
        source.pop("heartbeat_age_s", None)
        source.pop("event_age_s", None)
        source.pop("heartbeat_at", None)
    serialized = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def create_api_memecoin_opportunity_blueprint(ctx=None) -> Blueprint:
    ctx = dict(ctx or {})
    db_path = ctx.get("db_path")
    insight_db_path = ctx.get("insight_db_path")
    now_fn = ctx.get("now_fn") or real_utc_now_iso
    stale_after_s = float(ctx.get("stale_after_s", 5.0))
    sleep_fn = ctx.get("sleep_fn") or time.sleep
    monotonic_fn = ctx.get("monotonic_fn") or time.monotonic
    snapshot_cache_ttl_s = float(ctx.get("snapshot_cache_ttl_s", 1.0))
    insight_cache_ttl_s = float(ctx.get("insight_cache_ttl_s", 30.0))
    bp = Blueprint("api_memecoin_opportunity", __name__)
    insight_cache: dict[str, object] = {"loaded_at": 0.0, "payload": None}
    snapshot_cache: dict[str, object] = {"loaded_at": None, "payload": None}
    snapshot_lock = threading.Lock()

    def insight_snapshot(conn):
        now_mono = monotonic_fn()
        if (
            insight_cache["payload"] is None
            or now_mono - float(insight_cache["loaded_at"]) >= insight_cache_ttl_s
        ):
            historical = None
            if insight_db_path:
                try:
                    historical = connect_existing_insights(insight_db_path)
                except (FileNotFoundError, RuntimeError):
                    historical = None
            try:
                insight_cache["payload"] = read_live_insights(
                    conn, historical=historical
                )
            finally:
                if historical is not None:
                    historical.close()
            insight_cache["loaded_at"] = now_mono
        return insight_cache["payload"]

    def snapshot():
        now_mono = monotonic_fn()
        if (
            snapshot_cache["payload"] is not None
            and snapshot_cache["loaded_at"] is not None
            and now_mono - float(snapshot_cache["loaded_at"]) < snapshot_cache_ttl_s
        ):
            return snapshot_cache["payload"]
        with snapshot_lock:
            now_mono = monotonic_fn()
            if (
                snapshot_cache["payload"] is not None
                and snapshot_cache["loaded_at"] is not None
                and now_mono - float(snapshot_cache["loaded_at"]) < snapshot_cache_ttl_s
            ):
                return snapshot_cache["payload"]
            payload = _compact_snapshot(_load_snapshot())
            snapshot_cache["payload"] = payload
            snapshot_cache["loaded_at"] = now_mono
            return payload

    def _load_snapshot():
        try:
            conn = connect_existing(db_path)
        except (MissingOpportunityDatabase, OpportunitySchemaError) as exc:
            return _unavailable(str(exc))
        try:
            payload = read_radar_snapshot(
                conn, now_iso=now_fn(), stale_after_s=stale_after_s
            )
            payload["insights"] = insight_snapshot(conn)
            adapter_status = next(
                (
                    row for row in payload["source_health"]
                    if row["source_key"] == "opportunity:paper_adapter"
                ),
                None,
            )
            entry_test = paper_entry_test_summary(conn)
            entry_test["state"] = (
                "NOT RUNNING" if adapter_status is None
                else "COLLECTING" if (
                    adapter_status["status"] == "healthy"
                    and not adapter_status["stale"]
                )
                else "STALE"
            )
            payload["paper_entry_test"] = entry_test
            # Every cohort the ledger can hold must appear here. `radar-realistic-v3`
            # was absent, so activating it would have shown the operator v2's numbers
            # sitting where the new cohort belongs -- worse than showing nothing.
            payload["paper_results"] = {
                PAPER_LIFECYCLE_VERSION: realistic_result_summary(conn),
                REALISTIC_V3_LIFECYCLE_VERSION: realistic_result_summary(
                    conn, lifecycle_version=REALISTIC_V3_LIFECYCLE_VERSION
                ),
                LIFECYCLE_V3: adaptive_result_summary(conn),
            }
            market_sources = []
            maintenance = {}
            maintenance_health = {}
            for row in payload["source_health"]:
                if row["source_key"].startswith("opportunity:"):
                    key = row["source_key"].split(":", 1)[1]
                    try:
                        detail = json.loads(row.get("detail_json") or "{}")
                    except (TypeError, ValueError):
                        detail = {}
                    maintenance[key] = {
                        **detail,
                        "status": row["status"],
                        "blocker_code": row["blocker_code"],
                        "heartbeat_at": row["heartbeat_at"],
                    }
                    maintenance_health[key] = row
                else:
                    market_sources.append(row)
            payload["source_health"] = market_sources
            payload["storage"] = maintenance.get("storage", {})
            payload["rpc"] = maintenance.get("rpc", {})
            payload["solana_quote"] = maintenance.get("solana_quote", {})
            # Retired venues keep their old `opportunity:quote:<venue>` status rows, so
            # filter by authority: a stale row must not resurface a dead lane.
            payload["quote_paths"] = {
                key.split(":", 1)[1]: value
                for key, value in maintenance.items()
                if key.startswith("quote:") and key.split(":", 1)[1] in RADAR_VENUES
            }
            capture_sources = [
                row for row in market_sources
                if str(row["source_key"]).endswith(":ws")
            ]
            global_sources = capture_sources or market_sources
            capture_failures = [
                row for row in global_sources
                if row["stale"] or row.get("health_status") != "healthy"
            ]
            # A RETIRED venue keeps its old `quote:<venue>` row forever, and nothing
            # will refresh it, so counting it here grades the dashboard degraded
            # permanently. `quote_paths` already filtered it from the display; the
            # status computation has to agree.
            required_internal_keys = [
                key
                for key in maintenance_health
                if key == "runtime"
                or (key.startswith("quote:") and key.split(":", 1)[1] in RADAR_VENUES)
            ]
            internal_failures = [
                maintenance_health[key]
                for key in required_internal_keys
                if maintenance_health[key]["stale"]
                or maintenance_health[key].get("health_status") != "healthy"
            ]
            worker = maintenance_health.get("runtime")
            worker_stale = bool(worker and worker["stale"])
            all_capture_failed = bool(global_sources) and (
                len(capture_failures) == len(global_sources)
            )
            failed_paths = [
                str(row["source_key"])
                for row in (*capture_failures, *internal_failures)
            ]
            failed_paths = list(dict.fromkeys(failed_paths))
            if not market_sources:
                status = "disabled"
            elif all_capture_failed or worker_stale:
                status = "stale"
            elif failed_paths:
                status = "degraded"
            else:
                status = "healthy"
            payload["stale"] = status == "stale"
            payload["stale_sources"] = failed_paths
            payload["reason"] = (
                f"failed paths: {', '.join(failed_paths)}" if failed_paths else None
            )
            payload["status"] = status
            return payload
        finally:
            conn.close()

    @bp.get("/api/memecoin/opportunity-radar")
    def opportunity_radar_snapshot():
        return jsonify(snapshot())

    @bp.get("/api/memecoin/opportunity-radar/health")
    def opportunity_radar_health():
        payload = snapshot()
        return jsonify(
            {
                "status": payload["status"],
                "stale": payload["stale"],
                "as_of": payload.get("as_of"),
                "sources": payload.get("source_health", []),
                "storage": payload.get("storage", {}),
                "rpc": payload.get("rpc", {}),
                "solana_quote": payload.get("solana_quote", {}),
                "quote_paths": payload.get("quote_paths", {}),
                "reason": payload.get("reason"),
            }
        )

    @bp.get("/api/memecoin/opportunity-radar/evidence")
    def opportunity_radar_evidence():
        try:
            conn = connect_existing(db_path)
        except (MissingOpportunityDatabase, OpportunitySchemaError) as exc:
            return jsonify(_unavailable(str(exc)))
        try:
            outcomes = {
                row["outcome_kind"]: row["n"]
                for row in conn.execute(
                    "SELECT outcome_kind,count(*) AS n FROM op_competing_outcomes GROUP BY outcome_kind"
                ).fetchall()
            }
            counts = conn.execute(
                "SELECT count(DISTINCT launch_id) AS launches FROM op_launches"
            ).fetchone()
            would_fire = conn.execute(
                "SELECT count(*) FROM op_predictions WHERE would_fire=1"
            ).fetchone()[0]
            return jsonify(
                {
                    "status": "exploratory",
                    "launches": counts["launches"],
                    "would_fire": would_fire,
                    "outcomes": outcomes,
                    "insights": insight_snapshot(conn),
                    "edge_supported": False,
                }
            )
        finally:
            conn.close()

    @bp.get("/api/memecoin/opportunity-radar/launch/<network_id>/<venue_id>/<path:token_id>")
    def opportunity_radar_launch(network_id, venue_id, token_id):
        try:
            conn = connect_existing(db_path)
        except (MissingOpportunityDatabase, OpportunitySchemaError) as exc:
            return jsonify(_unavailable(str(exc))), 503
        try:
            token = normalize_identity(network_id, token_id)
            launch = conn.execute(
                "SELECT * FROM op_launches WHERE network_id=? AND venue_id=? AND token_id=?",
                (network_id, venue_id, token),
            ).fetchone()
            if launch is None:
                return jsonify({"error": "launch_not_found"}), 404
            states = [
                {**dict(row), "state": json.loads(row["state_json"]), "blocker_codes": json.loads(row["blocker_codes_json"])}
                for row in conn.execute(
                    "SELECT * FROM op_state_snapshots WHERE launch_id=? ORDER BY observed_at",
                    (launch["launch_id"],),
                ).fetchall()
            ]
            predictions = [
                {**dict(row), "reason_codes": json.loads(row["reason_codes_json"])}
                for row in conn.execute(
                    "SELECT p.* FROM op_predictions p JOIN op_state_snapshots s ON s.snapshot_id=p.snapshot_id "
                    "WHERE s.launch_id=? ORDER BY p.observed_at",
                    (launch["launch_id"],),
                ).fetchall()
            ]
            quotes = [
                dict(row)
                for row in conn.execute(
                    "SELECT q.*,s.observed_at AS state_observed_at FROM op_quote_observations q "
                    "JOIN op_state_snapshots s ON s.snapshot_id=q.snapshot_id "
                    "WHERE s.launch_id=? ORDER BY s.observed_at,q.clip_usd_micros,q.is_remote",
                    (launch["launch_id"],),
                ).fetchall()
            ]
            entries = [
                dict(row)
                for row in conn.execute(
                    "SELECT ce.*,lm.observed_event_id AS trigger_event_id FROM op_counterfactual_entries ce "
                    "JOIN op_landmark_states lm ON lm.landmark_id=ce.landmark_id "
                    "WHERE lm.launch_id=? ORDER BY ce.entry_effective_at,ce.entry_id",
                    (launch["launch_id"],),
                ).fetchall()
            ]
            observations = [
                dict(row)
                for row in conn.execute(
                    "SELECT xo.* FROM op_executable_observations xo "
                    "JOIN op_counterfactual_entries ce ON ce.entry_id=xo.entry_id "
                    "JOIN op_landmark_states lm ON lm.landmark_id=ce.landmark_id "
                    "WHERE lm.launch_id=? ORDER BY xo.observed_at,xo.observation_id",
                    (launch["launch_id"],),
                ).fetchall()
            ]
            outcomes = [
                dict(row)
                for row in conn.execute(
                    "SELECT outcome.* FROM op_competing_outcomes outcome "
                    "JOIN op_counterfactual_entries ce ON ce.entry_id=outcome.entry_id "
                    "JOIN op_landmark_states lm ON lm.landmark_id=ce.landmark_id "
                    "WHERE lm.launch_id=? ORDER BY outcome.inserted_at,outcome.outcome_id",
                    (launch["launch_id"],),
                ).fetchall()
            ]
            events = [
                dict(row)
                for row in conn.execute(
                    "SELECT ce.canonical_event_id,ce.event_kind,ce.confirmation,ce.block_or_slot,"
                    "ce.transaction_index,ce.event_index,ce.chain_timestamp,ce.received_at,"
                    "ce.decoded_at,ce.state_applied_at,re.source_key,re.source_provider,re.source_cursor "
                    "FROM op_canonical_events ce JOIN op_raw_events re USING(raw_event_id) "
                    "WHERE ce.launch_id=? ORDER BY ce.block_or_slot,COALESCE(ce.transaction_index,-1),"
                    "ce.event_index,ce.inserted_at",
                    (launch["launch_id"],),
                ).fetchall()
            ]
            source_prefix = f"{venue_id}:"
            source_health = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM op_runtime_status WHERE source_key LIKE ? ORDER BY source_key",
                    (source_prefix + "%",),
                ).fetchall()
            ]
            frontiers = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM op_source_frontiers WHERE source_key LIKE ? ORDER BY source_key",
                    (source_prefix + "%",),
                ).fetchall()
            ]
            gaps = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM op_data_gaps WHERE source_key LIKE ? ORDER BY opened_at,gap_id",
                    (source_prefix + "%",),
                ).fetchall()
            ]
            return jsonify(
                {
                    "launch": dict(launch),
                    "states": states,
                    "predictions": predictions,
                    "quotes": quotes,
                    "entries": entries,
                    "executable_observations": observations,
                    "outcomes": outcomes,
                    "events": events,
                    "source_health": source_health,
                    "frontiers": frontiers,
                    "gaps": gaps,
                }
            )
        finally:
            conn.close()

    @bp.get("/api/memecoin/opportunity-radar/stream")
    def opportunity_radar_stream():
        once = request.args.get("once") == "1"

        @stream_with_context
        def generate():
            last_fingerprint = None
            while True:
                payload = snapshot()
                fingerprint = _stream_fingerprint(payload)
                if fingerprint != last_fingerprint:
                    yield "event: snapshot\n" + "data: " + json.dumps(
                        payload, sort_keys=True, separators=(",", ":")
                    ) + "\n\n"
                    last_fingerprint = fingerprint
                else:
                    yield ": keepalive\n\n"
                if once:
                    return
                sleep_fn(0.5)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return bp

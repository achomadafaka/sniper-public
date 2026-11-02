import hashlib
import json
import sqlite3

from flask import Flask

from sniper.hyperlive import edge_desk as ED
from sniper.hyperlive import intel_store as IS
from sniper.routes import api_hyperlive as API


NOW_MS = 1_000_000


def _app():
    app = Flask(__name__)
    app.register_blueprint(API.create_api_hyperlive_blueprint("/missing-engine.sqlite"))
    return app.test_client()


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _arm_manifest(arm_id, report_path=None, report_sha=None):
    manifest = {
        "arm_id": arm_id,
        "code_sha": "a" * 40,
        "policy": {"alt_short_only": True},
        "schema_version": 2,
        "targets": ["standalone"] if arm_id.startswith("standalone") else ["0xwallet"],
        "taxonomy_version": "alt-v1",
        "benchmark_version": "basket-v1",
        "regime_version": "regime-v2",
    }
    if report_path and report_sha:
        manifest["policy"].update({"min_regime_episodes": 2, "min_episode_closes": 5})
        manifest["retrospective"] = {
            "report_path": report_path,
            "report_sha256": report_sha,
            "study_id": "study-v3",
        }
    return manifest


def _insert_arm(conn, manifest, policy_hash):
    conn.execute(
        "INSERT INTO hl_altshort_arms("
        "arm_id, activated_ts_ms, code_sha, policy_json, policy_hash, targets_json, "
        "taxonomy_version, benchmark_version, regime_version, manifest_json, created_ts_ms) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            manifest["arm_id"],
            NOW_MS - 100_000,
            manifest["code_sha"],
            json.dumps(manifest["policy"], sort_keys=True, separators=(",", ":")),
            policy_hash,
            json.dumps(manifest["targets"], sort_keys=True, separators=(",", ":")),
            manifest["taxonomy_version"],
            manifest["benchmark_version"],
            manifest["regime_version"],
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            NOW_MS - 100_000,
        ),
    )


def _seed(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    db_path = data_dir / "hl_intel.sqlite"
    conn = IS.connect(str(db_path))
    IS.ensure_schema(conn)

    report = {
        "study_id": "study-v3",
        "evidence_status": "partial_censored",
        "verdict": {
            "status": "paper_continue",
            "incomplete": ["regime_episodes"],
            "failed": [],
            "blocked": [],
        },
        "events": {
            "input_n": 200,
            "valid_n": 200,
            "mean_signal_net_bps": 148.54,
            "mean_basket_net_bps": -6.22,
            "mean_residual_bps": 154.76,
        },
        "inference": {"ci95": [70.17, 243.91], "clusters": 46},
        "regime_replication": {
            "episode_count": 1,
            "min_episode_residual_bps": 154.76,
            "leave_one_episode_out_residual_bps": None,
        },
        "exit_observation_bridge": {
            "valid_n": 200,
            "mean_signal_net_bps": 177.35,
            "mean_basket_net_bps": -2.63,
            "mean_residual_bps": 179.98,
            "limitation": "coarse_hourly_close_bridge_not_live_sampled_mark_replay",
        },
        "passive_alt_beta": {"net_bps": 1591.44},
    }
    seal = {
        "evidence_status": "partial_censored",
        "delisted_tail_status": "censored_archive_declined",
        "symbol_identity": "provider_exact_case_sensitive",
        "symbols": [
            {"provider_symbol": "A", "candles": {"status": "present"}},
            {"provider_symbol": "B", "candles": {"status": "present"}},
            {"provider_symbol": "OLD", "candles": {"status": "missing"}},
        ],
    }
    seal_path = data_dir / "research" / "altshort_study" / "cache.v4-partial" / "_cache_seal.json"
    _write_json(seal_path, seal)
    report["provenance"] = {
        "blockers": [],
        "cache_seal_sha256": hashlib.sha256(seal_path.read_bytes()).hexdigest(),
    }
    report_path = data_dir / "research" / "altshort_study" / "results" / "v3" / "report.json"
    _write_json(report_path, report)
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report_rel = "data/research/altshort_study/results/v3/report.json"

    standalone = _arm_manifest("standalone-v2", report_rel, report_sha)
    wallet_copy = _arm_manifest("wallet-copy-v2")
    _write_json(data_dir / "research" / "altshort_study" / "standalone_forward_arm.v2.json", standalone)
    _write_json(data_dir / "research" / "altshort_study" / "forward_arm.v2.json", wallet_copy)
    _insert_arm(conn, standalone, "standalone-policy")
    _insert_arm(conn, wallet_copy, "copy-policy")
    conn.execute(
        "INSERT INTO hl_altshort_signal_regimes(arm_id, decision_ts_ms, regime_json) "
        "VALUES (?,?,?)",
        (
            "standalone-v2",
            NOW_MS - 20_000,
            json.dumps({"label": "neutral", "basket_return_bps": {"259200000": 6.14}}),
        ),
    )
    conn.execute(
        "INSERT INTO hl_scorecard_snapshots("
        "ts_ms,holdout,realized_n,realized_mean,total_bps,open_n,top1_share,top5_share) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (NOW_MS - 4_000, 0, 498, 99.363, -6_391.8, 125, 0.146, 0.643),
    )
    conn.commit()
    conn.close()

    _write_json(
        data_dir / "hl_altshort_signal.heartbeat",
        {
            "ts_ms": NOW_MS - 5_000,
            "arm_id": "standalone-v2",
            "policy_hash": "standalone-policy",
            "counts": {"decisions": 0, "opened": 0, "closed": 0, "rejected": 0},
        },
    )
    _write_json(
        data_dir / "hl_copy.heartbeat",
        {
            "ts_ms": NOW_MS - 5_000,
            "arm_id": "wallet-copy-v2",
            "policy_hash": "copy-policy",
            "open": 5,
            "closed": 9,
        },
    )
    _write_json(
        data_dir / "hl_real.heartbeat",
        {
            "ts": (NOW_MS - 5_000) / 1000,
            "enabled": True,
            "dry_run": True,
            "open_n": 0,
            "pending": 0,
        },
    )
    monkeypatch.setattr(API, "INTEL_DB", str(db_path))
    monkeypatch.setattr(API, "DATA_DIR", str(data_dir))
    monkeypatch.setattr(API, "real_utc_now", lambda: NOW_MS / 1000, raising=False)
    return data_dir, db_path, report_path


def _seed_edge_paper(data_dir):
    path = data_dir / "hl_altshort_paper.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE hl_paper_arms (arm_id TEXT PRIMARY KEY, policy_hash TEXT, policy_json TEXT, "
        "taxonomy_version TEXT, benchmark_version TEXT, regime_version TEXT, clip_usd REAL, "
        "max_concurrent INTEGER, created_ts_ms INTEGER);"
        "CREATE TABLE hl_paper_observations (obs_id TEXT PRIMARY KEY, arm_id TEXT, coin TEXT, "
        "decision_ts_ms INTEGER, gated_in INTEGER, gate_reason TEXT, "
        "portfolio_slot_eligible INTEGER, entry_status TEXT, entry_reason TEXT, "
        "regime_label TEXT, regime_basket_bps REAL, regime_coverage REAL);"
        "CREATE TABLE hl_paper_regimes (arm_id TEXT, decision_ts_ms INTEGER, label TEXT, "
        "basket_return_bps REAL, coverage REAL, eligible_n INTEGER, covered_n INTEGER, "
        "created_ts_ms INTEGER, PRIMARY KEY (arm_id, decision_ts_ms));"
        "CREATE TABLE hl_paper_positions (position_id TEXT PRIMARY KEY, arm_id TEXT, obs_id TEXT, "
        "coin TEXT, status TEXT, entry_ts_ms INTEGER, exit_ts_ms INTEGER, exit_reason TEXT, "
        "net_bps_clip REAL, net_bps_ref REAL, fill_delta_bps REAL, funding_status TEXT);"
    )
    policy = {
        "runup_pct": 0.30, "runup_lookback_h": 72, "cap_hours": 24,
        "take_profit_mult": 0.85, "stop_loss_mult": 1.20,
        "funding_self_pct_max": 0.94993, "side": "short", "hedge": "none",
    }
    conn.execute(
        "INSERT INTO hl_paper_arms VALUES (?,?,?,?,?,?,?,?,?)",
        ("edge-paper-v2", "edge-policy", json.dumps(policy), "alt-v1", "naked-v1", "none-v1",
         40.0, 5, NOW_MS - 100_000),
    )
    observations = [
        ("o1", "edge-paper-v2", "kPEPE", NOW_MS - 90_000, 1, "accepted", 1, "opened", None,
         "bearish", -600.0, 0.9),
        ("o2", "edge-paper-v2", "ALT", NOW_MS - 80_000, 1, "accepted", 0, "opened", None,
         "neutral", 10.0, 0.9),
        ("o3", "edge-paper-v2", "SKIP", NOW_MS - 70_000, 0, "funding_top5pct_skip", 0,
         "stood_down", None, "neutral", 10.0, 0.9),
    ]
    conn.executemany("INSERT INTO hl_paper_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", observations)
    conn.executemany("INSERT INTO hl_paper_regimes VALUES (?,?,?,?,?,?,?,?)", [
        ("edge-paper-v2", NOW_MS - 90_000, "bearish", -600.0, 0.9, 100, 90, NOW_MS - 89_000),
        ("edge-paper-v2", NOW_MS - 80_000, "neutral", 10.0, 0.9, 100, 90, NOW_MS - 79_000),
    ])
    conn.executemany(
        "INSERT INTO hl_paper_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("p1", "edge-paper-v2", "o1", "kPEPE", "closed", NOW_MS - 90_000,
             NOW_MS - 10_000, "target", 100.0, 80.0, -12.0, "complete"),
            ("p2", "edge-paper-v2", "o2", "ALT", "closed", NOW_MS - 80_000,
             NOW_MS - 5_000, "stop", None, None, -30.0, "missing"),
        ],
    )
    conn.commit()
    conn.close()
    _write_json(data_dir / "hl_altshort_paper.heartbeat", {
        "ts_ms": NOW_MS - 2_000, "arm_id": "edge-paper-v2", "policy_hash": "edge-policy",
        "clip_usd": 40.0, "max_concurrent": 5, "universe": 174,
    })


def test_edge_desk_returns_verified_local_truth(tmp_path, monkeypatch):
    data_dir, _, _ = _seed(tmp_path, monkeypatch)
    _seed_edge_paper(data_dir)

    response = _app().get("/api/hyperlive/edge-desk")
    body = response.get_json()

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store, max-age=0"
    assert body["schema_version"] == 3
    assert body["available"] is True
    assert body["as_of_ts_ms"] == NOW_MS
    assert body["standalone"]["arm_id"] == "standalone-v2"
    assert body["standalone"]["fresh"] is True
    assert body["wallet_copy"]["arm_id"] == "wallet-copy-v2"
    assert body["real"]["dry_run"] is True
    assert body["real"]["fresh"] is True
    assert body["regime"] == {
        "available": True,
        "label": "neutral",
        "basket_72h_bps": 6.14,
        "decision_ts_ms": NOW_MS - 20_000,
    }
    assert body["study"]["integrity"] == "verified"
    assert body["study"]["events"]["mean_residual_bps"] == 154.76
    assert body["study"]["regime_replication"]["episode_count"] == 1
    assert "passive_alt_beta" not in body["study"]
    assert body["promotion_gate"] == {
        "status": "paper_continue",
        "episode_count": 1,
        "required_episodes": 2,
        "minimum_closes_per_episode": 5,
        "forward_closes": 0,
        "incomplete": ["regime_episodes"],
        "failed": [],
        "blocked": [],
    }
    assert body["positions"]["total"] == sum(body["positions"]["counts"].values())
    assert body["edge_atlas"]["available"] is True
    assert body["edge_atlas"]["focus"]["status"] == "no_history"
    assert body["edge_paper"]["fresh"] is True
    assert body["edge_paper"]["arm_id"] == "edge-paper-v2"
    assert body["edge_paper"]["counts"] == {
        "observations": 3, "accepted": 2, "stood_down": 1, "opened": 2,
        "rejected": 0, "portfolio_eligible": 1, "episode_repeats": 0,
        "open": 0, "closed": 2,
        "funding_complete": 1, "funding_missing": 1,
    }
    assert body["edge_paper"]["performance"]["clip"] == {
        "n": 1, "mean_bps": 100.0, "median_bps": 100.0, "win_rate": 1.0,
    }
    assert body["edge_paper"]["performance"]["fill_delta"]["mean_bps"] == -21.0
    assert body["edge_paper"]["milestones"] == {
        "fill_calibration_target": 25, "proof_target": 200,
        "fill_calibration_closes": 2, "complete_net_closes": 1,
        "regime_label_target": 2, "min_closes_per_regime_label": 20,
        "represented_regime_labels": 0,
    }
    assert body["edge_paper"]["regime_evidence"] == {
        "recorded_hours": 2,
        "closed_by_label": {"bearish": 1, "neutral": 1},
        "complete_net_by_label": {"bearish": 1},
    }
    assert body["evidence_debt"]["verdict"] == {
        "status": "paused",
        "label": "PROMOTION PAUSED",
        "reason": "forward_evidence_incomplete",
    }
    assert body["evidence_debt"]["collection"] == {
        "status": "running",
        "label": "FORWARD TEST RUNNING",
        "reason": "paper_collector_fresh",
    }
    assert body["evidence_debt"]["history"] == {
        "status": "blocked",
        "integrity": "verified",
        "provider_symbols": 3,
        "available_candles": 2,
        "missing_candles": 1,
        "free_archive": "unavailable",
        "evidence_status": "partial_censored",
        "delisted_tail_status": "censored_archive_declined",
        "symbol_identity": "provider_exact_case_sensitive",
    }
    assert body["evidence_debt"]["source_timing"]["historical_receive_recovery"] == "irrecoverable"
    assert body["evidence_debt"]["capacity"]["prospective_scope"] == "unknown"
    assert body["evidence_debt"]["forward"]["complete_net_closes"] == 1
    assert body["evidence_debt"]["forward"]["proof_target"] == 200
    assert body["evidence_debt"]["copy_book"] == {
        "status": "available",
        "as_of_ts_ms": NOW_MS - 4_000,
        "realized_n": 498,
        "realized_mean_bps": 99.363,
        "open_n": 125,
        "total_bps": -6_391.8,
        "top1_share": 0.146,
        "top5_share": 0.643,
    }
    assert "groups" not in body["positions"]


def test_edge_paper_missing_db_is_explicit_not_zero_filled(tmp_path):
    data_dir = tmp_path / "data"
    _write_json(data_dir / "hl_altshort_paper.heartbeat", {
        "ts_ms": NOW_MS, "arm_id": "edge-paper-v2", "policy_hash": "edge-policy",
    })

    payload = ED.read_edge_paper(data_dir=data_dir, now_ms=NOW_MS)

    assert payload == {
        "available": False, "fresh": True, "arm_id": "edge-paper-v2",
        "age_ms": 0, "reason": "database_missing",
    }


def test_edge_desk_embeds_local_edge_atlas_and_current_arm_progress(tmp_path, monkeypatch):
    _, db_path, _ = _seed(tmp_path, monkeypatch)
    conn = IS.connect(str(db_path))
    conn.execute(
        "INSERT INTO hl_follow_positions(wallet, coin, side, kind, detect_ts_ms, entry_mark, "
        "status, exit_ts_ms, exit_mark, net_bps, exit_reason) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            "0xwallet", "PEPE", "short", "entry", NOW_MS - 90_000, 1.0,
            "closed", NOW_MS - 30_000, 0.99, 100.0, "mirror_exit",
        ),
    )
    copy_id = IS.open_copy(
        conn, "0xwallet", "PEPE", "short", 1.0,
        ts_ms=NOW_MS - 80_000,
    )
    IS.record_altshort_observation(
        conn,
        arm_id="wallet-copy-v2",
        copy_id=copy_id,
        regime={"label": "neutral"},
        benchmark={},
    )
    conn.commit()
    conn.close()

    atlas = _app().get("/api/hyperlive/edge-desk").get_json()["edge_atlas"]

    assert "cells" not in atlas
    assert atlas["focus"]["status"] == "insufficient_recent"
    assert atlas["forward"]["open"] == 1
    assert atlas["forward"]["closed"] == 0
    assert atlas["next_direction"]["priority"] == "exit_replication"


def test_edge_desk_keeps_runtime_truth_when_atlas_ledger_is_missing(tmp_path, monkeypatch):
    _, db_path, _ = _seed(tmp_path, monkeypatch)
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE hl_follow_positions")
    conn.commit()
    conn.close()

    body = _app().get("/api/hyperlive/edge-desk").get_json()

    assert body["database"]["available"] is True
    assert body["wallet_copy"]["fresh"] is True
    assert body["edge_atlas"] == {"available": False, "reason": "schema_missing"}


def test_edge_desk_prefers_latest_copy_arm_manifest(tmp_path, monkeypatch):
    data_dir, db_path, _ = _seed(tmp_path, monkeypatch)
    manifest = _arm_manifest("wallet-copy-v3")
    _write_json(
        data_dir / "research" / "altshort_study" / "forward_arm.v3.json",
        manifest,
    )
    conn = IS.connect(str(db_path))
    _insert_arm(conn, manifest, "copy-policy-v3")
    conn.commit()
    conn.close()
    _write_json(
        data_dir / "hl_copy.heartbeat",
        {
            "ts_ms": NOW_MS - 5_000,
            "arm_id": "wallet-copy-v3",
            "policy_hash": "copy-policy-v3",
            "open": 2,
            "closed": 10,
        },
    )

    body = _app().get("/api/hyperlive/edge-desk").get_json()

    assert body["wallet_copy"]["arm_id"] == "wallet-copy-v3"
    assert body["wallet_copy"]["fresh"] is True


def test_edge_desk_prefers_latest_standalone_manifest(tmp_path):
    study_dir = tmp_path / "data" / "research" / "altshort_study"
    _write_json(study_dir / "standalone_forward_arm.v2.json", _arm_manifest("standalone-v2"))
    _write_json(study_dir / "standalone_forward_arm.v3.json", _arm_manifest("standalone-v3"))

    manifest, error = ED._latest_standalone_manifest(study_dir)

    assert error is None
    assert manifest["arm_id"] == "standalone-v3"


def test_edge_desk_separates_deployment_rollover_from_evidence_anchor(tmp_path, monkeypatch):
    data_dir, db_path, _ = _seed(tmp_path, monkeypatch)
    evidence = _arm_manifest("wallet-copy-v2")
    deployment = {**evidence, "arm_id": "wallet-copy-v3", "code_sha": "b" * 40}
    _write_json(data_dir / "research" / "altshort_study" / "forward_arm.v3.json", deployment)
    _write_json(data_dir / "hl_copy.heartbeat", {
        "ts_ms": NOW_MS - 5_000, "arm_id": "wallet-copy-v2",
        "evidence_arm_id": "wallet-copy-v2", "deployment_arm_id": "wallet-copy-v3",
        "policy_hash": "copy-policy", "open": 2, "closed": 10,
    })

    body = _app().get("/api/hyperlive/edge-desk").get_json()

    assert body["wallet_copy"]["fresh"] is True
    assert body["wallet_copy"]["arm_id"] == "wallet-copy-v2"
    assert body["wallet_copy"]["deployment_arm_id"] == "wallet-copy-v3"


def test_edge_desk_suppresses_numbers_when_report_sha_mismatches(tmp_path, monkeypatch):
    _, _, report_path = _seed(tmp_path, monkeypatch)
    report_path.write_text("{}", encoding="utf-8")

    study = _app().get("/api/hyperlive/edge-desk").get_json()["study"]

    assert study == {"available": False, "integrity": "failed", "reason": "sha256_mismatch"}


def test_edge_desk_marks_stale_and_malformed_heartbeats_independently(tmp_path, monkeypatch):
    data_dir, _, _ = _seed(tmp_path, monkeypatch)
    _write_json(
        data_dir / "hl_copy.heartbeat",
        {
            "ts_ms": NOW_MS - 300_001,
            "arm_id": "wallet-copy-v2",
            "policy_hash": "copy-policy",
        },
    )
    (data_dir / "hl_altshort_signal.heartbeat").write_text("not-json", encoding="utf-8")

    body = _app().get("/api/hyperlive/edge-desk").get_json()

    assert body["wallet_copy"]["fresh"] is False
    assert body["wallet_copy"]["reason"] == "heartbeat_stale_or_future"
    assert body["standalone"] == {
        "available": False,
        "fresh": False,
        "reason": "heartbeat_malformed",
    }
    assert body["study"]["integrity"] == "verified"


def test_edge_desk_preserves_real_dry_run_when_raw_host_timestamp_is_future(tmp_path, monkeypatch):
    data_dir, _, _ = _seed(tmp_path, monkeypatch)
    _write_json(
        data_dir / "hl_real.heartbeat",
        {
            "ts": (NOW_MS + 60_001) / 1000,
            "enabled": True,
            "dry_run": True,
            "open_n": 0,
            "pending": 0,
        },
    )

    real = _app().get("/api/hyperlive/edge-desk").get_json()["real"]

    assert real["enabled"] is True and real["dry_run"] is True
    assert real["fresh"] is False
    assert real["reason"] == "heartbeat_stale_or_future"


def test_edge_desk_returns_file_truth_when_database_is_missing(tmp_path, monkeypatch):
    data_dir, _, _ = _seed(tmp_path, monkeypatch)
    monkeypatch.setattr(API, "INTEL_DB", str(tmp_path / "missing.sqlite"))

    body = _app().get("/api/hyperlive/edge-desk").get_json()

    assert body["available"] is True
    assert body["database"] == {"available": False, "reason": "missing"}
    assert body["study"]["integrity"] == "verified"
    assert body["positions"]["total"] == 0
    assert body["wallet_copy"]["reason"] == "database_missing"
    assert data_dir.exists()


def test_edge_desk_reports_schema_missing_without_migrating(tmp_path, monkeypatch):
    data_dir, _, _ = _seed(tmp_path, monkeypatch)
    empty_db = tmp_path / "empty.sqlite"
    sqlite3.connect(empty_db).close()
    monkeypatch.setattr(API, "INTEL_DB", str(empty_db))
    before = empty_db.read_bytes()

    body = _app().get("/api/hyperlive/edge-desk").get_json()

    assert body["database"] == {"available": False, "reason": "schema_missing"}
    assert body["positions"]["total"] == 0
    assert empty_db.read_bytes() == before


def test_latest_regime_accepts_live_scalar_basket_return():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE hl_altshort_signal_regimes ("
        "arm_id TEXT, decision_ts_ms INTEGER, regime_json TEXT)"
    )
    conn.execute(
        "INSERT INTO hl_altshort_signal_regimes VALUES (?,?,?)",
        (
            "standalone-live",
            1_785_096_000_000,
            json.dumps({
                "label": "neutral",
                "basket_return_bps": -52.363455087625454,
                "lookback_hours": 72,
            }),
        ),
    )

    assert ED._latest_regime(conn, "standalone-live") == {
        "available": True,
        "label": "neutral",
        "basket_72h_bps": -52.363455087625454,
        "decision_ts_ms": 1_785_096_000_000,
    }

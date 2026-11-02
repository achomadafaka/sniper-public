import importlib.util
import json
import sys

import pytest

from sniper.hyperlive import intel_store as IS


def _load_reporter():
    spec = importlib.util.spec_from_file_location("hl_altshort_arm", "scripts/hl_altshort_arm.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hl_altshort_arm"] = mod
    spec.loader.exec_module(mod)
    return mod


def _setup(tmp_path):
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    manifest = {
        "arm_id": "forward-v1", "code_sha": "abc",
        "policy": {"alt_short_only": True, "cost_bps": 10, "slots": 2,
                   "initial_equity": 1000, "retrospective_exit_advantage_bps": 1},
        "targets": ["0xabc"], "taxonomy_version": "altshort-core-v1",
        "benchmark_version": "loo-equal-weight-v1", "regime_version": "causal-hourly-v1",
    }
    IS.create_altshort_arm(conn, manifest, activated_ts_ms=1000,
                           code_sha="abc", created_ts_ms=900)
    return conn


def _observe(conn, *, coin, detect, status, wallet_bps=None, mark=90, mark_ts=3000,
             benchmark_bps=None, arm_id="forward-v1"):
    copy_id = IS.open_copy(conn, "0xabc", coin, "short", 100.0, ts_ms=detect)
    IS.record_altshort_observation(
        conn, arm_id=arm_id, copy_id=copy_id,
        regime={"label": "unknown", "decision_ts_ms": detect},
        benchmark={"BBB": 100.0},
    )
    IS.update_altshort_observation_mark(conn, copy_id=copy_id, mark=mark, ts_ms=mark_ts)
    if status == "closed":
        IS.close_copy(conn, copy_id, exit_mark=mark, pnl_bps=wallet_bps, ts_ms=mark_ts)
        IS.close_altshort_observation(
            conn, copy_id=copy_id, exit_mark=mark, exit_ts_ms=mark_ts,
            benchmark={"valid": True, "basket_net_bps": benchmark_bps, "coverage": 1.0},
        )
    return copy_id


def test_report_includes_closed_open_mtm_beta_and_provenance(tmp_path):
    reporter = _load_reporter()
    conn = _setup(tmp_path)
    _observe(conn, coin="AAA", detect=2000, status="closed", wallet_bps=100,
             mark=99, mark_ts=3000, benchmark_bps=20)
    _observe(conn, coin="CCC", detect=4000, status="open", mark=90, mark_ts=4900)
    report = reporter.build_arm_report(conn, arm_id="forward-v1", now_ms=5000,
                                       max_mark_age_ms=200)
    assert report["arm"]["arm_id"] == "forward-v1"
    assert report["arm"]["activated_ts_ms"] == 1000
    assert report["arm"]["activated_utc"] == "1970-01-01T00:00:01+00:00"
    assert report["closed"]["n"] == 1 and report["closed"]["mean_bps"] == 100
    assert report["open"]["n"] == 1 and report["open"]["mtm_bps"] == pytest.approx(990)
    assert report["beta_residual"]["n"] == 1
    assert report["beta_residual"]["mean_bps"] == 80
    assert report["total_book"]["bps_sum"] == pytest.approx(1090)
    assert report["provenance"]["invalid_n"] == 0
    assert report["verdict"]["status"] == "paper_continue"  # forward sample gate not met


def test_report_marks_stale_open_positions_and_never_assumes_zero(tmp_path):
    reporter = _load_reporter()
    conn = _setup(tmp_path)
    _observe(conn, coin="AAA", detect=2000, status="open", mark=90, mark_ts=2100)
    report = reporter.build_arm_report(conn, arm_id="forward-v1", now_ms=5000,
                                       max_mark_age_ms=100)
    assert report["open"]["stale_n"] == 1
    assert report["open"]["mtm_bps"] is None
    assert report["verdict"]["status"] == "paper_continue"
    assert "stale_open_marks" in report["verdict"]["incomplete"]


def test_json_output_is_stable(tmp_path):
    reporter = _load_reporter()
    conn = _setup(tmp_path)
    _observe(conn, coin="AAA", detect=2000, status="closed", wallet_bps=100,
             mark=99, mark_ts=3000, benchmark_bps=20)
    report = reporter.build_arm_report(conn, arm_id="forward-v1", now_ms=5000)
    assert reporter.render_json(report) == reporter.render_json(report)
    json.loads(reporter.render_json(report))


def test_report_aggregates_code_only_arm_versions_by_semantic_identity(tmp_path):
    reporter = _load_reporter()
    conn = _setup(tmp_path)
    base = json.loads(conn.execute(
        "SELECT manifest_json FROM hl_altshort_arms WHERE arm_id='forward-v1'"
    ).fetchone()[0])
    base.update({"arm_id": "forward-v2", "code_sha": "def"})
    IS.create_altshort_arm(
        conn, base, activated_ts_ms=1000, code_sha="def", created_ts_ms=1000
    )
    _observe(
        conn, coin="AAA", detect=2000, status="closed", wallet_bps=100,
        mark=99, mark_ts=3000, benchmark_bps=20, arm_id="forward-v1",
    )
    _observe(
        conn, coin="CCC", detect=4000, status="open", mark=90, mark_ts=4900,
        arm_id="forward-v2",
    )

    report = reporter.build_arm_report(
        conn, arm_id="forward-v2", now_ms=5000, max_mark_age_ms=200
    )

    assert report["arm"]["compatible_arm_ids"] == ["forward-v1", "forward-v2"]
    assert report["forward_sample"]["observations"] == 2
    assert report["closed"]["n"] == 1
    assert report["open"]["n"] == 1


def test_report_excludes_closed_rows_without_valid_provenance(tmp_path):
    reporter = _load_reporter()
    conn = _setup(tmp_path)
    copy_id = _observe(conn, coin="AAA", detect=2000, status="closed", wallet_bps=100,
                       mark=99, mark_ts=3000, benchmark_bps=20)
    conn.execute(
        "UPDATE hl_altshort_observations SET provenance_status='missing_signal_funding' "
        "WHERE copy_id=?", (copy_id,)
    )
    conn.commit()

    report = reporter.build_arm_report(conn, arm_id="forward-v1", now_ms=5000)

    assert report["closed"]["n"] == 0
    assert report["provenance"]["invalid_n"] == 1

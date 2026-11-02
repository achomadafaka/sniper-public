import importlib.util
import json
import sys

import pytest

from sniper.hyperlive import intel_store as IS


def _reporter():
    spec = importlib.util.spec_from_file_location(
        "hl_altshort_signal_arm", "scripts/hl_altshort_signal_arm.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _setup(tmp_path):
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    arm = IS.create_altshort_arm(
        conn,
        {"arm_id": "standalone-v1", "policy": {"alt_short_only": True, "cost_bps": 39,
          "slots": 8, "initial_equity": 1000, "max_top_coin_share": 0.35,
          "max_top_week_share": 0.5, "min_coverage": 0.8},
         "targets": ["standalone-exhaustion"], "taxonomy_version": "alt-v1",
         "benchmark_version": "loo-v1", "regime_version": "causal-v1"},
        activated_ts_ms=1000, code_sha="abc", created_ts_ms=900,
    )
    return conn, arm


def _decision(conn, coin, ts, *, episode_id=1):
    row = IS.record_altshort_signal_decision(
        conn, arm_id="standalone-v1", coin=coin, decision_ts_ms=ts,
        decision_mark=130, runup=0.30,
        regime={"label": "bearish", "coverage": 1.0, "episode_id": episode_id},
        benchmark_entry={}, gated_in=True,
    )
    IS.open_altshort_signal(
        conn, signal_id=row["id"], entry_ts_ms=3_600_000, entry_mark=100,
        benchmark_entry={"BBB": 100},
    )
    return row


def test_report_includes_open_mtm_closed_residual_and_sample_gate(tmp_path):
    reporter = _reporter()
    conn, _ = _setup(tmp_path)
    closed = _decision(conn, "AAA", 2000)
    IS.close_altshort_signal(
        conn, signal_id=closed["id"], exit_ts_ms=7_200_000, exit_mark=90,
        exit_reason="time_cap", funding_bps=2, cost_bps=39,
        benchmark={"valid": True, "basket_net_bps": 100, "coverage": 1.0},
    )
    opened = _decision(conn, "CCC", 3000)
    conn.execute(
        "INSERT INTO hl_altshort_regime_marks("
        "ts_ms,coin,mark,open_mark,high_mark,low_mark,funding_rate) "
        "VALUES (?,?,?,?,?,?,?)",
        (3_600_000, "CCC", 100, 100, 100, 100, 0.0001),
    )
    conn.commit()
    IS.update_altshort_signal_mark(
        conn, signal_id=opened["id"], mark=95, ts_ms=7_199_900
    )

    report = reporter.build_signal_report(
        conn, arm_id="standalone-v1", now_ms=7_200_000, max_mark_age_ms=200
    )

    assert report["closed"]["n"] == 1
    assert report["closed"]["mean_bps"] == pytest.approx(1000 + 2 - 39)
    assert report["open"]["n"] == 1
    assert report["open"]["mtm_bps"] == pytest.approx(500 + 1 - 39)
    assert report["beta_residual"]["mean_bps"] == pytest.approx(1000 + 2 - 39 - 100)
    assert report["verdict"]["status"] == "paper_continue"
    assert "forward_sample" in report["verdict"]["incomplete"]


def test_report_blocks_stale_open_mark(tmp_path):
    reporter = _reporter()
    conn, _ = _setup(tmp_path)
    _decision(conn, "AAA", 2000)
    report = reporter.build_signal_report(
        conn, arm_id="standalone-v1", now_ms=8_000_000, max_mark_age_ms=100
    )
    assert report["open"]["stale_n"] == 1
    assert report["open"]["mtm_bps"] is None
    assert "stale_open_marks" in report["verdict"]["incomplete"]


def test_report_blocks_open_position_with_missing_funding(tmp_path):
    reporter = _reporter()
    conn, _ = _setup(tmp_path)
    opened = _decision(conn, "AAA", 2000)
    IS.update_altshort_signal_mark(
        conn, signal_id=opened["id"], mark=95, ts_ms=7_199_900
    )

    report = reporter.build_signal_report(
        conn, arm_id="standalone-v1", now_ms=7_200_000, max_mark_age_ms=200
    )

    assert report["open"]["funding_complete_n"] == 0
    assert report["open"]["mtm_bps"] is None
    assert report["portfolio"] == {"valid": False, "reason": "missing_open_funding"}
    assert "missing_open_funding" in report["verdict"]["incomplete"]


def test_report_calls_frozen_verdict_after_sample_gate(monkeypatch, tmp_path):
    reporter = _reporter()
    conn, _ = _setup(tmp_path)
    policy = json.loads(conn.execute(
        "SELECT policy_json FROM hl_altshort_arms WHERE arm_id='standalone-v1'"
    ).fetchone()[0])
    policy.update({
        "min_closed": 0, "min_entry_days": 0,
        "min_regime_episodes": 0, "min_episode_closes": 0,
    })
    conn.execute(
        "UPDATE hl_altshort_arms SET policy_json=? WHERE arm_id='standalone-v1'",
        (json.dumps(policy),),
    )
    conn.commit()
    called = {}

    def fake_verdict(evidence, **kwargs):
        called["evidence"] = evidence
        called["kwargs"] = kwargs
        return {"status": "paper_reject", "blocked": [], "incomplete": [], "failed": []}

    monkeypatch.setattr(reporter, "altshort_verdict", fake_verdict)
    report = reporter.build_signal_report(conn, arm_id="standalone-v1", now_ms=5000)

    assert called["kwargs"]["min_day_clusters"] == 0
    assert report["verdict"]["status"] == "paper_reject"


def test_report_passes_two_episode_replication_evidence_to_verdict(monkeypatch, tmp_path):
    reporter = _reporter()
    conn, _ = _setup(tmp_path)
    policy = json.loads(conn.execute(
        "SELECT policy_json FROM hl_altshort_arms WHERE arm_id='standalone-v1'"
    ).fetchone()[0])
    policy.update({
        "min_closed": 0, "min_entry_days": 0,
        "min_regime_episodes": 2, "min_episode_closes": 5,
    })
    conn.execute(
        "UPDATE hl_altshort_arms SET policy_json=? WHERE arm_id='standalone-v1'",
        (json.dumps(policy),),
    )
    for index in range(10):
        row = _decision(
            conn, f"C{index}", 2_000 + index,
            episode_id=1 if index < 5 else 2,
        )
        IS.close_altshort_signal(
            conn, signal_id=row["id"], exit_ts_ms=7_200_000, exit_mark=90,
            exit_reason="time_cap", funding_bps=0, cost_bps=39,
            benchmark={"valid": True, "basket_net_bps": 100, "coverage": 1.0},
        )
    called = {}

    def fake_verdict(evidence, **kwargs):
        called["evidence"] = evidence
        called["kwargs"] = kwargs
        return {"status": "paper_promote", "blocked": [], "incomplete": [], "failed": []}

    monkeypatch.setattr(reporter, "altshort_verdict", fake_verdict)
    report = reporter.build_signal_report(conn, arm_id="standalone-v1", now_ms=8_000_000)

    assert report["regime_replication"]["episode_count"] == 2
    assert report["regime_replication"]["min_episode_closes"] == 5
    assert report["regime_replication"]["min_episode_residual_bps"] > 0
    assert report["regime_replication"]["leave_one_episode_out_residual_bps"] > 0
    assert called["evidence"]["regime_episode_count"] == 2
    assert called["kwargs"]["min_regime_episodes"] == 2
    assert called["kwargs"]["min_episode_closes"] == 5


def test_report_json_is_stable(tmp_path):
    reporter = _reporter()
    conn, _ = _setup(tmp_path)
    report = reporter.build_signal_report(conn, arm_id="standalone-v1", now_ms=5000)
    assert reporter.render_json(report) == reporter.render_json(report)
    json.loads(reporter.render_json(report))

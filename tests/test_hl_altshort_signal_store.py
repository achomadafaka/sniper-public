import json

import pytest

from sniper.hyperlive import intel_store as IS


def _conn(tmp_path):
    conn = IS.connect(str(tmp_path / "hl.sqlite"))
    IS.ensure_schema(conn)
    IS.create_altshort_arm(
        conn,
        {"arm_id": "standalone-v1", "policy": {"alt_short_only": True, "cost_bps": 39},
         "targets": ["standalone-exhaustion"], "taxonomy_version": "alt-v1",
         "benchmark_version": "loo-v1", "regime_version": "causal-v1"},
        activated_ts_ms=1_000, code_sha="abc", created_ts_ms=900,
    )
    return conn


def _decision(conn, *, ts=3_600_000, gated=True):
    return IS.record_altshort_signal_decision(
        conn, arm_id="standalone-v1", coin="AAA", decision_ts_ms=ts,
        decision_mark=130.0, runup=0.30,
        regime={"label": "bearish" if gated else "neutral", "coverage": 1.0},
        benchmark_entry={"BBB": 100.0, "CCC": 200.0}, gated_in=gated,
    )


def test_signal_decision_requires_epoch_and_is_immutable(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError, match="pre_arm_epoch"):
        _decision(conn, ts=999)
    first = _decision(conn)
    replay = _decision(conn)
    assert replay == first
    assert first["status"] == "pending"
    with pytest.raises(ValueError, match="signal decision conflict"):
        IS.record_altshort_signal_decision(
            conn, arm_id="standalone-v1", coin="AAA", decision_ts_ms=3_600_000,
            decision_mark=131.0, runup=0.31, regime={"label": "bearish"},
            benchmark_entry={"BBB": 100.0}, gated_in=True,
        )


def test_neutral_crossing_is_logged_as_stood_down(tmp_path):
    conn = _conn(tmp_path)
    row = _decision(conn, gated=False)
    assert row["status"] == "stood_down"
    assert json.loads(row["regime_json"])["label"] == "neutral"


def test_signal_position_enters_marks_and_closes_costed(tmp_path):
    conn = _conn(tmp_path)
    row = _decision(conn)
    IS.open_altshort_signal(
        conn, signal_id=row["id"], entry_ts_ms=7_200_000, entry_mark=120.0,
        benchmark_entry={"BBB": 99.0, "CCC": 198.0},
    )
    assert json.loads(IS.get_altshort_signal(conn, signal_id=row["id"])[
        "benchmark_entry_json"
    ]) == {"BBB": 99.0, "CCC": 198.0}
    IS.update_altshort_signal_mark(conn, signal_id=row["id"], mark=110.0,
                                   ts_ms=10_800_000)
    IS.close_altshort_signal(
        conn, signal_id=row["id"], exit_ts_ms=36_000_000, exit_mark=100.0,
        exit_reason="time_cap", funding_bps=2.0,
        benchmark={"valid": True, "basket_net_bps": 50.0}, cost_bps=39.0,
    )
    closed = IS.get_altshort_signal(conn, signal_id=row["id"])
    assert closed["status"] == "closed"
    assert closed["gross_bps"] == pytest.approx((120 - 100) / 120 * 10_000)
    assert closed["net_bps"] == pytest.approx(closed["gross_bps"] + 2 - 39)
    assert closed["funding_pnl_bps"] == 2.0
    assert closed["funding_provenance_status"] == "complete"
    assert closed["latest_mark"] == 100.0


def test_signal_state_transition_guards(tmp_path):
    conn = _conn(tmp_path)
    row = _decision(conn)
    with pytest.raises(ValueError, match="entry_not_next_hour"):
        IS.open_altshort_signal(conn, signal_id=row["id"], entry_ts_ms=3_600_000,
                                entry_mark=120.0)
    IS.reject_altshort_signal(conn, signal_id=row["id"], reason="slot_capacity",
                              ts_ms=7_200_000)
    with pytest.raises(ValueError, match="signal_not_pending"):
        IS.open_altshort_signal(conn, signal_id=row["id"], entry_ts_ms=7_200_000,
                                entry_mark=120.0)


def test_hourly_regime_observation_requires_epoch_and_is_immutable(tmp_path):
    conn = _conn(tmp_path)
    regime = {"label": "bearish", "coverage": 0.9, "basket_return_bps": -600}
    with pytest.raises(ValueError, match="pre_arm_epoch"):
        IS.record_altshort_signal_regime(
            conn, arm_id="standalone-v1", decision_ts_ms=999, regime=regime
        )
    first = IS.record_altshort_signal_regime(
        conn, arm_id="standalone-v1", decision_ts_ms=3_600_000, regime=regime
    )
    replay = IS.record_altshort_signal_regime(
        conn, arm_id="standalone-v1", decision_ts_ms=3_600_000, regime=regime
    )
    assert replay == first
    assert json.loads(first["regime_json"])["label"] == "bearish"
    with pytest.raises(ValueError, match="signal regime conflict"):
        IS.record_altshort_signal_regime(
            conn, arm_id="standalone-v1", decision_ts_ms=3_600_000,
            regime={"label": "neutral", "coverage": 0.9},
        )
    assert [row["decision_ts_ms"] for row in IS.list_altshort_signal_regimes(
        conn, arm_id="standalone-v1"
    )] == [3_600_000]


def test_regime_marks_and_signal_decisions_preserve_provider_symbol_case(tmp_path):
    conn = _conn(tmp_path)
    IS.record_altshort_regime_marks(
        conn,
        {"kPEPE": 0.01, "KAS": 0.02},
        funding_rates={"kPEPE": 0.0001, "KAS": 0.0002},
        ts_ms=3_600_100,
    )
    bars = IS.load_altshort_regime_bars(
        conn, start_ts_ms=3_600_000, end_ts_ms=3_600_000
    )
    assert set(bars) == {"kPEPE", "KAS"}

    row = IS.record_altshort_signal_decision(
        conn, arm_id="standalone-v1", coin="kPEPE", decision_ts_ms=3_600_000,
        decision_mark=0.01, runup=0.30, regime={"label": "bearish"},
        benchmark_entry={"KAS": 0.02}, gated_in=True,
    )
    assert row["coin"] == "kPEPE"

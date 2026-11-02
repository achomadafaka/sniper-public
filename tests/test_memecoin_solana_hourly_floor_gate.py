"""Gate wiring for the Solana hourly floor (operator spec
docs/superpowers/specs/2026-09-17-solana-hourly-floor-activity-design.md).

Only a Solana `turnover_under_floor` / `under_abs_floor` refusal runs the review. S1 (prior feed buys)
can satisfy the hourly dollar floor through the pure evaluator. S2 (one-page address activity) can grant
`h1_floor_activity_exception` only with a reviewed cutoff; an unset cutoff records evidence only. Every
other refusal family survives a high activity count, and the review never runs for it.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.services import memecoin_solana_address_activity as A

MINT = "B5ANwoWkptDv1QszdPHXjCF2gTWdo27FGLffdfXzzoe6"
SRC = "5" * 88
T = "2026-09-16T07:45:21.226799+00:00"


def _decide(vol_h1, mcap, **kw):
    args = dict(abs_floor=3000.0, turnover_floor=0.01, signal_usd=698.6)
    args.update(kw)
    return R.volume_alive_decision(vol_h1, mcap, **args)


def test_the_pure_evaluator_uses_the_dollar_bound_on_both_hourly_legs_only():
    assert _decide(22.08, 79_675.44)[2] == "turnover_under_floor"
    assert _decide(22.08, 79_675.44, h1_floor_lower_bound=1200.0)[0] is True
    assert _decide(22.08, 79_675.44, h1_floor_lower_bound=500.0)[2] == "turnover_under_floor"
    assert _decide(100.0, None, h1_floor_lower_bound=3500.0)[0] is True
    assert _decide(100.0, None, h1_floor_lower_bound=2000.0)[2] == "under_abs_floor"
    assert R.volume_alive_decision(10000, 1000, abs_floor=0, turnover_floor=0, vol_m5=900, m5_floor=.02,
                                   m5_ceiling=.5, h1_floor_lower_bound=1e9)[2] == "m5_turnover_over_ceiling"
    assert R.volume_alive_decision(10000, 100_000, abs_floor=0, turnover_floor=0, vol_m5=10, m5_floor=.1,
                                   h1_floor_lower_bound=1e9)[2] == "m5_turnover_under_floor"
    assert _decide(None, 79_675.44, h1_floor_lower_bound=0.0)[2] == "no_volume_data"
    # Never the sum: the same trades can be in both. $600 provider and $500 feed stay under $797.
    assert _decide(600.0, 79_675.44, signal_usd=0.0, h1_floor_lower_bound=500.0)[2] == "turnover_under_floor"


class _Probe:
    def __init__(self, result="exception", count=950):
        self.calls = []
        self.result, self.count = result, count

    def probe(self, mint, *, decision_ts, source_signature):
        self.calls.append((mint, decision_ts, source_signature))
        return {"method_version": A.METHOD_VERSION, "address_type": "mint", "anchor": source_signature,
                "cutoff": 400 if self.result in ("exception", "below_cutoff") else None,
                "call_count": 1, "elapsed_ms": 210.0, "result_type": self.result,
                "exception_used": self.result == "exception", "budget_state": "ok",
                "valid_signature_count": self.count, "failed_count": 40, "missing_time_count": 0,
                "page_saturated": True, "oldest_block_time": 1_758_000_000, "returned_count": 1000}


@pytest.fixture
def gate(monkeypatch):
    for name, value in {
        "MEMECOIN_REAL_MIN_VOLUME_H1_USD": 3000.0, "MEMECOIN_REAL_MIN_TURNOVER_H1": 0.01,
        "MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED": True,
        "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD": 2_000_000.0,
        "MEMECOIN_REAL_STRICT_VOLUME_SOURCE_IDS": frozenset(), "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED": True,
        "MEMECOIN_REAL_MIN_TXNS_M5": 0, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO": 0.0,
        "MEMECOIN_REAL_MAX_SELL_SHARE_PCT": 0.0,
    }.items():
        monkeypatch.setattr(R.cfg, name, value, raising=False)
    monkeypatch.setattr(R.cfg, "inband_min_turnover_m5_for_chain", lambda chain: 0.0)
    monkeypatch.setattr(R.cfg, "inband_max_turnover_m5_for", lambda *args: 0.0)
    monkeypatch.setattr(R, "_alert_entry_result", lambda *args, **kwargs: None)
    probe = _Probe()
    monkeypatch.setattr(A, "runtime_probe", lambda: (probe, None))
    return probe


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "Tekkerrss")
    return conn


def _buy(conn, sig_char, usd, at="2026-09-16T07:30:00.000Z", fid=None):
    conn.execute(
        "INSERT INTO memecoin_events(fomo_event_id, source_user_id, source_handle, mint, side, their_size_usd, "
        "created_at, received_at_normalized, network_id) VALUES(?,?,?,?,?,?,?,?,?)",
        (fid or "swap:raybot:%s:w2:%s" % (sig_char * 88, MINT), "u2", "w2", MINT, "buy", usd, at,
         "2026-09-16T07:30:00.500000+00:00", "solana"))
    conn.commit()


def _hook(conn, monkeypatch, *, exemptions=(), stats=None, origin=None, signal="follow_activity", decision_at=None):
    monkeypatch.setattr(m, "entry_gate_exemptions", lambda *a, **k: frozenset(exemptions))
    reached = []
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: reached.append(True) or {"status": "test_reached"})
    event = SimpleNamespace(
        mint=MINT, ticker="POKEMON", source_user_id="u1", source_handle="Tekkerrss", network_id="solana",
        side="buy", fomo_event_id=origin or "swap:raybot:%s:u1:%s" % (SRC, MINT), created_at=T,
        their_price=8.524864677105648e-05, their_size_usd=698.6, signal_source=signal, is_first_buy=True)
    market = {"price": event.their_price, "market_cap": 79704, "volume_h1": 22.08, "volume_m5": None,
              "liquidity": 26573.83, "pair_address": "PairSelected111", "dex_id": "pumpswap"}
    market.update(stats or {})
    result = R.real_entry_hook(
        conn, event, {"solana": {}}, now_fn=lambda: T, trade_size=175, max_age_s=120,
        decision_at=decision_at,
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={"effective_mcap": 79_675.43995664542, "assumed_micro": False,
                                "consensus_n_60m": 1, "market_stats": market})
    return result, reached


def _evidence(conn):
    trace = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])
    return next((e for e in trace["substage_events"] if e.get("name") == "solana_hourly_floor_evidence"), None)


def test_feed_buys_over_the_floor_admit_without_an_rpc_call(gate, monkeypatch):
    conn = _conn()
    _buy(conn, "A", 900.0)
    result, reached = _hook(conn, monkeypatch)
    assert result == {"status": "test_reached"} and reached == [True]
    assert gate.calls == []
    ev = _evidence(conn)
    assert (ev["final_gate_decision"], ev["final_reason"], ev["feed_buy_usd"]) == (
        "admit", "h1_floor_met_by_feed_buys", 900.0)
    assert (ev["activity_result"], ev["activity_call_count"]) == ("not_run", 0)
    assert ev["selected_provider_pool"] == "PairSelected111" and ev["evidence_version"] == 1


def test_every_record_names_the_identity_kind_of_each_counted_row(gate, monkeypatch):
    conn = _conn()
    uuid_id = "swap:3ac25ded-177d-4bac-af84-45a07e00bbbc"
    _buy(conn, None, 100.0, fid=uuid_id)
    _buy(conn, "A", 50.0, at="2026-09-16T07:20:00.000Z")
    _hook(conn, monkeypatch)                                   # $150 < floor: the S2 path runs
    ev = _evidence(conn)
    assert (ev["feed_buy_usd"], ev["feed_eligible_count"], ev["final_reason"]) == (
        150.0, 2, "h1_floor_activity_exception")
    assert sorted((r["identity_kind"], r["identity"]) for r in ev["feed_rows"]) == [
        ("signature", "A" * 88), ("trade_id", uuid_id)]


def test_an_activity_count_at_the_cutoff_grants_the_named_exception(gate, monkeypatch):
    conn = _conn()
    _buy(conn, "A", 100.0)
    result, reached = _hook(conn, monkeypatch)
    assert result == {"status": "test_reached"}
    from datetime import datetime
    assert gate.calls == [(MINT, int(datetime.fromisoformat(T).timestamp()), SRC)]
    ev = _evidence(conn)
    assert (ev["final_gate_decision"], ev["final_reason"], ev["activity_exception_used"]) == (
        "admit", "h1_floor_activity_exception", True)
    assert (ev["activity_valid_signature_count"], ev["activity_page_saturated"], ev["feed_buy_usd"]) == (950, True, 100.0)
    assert "activity_valid_signature_count" in ev and "feed_buy_usd" in ev            # counts and dollars apart


@pytest.mark.parametrize("result_type", ["no_cutoff", "below_cutoff", "unknown"])
def test_no_cutoff_below_cutoff_or_unknown_keeps_the_refusal_and_records_the_evidence(gate, monkeypatch, result_type):
    gate.result = result_type
    conn = _conn()
    result, reached = _hook(conn, monkeypatch)
    assert result["status"] == "rejected" and "turnover_under_floor" in result["reason"] and reached == []
    ev = _evidence(conn)
    assert (ev["final_gate_decision"], ev["activity_result"]) == ("refuse", result_type)


@pytest.mark.parametrize("stats,monkeys,reason", [
    ({"volume_m5": 22.08}, {"inband_min_turnover_m5_for_chain": lambda chain: 0.1}, "m5_turnover_under_floor"),
    ({"volume_m5": 500_000.0}, {"inband_max_turnover_m5_for": lambda *a: 0.5}, "m5_turnover_over_ceiling"),
    ({"txns_m5_buys": 0, "txns_m5_sells": 0},
     {"MEMECOIN_REAL_MIN_TXNS_M5": 2, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD": 50_000.0,
      "MEMECOIN_REAL_MIN_TURNOVER_M5": 0.0, "MEMECOIN_REAL_MAX_TURNOVER_M5": 0.0}, "m5_txns_under_floor"),
])
def test_every_non_hourly_refusal_survives_a_high_activity_count(gate, monkeypatch, stats, monkeys, reason):
    for name, value in monkeys.items():
        monkeypatch.setattr(R.cfg, name, value, raising=False)
    monkeypatch.setattr(m, "solana_prior_feed_buys", lambda *a, **k: pytest.fail("no feed read"))
    conn = _conn()
    result, reached = _hook(conn, monkeypatch, stats=stats)
    assert result["status"] == "rejected" and reason in result["reason"] and reached == []
    assert gate.calls == [] and _evidence(conn) is None


def test_a_manual_policy_exemption_does_not_run_the_review(gate, monkeypatch):
    monkeypatch.setattr(m, "solana_prior_feed_buys", lambda *a, **k: pytest.fail("no feed read"))
    conn = _conn()
    result, _ = _hook(conn, monkeypatch, exemptions=("turnover_under_floor",))
    assert result == {"status": "test_reached"} and gate.calls == [] and _evidence(conn) is None


def test_no_source_signature_is_recorded_as_unknown_with_no_call(gate, monkeypatch):
    conn = _conn()
    result, _ = _hook(conn, monkeypatch, origin="swap:3ac25ded-177d-4bac-af84-45a07e00bbbc")
    assert result["status"] == "rejected" and gate.calls == []
    ev = _evidence(conn)
    assert (ev["activity_result"], ev["activity_budget_state"], ev["source_signature"]) == (
        "unknown", "no_source_signature", None)


def test_a_disabled_probe_keeps_s1_active_and_records_the_reason(gate, monkeypatch):
    monkeypatch.setattr(A, "runtime_probe", lambda: (None, "no_endpoint"))
    conn = _conn()
    _buy(conn, "A", 100.0)
    result, _ = _hook(conn, monkeypatch)
    assert result["status"] == "rejected"
    ev = _evidence(conn)
    assert (ev["activity_result"], ev["activity_budget_state"], ev["feed_buy_usd"]) == ("disabled", "no_endpoint", 100.0)


def test_a_review_failure_leaves_the_provider_refusal_standing(gate, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("feed store unreadable")
    monkeypatch.setattr(m, "solana_prior_feed_buys", boom)
    conn = _conn()
    result, reached = _hook(conn, monkeypatch)
    assert result["status"] == "rejected" and "turnover_under_floor" in result["reason"] and reached == []


def test_the_review_is_solana_only():
    out = R.solana_hourly_floor_review(
        None, network_id="4663", mint="0xabc", origin_event_id="onchain:x", attempt_id=None, decision_at=T,
        stats={}, vol_h1=1.0, mcap=1000.0, decision_kwargs=dict(abs_floor=0, turnover_floor=0.01),
        why="turnover_under_floor", need=10.0, effective_ceiling=0.0, source_created_at=T)
    assert out is None


def test_a_late_response_from_the_real_probe_never_changes_the_decision(gate, monkeypatch):
    import threading
    import time
    finished = threading.Event()

    def slow_post(url, body, timeout):
        time.sleep(1.0)
        finished.set()
        rows = [{"signature": "%03d" % i + "B" * 85, "slot": 1, "err": None, "blockTime": 1789544700,
                 "confirmationStatus": "confirmed"} for i in range(1, 10)]
        return 200, {"result": rows}
    probe = A.ActivityProbe("https://research.example/k", 1, post=slow_post)
    monkeypatch.setattr(A, "runtime_probe", lambda: (probe, None))
    conn = _conn()
    t0 = time.monotonic()
    result, reached = _hook(conn, monkeypatch)
    assert time.monotonic() - t0 < 1.0                         # the decision did not wait for the page
    assert result["status"] == "rejected" and reached == []
    ev = _evidence(conn)
    assert (ev["activity_result"], ev["activity_budget_state"], ev["activity_call_count"]) == ("unknown", "timeout", 1)
    finished.wait(2)
    assert result["status"] == "rejected" and _evidence(conn)["activity_result"] == "unknown"


def test_the_calibration_study_counts_with_the_live_count_function():
    import importlib.util
    import pathlib
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "research" / "solana_hourly_activity_cutoff_study.py"
    spec = importlib.util.spec_from_file_location("cutoff_study", path)
    study = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(study)
    assert study.count_page is A.count_page
    assert study.source_signature is A.source_signature


def test_the_window_ends_at_the_frozen_decision_time_not_a_later_hook_sample(gate, monkeypatch):
    """Spec: rows must be received before T, the FROZEN entry-decision time. The hook samples the clock
    again after resolution work; a buy received between the two samples is not prior evidence."""
    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_events(fomo_event_id, source_user_id, source_handle, mint, side, their_size_usd, "
        "created_at, received_at_normalized, network_id) VALUES(?,?,?,?,?,?,?,?,?)",
        ("swap:raybot:%s:w2:%s" % ("A" * 88, MINT), "u2", "w2", MINT, "buy", 5000.0,
         "2026-09-16T07:45:21.000Z", "2026-09-16T07:45:22.000000+00:00", "solana"))   # received 0.8 s after T
    conn.commit()
    samples = iter(["2026-09-16T07:45:21.226799+00:00"] + ["2026-09-16T07:45:26.000000+00:00"] * 50)
    monkeypatch.setattr(m, "entry_gate_exemptions", lambda *a, **k: frozenset())
    monkeypatch.setattr(R, "real_entry", lambda *a, **k: {"status": "test_reached"})
    event = SimpleNamespace(
        mint=MINT, ticker="POKEMON", source_user_id="u1", source_handle="Tekkerrss", network_id="solana",
        side="buy", fomo_event_id="swap:raybot:%s:u1:%s" % (SRC, MINT), created_at=T,
        their_price=8.524864677105648e-05, their_size_usd=698.6, signal_source="follow_activity", is_first_buy=True)
    gate.result = "no_cutoff"
    result = R.real_entry_hook(
        conn, event, {"solana": {}}, now_fn=lambda: next(samples), trade_size=175, max_age_s=120,
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={"effective_mcap": 79_675.43995664542, "assumed_micro": False, "consensus_n_60m": 1,
                                "market_stats": {"price": event.their_price, "market_cap": 79704, "volume_h1": 22.08,
                                                 "volume_m5": None, "liquidity": 26573.83}})
    assert result["status"] == "rejected"
    ev = _evidence(conn)
    assert ev["decision_time"] == T and ev["feed_late_receipt_count"] == 1 and ev["feed_buy_usd"] is None


def test_micro_refusal_text_matches_the_recorded_gate_input(gate, monkeypatch):
    monkeypatch.setattr(R.cfg, "micro_min_volume_m5_usd_for", lambda *args: 5000.0)
    monkeypatch.setattr(R.cfg, "provider_index_lag_s", lambda chain: 90.0)
    conn = _conn()
    result, reached = _hook(conn, monkeypatch, stats={"volume_m5": 4000.0}, decision_at=T)
    assert not reached
    assert "volume $4000 under floor $5000" in result["reason"]
    trace = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])
    evidence = next(e for e in trace["substage_events"] if e["name"] == "volume_gate")
    assert evidence["tested_volume_m5"] == 4000.0
    assert evidence["threshold"] == 5000.0


def test_waived_m5_refusal_preserves_the_hourly_gate_value(gate, monkeypatch):
    monkeypatch.setattr(R.cfg, "provider_index_lag_s", lambda chain: 90.0)
    monkeypatch.setattr(R.cfg, "micro_min_volume_m5_usd_for", lambda *args: 5000.0)
    monkeypatch.setattr(R, "solana_hourly_floor_review", lambda *args, **kwargs: None)
    conn = _conn()
    result, reached = _hook(
        conn, monkeypatch, exemptions=("m5_volume_under_abs_floor",),
        stats={"volume_m5": 4000.0, "volume_h1": 1000.0}, decision_at=T)
    assert not reached
    assert result["reason"] == (
        "h1 organic volume $301 under floor $797 (turnover_under_floor)")

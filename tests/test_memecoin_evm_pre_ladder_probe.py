"""Measurement probe for the EVM pre-ladder stretch (2026-09-27, Alpha GO). Behaviour unchanged.

On funded 4663 fills since 09-20 the trace gap dex_pair_selection -> chase_first_seen is p50 188 ms,
while everything that stretch does (classify_funded_entry p50 6.4 ms, first_seen_buy_price and
wallet_chase_cap sub-ms, no provider call on any of the 55 fills) is a few ms when timed alone. The
probe splits the stretch: a timed `SELECT 1` on the funded connection at its start (a long one says
the connection was busy with another thread), then marks before and after the classification.

SOLANA TOO since 2026-09-28 (Alpha GO): J7 Solana's reservation stage is p50 288 ms with no network
call in it. Two more hook marks (before the volume gate, before the leg) and three in the leg
(its start, before the preflight, before reserve_entry) split it. A substage's `duration_ms`
counts from the previous substage, so the substage after a probe now reads shorter; compare
across deploys on `elapsed_ms`.
"""
import time
from types import SimpleNamespace

import pytest

import sniper.services.memecoin_volume_writer  # noqa: F401  the hook imports it lazily
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


def _run(monkeypatch, network_id, mint):
    from test_memecoin_entry_identity import FakeClock, NOW, _arm, _conn, _event
    _arm(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS", False, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS_EVM", False, raising=False)
    marks = []
    monkeypatch.setattr(R, "real_entry", lambda conn, ev, **kw: {"status": "rejected",
                                                                "reason": "stub"})
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    trace = SimpleNamespace(
        attempt_id="attempt-probe", mark=lambda *a, **k: True, finish=lambda *a, **k: True,
        mark_substage=lambda name, **kw: marks.append({"name": name, **kw}) or True)
    ctx = {"usdc_fn": lambda: 300.0, "native_fn": lambda: 1.0, "sol_fn": lambda: 1.0,
           "token_safety_fn": lambda mint, **kw: (True, "ok"),
           "execute_fn": lambda *a, **k: {"status": "unknown"}, "kill_path": None}
    R.real_entry_hook(_conn(), _event(network_id=network_id, mint=mint), {network_id: ctx},
                      now_fn=lambda: NOW, trade_size=10.0, max_age_s=120,
                      stats_fn=lambda mint, network_id=None: {}, trace=trace,
                      trace_clock=FakeClock())
    return [m["name"] for m in marks], {m["name"]: m for m in marks}


def test_the_evm_stretch_is_split_in_order_with_a_timed_connection_probe(monkeypatch):
    names, by = _run(monkeypatch, "4663", "0xabc")
    # dex_pair_selection is marked only when the market data carries a pair; this stub has none.
    order = ["probe_conn_select1", "probe_pre_classify", "probe_classified", "chase_first_seen"]
    assert [n for n in names if n in order] == order, names
    assert 0.0 <= by["probe_conn_select1"]["duration_ms"] < 50.0


def test_the_solana_stretch_is_split_in_order_too(monkeypatch):
    names, by = _run(monkeypatch, "solana", "So1Mint1111111111111111111111111111111111111")
    order = ["probe_pre_source_floor", "probe_conn_select1", "probe_pre_classify",
             "probe_classified", "probe_pre_volume", "probe_pre_leg"]
    assert [n for n in names if n.startswith("probe_")] == order, names
    assert names.index("probe_pre_source_floor") < names.index("source_buy_floor"), names
    assert 0.0 <= by["probe_conn_select1"]["duration_ms"] < 50.0


def test_the_hook_marks_the_shell_row_write_first(monkeypatch):
    """A trace the hook starts itself: its first substage is `attempt_row`, whose elapsed is the
    committed shell-row INSERT (the trace clock starts before it)."""
    from test_memecoin_entry_identity import NOW, _arm, _conn, _event
    _arm(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_EARLY_GATE_READS", False, raising=False)
    monkeypatch.setattr(R, "real_entry", lambda conn, ev, **kw: {"status": "rejected",
                                                                "reason": "stub"})
    monkeypatch.setattr(R, "_alert_entry_result", lambda *a, **k: None)
    conn = _conn()
    ctx = {"usdc_fn": lambda: 300.0, "sol_fn": lambda: 1.0,
           "token_safety_fn": lambda mint, **kw: (True, "ok"),
           "execute_fn": lambda *a, **k: {"status": "unknown"}, "kill_path": None}
    R.real_entry_hook(conn, _event(network_id="solana", mint="So1Mint1111111111111111111111111111111111111"),
                      {"solana": ctx}, now_fn=lambda: NOW, trade_size=10.0, max_age_s=120,
                      stats_fn=lambda mint, network_id=None: {})
    import json
    trace = json.loads(conn.execute("SELECT trace_json FROM memecoin_entry_attempts").fetchone()[0])
    names = [e["name"] for e in trace["substage_events"]]
    assert names[0] == "attempt_row", names


def test_the_solana_leg_marks_its_start_preflight_and_reservation(monkeypatch, tmp_path):
    from test_memecoin_entry_drift import _conn
    from sniper.services import memecoin_arming
    from sniper.services.memecoin_real_leg import real_entry
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    events = []
    trace = SimpleNamespace(
        attempt_id="attempt-leg-probe",
        mark=lambda stage, *a, **k: events.append(("stage", stage)) or True,
        mark_substage=lambda name, **kw: events.append(("sub", name)) or True)
    ev = SimpleNamespace(fomo_event_id="e1", source_user_id="trader", source_handle="alice",
                         signal_source="global_feed", network_id="solana", mint="MINT",
                         ticker="M", side="buy", their_price=0.02, their_market_cap=1e6,
                         created_at="2026-07-15T11:59:59+00:00")
    real_entry(_conn(), ev, day="2026-07-15", usdc_balance=300.0, sol_balance=1.0,
               now_iso="2026-07-15T12:00:00+00:00", trade_size=10.0,
               token_safety_fn=lambda _m: (False, "stub"),
               execute_fn=lambda *a, **k: pytest.fail("no send in this test"),
               kill_path=str(tmp_path / "kill"), trace=trace)
    probes = [name for kind, name in events if kind == "sub" and name.startswith("probe_")]
    assert probes == ["probe_leg_start", "probe_pre_preflight", "probe_pre_reserve"], events
    assert events.index(("sub", "probe_pre_reserve")) < events.index(("stage", "reservation"))


def test_a_probe_that_raises_never_reaches_the_entry():
    from sniper.services.memecoin_real_leg import _probe
    _probe(SimpleNamespace(mark_substage=lambda name: 1 / 0), "probe_leg_start")
    _probe(None, "probe_leg_start")


def test_the_m5_gate_read_has_its_own_mark_before_the_tape_shadow(monkeypatch):
    # 2026-10-14: chase_tape_move_shadow's 5-11 ms window held this gate read; the mark separates them.
    names, _by = _run(monkeypatch, "4663", "0xabc")
    assert "onchain_m5_read" in names, names
    assert names.index("probe_pre_volume") < names.index("onchain_m5_read"), names
    if "chase_tape_move_shadow" in names:
        assert names.index("onchain_m5_read") < names.index("chase_tape_move_shadow"), names

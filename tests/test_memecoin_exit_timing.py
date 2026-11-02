"""Sell-speed measurement for real exits.

`execute_exit` receives ONE frozen `now_iso` and reuses it for the claim, the pre-broadcast
write-ahead and the finalization, so `closed_at - close_submitted_at` read 0.00s on all five funded
closes on 2026-08-13. The column pair cannot measure anything. Entries carry stage traces in
`memecoin_entry_attempts`; exits carried nothing at all.
"""
import json
import sqlite3

import pytest

from sniper.execution.exit_timing import ExitTiming
from sniper.persistence import db_memecoin as m


def _clock(seq):
    box = {"n": 0}

    def clk():
        v = seq[min(box["n"], len(seq) - 1)]
        box["n"] += 1
        return v
    return clk


def test_it_separates_the_latency_that_costs_money_from_post_broadcast_patience():
    """quote/sign/submit happen BEFORE the sell is broadcast. confirm happens after the transaction
    is already on-chain and costs nothing but waiting — counting it as sell latency would make a
    fast sell look slow."""
    t = ExitTiming(clock=_clock([0.0, 0.30, 0.72, 0.83, 1.55]))
    t.rung()
    t.stage("quote"); t.stage("sign"); t.stage("submit"); t.stage("confirm")
    d = json.loads(t.to_json())
    assert d["stages_ms"] == {"quote": 300.0, "sign": 420.0, "submit": 110.0, "confirm": 720.0}
    assert d["pre_broadcast_ms"] == 830.0          # confirm excluded
    assert d["total_ms"] == 1550.0
    assert d["ladder_rungs"] == 1


def test_ladder_retries_accumulate_instead_of_overwriting():
    """The slippage ladder re-quotes at 3% -> 10% -> 25%. Reporting only the last rung would hide
    the cost of the retries, which is exactly the latency an operator is asking about."""
    t = ExitTiming(clock=_clock([0.0, 0.10, 0.35, 0.50]))
    t.rung(); t.stage("quote")
    t.rung(); t.stage("quote")
    d = json.loads(t.to_json())
    assert d["stages_ms"]["quote"] == pytest.approx(350.0)
    assert d["ladder_rungs"] == 2


def test_a_measurement_failure_never_reaches_the_sell():
    """A broken clock must not raise into the exit path — a sell is money, the timing is not."""
    def boom():
        raise RuntimeError("clock exploded")
    t = ExitTiming(clock=boom)                      # raises inside __init__'s first read? no: stored
    t.stage("quote")                                # swallowed
    assert t.to_json() is None                      # and reports nothing rather than a wrong number


def test_record_exit_timing_is_best_effort_and_never_raises():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_positions)")}
    assert "exit_timing_json" in cols
    assert m.record_exit_timing(conn, 1, None) is False          # nothing to write
    conn.execute("INSERT INTO memecoin_positions (id, mint, entry_price) VALUES (1,'M',1.0)")
    conn.commit()
    assert m.record_exit_timing(conn, 1, '{"total_ms":5}') is True
    got = conn.execute("SELECT exit_timing_json FROM memecoin_positions WHERE id=1").fetchone()[0]
    assert json.loads(got)["total_ms"] == 5


def test_the_executor_records_timing_on_a_real_close():
    """The wiring, not the arithmetic: a closed exit must persist its measurement."""
    import inspect
    from sniper.execution import solana_exit_executor as ex
    src = inspect.getsource(ex)
    assert "ExitTiming()" in src
    assert 'timing.stage("quote")' in src and 'timing.stage("sign")' in src
    assert 'timing.stage("submit")' in src and 'timing.stage("confirm")' in src
    assert "m.record_exit_timing(conn, pid, timing.to_json())" in src
    # and it must not write a wall-clock fact — now_iso stays the ledger's only timestamp
    assert "ExitTiming(now_iso" not in src


def test_an_unreadable_clock_cannot_break_a_completed_sell():
    """total_ms() returns None when the clock failed, and round(None) raises. A measurement must
    never turn a sell that already happened into an exception on the way out."""
    import inspect
    from sniper.execution import solana_exit_executor as ex
    src = inspect.getsource(ex)
    assert "round(timing.total_ms(), 1)" not in src
    assert "None if _total is None else round(_total, 1)" in src

    def boom():
        raise RuntimeError("clock exploded")
    t = ExitTiming(clock=boom)
    assert t.total_ms() is None
    assert t.pre_broadcast_ms() == 0        # no stages recorded, not a crash


def test_the_evm_exit_is_instrumented_the_same_way():
    """4663 is where the RPC rate limiting lives, so sell speed there is the half most worth
    measuring. The EVM exit executor is a separate file and had no timing at all."""
    import inspect
    from sniper.execution import evm_exit_executor as ex
    src = inspect.getsource(ex)
    assert "ExitTiming()" in src
    for stage in ("quote", "sign", "submit", "confirm"):
        assert f'timing.stage("{stage}")' in src, stage
    assert "m.record_exit_timing(conn, pid, timing.to_json())" in src
    assert "None if _total is None else round(_total, 1)" in src   # same None guard as solana


def test_only_the_full_close_ladder_is_instrumented_not_the_partial():
    """execute_partial_exit_evm is a different decision with its own ladder; instrumenting it under
    the same position column would blend two measurements into one field."""
    import inspect
    from sniper.execution import evm_exit_executor as ex
    src = inspect.getsource(ex)
    partial = src[src.index("def execute_partial_exit_evm"):]
    assert "timing." not in partial

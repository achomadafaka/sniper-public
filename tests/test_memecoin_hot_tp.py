"""Rail wake: trigger semantics, cache hook, anchoring from the sweep's exact quote, wake path.

Spec: docs/superpowers/specs/2026-08-22-memecoin-hot-tp-lane-design.md (hot TP lane), extended
2026-09-27 to every price rail (operator order: funded exits must see price changes at sub-second
speed). A mark estimates the executable price as E x q/q0 against the last exact quote E.
"""
import sqlite3
import threading
import time

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_fastlane import rearm_rail_wake
from sniper.services.memecoin_funded_policy import (
    FundedExitPolicy,
    build_funded_contract,
    canonical_funded_contract,
)
from sniper.services.memecoin_market_state import EntryClassification
from sniper.services.memecoin_hot_tp import HotTpTrigger, tp_target_from
from sniper.services.memecoin_solana_swap_marks import (
    PoolBinding, QuoteMark, SolanaSwapMarkCache)

WSOL = "So11111111111111111111111111111111111111112"


def _mark(mint, qpt, *, slot=1, source="fastlane_sol_swap"):
    return QuoteMark(mint=mint, pool="P", quote_mint=WSOL, quote_per_token=qpt,
                     slot=slot, observed_ts=0.0, observed_at="t", source=source)


def _trigger(**kw):
    clock = {"t": 100.0}
    t = HotTpTrigger(**{"min_interval_s": 0.25, "big_move_pct": 3.0, **kw},
                     clock=lambda: clock["t"])
    return t, clock


def _armed(t, *, down=0.95, up=1.10, ids=(7,), q0=0.01, stamp="s1"):
    """Anchor mint M at q0: a stop 5% below the last exact quote, a take-profit 10% above."""
    t.on_mark(_mark("M", q0))
    t.rearm({"M": (down, up, set(ids), stamp)}, keep={"M"})


def test_target_math():
    assert tp_target_from(2.0, 25) == 2.5
    assert tp_target_from(0, 25) is None
    assert tp_target_from(2.0, 0) is None
    assert tp_target_from(None, 25) is None


def test_a_mark_that_crosses_the_stop_wakes_a_focus_pass_for_that_position():
    t, _ = _trigger(big_move_pct=50.0)
    _armed(t)
    assert t.on_mark(_mark("M", 0.00951)) is None         # E x q/q0 = 0.951 E: above the stop
    assert t.take_woken() == set()
    assert t.on_mark(_mark("M", 0.00949)) == "down_rail"  # 0.949 E: through the 0.95 stop
    assert t.wake.is_set() and t.take_woken() == {7}
    assert t.take_woken() == set()                        # drained


def test_a_mark_that_crosses_the_take_profit_wakes():
    t, _ = _trigger(big_move_pct=50.0)
    _armed(t)
    assert t.on_mark(_mark("M", 0.01099)) is None
    assert t.on_mark(_mark("M", 0.01101)) == "up_rail"


def test_a_big_move_wakes_when_no_rail_is_near():
    t, _ = _trigger()
    _armed(t, down=None, up=None)
    assert t.on_mark(_mark("M", 0.01029)) is None         # +2.9%
    assert t.on_mark(_mark("M", 0.01030)) == "big_move"   # +3.0%


def test_wakes_coalesce_per_mint_and_other_mints_are_independent():
    t, clock = _trigger()
    _armed(t)
    t.on_mark(_mark("N", 0.02))
    t.rearm({"M": (0.95, 1.10, {7}, "s1"), "N": (0.95, 1.10, {8}, "n1")}, keep={"M", "N"})
    assert t.on_mark(_mark("M", 0.009)) == "down_rail"
    assert t.on_mark(_mark("M", 0.008)) is None           # inside 250 ms: coalesced
    assert t.on_mark(_mark("N", 0.018)) == "down_rail"    # another mint is not held back
    clock["t"] += 0.3
    assert t.on_mark(_mark("M", 0.008)) == "down_rail"
    assert t.take_woken() == {7, 8} and t.suppressed == 1


def test_no_anchor_never_wakes_and_the_first_mark_after_a_new_quote_only_anchors():
    t, _ = _trigger()
    assert t.on_mark(_mark("M", 0.001)) is None           # not held: never wakes
    t.rearm({"X": (0.95, 1.10, {9}, "x1")}, keep={"X"})    # held, but no mark seen yet
    assert t.on_mark(_mark("X", 0.5)) is None             # becomes q0
    assert t.on_mark(_mark("X", 0.4)) == "down_rail"      # measured against it


def test_the_anchor_moves_only_when_the_exact_quote_is_new():
    t, clock = _trigger(big_move_pct=50.0)
    _armed(t, stamp="s1")
    t.on_mark(_mark("M", 0.0097))
    t.rearm({"M": (0.95, 1.10, {7}, "s1")}, keep={"M"})   # same quote: q0 stays 0.01
    assert t.on_mark(_mark("M", 0.00949)) == "down_rail"
    clock["t"] += 1.0
    t.rearm({"M": (0.95, 1.10, {7}, "s2")}, keep={"M"})   # new quote: q0 = latest mark 0.00949
    assert t.on_mark(_mark("M", 0.0091)) is None          # 0.959 of the new anchor
    assert t.on_mark(_mark("M", 0.0090)) == "down_rail"   # 0.948


def test_a_closed_position_is_dropped():
    t, _ = _trigger()
    _armed(t)
    t.rearm({}, keep=set())
    assert t.on_mark(_mark("M", 0.001)) is None
    assert t.snapshot()["anchors"] == 0


def test_shadow_records_the_wake_but_never_wakes():
    t, _ = _trigger(shadow=True)
    _armed(t)
    assert t.on_mark(_mark("M", 0.009, slot=450000000)) == "down_rail"
    assert not t.wake.is_set() and t.take_woken() == set()
    [rec] = t.take_records()
    assert rec["shadow"] and rec["slot"] == 450000000 and rec["reason"] == "down_rail"
    assert rec["ids"] == [7] and rec["source"] == "fastlane_sol_swap"


def test_the_global_cap_refuses_a_wake_storm():
    t, clock = _trigger(max_per_minute=6.0)                # burst capacity 1
    _armed(t)
    t.on_mark(_mark("N", 0.02))
    t.rearm({"M": (0.95, 1.10, {7}, "s1"), "N": (0.95, 1.10, {8}, "n1")}, keep={"M", "N"})
    assert t.on_mark(_mark("M", 0.009)) == "down_rail"
    assert t.on_mark(_mark("N", 0.018)) is None           # cap, not coalescing: another mint
    assert t.suppressed == 1


def test_a_bad_mark_is_ignored():
    t, _ = _trigger()
    _armed(t)
    for qpt in (None, 0, -1.0, "x"):
        assert t.on_mark(_mark("M", qpt)) is None


def test_wait_returns_true_only_when_woken():
    t, _ = _trigger()
    assert t.wait(0.01) is False
    t.wake.set()
    assert t.wait(0.01) is True
    assert not t.wake.is_set()                          # cleared for the next wake


def test_cache_calls_hook_for_stored_marks_only():
    seen = []
    cache = SolanaSwapMarkCache(on_mark=seen.append)
    b = PoolBinding(mint="M", pool="P", coin_vault="cv", quote_vault="qv", quote_mint=WSOL)
    ok = cache.record({"kind": "sol_swap", "pool": "P", "amount0": -1000, "amount1": 50,
                       "decimals0": 6, "decimals1": 9, "block_no": 7}, b,
                      now_ts=1.0, observed_at="t")
    assert ok and len(seen) == 1 and seen[0].mint == "M"
    refused = cache.record({"kind": "sol_swap", "pool": "OTHER", "amount0": -1, "amount1": 1,
                            "decimals0": 6, "decimals1": 9, "block_no": 8}, b,
                           now_ts=1.0, observed_at="t")
    assert not refused and len(seen) == 1                # refused rows never notify
    assert cache.bindings_by_mint() == {"M": WSOL}


def test_cache_hook_fault_never_escapes():
    def boom(mark):
        raise RuntimeError("hook")
    cache = SolanaSwapMarkCache(on_mark=boom)
    b = PoolBinding(mint="M", pool="P", coin_vault="cv", quote_vault="qv", quote_mint=WSOL)
    assert cache.record({"kind": "sol_swap", "pool": "P", "amount0": -1000, "amount1": 50,
                         "decimals0": 6, "decimals1": 9, "block_no": 7}, b,
                        now_ts=1.0, observed_at="t")


def test_real_position_stores_tp_target_in_open_insert():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    classification = EntryClassification(
        classification_version=1, network_id="solana", mint="MINT",
        signal_source="global_feed", decision_at="2026-08-22T00:00:00+00:00",
        coin_band="nano", lifecycle="young", liquidity_class="balanced",
        momentum_phase="climbing", market_regime="hot_narrow",
        data_quality="partial", risk_scale=1.0, reason_codes=(), facts={},
    )
    policy = FundedExitPolicy(
        policy_id="test-scalp", policy_version=1, coin_band="nano", tp_pct=25.0,
        derisk_pct=0.0, derisk_mode="off", trail_pct=0.0, trail_arm_pct=0.0,
        trail_requires_derisk=False, stop_pct=35.0, catastrophe_pct=60.0,
        stop_debounce_sweeps=1, max_hold_s=120.0, unquotable_exit_s=60.0,
        mirror_on_sell=False,
    )
    contract = build_funded_contract(
        classification=classification, base_clip_usd=25.0, approved_clip_usd=25.0,
        policy=policy, origin_event_id="event-1",
    )
    payload, digest = canonical_funded_contract(contract)

    pid = m.open_position(
        conn, source_user_id="u1", source_handle="h", mint="MINT", ticker="T",
        entry_price=2.0, entry_notional=25.0, entry_fill_source="chain",
        entry_lag_s=1.0, their_size_usd=25.0, opened_at="2026-08-22T00:00:01+00:00",
        execution_mode="real", network_id="solana", funded_contract_version=1,
        funded_contract_hash=digest, funded_contract_json=payload,
    )

    row = conn.execute(
        "SELECT entry_price,tp_target_price FROM memecoin_positions WHERE id=?", (pid,)
    ).fetchone()
    assert tuple(row) == (2.0, 2.5)


def test_wake_runs_pass_before_tick_elapses():
    """A stream mark must make the worker pass run well inside a 1 s tick."""
    t, _ = _trigger()
    _armed(t)
    ran = threading.Event()

    def worker():
        if t.wait(1.0) and t.take_woken() == {7}:
            ran.set()

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    time.sleep(0.05)
    t0 = time.monotonic()
    t.on_mark(_mark("M", 0.009))
    assert ran.wait(0.5)
    assert time.monotonic() - t0 < 0.5


def _open_funded(conn, *, mint="MINT", entry=2.0, stop=35.0, tp=25.0, trail=0.0, arm=0.0,
                 derisk=0.0):
    classification = EntryClassification(
        classification_version=1, network_id="solana", mint=mint,
        signal_source="global_feed", decision_at="2026-08-22T00:00:00+00:00",
        coin_band="nano", lifecycle="young", liquidity_class="balanced",
        momentum_phase="climbing", market_regime="hot_narrow",
        data_quality="partial", risk_scale=1.0, reason_codes=(), facts={},
    )
    policy = FundedExitPolicy(
        policy_id="test-scalp", policy_version=1, coin_band="nano", tp_pct=tp,
        derisk_pct=derisk, derisk_mode="off" if not derisk else "principal",
        trail_pct=trail, trail_arm_pct=arm, trail_requires_derisk=False, stop_pct=stop,
        catastrophe_pct=60.0, stop_debounce_sweeps=1, max_hold_s=120.0,
        unquotable_exit_s=60.0, mirror_on_sell=False,
    )
    contract = build_funded_contract(
        classification=classification, base_clip_usd=25.0, approved_clip_usd=25.0,
        policy=policy, origin_event_id="event-" + mint,
    )
    payload, digest = canonical_funded_contract(contract)
    return m.open_position(
        conn, source_user_id="u1", source_handle="h", mint=mint, ticker="T",
        entry_price=entry, entry_notional=25.0, entry_fill_source="chain",
        entry_lag_s=1.0, their_size_usd=25.0, opened_at="2026-08-22T00:00:01+00:00",
        execution_mode="real", network_id="solana", funded_contract_version=1,
        funded_contract_hash=digest, funded_contract_json=payload,
    )


def test_rearm_reads_the_sweeps_rails_as_ratios_to_the_last_exact_quote():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = _open_funded(conn, entry=2.0, stop=35.0, tp=25.0)
    t, _ = _trigger(big_move_pct=50.0)
    rearm_rail_wake(conn, t)
    assert t.snapshot()["anchors"] == 0                   # no exact quote yet: tick only
    m.set_position_last_executable(conn, pid, 1.6, "2026-08-22T00:01:00+00:00")
    rearm_rail_wake(conn, t)
    st = t._rails["MINT"]
    assert st["ids"] == {pid}
    assert abs(st["down"] - 1.3 / 1.6) < 1e-12            # stop: 2.0 x (1 - 0.35) over E
    assert abs(st["up"] - 2.5 / 1.6) < 1e-12              # TP: 2.0 x 1.25 over E
    t.on_mark(_mark("MINT", 0.01))                        # q0
    assert t.on_mark(_mark("MINT", 0.01 * 1.31 / 1.6)) is None   # estimate 1.31: above 1.30
    assert t.on_mark(_mark("MINT", 0.01 * 1.29 / 1.6)) == "down_rail"


def test_a_level_the_exact_quote_already_passed_is_not_a_wake():
    """An exit in flight would otherwise wake on every mark."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    pid = _open_funded(conn, entry=2.0, stop=35.0, tp=25.0)
    m.set_position_last_executable(conn, pid, 1.2, "2026-08-22T00:01:00+00:00")   # below stop
    t, _ = _trigger()
    rearm_rail_wake(conn, t)
    assert t._rails["MINT"]["down"] is None and abs(t._rails["MINT"]["up"] - 2.5 / 1.2) < 1e-12


@pytest.mark.parametrize("gap", [1.04, 1.06, 1.10])
def test_alternating_state_and_swap_marks_on_a_flat_price_never_wake(gap):
    """First live data, 2026-09-27: pool-state and swap marks sat 4-10% apart on a thin coin, and
    one shared anchor read every switch of source as a big move (81 wakes in 47 s)."""
    t, clock = _trigger(big_move_pct=5.0)
    t.on_mark(_mark("M", 0.0100, source="fastlane_sol_swap"))
    t.on_mark(_mark("M", 0.0100 * gap, source="fastlane_sol_state"))
    t.rearm({"M": (0.80, 1.30, {7}, "s1")}, keep={"M"})
    for _ in range(20):
        clock["t"] += 0.3
        assert t.on_mark(_mark("M", 0.0100 * gap, source="fastlane_sol_state")) is None
        assert t.on_mark(_mark("M", 0.0100, source="fastlane_sol_swap")) is None
    assert t.wakes == 0
    clock["t"] += 0.3
    assert t.on_mark(_mark("M", 0.0106 * gap, source="fastlane_sol_state")) == "big_move"  # +6% itself

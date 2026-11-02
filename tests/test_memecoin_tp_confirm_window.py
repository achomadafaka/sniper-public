"""The TP confirmation window: hold a take-profit while the breach stays continuous, sell at the
CURRENT mark once it has held.

The inverse of the stop's confirm, and the two must never merge. The stop is debounced against a
bad tick reading LOW; the take-profit is held to sell into CONTINUATION. Opposite intentions that
share one elapsed-time helper.

Every failure here is quiet: a window that never engages still sells, a window that never clears
still sells eventually, and a stop that accidentally inherits the debounce still exits — later,
into a falling coin.
"""
import ast
import json
import pathlib

import pytest

from sniper.services import memecoin_real_runner as RR
from sniper.settings import memecoin as cfg

RUNNER = pathlib.Path(RR.__file__)


# ---------------------------------------------------------------- the window itself

def test_zero_is_off_and_reproduces_todays_instant_take_profit():
    """Rollback is the env, not a code change. 0 must be indistinguishable from before."""
    assert cfg.tp_confirm_window_s(0) == 0.0
    assert cfg.tp_confirm_window_s(None) == 0.0, "the shipped default must be off"
    assert cfg.MEMECOIN_REAL_TP_CONFIRM_SECONDS == 0.0


def test_the_window_is_clamped_so_no_config_can_defer_a_take_profit_forever():
    assert cfg.tp_confirm_window_s(2) == 2.0
    assert cfg.tp_confirm_window_s(10_000) == cfg.MEMECOIN_REAL_TP_CONFIRM_MAX_S
    assert cfg.MEMECOIN_REAL_TP_CONFIRM_MAX_S <= 60, (
        "a take-profit deferred beyond a minute is a different strategy, not a confirmation")


@pytest.mark.parametrize("bad", [-1, float("nan"), 0])
def test_a_broken_window_disables_rather_than_defers(bad):
    assert cfg.tp_confirm_window_s(bad) == 0.0


# ---------------------------------------------------------------- the elapsed-time helper

def test_a_breach_shorter_than_the_window_is_not_confirmed():
    ok, elapsed = RR.stop_breach_confirmed(
        "2026-08-29T10:00:00+00:00", "2026-08-29T10:00:01+00:00", 2.0)
    assert ok is False and elapsed == pytest.approx(1.0)


def test_a_breach_that_reaches_the_window_confirms():
    ok, elapsed = RR.stop_breach_confirmed(
        "2026-08-29T10:00:00+00:00", "2026-08-29T10:00:02+00:00", 2.0)
    assert ok is True and elapsed == pytest.approx(2.0)


def test_every_failure_mode_resolves_toward_SELLING():
    """For a take-profit that means falling back to today's instant exit. Resolving toward HOLD
    would defer a profit indefinitely on a clock fault or an unparseable stamp."""
    assert RR.stop_breach_confirmed(None, "2026-08-29T10:00:00+00:00", 2.0)[0] is True
    assert RR.stop_breach_confirmed("garbage", "2026-08-29T10:00:00+00:00", 2.0)[0] is True
    # clock moved backwards
    assert RR.stop_breach_confirmed(
        "2026-08-29T10:00:05+00:00", "2026-08-29T10:00:00+00:00", 2.0)[0] is True


# ---------------------------------------------------------------- scope

def test_the_stop_is_NOT_in_the_tp_confirm_set():
    """The guard Alpha asked to keep green. Merging the sets would debounce the stop through the
    take-profit's window — a downside rail deferred by an upside rule."""
    assert RR._TP_CONFIRM_REASONS == {"real_tp"}
    assert not (RR._TP_CONFIRM_REASONS & RR._STOP_DEBOUNCE_REASONS)
    assert "b_stop" not in RR._TP_CONFIRM_REASONS
    assert "catastrophe" not in RR._TP_CONFIRM_REASONS


def test_only_the_take_profit_family_is_confirmed():
    """Derisk, trail, mirror, timecap and vol_death are untouched by this change."""
    for reason in ("derisk", "real_trail", "mirror", "b_timecap", "vol_death", "unquotable"):
        assert reason not in RR._TP_CONFIRM_REASONS, reason


def test_the_two_rails_use_SEPARATE_stamps():
    """A position can breach one rail while the other is clear. One shared column would let a stop
    breach reset a take-profit's clock."""
    from sniper.persistence import db_memecoin as m
    assert hasattr(m, "set_position_tp_breach_started")
    assert hasattr(m, "set_position_stop_breach_started")
    src = pathlib.Path(m.__file__).read_text()
    assert "tp_breach_started_at=?" in src and "stop_breach_started_at=?" in src


# ---------------------------------------------------------------- one decision site

def test_there_is_exactly_ONE_funded_take_profit_decision_site():
    """THE EIGHT-IF LESSON. A wake that fired instantly through a second code path would make the
    window a lie on exactly the fast exits it targets.

    The funded reason comes from `alpha_b_real_exit_reason`, and the only call that can act on it
    is inside `real_exit_sweep`. The hot lane does not decide: it supplies `demand_fn` and wakes
    the same sweep, so the wake path inherits the window rather than needing its own.
    """
    from _source_guard import code_of
    tree = ast.parse(code_of(RUNNER))
    acting = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef,)):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and getattr(call.func, "id", "") == "alpha_b_real_exit_reason"):
                acting.append(node.name)
    assert acting == ["real_exit_sweep"], (
        f"a second funded exit decision site appeared: {acting}. The TP window lives in "
        "real_exit_sweep; any other site would bypass it.")


def test_the_wake_path_reaches_the_window_through_the_same_sweep():
    loop = (pathlib.Path(RR.__file__).resolve().parents[2]
            / "scripts" / "memecoin_runner_loop.py").read_text()
    assert 'lane="fastlane_wake"' in loop
    i = loop.index('lane="fastlane_wake"')
    assert "real_exit_sweep(" in loop[max(0, i - 400):i], (
        "the wake must call real_exit_sweep, or it decides outside the confirmation window")


# ---------------------------------------------------------------- the branch, as code

def _sweep_source():
    from _source_guard import code_of
    tree = ast.parse(code_of(RUNNER))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "real_exit_sweep":
            return ast.unparse(node)
    raise AssertionError("real_exit_sweep not found")


def test_an_exact_failure_clears_the_stamp_and_a_quoted_revert_re_arms():
    src = _sweep_source()
    assert "not (exact_mid and exact_mid > 0)" in src
    assert "elif pos.get('tp_breach_started_at') and decision_mid and (decision_mid > 0)" in src


def test_the_confirm_runs_before_the_stop_chain_so_neither_rail_shadows_the_other():
    src = _sweep_source()
    assert src.index("_TP_CONFIRM_REASONS") < src.index("_STOP_DEBOUNCE_REASONS")


# ---------------------------------------------------------------- behaviour, through the sweep

def _harness():
    """A funded position with a +60% take-profit and every other rail off, so the only thing that
    can fire is the one under test."""
    import sqlite3
    from sniper.persistence import db_memecoin as m
    import test_memecoin_real_rails_v2 as h

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    frozen = h._frozen_pos(derisk=0.0, trail=0.0, trail_arm=0.0, stop=90.0, catastrophe=95.0,
                           debounce=1, max_hold=999999.0, tp=60.0)
    pid = h._persist_frozen_position(c, frozen)
    return c, pid, m


def _robinhood_v3_harness():
    import sqlite3
    from sniper.persistence import db_memecoin as m
    import test_memecoin_real_rails_v2 as h

    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    frozen = h._frozen_pos(derisk=0.0, trail=0.0, trail_arm=0.0, stop=90.0,
                           catastrophe=95.0, debounce=1, max_hold=999999.0, tp=60.0)
    contract = json.loads(frozen["funded_contract_json"])
    contract["identity"]["network_id"] = "4663"
    contract["identity"]["mint"] = "0x1111111111111111111111111111111111111111"
    contract["policy"]["policy_id"] = "follow-30-50-v3"
    contract["policy"]["mirror_on_sell"] = False
    payload, digest = m.canonical_funded_json(contract)
    frozen.update(network_id="4663", mint=contract["identity"]["mint"],
                  funded_contract_json=payload, funded_contract_hash=digest)
    return c, h._persist_frozen_position(c, frozen), m


def _sweep(c, price, now):
    """One sweep at `price`. The preview FOLLOWS the price, deliberately.

    A fixed preview fights the executable confirmation: the funded rails re-check every price
    reason against exact-balance proceeds, so a preview saying $99 while the mark says -95% is
    the executable lane correctly refusing a stop on a position it can see is up. The harness has
    to be coherent or it tests the fixture.
    """
    exits = []
    qty = 50.0                      # the entry fill's quantity in _persist_frozen_position
    contexts = {"solana": {
        "preview_exit_fn": lambda _pos: {"expected_usdc": price * qty},
        "exit_fn": lambda *_a: exits.append("exit") or {"status": "failed"},
    }}
    RR.real_exit_sweep(c, contexts, price_fn=lambda _mints: {"MINT": {"price": price}},
                       now_fn=lambda: now)
    return exits


ABOVE, BELOW = 2.0, 1.1        # entry is 1.0, so +100% and +10% against a +60% target


def test_env_zero_reproduces_todays_instant_take_profit(monkeypatch):
    """Bit-for-bit: with the window off, one sweep at the target exits immediately."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_CONFIRM_SECONDS", 0.0)
    c, pid, m = _harness()
    assert _sweep(c, ABOVE, "2026-07-18T12:00:00+00:00") == ["exit"]
    assert m.get_position(c, pid)["tp_breach_started_at"] is None, "no stamp when the window is off"


def test_the_window_holds_the_first_touch_then_sells_when_it_persists(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_CONFIRM_SECONDS", 2.0)
    c, pid, m = _harness()
    assert _sweep(c, ABOVE, "2026-07-18T12:00:00+00:00") == [], "the touch must not sell"
    assert m.get_position(c, pid)["tp_breach_started_at"] == "2026-07-18T12:00:00+00:00"
    assert _sweep(c, ABOVE, "2026-07-18T12:00:01+00:00") == [], "1s of a 2s window"
    assert _sweep(c, ABOVE, "2026-07-18T12:00:02+00:00") == ["exit"], "2s held -> sell"


def test_a_held_tp_needs_two_EXACT_quotes_not_display_marks(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_CONFIRM_SECONDS", 2.0)
    c, pid, m = _robinhood_v3_harness()
    exits = []
    exact = {"value": None}
    contexts = {"4663": {
        "preview_exit_fn": lambda _pos: (
            None if exact["value"] is None else {"expected_usdc": exact["value"] * 50.0}),
        "exit_fn": lambda *_a: exits.append("exit") or {"status": "failed"},
    }}

    def sweep(now):
        RR.real_exit_sweep(c, contexts, price_fn=lambda _m: {
            m.get_position(c, pid)["mint"]: {"price": ABOVE}},
                           now_fn=lambda: now)

    sweep("2026-07-18T12:00:00+00:00")
    assert exits == []
    assert m.get_position(c, pid)["tp_breach_started_at"] is None

    exact["value"] = ABOVE
    sweep("2026-07-18T12:00:01+00:00")
    assert exits == []
    assert m.get_position(c, pid)["tp_breach_started_at"] == "2026-07-18T12:00:01+00:00"

    exact["value"] = None
    sweep("2026-07-18T12:00:02+00:00")
    assert m.get_position(c, pid)["tp_breach_started_at"] is None

    exact["value"] = ABOVE
    sweep("2026-07-18T12:00:03+00:00")
    assert m.get_position(c, pid)["tp_breach_started_at"] == "2026-07-18T12:00:03+00:00"

    # 2.0s window from the 12:00:03 stamp. These two lines used to read 05 -> hold, 06 -> sell,
    # which was the removed hardcoded 3.0s Robinhood override timing, NOT the window this test
    # sets. The test asserted the override while claiming to assert the env value.
    sweep("2026-07-18T12:00:04+00:00")
    assert exits == [], "1.0s of a 2.0s window"
    sweep("2026-07-18T12:00:05+00:00")
    assert exits == ["exit"], "2.0s held -> sell, on the window the env actually sets"
    assert m.get_position(c, pid)["tp_breach_started_at"] is None


def test_a_sub_window_revert_clears_the_stamp_and_the_tp_re_arms(monkeypatch):
    """The accepted leak, asserted as behaviour: a coin that dips inside the window loses that
    exit, and the next touch starts a fresh window rather than inheriting the old one."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_CONFIRM_SECONDS", 2.0)
    c, pid, m = _harness()
    assert _sweep(c, ABOVE, "2026-07-18T12:00:00+00:00") == []
    assert m.get_position(c, pid)["tp_breach_started_at"] is not None
    assert _sweep(c, BELOW, "2026-07-18T12:00:01+00:00") == []
    assert m.get_position(c, pid)["tp_breach_started_at"] is None, "a dip must clear the stamp"

    # re-arm: the second touch starts its own window, and the old elapsed time is NOT inherited
    assert _sweep(c, ABOVE, "2026-07-18T12:00:03+00:00") == [], "the re-touch is a fresh window"
    assert m.get_position(c, pid)["tp_breach_started_at"] == "2026-07-18T12:00:03+00:00"
    assert _sweep(c, ABOVE, "2026-07-18T12:00:05+00:00") == ["exit"]


def test_the_sell_happens_at_the_CURRENT_mark_not_at_the_target(monkeypatch):
    """Selling into continuation is the whole point. The exit fires on the sweep whose price is
    whatever the coin is worth THEN, which is why the window can pay."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_CONFIRM_SECONDS", 2.0)
    c, pid, m = _harness()
    seen = []
    price = {"v": 1.7}
    contexts = {"solana": {
        "preview_exit_fn": lambda _pos: {"expected_usdc": price["v"] * 50.0},
        "exit_fn": lambda *_a: seen.append("exit") or {"status": "failed"},
    }}
    RR.real_exit_sweep(c, contexts, price_fn=lambda _m: {"MINT": {"price": price["v"]}},
                       now_fn=lambda: "2026-07-18T12:00:00+00:00")
    assert seen == []
    price["v"] = 3.0
    RR.real_exit_sweep(c, contexts, price_fn=lambda _m: {"MINT": {"price": price["v"]}},
                       now_fn=lambda: "2026-07-18T12:00:02+00:00")
    assert seen == ["exit"], "it must still fire after the window, at the higher mark"


def test_the_stop_is_NOT_deferred_by_the_tp_window(monkeypatch):
    """The guard that must stay green. A downside rail must never inherit an upside rule's delay."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_CONFIRM_SECONDS", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SECONDS", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 1)
    c, pid, m = _harness()
    assert _sweep(c, 0.05, "2026-07-18T12:00:00+00:00") == ["exit"], "the stop must be instant"


# ---------------------------------------------------------------- the env is the only window

def test_no_policy_id_can_force_a_window_the_env_says_is_off(monkeypatch):
    """THE DEFECT, 2026-09-03. `.env` carried MEMECOIN_REAL_TP_CONFIRM_SECONDS=0 while a hardcoded
    ROBINHOOD_TP_CONFIRM_SECONDS=3.0 outranked it for policy ids follow-30-50-v3/v4 on 4663 — every
    funded Robinhood position. The setting that documents this rail was false for the only chain
    running it, and 3.0s is unsatisfiable at the measured 1.73s p50 sweep cadence when a coin
    spikes for one sample: DOUGH 49002 quoted $66.98 on a $50.94 entry at +31.5%, reverted to
    +13.4% 2.6s later, and rode to b_stop at -$18.39.

    The window must come from ONE place. A policy id may not reintroduce its own.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_CONFIRM_SECONDS", 0.0)
    assert RR._tp_confirm_window_s() == 0.0

    import inspect
    assert not inspect.signature(RR._tp_confirm_window_s).parameters, (
        "the window takes no position/policy argument — that is what made an override possible")

    src = inspect.getsource(RR)
    for gone in ("ROBINHOOD_TP_CONFIRM_SECONDS", "_is_robinhood_tp_confirm_policy",
                 "is_robinhood_tp_confirm_policy"):
        assert gone not in src, f"{gone} is back; the take-profit window has a second source again"


def test_a_funded_4663_position_takes_profit_on_the_touch(monkeypatch):
    """DOUGH 49002, end to end: with the env window off, the first sweep at the target sells.

    The harness is the real 4663 follow policy, the one that carried the override.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_CONFIRM_SECONDS", 0.0)
    c, pid, m = _robinhood_v3_harness()
    exits = []
    contexts = {"4663": {
        "preview_exit_fn": lambda _pos: {"expected_usdc": ABOVE * 50.0},
        "exit_fn": lambda *_a: exits.append("exit") or {"status": "failed"},
    }}
    RR.real_exit_sweep(c, contexts,
                       price_fn=lambda _mints: {m.get_position(c, pid)["mint"]: {"price": ABOVE}},
                       now_fn=lambda: "2026-09-03T11:31:32+00:00")
    assert exits == ["exit"], "the take-profit must fire on the touch, not 3 seconds later"
    assert m.get_position(c, pid)["tp_breach_started_at"] is None

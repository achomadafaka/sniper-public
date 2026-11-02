"""A read deep enough is not a bad tick, so it must not wait for a second one.

2026-09-03. Two funded 4663 positions closed the same day having blown straight through their
frozen -35% stop: FATCOIN -56.9% on a 4s hold (21.9pp past the stop) and MEOW -48.5% on a 9s hold
(13.5pp past). Together -$83.89, 62% of that day's gross losses. Both had peak == entry: they
never ticked up once.

The debounce is the bad-tick guard and it is right to exist. What it cannot do is tell a wick from
a collapse, and the measured cost of "one more read" on 4663 is p50 1.73s -- with 0% of reads at
or under 1.2s and one 35.63s gap that same day. So depth bypasses the count.

Operator: "make the stop loss activate just if the runner/rails read the price below or at -35%
twice in a row, whatever that takes as time ... if we read a price lets say -50% we should sell
instantly i believe".
"""
from __future__ import annotations

import ast
import inspect
import pathlib

from sniper.services import memecoin_real_runner as RR
from sniper.settings import memecoin as cfg

RUNNER = pathlib.Path(RR.__file__)


def _sweep_source():
    from _source_guard import code_of
    tree = ast.parse(code_of(RUNNER))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "real_exit_sweep":
            return ast.unparse(node)
    raise AssertionError("real_exit_sweep not found")


# ---------------------------------------------------------------- the threshold

def test_the_depth_default_is_ACTIVE_not_inert():
    """Unlike the confirmation knobs, this one defaults on. For a loss rail the safe direction is
    SELLING, so a lost or empty .env must not silently restore the slower behaviour."""
    assert cfg.MEMECOIN_REAL_STOP_INSTANT_PCT == 50.0


def test_zero_disables_it_and_restores_pure_count_debounce():
    src = _sweep_source()
    assert "_instant_pct > 0" in src, "0 must disable the bypass, not divide or fire at 0%"


# ---------------------------------------------------------------- what it may and may not do

def test_it_only_ever_bypasses_a_LOSS_rail():
    """It must not accelerate a take-profit, a de-risk, a trail or a timecap. The gate is the
    already-nominated reason, and only the two loss reasons qualify."""
    src = _sweep_source()
    i = src.index("_sell_now = ")
    clause = src[i:i + 400]
    assert "reason in _STOP_DEBOUNCE_REASONS" in clause, (
        "depth must only bypass confirmation for b_stop / catastrophe")
    assert RR._STOP_DEBOUNCE_REASONS == {"b_stop", "catastrophe"}


def test_it_can_never_NOMINATE_a_rail_only_skip_the_wait():
    """`reason` is decided by alpha_b_real_exit_reason against the FROZEN thresholds well above
    this point. Depth must not introduce a sell where the contract nominated nothing."""
    src = _sweep_source()
    # `reason` must be settled before the bypass, and the bypass body must not touch it.
    assert src.index("alpha_b_real_exit_reason(") < src.index("_sell_now = "), (
        "the reason must be decided by the frozen thresholds BEFORE depth is consulted")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                and node.test.id == "_sell_now"):
            for sub in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    assert sub.id != "reason", "the bypass body must never reassign `reason`"
            break
    else:
        raise AssertionError("no `if _sell_now:` branch found")
    body = inspect.getsource(RR.alpha_b_real_exit_reason)
    assert "STOP_INSTANT" not in body, (
        "the depth knob must stay OUT of the pure rail decision — it is a confirmation bypass")


def test_the_executable_confirmation_still_runs_after_the_bypass():
    """Skipping the debounce must not skip the exact-balance recheck. A deep NOMINAL read still
    cannot sell without exact proceeds."""
    src = _sweep_source()
    assert src.index("_sell_now = ") < src.index("_EXECUTABLE_PRICE_REASONS"), (
        "the bypass must sit BEFORE the executable confirmation, not replace it")


def test_a_shallow_breach_still_needs_two_reads():
    """The operator kept the count for the -35% case: 'twice in a row, whatever that takes as
    time'. So the sweep-count branch must survive, and the elapsed-time branch must not be what
    governs it."""
    src = _sweep_source()
    assert "required_stop_hits" in src and "hits < required_stop_hits" in src
    assert cfg.stop_confirm_window_s() == 0.0, (
        "the elapsed-time window must be OFF so the frozen 2-sweep count governs")


def test_the_bypass_is_LOGGED_because_no_row_is_what_an_unchecked_fire_looks_like():
    src = _sweep_source()
    i = src.index("_sell_now = ")
    assert "INSTANT" in src[i:i + 900], "an unlogged bypass is indistinguishable from no check"


# ---------------------------------------------------------------- the two real positions

def test_the_two_positions_that_motivated_this_would_have_bypassed():
    """FATCOIN -56.9% and MEOW -48.5% against a 50% depth: FATCOIN qualifies on its realized
    price, MEOW is the honest near-miss at -48.5% and would still have taken two reads. Recorded so
    the threshold is not mistaken for one that catches both."""
    pct = cfg.MEMECOIN_REAL_STOP_INSTANT_PCT
    assert -56.9 <= -pct, "FATCOIN at -56.9% must bypass at a 50% depth"
    assert not (-48.5 <= -pct), (
        "MEOW at -48.5% does NOT bypass at 50 — this is a known limit of the chosen depth, "
        "not a passing case; lowering the depth is a separate decision with its own evidence")


def test_it_reads_an_EXACT_quote_never_a_nominal_mark():
    """The debounce exists to stop one bad tick selling a healthy coin, and nominal marks are what
    manufacture those: 23 consecutive IDENTICAL dexscreener marks gave LOCK a fabricated -47% on
    2026-09-03 while its executable price was roughly double that. A fabricated tick must still
    prove itself across two reads; only a price the bag can actually fetch is evidence at depth."""
    src = _sweep_source()
    i = src.index("_instant_loss = ")
    clause = src[i:i + 260]
    assert "exact_mid" in clause, "the depth bypass must read the exact whole-bag quote"
    assert "decision_mid" not in clause, (
        "decision_mid can be a nominal mark on a non-exact-policy lane — a fabricated tick must "
        "not be able to trigger an instant sell")


def test_a_stop_at_or_past_the_depth_LOSES_its_two_read_count():
    """A consequence worth stating, because three separate test files tripped over it. If a frozen
    stop_pct is >= the depth, every breach of that stop is instantly deep and the count never
    applies to it.

    Live funded contracts are follow-30-50-v4 with stop_pct=35 against a depth of 50, so they keep
    a real -35%..-50% band where two reads ARE required. Legacy follow-30-50-v1 (stop 50) does not.
    This is not a bug; it is what "sell instantly at -50%" means for a rail that already fires at
    -50%. It is asserted so a future change to either number is made with it in view."""
    depth = cfg.MEMECOIN_REAL_STOP_INSTANT_PCT
    assert 35.0 < depth, (
        "the live v4 stop is 35: a depth at or below it would erase the two-read count entirely")
    assert 50.0 >= depth, (
        "at a depth above 50 the legacy stop-50 contracts would regain a count they no longer "
        "have; changing this is a policy decision, not a refactor")

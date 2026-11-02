"""A fresh follow must not bypass the first-seen chase ratio.

Live 2026-08-23, Veyr / sambaxbt. Both events were fresh follows of the SAME armed wallet:

    14:24:56  they buy 1.801e-05   our exact quote 2.442e-05  = +35.5%  refused (cap 30%)
    14:31:41  they buy 4.801e-05   our exact quote 5.607e-05  = +16.8%  FILLED
    14:33:08  b_stop, -$17.64 on a $36.78 clip

Their own re-buy reset the reference price the drift gate compares against, so the gate read the
run as small and opened at the top. We refused 2.442e-05 as too expensive and paid 5.608e-05 seven
minutes later — 3.11x the first-seen price against a 3.0x cap that the bypass had switched off.
"""
from types import SimpleNamespace

from sniper.services import memecoin_real_runner as RR

FIRST_SEEN = 1.801248932142609e-05
FILLED_AT = 5.608461860783801e-05
REFUSED_AT = 2.4415141425262464e-05


def _follow_event(lag_s):
    return SimpleNamespace(signal_source="follow_activity", lag_s=lag_s)


def test_the_live_fill_would_have_been_refused_by_the_ratio():
    assert FILLED_AT / FIRST_SEEN > 3.0                       # 3.11x
    assert RR.first_seen_chase_ok(
        first_price=FIRST_SEEN, price_now=FILLED_AT, max_ratio=3.0) is False


def test_the_price_we_refused_was_inside_the_ratio():
    """The gate had it backwards: the cheap entry was fine on coin-origin terms."""
    assert RR.first_seen_chase_ok(
        first_price=FIRST_SEEN, price_now=REFUSED_AT, max_ratio=3.0) is True


def test_a_fresh_follow_is_still_recognised_for_the_drift_cap():
    """The bypass is gone, but is_fresh_follow_event still selects the looser drift cap."""
    assert RR.is_fresh_follow_event(_follow_event(5.0), 60.0) is True
    assert RR.is_fresh_follow_event(_follow_event(500.0), 60.0) is False


def test_the_ratio_guard_no_longer_reads_fresh_follow():
    """Regression on the bypass itself: the hook must not gate the ratio on freshness.

    Rewritten 2026-08-27. This asserted the literal `... > 0:` to mean "the condition is bare".
    The manual-entry exemption made it non-bare and the assertion fired, which is the guard working
    — but a literal cannot tell a new bypass from a permitted one. It now reads the condition and
    checks its TERMS, so any future extra term fails until someone states it here on purpose.
    """
    import inspect
    src = inspect.getsource(RR.real_entry_hook)
    line = next(ln for ln in src.splitlines()
                if "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO > 0" in ln)
    assert "fresh_follow" not in line, "freshness must never gate the coin-origin ratio again"
    extra = line.split("MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO > 0", 1)[1].rstrip(":").strip()
    assert extra in ("", "and not _manual_entry"), \
        f"unreviewed bypass added to the first-seen guard: {extra!r}"


def test_missing_anchor_still_fails_open():
    assert RR.first_seen_chase_ok(first_price=None, price_now=FILLED_AT, max_ratio=3.0) is True
    assert RR.first_seen_chase_ok(first_price=0.0, price_now=FILLED_AT, max_ratio=3.0) is True

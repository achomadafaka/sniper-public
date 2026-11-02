"""The mcap ceiling is observed after the fact — but only where the answer is decidable.

It is the largest refusal family on the funded lane: 119 of 183 armed-wallet refusals on
2026-09-09, across 38 coins, and it had NO capture family at all. So "is $250k the right ceiling"
could only ever be argued from backtests, never from what the refused coins actually did. Same hole
as `roundtrip_cap`, closed the same morning.

WHY IT IS BOUNDED RATHER THAN BLANKET. Of 182 distinct mints refused in three days, 40% were above
20x the ceiling and 34% above 4x. Observing a $15M coin answers nothing — no plausible ceiling would
admit it — while blanket capture costs ~1,280 extra Jupiter quotes/day against ~3,065 funded
quotes/day, a 42% rise on a key already taking 429s at 1.4% loss. Only the 7.7% within 2x are
observed: ~5 mints/day, ~160 quotes/day, about +5%.

The ratio is parsed from the reason string itself, which has carried both numbers since 40f37bfe
("mcap $343,423 over ceiling $250,000 (over_ceiling)"). No new input and no plumbing — and it
matches only the RENDERED form, so the bare `over_ceiling` strings from the RETIRED mcap band stay
unmatched. This module's header warns specifically against pooling those two.
"""
import pytest

from sniper.services.memecoin_refusal_capture import (
    GATES, _MCAP_CAPTURE_MAX_MULT, _mcap_near_ceiling, should_capture,
)


def family(reason):
    return next((g for g, match in GATES if match(reason)), None)


def _r(mcap, ceiling=250_000):
    return "mcap $%s over ceiling $%s (over_ceiling)" % (format(mcap, ","), format(ceiling, ","))


# --- the decidable region is captured ------------------------------------------------------------

@pytest.mark.parametrize("mcap", [250_001, 343_423, 463_597, 499_999])
def test_a_coin_near_the_line_is_captured(mcap):
    """OSR at $343,423 and USELESS at $463,597 are the cases that make the ceiling answerable."""
    assert family(_r(mcap)) == "mcap_over_ceiling"
    assert should_capture("mcap_over_ceiling") is True


@pytest.mark.parametrize("mcap", [723_974, 2_011_749, 15_674_222])
def test_a_coin_far_over_is_not_captured(mcap):
    """Quota spent on a $15M coin buys no decision. 40% of the population sits above 20x."""
    assert family(_r(mcap)) is None


def test_the_boundary_is_inclusive_and_exact():
    ceiling = 250_000
    assert _mcap_near_ceiling(_r(int(_MCAP_CAPTURE_MAX_MULT * ceiling), ceiling)) is True
    assert _mcap_near_ceiling(_r(int(_MCAP_CAPTURE_MAX_MULT * ceiling) + 1, ceiling)) is False


def test_the_bound_follows_the_ceiling_rather_than_a_hardcoded_dollar_figure():
    """If the operator moves the ceiling, the captured region moves with it."""
    assert _mcap_near_ceiling(_r(900_000, 500_000)) is True     # 1.8x of a raised ceiling
    assert _mcap_near_ceiling(_r(900_000, 250_000)) is False    # 3.6x of the current one


# --- it must not pool with the retired gate, which is what the module header warns about ----------

def test_the_bare_retired_string_is_never_matched():
    assert family("over_ceiling") is None
    assert _mcap_near_ceiling("over_ceiling") is False


def test_the_floor_side_is_its_own_family_not_the_ceiling(monkeypatch):
    """Since 2026-09-18 the floor side is captured too — under its OWN name, because the per-wallet
    floor must leave the refused small caps observable. The ceiling matcher still ignores it."""
    from sniper.services.memecoin_refusal_capture import _mcap_near_ceiling

    reason = "mcap $4,000 under floor $15,000 (under_floor)"
    assert _mcap_near_ceiling(reason) is False
    assert family(reason) == "mcap_under_floor"


def test_it_does_not_collide_with_the_volume_ceiling():
    reason = "m5 volume $110754 OVER ceiling $27161 (m5_turnover_over_ceiling, revival guard)"
    assert family(reason) == "m5_turnover_over_ceiling"


# --- unparseable input must not spend quota ------------------------------------------------------

@pytest.mark.parametrize("bad", [
    "", None, "mcap $ over ceiling $250,000 (over_ceiling)",
    "mcap $abc over ceiling $250,000 (over_ceiling)",
    "mcap $343,423 over ceiling $0 (over_ceiling)",
    "mcap $343,423 over ceiling $abc (over_ceiling)",
    "mcap $-5 over ceiling $250,000 (over_ceiling)",
])
def test_an_unreadable_reason_fails_closed(bad):
    """Fail-closed means NOT captured: treating an unparseable reason as near the line would spend
    the quota this bound exists to protect."""
    assert _mcap_near_ceiling(bad) is False


def test_every_family_name_is_still_unique():
    names = [g for g, _ in GATES]
    assert len(names) == len(set(names)), sorted(n for n in names if names.count(n) > 1)

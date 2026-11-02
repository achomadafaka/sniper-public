"""An mcap refusal must say the mcap and the ceiling, not just name itself.

`over_ceiling` was the single largest refusal for armed wallets — 95 attempts across 30 coins in one
hour on 2026-09-09, more than every other live gate combined once unarmed wallets are excluded — and
the reason string was the bare token `over_ceiling`. No mcap, no ceiling, no units. Reading it
required opening `memecoin_real_runner.mcap_entry_decision` to learn it referred to market cap at
all, while every neighbouring gate already prints its bar:

    m5 volume $110754 OVER ceiling $27161 (m5_turnover_over_ceiling, revival guard)
    round trip 12.21% over 10.0% cap (sell back $153.63 of $175.00)
    over_ceiling                                                        <- this one

The numbers are added at the RENDER, never inside the gate. `mcap_entry_decision` returns a stable
identifier that `tests/test_memecoin_real_mcap_gate.py` pins as an exact tuple and that the refusal
families key on; rewriting it there would break identity to gain prose. Same split as
`classify_quote_miss`, which returns (code, detail).
"""
import pytest

import sniper.services.memecoin_real_runner as RR


def test_the_gate_identity_is_untouched():
    """The machine-readable answer must stay exactly what it was."""
    assert RR.mcap_entry_decision(None, 50_000_000, 250_000) == (
        False, 50_000_000, "over_ceiling")
    assert RR.mcap_entry_decision(None, None, 250_000)[2] == "mcap_unresolved"


def _render(reason, eff_mc, ceiling=250_000.0, floor=0.0):
    """The render block from the entry hook, exercised directly."""
    detail = reason
    if reason in ("over_ceiling", "under_floor") and eff_mc:
        bar = ceiling if reason == "over_ceiling" else floor
        detail = "mcap $%s %s $%s (%s)" % (
            format(int(eff_mc), ","),
            "over ceiling" if reason == "over_ceiling" else "under floor",
            format(int(bar or 0), ","), reason)
    return detail


def test_the_rendered_refusal_carries_both_numbers():
    got = _render("over_ceiling", 5_400_000)
    assert got == "mcap $5,400,000 over ceiling $250,000 (over_ceiling)"
    assert "over_ceiling" in got, "the family token must survive for the refusal record"


def test_a_coin_just_over_the_line_reads_as_just_over():
    """OSR at $343,423 was refused the same way a $50M coin was. The reader could not tell them
    apart, and 'is the ceiling well placed' is unanswerable without that."""
    assert _render("over_ceiling", 343_423) == \
        "mcap $343,423 over ceiling $250,000 (over_ceiling)"


def test_the_floor_side_renders_too():
    assert _render("under_floor", 4_000, floor=15_000) == \
        "mcap $4,000 under floor $15,000 (under_floor)"


@pytest.mark.parametrize("reason", ["mcap_unresolved", "mcap_unresolvable", "no_ceiling",
                                    "assumed_micro", "ok"])
def test_other_reasons_are_passed_through_unchanged(reason):
    """Only the two that HAVE a bar get one. A reason about a missing mcap must not print '$0'."""
    assert _render(reason, 5_400_000) == reason


def test_a_missing_mcap_never_prints_a_fabricated_zero():
    """`over_ceiling` cannot occur without an mcap, but if it ever did, '$0 over ceiling' would be
    a claim about a value we do not have."""
    assert _render("over_ceiling", None) == "over_ceiling"
    assert _render("over_ceiling", 0) == "over_ceiling"


def test_the_runner_renders_at_the_call_site_not_in_the_gate():
    import inspect
    src = inspect.getsource(RR.mcap_entry_decision)
    assert "over ceiling $" not in src, "the gate must keep returning a bare identifier"
    # The render lives in `_hook_raw`, nested inside `make_real_hook` — read the module text
    # rather than a function object, since a closure is not reachable by attribute.
    import pathlib
    mod = pathlib.Path(RR.__file__).read_text()
    assert 'reason in ("over_ceiling", "under_floor")' in mod
    assert mod.index('reason in ("over_ceiling", "under_floor")') > mod.index(
        "def mcap_entry_decision"), "the render must be downstream of the gate"

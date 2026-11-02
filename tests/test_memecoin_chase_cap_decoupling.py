"""Turning the chase cap off must not change ANYTHING except the chase refusal.

The cap and the market-data read shared one condition, so setting the cap to 0 also stopped the
`stats_fn` call. The classifier then received an empty market_stats, mcap became unknown,
MEMECOIN_REAL_MCAP_ASSUME_MICRO promoted the coin to micro, and the operator's mcap CEILING
stopped applying. A guard being switched off must never widen a different gate.
"""
from __future__ import annotations

import ast
import re

from _source_guard import _DropDocstrings, code_of


def code_of_text(text):
    """`code_of` for a source STRING — the mutation cases below build sabotaged variants."""
    return ast.unparse(_DropDocstrings().visit(ast.parse(text)))

from sniper.services import memecoin_real_runner as rr


def _entry_source():
    """CODE ONLY — comments and docstrings stripped, statements un-wrapped.

    `inspect.getsource` hands back prose, and this module's prose necessarily describes the very
    patterns these guards look for. Proved on 2026-08-27: delete the real chase gate, leave a
    comment describing it, and the regex below still matched — the guard passed over code that no
    longer existed. `ast.parse` drops comments (they are not in the tree) and `ast.unparse` joins a
    wrapped condition onto one line, which also removes the line-vs-statement trap.

    See AGENTS.md "Source-scanning guards" (925d8d77).
    """
    return code_of(rr)


def test_market_stats_are_fetched_independently_of_the_chase_cap():
    src = _entry_source()
    # normalised source puts the nested condition on consecutive lines with uniform indent
    assert re.search(r"if stats_fn is not None:\s*\n\s*if cached_stats is None:", src), (
        "the market-data read must be conditioned on stats_fn alone; coupling it to the chase cap "
        "means disabling the cap also blinds the mcap classifier")


def test_the_chase_cap_gates_only_the_refusal():
    """The cap is now resolved PER EVENT — a fresh follow reads
    MEMECOIN_REAL_FRESH_FOLLOW_CHASE_MAX_PCT and everything else reads
    MEMECOIN_REAL_CHASE_MAX_PCT. The invariant is unchanged and still holds against the resolved
    value: whichever cap applies, a 0 means 'do not refuse' and nothing else. This assertion named
    the old single-knob spelling and went red on main when the per-event cap landed, which blocked
    every funded deploy on a stale string rather than on a defect (2026-08-13)."""
    src = _entry_source()
    # MATCH THE INVARIANT, NOT THE ADJACENCY. This assertion was a literal
    # `"if event_chase_cap > 0 and not chase_ok("` and it went red the moment a `_manual_entry`
    # bypass was inserted between the two halves — a real edit, no behaviour change, one failing
    # test blocking a funded deploy for the second time. The property is that the resolved cap
    # gates the refusal and that the refusal is measured against that SAME cap. Anything may sit
    # between them.
    cond = re.search(r"if \(?event_chase_cap > 0\b.*?chase_ok\((.*?)\)\)", src, re.S)
    assert cond, ("the resolved cap must gate the refusal itself, so 0 means 'do not refuse' "
                  "and nothing else")
    assert "max_pct=event_chase_cap" in cond.group(1), (
        "the refusal must be measured against the SAME cap it was gated on")


_GATE_RX = r"if \(?event_chase_cap > 0\b.*?chase_ok\((.*?)\)\)"


def _gate_holds(text):
    """Run the guard's own check against arbitrary source. Used to prove it can fail."""
    m = re.search(_GATE_RX, code_of_text(text), re.S)
    return bool(m) and "max_pct=event_chase_cap" in m.group(1)


def test_the_guard_itself_can_fail():
    """AGENTS.md "Source-scanning guards" rule 3: inject each violation shape, watch it fire.

    A guard that cannot fail proves nothing, and this one could not. Before the prose was stripped,
    deleting the real gate and leaving a comment describing it still MATCHED — the guard passed
    over code that no longer existed. The last case is the opposite failure and the reason this
    assertion is a regex rather than a literal: a legitimate edit must not break it.
    """
    valid = """
def hook():
    if (event_chase_cap > 0 and not _manual_entry
            and not _waived_early('chase_past_entry') and not chase_ok(
            ev, our_price=our_price, max_pct=event_chase_cap)):
        return 'refused'
"""
    assert _gate_holds(valid), "baseline: the valid sample must satisfy the guard"

    deleted = """
def hook():
    # if event_chase_cap > 0 and not chase_ok(ev, max_pct=event_chase_cap):
    if False:
        return 'refused'
"""
    assert not _gate_holds(deleted), "a deleted gate described only in a comment must FIRE"

    wrong_cap = valid.replace("max_pct=event_chase_cap", "max_pct=base_cap")
    assert not _gate_holds(wrong_cap), "gating on one cap and measuring another must FIRE"

    extra_term = valid.replace("and not _manual_entry", "and not _manual_entry and not _skip")
    assert _gate_holds(extra_term), (
        "a legitimate new term between the halves must NOT fire — that break blocked a funded "
        "deploy twice")


def test_a_zero_cap_does_not_appear_in_the_fetch_condition():
    """The specific coupling that caused the bug must not come back in either order."""
    src = _entry_source()
    for bad in (r"cfg\.MEMECOIN_REAL_CHASE_MAX_PCT > 0 and stats_fn is not None",
                r"stats_fn is not None and cfg\.MEMECOIN_REAL_CHASE_MAX_PCT > 0"):
        assert not re.search(bad, src), f"the fetch must not depend on the cap: {bad!r}"


def test_chase_ok_itself_is_unchanged_and_still_refuses_a_runner():
    """The predicate keeps working; only its call site is now conditional."""
    class Ev:
        their_price = 1.0
        mint = "m"
    assert rr.chase_ok(Ev(), our_price=1.10, max_pct=40) is True
    assert rr.chase_ok(Ev(), our_price=1.53, max_pct=40) is False
    # fail-OPEN on missing data is preserved: an unknown price cannot refuse
    assert rr.chase_ok(Ev(), our_price=None, max_pct=40) is True

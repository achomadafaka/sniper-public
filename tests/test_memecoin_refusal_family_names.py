"""A refusal family must name the gate that actually refused.

Controls review, 2026-09-08: "a Solana chase refusal is also classified as an execution-premium
refusal". It is, and on both chains.

The chase-on-quote gate returns `status: "skipped_chase"` and the reason

    "exact quote is +75% above source price (cap 50%)"

built in `solana_swap_executor.py:550` and `evm_swap_executor.py:569` from `chase_cap`
(MEMECOIN_REAL_CHASE_MAX_PCT, 50). The capture taxonomy filed any string starting "exact quote is "
and containing "above source price" under `execution_premium`.

They are different gates measured against different references:
  chase             quote vs the COPIED TRADER'S price, cap MEMECOIN_REAL_CHASE_MAX_PCT
  execution premium quote vs the CHART/DISPLAY price, cap MEMECOIN_REAL_MAX_EXEC_PREMIUM
                    -> "route quote is 2.20x the chart mid (cap 1.60x)", family `route_over_mid`

So every chase-quote refusal was counted as an execution-premium refusal, and the real
execution-premium refusals sat under a differently named family. Any report grouping by family was
wrong in both directions.

`execution_premium` remains correct everywhere else — the trace substage, the knob's gate_family,
`release_pending`. Only the capture family matched the wrong string, which is why the fix is one
line rather than a rename across the codebase.
"""
from sniper.services import memecoin_refusal_capture as R

CHASE_QUOTE = "exact quote is +75% above source price (cap 50%)"
EXEC_PREMIUM = "route quote is 2.20x the chart mid (cap 1.60x)"
CHASE_FIRST_SEEN = "coin ran 3.4x above first-seen price (cap 3.0x)"
CHASE_PAST_ENTRY = "coin already ran +52% past their entry (cap 50%)"


def _family(reason):
    for name, match in R.GATES:
        if match(reason):
            return name
    return None


def test_a_chase_quote_refusal_is_not_called_execution_premium():
    """THE REGRESSION."""
    assert _family(CHASE_QUOTE) == "chase_quote"


def test_the_real_execution_premium_refusal_keeps_its_own_family():
    assert _family(EXEC_PREMIUM) == "route_over_mid"


def test_the_two_chase_families_are_untouched():
    assert _family(CHASE_FIRST_SEEN) == "chase_first_seen"
    assert _family(CHASE_PAST_ENTRY) == "chase_past_entry"


def test_no_family_matches_two_different_gates():
    """The taxonomy is only useful if one refusal maps to one family."""
    reasons = [CHASE_QUOTE, EXEC_PREMIUM, CHASE_FIRST_SEEN, CHASE_PAST_ENTRY]
    fams = [_family(r) for r in reasons]
    assert len(set(fams)) == len(fams), dict(zip(reasons, fams))
    assert None not in fams


def test_execution_premium_survives_where_it_is_correct():
    """The substage, the knob gate_family and release_pending all name the real gate and must not
    be swept up in the rename."""
    from sniper.services import memecoin_entry_knobs as K
    knob = {k.name: k for k in K._KNOBS}["MEMECOIN_REAL_MAX_EXEC_PREMIUM"]
    assert knob.gate_family == "execution_premium"


def test_a_drawdown_pause_refusal_is_named_and_captured():
    """2026-09-17 (operator): the pause's first two windows could only be judged from a DexScreener
    snapshot hours later, because nothing priced the refused coins."""
    from sniper.services import memecoin_refusal_capture as cap

    reason = ("drawdown pause: today's realized PnL fell $100 below the day's high; "
              "automatic entries resume 20:13:22 UTC")

    assert cap.classify_gate(reason) == "drawdown_pause"
    assert "drawdown_pause" in cap.CAPTURED_FAMILIES
    assert "drawdown_pause" not in cap.SIZED_FAMILIES
    # a daily-loss refusal is a different sentence and stays unnamed
    assert cap.classify_gate("daily loss limit reached (latched)") != "drawdown_pause"


def test_a_rendered_mcap_floor_refusal_is_named_and_captured():
    """The per-wallet floor (2026-09-18, smol_intern on 4663) must leave its refused small caps
    observable, or the band split can never be re-read from that wallet's own coins."""
    from sniper.services import memecoin_refusal_capture as cap

    assert cap.classify_gate("mcap $84,120 under floor $250,000 (under_floor)") == "mcap_under_floor"
    assert "mcap_under_floor" in cap.CAPTURED_FAMILIES
    # the retired bare token stays unmatched, as it does for the ceiling
    assert cap.classify_gate("under_floor") != "mcap_under_floor"

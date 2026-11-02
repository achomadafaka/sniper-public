"""RISK prompt v9 (Codex eCat review): an unknown or missing fact is never a break, precedence is explicit, citations need positive
gap-free evidence. Prompt-only change: validation, caps, cooldowns and apply guards are untouched (their own tests)."""
import re

from sniper.services import memecoin_desk_risk_live as L

P = " ".join(L.SYSTEM_PROMPT.split())


def test_version_names_the_prompt_change():
    assert L.RISK_LIVE_VERSION == "desk-risk-live-v12"


def test_a_missing_support_or_swing_is_unknown_never_an_exit_trigger():
    assert "support_below_pct missing or price under" not in P                       # the v8 contradiction
    m = re.search(r"support lost \((.*?)\), control flips", P)
    assert m and "UNKNOWN, not a break" in m.group(1) and "missing" in m.group(1)
    assert "sb = 1, or the bars show closes under the last swing low" in m.group(1)               # the evidence that CAN prove it
    assert "healthy run with no pivot under price AND after a pivot has been crossed" in m.group(1)
    assert 'A window with state "unknown" is no evidence either way' in P             # and the two rules now agree
    assert "on a break you can SEE in the data" in P


def test_precedence_is_explicit_engine_then_position_evidence_then_the_model():
    assert "PRECEDENCE:" in P
    i = P.index("PRECEDENCE:")
    block = P[i:i + 900]
    assert block.index("rails come first") < block.index("valid position facts and evidence") < block.index("then your action")


def test_unknown_stale_sparse_and_graduation_never_read_as_loss_or_collapse():
    block = P[P.index("PRECEDENCE:"):]
    for phrase in ("A missing or stale quote or mark is not a loss", "sparse tape, a gap, a missing USD conversion or a graduation step is not a collapse (a real, valid price or FX move still is evidence)",
                   "no attention signal is not calm", "mk counts addresses, not people"):
        assert phrase in block, phrase


def test_citations_need_positive_gap_free_evidence_and_hold_never_rescues_a_triggered_rail():
    block = P[P.index("PRECEDENCE:"):]
    assert "positively present in gap-free data" in block and "never infer one" in block
    assert "HOLD leaves the rails running" in block and "never loosen a stop or trail to rescue a rail that has triggered" in block


def test_the_existing_structure_rules_survive():
    for phrase in ("STRUCTURE RULES", "control flips to sellers", "failed breakout NOW",
                   "TIGHTEN while the trend holds"):
        assert phrase in P, phrase


def test_a_failed_breakout_needs_current_evidence_not_the_path_dependent_flag_alone():
    assert "failed breakout NOW (broke true and holding false AND the broken level and the failure are both observable in the supplied bars and trend, price back under that level" in P
    assert "holding is path-dependent and also false on a touch or equality, one earlier undercut keeps it false after a reclaim, so false alone is not a failure now" in P
    assert "the sheet publishes no breakout reference price and crossed pivots disappear, so an absent observable reference is UNKNOWN: never invent a level" in P


# The two data facts the wording rests on (structure.py), so the prompt cannot drift from the numbers it describes.
def test_support_below_is_none_on_a_healthy_no_pivot_run_and_after_a_pivot_is_crossed():
    from sniper.services import memecoin_desk_structure as S
    T = 1_000_000.0
    sw = lambda rel, price, i: {"received_at": T + rel, "slot": 1000 + i, "signature": "s%d" % i, "ordinal": 0, "is_buy": 1,
                                "usd": 10.0, "price_usd": price, "actor": "a%d" % (i % 3)}
    monotone = [sw(-290 + i * 10, 1.0 + 0.01 * i, i) for i in range(29)]                          # rising, no pivot at all
    assert S.structure(monotone, as_of=T, cut_slot=10**6)["swings"]["support_below_pct"] is None
    crossed = [sw(-290 + i * 10, pr, i) for i, pr in enumerate([1.2, 1.0, 1.3, 1.1, 1.4, 1.2, 1.35, 0.8, 0.75])]
    out = S.structure(crossed, as_of=T, cut_slot=10**6)
    assert out["price_usd"] == 0.75 and out["swings"]["support_below_pct"] is None                  # pivots 1.0/1.1/1.2 were crossed: they vanish
    held = [sw(-290 + i * 10, pr, i) for i, pr in enumerate([1.2, 1.0, 1.3, 1.1, 1.4, 1.2, 1.35, 1.3, 1.34])]
    assert S.structure(held, as_of=T, cut_slot=10**6)["swings"]["support_below_pct"] is not None   # the same coin above its pivots: a number


def test_breakout_holding_stays_false_after_a_dip_below_the_level_and_a_full_reclaim():
    from sniper.services import memecoin_desk_structure as S
    T = 1_000_000.0
    sw = lambda rel, price, i: {"received_at": T + rel, "slot": 1000 + i, "signature": "s%d" % i, "ordinal": 0, "is_buy": 1,
                                "usd": 10.0, "price_usd": price, "actor": "a%d" % (i % 3)}
    # level 1.5 set early; a break to 1.6 inside the last 15 s... then an undercut to 1.4 and a reclaim to 1.9 within the 15 s tail
    path = [(-50, 1.0), (-40, 1.5), (-30, 1.2), (-14, 1.6), (-10, 1.4), (-5, 1.9), (-1, 1.95)]
    out = S.structure([sw(r, p, i) for i, (r, p) in enumerate(path)], as_of=T, cut_slot=10**6)
    b = out["breakout"]["60s"]
    assert b["broke"] is True and b["holding"] is False and out["price_usd"] == 1.95              # reclaimed, yet holding says False


def test_the_three_bases_and_agent_volume_are_stated_apart():
    block = P[P.index("PRECEDENCE:"):]
    assert "agent volume does not prove independent organic demand" in block
    assert ("rows are the price as a ratio of your FILL in bp (10000 = your fill price), the st chart is the CHANGE versus the CURRENT "
            "price in bp (0 = the current price): two different encodings, not one baseline; pnl_pct_exact is the executable whole-bag PnL") in block

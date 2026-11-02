"""exit_card — actual realized vs hold-to-resolution, entry-band stratified.

Positions: {entry_price, size, won (0/1), realized_pnl, is_early_exit}. Early exits
are taken ONLY from the explicit `is_early_exit` attribution flag — NEVER inferred
from realized_pnl != hold (a fee/rounding mismatch is not an exit; see
exit_attribution.py's SELL-evidence requirement). hold-to-resolution PnL =
size*(won - entry_price); exit_gain = realized_pnl - hold.

DATA TIER = PROXY: the pre-registered null is a within-band exit-timestamp shuffle,
which needs per-position exit-time price paths we do NOT have. The sign-flip null is
a labelled proxy only, so the card is DESCRIPTIVE_ONLY and leaves placebo_passed=None
— admission (which requires placebo_passed is True) will NOT promote it until a real
exit-time price feed exists.
"""
from __future__ import annotations

from sniper.analysis.thesis_cards.exit_card import exit_card


def _cut_loser(entry=0.5, size=100, exit_price=0.3):
    hold = size * (0 - entry)
    realized = size * (exit_price - entry)
    return {"entry_price": entry, "size": size, "won": 0, "realized_pnl": realized,
            "is_early_exit": True}


def test_exit_card_describes_positive_exit_gain_but_is_not_promotable():
    # 20 attributed early-exits in the 0.4-0.6 band, each cut a loser (realized -20 vs hold -50 = +30)
    positions = [_cut_loser() for _ in range(20)]
    card = exit_card(positions, n_perm=300, seed=1)
    assert card.card_id == "exit_card"
    assert card.effect > 0                          # exits beat hold (descriptive)
    assert card.evidence_state == "descriptive_only"
    assert card.placebo_passed is None              # proxy null is not the pre-registered placebo
    assert card.evidence.get("data_tier") == "PROXY"
    assert card.q_value is None and card.eligible_for_paper is None  # router-owned


def test_exit_card_no_attributed_exits_is_underpowered():
    # everything held to resolution (is_early_exit False) → no scored exits → underpowered
    positions = [{"entry_price": 0.5, "size": 100, "won": 1, "realized_pnl": 50, "is_early_exit": False}
                 for _ in range(30)]
    card = exit_card(positions, n_perm=300, seed=1)
    assert card.evidence_state == "underpowered"


def test_exit_card_does_not_infer_exits_from_pnl_mismatch():
    # tiny fee/rounding mismatch with NO is_early_exit flag must NOT be counted as an exit
    positions = [{"entry_price": 0.5, "size": 100, "won": 1, "realized_pnl": 49.9999}  # ≈ hold 50, no flag
                 for _ in range(30)]
    card = exit_card(positions, n_perm=300, seed=1)
    assert card.evidence_state == "underpowered"
    assert card.n == 0


def test_exit_card_worse_than_hold_is_contradicted():
    # she exits winners early and gives up gains: realized 20 vs hold 50 = -30
    positions = [{"entry_price": 0.5, "size": 100, "won": 1, "realized_pnl": 20, "is_early_exit": True}
                 for _ in range(20)]
    card = exit_card(positions, n_perm=300, seed=1)
    assert card.effect < 0
    assert card.evidence_state == "contradicted"


def test_exit_card_stratifies_by_entry_band():
    band_a = [_cut_loser(entry=0.5) for _ in range(18)]            # gain +30
    band_b = [{"entry_price": 0.15, "size": 100, "won": 0, "realized_pnl": -15, "is_early_exit": True}
              for _ in range(18)]                                   # hold -15, gain 0
    card = exit_card(band_a + band_b, n_perm=300, seed=2)
    assert "by_band" in card.evidence
    assert card.effect > 0


def test_exit_card_is_pure_deterministic():
    p = [_cut_loser() for _ in range(20)]
    a = exit_card(p, n_perm=200, seed=7)
    b = exit_card(p, n_perm=200, seed=7)
    assert (a.effect, a.p_value, a.evidence_state) == (b.effect, b.p_value, b.evidence_state)

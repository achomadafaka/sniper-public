"""Unit tests for the Binance-reactive maker quoting policy (pure compute)."""
import pytest

from sniper.algos.polymarket_crypto5m.maker_policy import desired_quotes, should_unwind, Quote


def _base(**kw):
    args = dict(fair=0.60, best_bid=0.55, best_ask=0.65, inventory=0, max_inventory=200,
                seconds_left=120, base_margin=0.02, base_size=50.0, inv_skew=0.5)
    args.update(kw)
    return desired_quotes(**args)


def test_flat_inventory_posts_bid_only_no_naked_ask():
    # LONG-ONLY contract: with no inventory there is nothing to sell — an ask here would be a
    # naked synthetic short (impossible on Polymarket without minting a set). Bid only.
    q = _base()
    assert q["bid"].side == "bid" and q["bid"].price == 0.58
    assert q["ask"] is None


def test_held_inventory_straddles_fair_with_no_loss_guards():
    q = _base(inventory=60)
    assert q["bid"].side == "bid" and q["ask"].side == "ask"
    # never bid above fair, never ask below fair
    assert q["bid"].price < 0.60 < q["ask"].price
    # ask sized to what we actually hold (scale-out), never more
    assert q["ask"].size <= 60


def test_ask_size_never_exceeds_held_inventory():
    q = _base(inventory=20)
    assert q["ask"] is not None and q["ask"].size <= 20


def test_top_of_book_improves_into_a_wide_book():
    # wide 0.40/0.80 book; our fair-based bid 0.58 should sit inside (top of book), not at 0.40
    q = _base(best_bid=0.40, best_ask=0.80)
    assert q["bid"].price == 0.58
    assert "top-of-book" in q["reason"]


def test_bid_does_not_chase_when_our_fair_is_below_market():
    # fair 0.50 but market bid is 0.55: we rest below, we do NOT lift to 0.55 (no chasing)
    q = _base(fair=0.50, best_bid=0.55, best_ask=0.65)
    assert q["bid"].price < 0.55
    assert q["bid"].price < 0.50  # still below fair


def test_min_quote_edge_keeps_bid_away_from_fair_even_when_top_of_book_is_close():
    q = _base(fair=0.60, best_bid=0.58, best_ask=0.70, min_quote_edge=0.04)
    assert q["bid"].price == 0.56
    assert 0.60 - q["bid"].price == pytest.approx(0.04)


def test_min_quote_edge_keeps_ask_away_from_fair_even_when_top_of_book_is_close():
    q = _base(fair=0.60, best_bid=0.50, best_ask=0.62, inventory=50, min_quote_edge=0.04)
    assert q["ask"].price == 0.64
    assert q["ask"].price - 0.60 == pytest.approx(0.04)


def test_long_inventory_shrinks_bid_and_keeps_ask():
    # half the long cap -> post less bid (don't get longer), full ask (scale out): native partial exit
    q = _base(inventory=100)
    assert q["bid"].size == 25.0      # 50 * (1 - 0.5)
    assert q["ask"].size == 50.0


def test_max_long_stops_bidding():
    q = _base(inventory=200)
    assert q["bid"] is None
    assert q["ask"] is not None       # still willing to sell down


def test_quotes_pulled_in_final_seconds():
    q = _base(seconds_left=2)
    assert q["bid"] is None and q["ask"] is None
    assert "settling" in q["reason"]


def test_quotes_pulled_earlier_than_before_settlement():
    # near-settlement extremes are max adverse selection — the pull window widened 4s -> 12s
    q = _base(seconds_left=10)
    assert q["bid"] is None and q["ask"] is None
    assert "settling" in q["reason"]


def test_price_band_no_quotes_on_near_certain_favorite():
    # fair 0.97: 3c of payoff left, unlimited downside on a late flip — don't quote at all
    q = _base(fair=0.97, best_bid=0.96, best_ask=0.98, inventory=60)
    assert q["bid"] is None and q["ask"] is None
    assert "band" in q["reason"]


def test_price_band_no_quotes_on_deep_underdog():
    q = _base(fair=0.03, best_bid=0.02, best_ask=0.04, inventory=60)
    assert q["bid"] is None and q["ask"] is None
    assert "band" in q["reason"]


def test_no_fair_no_quotes():
    q = _base(fair=None)
    assert q["bid"] is None and q["ask"] is None


def test_should_unwind_hard_cut():
    assert should_unwind(fair=0.45, inventory=100, entry_price=0.60, hard_cut=0.10)[0] is True
    assert should_unwind(fair=0.55, inventory=100, entry_price=0.60, hard_cut=0.10)[0] is False
    assert should_unwind(fair=0.55, inventory=-100, entry_price=0.40, hard_cut=0.10)[0] is True
    assert should_unwind(fair=0.50, inventory=0, entry_price=0.60)[0] is False


# ---- maker-deep-only-v1: min_bid_depth (never lead the book; only rest deep) ----
def test_deep_only_posts_a_deep_bid():
    q = desired_quotes(fair=0.50, best_bid=0.49, best_ask=0.51, inventory=0.0, max_inventory=50,
                       seconds_left=120, base_margin=0.05, base_size=25, min_bid_depth=0.03)
    assert q["bid"] is not None
    assert (0.49 - q["bid"].price) >= 0.03 - 1e-9      # at least 3c below the touch
    assert "deep-only" in q["reason"]
    assert "top-of-book" not in q["reason"]            # never leads the book


def test_deep_only_skips_a_near_touch_bid():
    # margin 0.02 -> raw bid ~1c below touch -> too shallow for a 3c floor -> no bid
    q = desired_quotes(fair=0.50, best_bid=0.49, best_ask=0.51, inventory=0.0, max_inventory=50,
                       seconds_left=120, base_margin=0.02, base_size=25, min_bid_depth=0.03)
    assert q["bid"] is None


def test_deep_only_never_leads_the_book_even_when_fair_is_at_touch():
    # a fair right at the book would normally trigger the top-of-book jump; deep-only must not
    q = desired_quotes(fair=0.49, best_bid=0.48, best_ask=0.50, inventory=0.0, max_inventory=50,
                       seconds_left=120, base_margin=0.005, base_size=25, min_bid_depth=0.03)
    assert q["bid"] is None or "top-of-book" not in q["reason"]


def test_min_bid_depth_default_is_unchanged_behavior():
    # min_bid_depth defaults to 0 -> identical to not passing it (isolation for all other campaigns)
    kw = dict(fair=0.50, best_bid=0.49, best_ask=0.51, inventory=0.0, max_inventory=50,
              seconds_left=120, base_margin=0.05, base_size=25)
    assert desired_quotes(**kw) == desired_quotes(**kw, min_bid_depth=0.0)

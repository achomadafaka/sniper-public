"""A chase guard on the REAL path — and the follow-activity signals it unlocks.

TWO holes, found 2026-07-14 by asking "what enters paper, wins, and never becomes real money?":

1. `memecoin_follow_activity.py` never called `real_hook` (grep -c -> 0). The real leg only ever saw
   `global_feed`, so ~50% of the curated buy tape (286 buys) was untradeable by construction. On the
   real_shadow arm (the live real rails), armed wallets, mint grain: follow_activity produced 61
   priceable mints worth +$391 vs global_feed's 52 mints / +$188. Honest read — strip the single best
   coin from each and the per-shot edge VANISHES ($98 vs $112). The value is not a better shot, it is
   roughly DOUBLE the at-bats from the same allowlist. On a lottery-shaped payoff that is the highest-
   leverage change available; it is NOT 2x the PnL per dollar and must not be sold as one.

2. There was NO CHASE GUARD ANYWHERE ON THE REAL PATH. Its only protection against a stale or
   already-pumped signal was the 120s staleness gate. That is fine for a live feed; it is NOT fine for
   follow-activity, whose poll cadence is 300s — those signals are 120-900s old BY CONSTRUCTION, so
   the coin has had minutes to run before we could fill. Wiring follow-activity to real money without
   a chase guard would mean buying coins that already moved, blind.

So the guard is built FIRST, and it is what makes the wiring safe. It is a positive-selection gate
(don't buy a coin that already ran away from the trader's entry), not a blanket safety.
"""
from types import SimpleNamespace

import pytest

from sniper.services.memecoin_real_runner import chase_gap_pct, chase_ok


def _ev(their_price):
    return SimpleNamespace(side="buy", source_user_id="u1", mint="M", ticker="T",
                           their_price=their_price, network_id="solana")


def test_gap_is_our_price_vs_the_traders_entry():
    assert chase_gap_pct(_ev(1.0), our_price=1.20) == pytest.approx(20.0)
    assert chase_gap_pct(_ev(1.0), our_price=0.80) == pytest.approx(-20.0)
    assert chase_gap_pct(_ev(1.0), our_price=1.00) == pytest.approx(0.0)


def test_a_coin_that_already_ran_is_refused():
    """The whole point: by the time a 300s-cadence follow-activity signal reaches us, the coin may
    have already doubled. Buying THAT is not copying the trader, it is buying his exit liquidity."""
    assert chase_ok(_ev(1.0), our_price=1.30, max_pct=40) is True     # +30% — still inside
    assert chase_ok(_ev(1.0), our_price=1.60, max_pct=40) is False    # +60% — it ran, skip
    assert chase_ok(_ev(1.0), our_price=3.00, max_pct=40) is False    # +200% — absolutely not


def test_buying_BELOW_the_trader_is_never_a_chase():
    """A better price than the trader got is a gift, never a refusal — no lower bound here."""
    assert chase_ok(_ev(1.0), our_price=0.50, max_pct=40) is True
    assert chase_ok(_ev(1.0), our_price=0.01, max_pct=40) is True


def test_unknown_prices_do_not_block_the_entry():
    """Fail-OPEN on missing data. A gate that fires on absent data is the bug class that already cost
    us the EVM leg (honeypot) and the sub-$1M winners (mcap on feed gaps). If we cannot compute the
    gap we do not invent one — the other gates still apply."""
    assert chase_ok(_ev(None), our_price=1.0, max_pct=40) is True
    assert chase_ok(_ev(0), our_price=1.0, max_pct=40) is True
    assert chase_ok(_ev(1.0), our_price=None, max_pct=40) is True
    assert chase_ok(_ev(1.0), our_price=0, max_pct=40) is True


def test_a_broken_quote_is_refused_not_chased():
    """DexScreener has returned a 493,178% gap (a broken pair). That is not a pumping coin, it is a
    bad quote — and the chase cap is currently the ONLY thing catching it. Refuse it loudly."""
    assert chase_ok(_ev(1.0), our_price=5_000.0, max_pct=40) is False


# --- anchor window plumbing (2026-08-06) ---------------------------------------

def test_first_seen_floor_bounds_the_anchor():
    from sniper.services.memecoin_real_runner import _first_seen_floor
    floor = _first_seen_floor("2026-08-06T03:00:00+00:00", 21600)
    assert floor.startswith("2026-08-05T21:00:00")


def test_first_seen_floor_is_none_when_the_window_is_disabled():
    from sniper.services.memecoin_real_runner import _first_seen_floor
    for window in (0, -1, None, "nonsense"):
        assert _first_seen_floor("2026-08-06T03:00:00+00:00", window) is None
    assert _first_seen_floor(None, 21600) is None


def test_guard_still_refuses_a_genuine_recent_chase():
    """The documented losses entered 25-262 MINUTES after first-seen; a 6h window keeps every
    one of them anchored, so the guard that motivated this rule still fires."""
    from sniper.services.memecoin_real_runner import _first_seen_floor, first_seen_chase_ok
    floor = _first_seen_floor("2026-08-06T03:00:00+00:00", 21600)
    assert floor < "2026-08-06T02:35:00+00:00"        # a 25-minute-old anchor is inside
    assert floor < "2026-08-05T22:38:00+00:00"        # a 262-minute-old anchor is inside
    assert first_seen_chase_ok(first_price=1.0, price_now=4.0, max_ratio=3.0) is False
    assert first_seen_chase_ok(first_price=1.0, price_now=2.5, max_ratio=3.0) is True

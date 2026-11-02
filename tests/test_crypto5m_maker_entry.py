"""Tests for maker-entry execution (sniper/algos/polymarket_crypto5m/maker_entry.py) — the scalper's
maker-entry-v1 pivot: post a resting bid on the confirmed side instead of paying the ask."""
from __future__ import annotations

import pytest

from sniper.algos.polymarket_crypto5m.maker_entry import (
    MAX_ENTRY_PRICE, QUOTE_TTL_S, PendingQuote, desired_entry_bid, update_quote,
)


def _book(up_bid=None, up_ask=None, down_bid=None, down_ask=None):
    return {"up_bid": up_bid, "up_ask": up_ask, "down_bid": down_bid, "down_ask": down_ask}


CONFIRMED_UP = {"confirmed": True, "confirmed_side": "Up"}


# ---------- desired_entry_bid -----------------------------------------------------------------------

def test_improves_best_bid_by_one_tick():
    assert desired_entry_bid("Up", _book(up_bid=0.55, up_ask=0.58)) == 0.56


def test_never_crosses_the_ask():
    # bid+tick would equal the ask -> cap at ask-tick (maker, never taker)
    assert desired_entry_bid("Up", _book(up_bid=0.57, up_ask=0.58)) == 0.57


def test_caps_at_max_entry_price():
    assert desired_entry_bid("Up", _book(up_bid=0.95, up_ask=0.99)) == MAX_ENTRY_PRICE


def test_no_bid_side_rests_below_the_ask():
    assert desired_entry_bid("Up", _book(up_ask=0.50)) == 0.49


def test_empty_book_returns_none():
    assert desired_entry_bid("Up", _book()) is None


def test_down_side_uses_down_touch():
    assert desired_entry_bid("Down", _book(up_bid=0.90, up_ask=0.95, down_bid=0.05, down_ask=0.10)) == 0.06


# ---------- update_quote: fills ---------------------------------------------------------------------

def _pq(price=0.56, posted_ts=1000, decision_ask=0.58, side="Up", reprices=0):
    return PendingQuote(side=side, price=price, posted_ts=posted_ts, decision_ask=decision_ask,
                        reprices=reprices)


def test_fill_on_print_at_or_below_our_bid():
    r = update_quote(_pq(), book=_book(up_bid=0.55, up_ask=0.58), confirm=CONFIRMED_UP,
                     now_ts=1003, seconds_left=120, prints=[(0.56, 20.0)])
    assert r["action"] == "fill" and r["price"] == 0.56 and r["fill_model"] == "print"
    assert r["slip"] == pytest.approx(-0.02)   # 2c BETTER than the taker counterfactual


def test_no_fill_on_prints_above_our_bid():
    r = update_quote(_pq(), book=_book(up_bid=0.55, up_ask=0.58), confirm=CONFIRMED_UP,
                     now_ts=1003, seconds_left=120, prints=[(0.57, 20.0)])
    assert r["action"] == "rest"


def test_tape_alive_but_quiet_means_no_fill_even_if_book_quoted_through():
    r = update_quote(_pq(), book=_book(up_bid=0.50, up_ask=0.55), confirm=CONFIRMED_UP,
                     now_ts=1003, seconds_left=120, prints=[])
    assert r["action"] == "rest"


def test_tape_down_means_no_fill_print_only_contract():
    # tape unreachable -> NO fill, even though the book quoted through our price. Never fail-open:
    # a quote moving to our level is not a trade (codex review 2026-07-04).
    r = update_quote(_pq(), book=_book(up_bid=0.50, up_ask=0.55), confirm=CONFIRMED_UP,
                     now_ts=1003, seconds_left=120, prints=None)
    assert r["action"] == "rest"


# ---------- update_quote: cancels -------------------------------------------------------------------

def test_cancel_when_read_unconfirms():
    r = update_quote(_pq(), book=_book(up_bid=0.55, up_ask=0.58),
                     confirm={"confirmed": False, "confirmed_side": None},
                     now_ts=1003, seconds_left=120, prints=[])
    assert r["action"] == "cancel"


def test_cancel_when_read_flips_side():
    r = update_quote(_pq(), book=_book(up_bid=0.55, up_ask=0.58),
                     confirm={"confirmed": True, "confirmed_side": "Down"},
                     now_ts=1003, seconds_left=120, prints=[])
    assert r["action"] == "cancel"


def test_cancel_on_ttl():
    r = update_quote(_pq(posted_ts=1000), book=_book(up_bid=0.55, up_ask=0.58), confirm=CONFIRMED_UP,
                     now_ts=1000 + QUOTE_TTL_S, seconds_left=120, prints=[])
    assert r["action"] == "cancel"


def test_cancel_when_window_closing():
    r = update_quote(_pq(), book=_book(up_bid=0.55, up_ask=0.58), confirm=CONFIRMED_UP,
                     now_ts=1003, seconds_left=10, prints=[])
    assert r["action"] == "cancel"


# ---------- update_quote: repricing -----------------------------------------------------------------

def test_reprices_when_best_bid_moves_past_us():
    r = update_quote(_pq(price=0.56), book=_book(up_bid=0.58, up_ask=0.61), confirm=CONFIRMED_UP,
                     now_ts=1003, seconds_left=120, prints=[])
    assert r["action"] == "reprice"
    assert r["price"] == pytest.approx(0.57)   # capped at decision_ask(0.58) - tick — never pay taker+


def test_reprice_never_chases_past_the_taker_counterfactual():
    # best bid ran to 0.63, but the taker would have paid 0.58 at decision — chasing past that is pointless
    r = update_quote(_pq(price=0.56, decision_ask=0.58), book=_book(up_bid=0.63, up_ask=0.66),
                     confirm=CONFIRMED_UP, now_ts=1003, seconds_left=120, prints=[])
    assert r["action"] == "reprice" and r["price"] == pytest.approx(0.57)


def test_reprice_stops_after_max_reprices():
    r = update_quote(_pq(price=0.56, reprices=3), book=_book(up_bid=0.58, up_ask=0.61),
                     confirm=CONFIRMED_UP, now_ts=1003, seconds_left=120, prints=[])
    assert r["action"] == "rest"


def test_rests_when_still_top_of_book():
    r = update_quote(_pq(price=0.56), book=_book(up_bid=0.56, up_ask=0.58), confirm=CONFIRMED_UP,
                     now_ts=1003, seconds_left=120, prints=[])
    assert r["action"] == "rest"

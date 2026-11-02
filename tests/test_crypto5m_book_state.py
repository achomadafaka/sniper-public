"""Deterministic per-token order book. Best computed from a price->size map, never array position."""
from __future__ import annotations

import json
import os

from sniper.algos.polymarket_crypto5m.book_state import BookState

FX = os.path.join(os.path.dirname(__file__), "fixtures", "crypto5m")


def test_best_is_independent_of_input_array_order():
    # REST returns bids ascending / asks descending; WS doc says the opposite. Both must agree.
    rest = BookState(token_id="t", condition_id="c", side="Up")
    rest.apply_book(bids=[{"price": "0.01", "size": "10"}, {"price": "0.31", "size": "5"}],
                    asks=[{"price": "0.99", "size": "10"}, {"price": "0.32", "size": "5"}])
    ws = BookState(token_id="t", condition_id="c", side="Up")
    ws.apply_book(bids=[["0.31", "5"], ["0.01", "10"]],
                  asks=[["0.32", "5"], ["0.99", "10"]])
    assert rest.best_bid() == ws.best_bid() == 0.31
    assert rest.best_ask() == ws.best_ask() == 0.32
    assert round(rest.spread(), 9) == 0.01


def test_apply_book_drops_zero_size_levels():
    b = BookState(token_id="t", condition_id="c", side="Up")
    b.apply_book(bids=[["0.30", "0"], ["0.29", "100"]], asks=[["0.40", "50"]])
    assert b.best_bid() == 0.29


def test_price_change_updates_and_removes_levels():
    b = BookState(token_id="t", condition_id="c", side="Up")
    b.apply_book(bids=[["0.30", "100"]], asks=[["0.40", "100"]])
    b.apply_price_change([{"price": "0.31", "size": "50", "side": "BUY"}])   # new best bid
    assert b.best_bid() == 0.31
    b.apply_price_change([{"price": "0.31", "size": "0", "side": "BUY"}])    # cancel it
    assert b.best_bid() == 0.30
    b.apply_price_change([{"price": "0.40", "size": "0", "side": "SELL"}])   # cancel best ask
    assert b.best_ask() is None


def test_update_to_one_token_does_not_touch_another():
    up = BookState(token_id="UP", condition_id="c", side="Up")
    down = BookState(token_id="DOWN", condition_id="c", side="Down")
    up.apply_book(bids=[["0.30", "100"]], asks=[["0.40", "100"]])
    down.apply_book(bids=[["0.55", "100"]], asks=[["0.65", "100"]])
    up.apply_price_change([{"price": "0.31", "size": "10", "side": "BUY"}])
    # no complement arithmetic: Down is untouched and is NOT inferred from Up
    assert down.best_bid() == 0.55 and down.best_ask() == 0.65


def test_tick_size_and_last_trade_tracked():
    b = BookState(token_id="t", condition_id="c", side="Up")
    b.apply_tick_size_change(old="0.01", new="0.001")
    assert b.tick_size == 0.001
    b.apply_last_trade_price(price="0.33", side="BUY", size="20", fee_rate_bps="700")
    assert b.last_trade_price == 0.33
    assert b.last_trade_fee_rate_bps == 700.0


def test_taker_fill_walks_depth_for_fixed_notional():
    b = BookState(token_id="t", condition_id="c", side="Up")
    b.apply_book(bids=[["0.30", "1000"]],
                 asks=[["0.32", "50"], ["0.33", "1000"]])  # buy walks asks ascending
    fill = b.taker_buy_fill(usdc=20.0)
    # level 1: 50 sh * 0.32 = $16; remaining $4 at 0.33 -> 12.121 sh
    assert fill["fully_filled"] is True
    assert abs(fill["filled_usdc"] - 20.0) < 1e-9
    assert abs(fill["shares"] - (50 + 4 / 0.33)) < 1e-6
    assert 0.32 <= fill["avg_price"] <= 0.33


def test_taker_fill_partial_when_depth_insufficient():
    b = BookState(token_id="t", condition_id="c", side="Up")
    b.apply_book(bids=[], asks=[["0.50", "10"]])           # only $5 of depth
    fill = b.taker_buy_fill(usdc=20.0)
    assert fill["fully_filled"] is False
    assert abs(fill["filled_usdc"] - 5.0) < 1e-9
    assert abs(fill["shares"] - 10) < 1e-9


def test_real_rest_fixture_best_bid_ask():
    with open(os.path.join(FX, "clob_book_up.json")) as f:
        book = json.load(f)
    b = BookState(token_id=book["asset_id"], condition_id=book.get("market", "c"), side="Up")
    b.apply_book(bids=book["bids"], asks=book["asks"], book_hash=book.get("hash"))
    bb, ba = b.best_bid(), b.best_ask()
    assert bb is not None and ba is not None and bb < ba
    assert bb == max(float(x["price"]) for x in book["bids"])
    assert ba == min(float(x["price"]) for x in book["asks"])

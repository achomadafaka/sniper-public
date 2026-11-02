# tests/sim/test_replay_context.py
from sniper.sim.replay.context import ReplayContext, Fill, OrderResult
from sniper.sim.replay.fees import polymarket_taker_fee


def test_place_buy_order_fills_at_ask():
    ctx = ReplayContext(
        cash=1000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: polymarket_taker_fee(price, qty, 0.02),
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=100)
    assert result.filled == 100
    assert result.fill_price == 0.65
    assert result.fee > 0
    assert ctx.get_cash() < 1000.0
    assert ctx.get_positions()["YES"] == 100


def test_order_rejected_no_quote():
    ctx = ReplayContext(
        cash=1000.0,
        positions={},
        current_quotes={},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: 0.0,
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=100)
    assert result.filled == 0
    assert result.rejected


def test_fill_capped_to_liquidity():
    ctx = ReplayContext(
        cash=100000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 50.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: 0.0,
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=1000)
    assert result.filled == 50  # capped to ask_size
    assert result.partial


def test_fill_rejected_null_size():
    ctx = ReplayContext(
        cash=100000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": None}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: 0.0,
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=100)
    assert result.filled == 0
    assert result.rejected


def test_sell_fills_at_bid():
    ctx = ReplayContext(
        cash=0.0,
        positions={"YES": 200},
        current_quotes={"YES": {"best_bid": 0.70, "best_ask": 0.75, "bid_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: polymarket_taker_fee(price, qty, 0.02),
    )
    result = ctx.place_order(ticker="YES", side="SELL", count=100)
    assert result.filled == 100
    assert result.fill_price == 0.70
    assert ctx.get_positions()["YES"] == 100
    assert ctx.get_cash() > 0


def test_insufficient_cash():
    ctx = ReplayContext(
        cash=10.0,  # not enough for 100 @ 0.65
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: 0.0,
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=100)
    # Should fill what cash allows: 10 / 0.65 ≈ 15 shares
    assert result.filled <= 15
    assert result.partial


def test_settle_winning_position():
    """Buy YES → YES wins → positive PnL."""
    ctx = ReplayContext(
        cash=1000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: 0.0,  # zero fees for clarity
    )
    # Buy 100 YES @ 0.65 = cost $65
    ctx.place_order(ticker="YES", side="BUY", count=100)
    assert ctx.get_cash() == 935.0  # 1000 - 65
    assert ctx.get_positions()["YES"] == 100

    # YES wins: payout = 100 × $1.00 = $100
    payout = ctx.settle({"YES": 1.0, "NO": 0.0})
    assert payout == 100.0
    assert ctx.get_cash() == 1035.0  # 935 + 100
    assert ctx.get_pnl() == 35.0  # 1035 - 1000
    assert ctx.get_positions() == {}


def test_settle_losing_position():
    """Buy YES → NO wins → negative PnL."""
    ctx = ReplayContext(
        cash=1000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: 0.0,
    )
    ctx.place_order(ticker="YES", side="BUY", count=100)
    # NO wins: YES payout = 0
    payout = ctx.settle({"YES": 0.0, "NO": 1.0})
    assert payout == 0.0
    assert ctx.get_cash() == 935.0  # only remaining cash
    assert ctx.get_pnl() == -65.0  # lost the $65 cost


def test_settle_with_fees():
    """Full lifecycle with fees: buy → settle → PnL accounts for fees."""
    ctx = ReplayContext(
        cash=1000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: polymarket_taker_fee(price, qty, 0.02),
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=100)
    cost = 0.65 * 100  # $65
    fee = 0.02 * 0.65 * 0.35 * 100  # $0.455
    assert abs(result.fee - fee) < 0.001

    # YES wins
    ctx.settle({"YES": 1.0, "NO": 0.0})
    # PnL = payout(100) - cost(65) - fee(0.455) = 34.545
    assert abs(ctx.get_pnl() - (100.0 - cost - fee)) < 0.01


def test_get_fills_returns_history():
    """get_fills() returns ordered fill records."""
    ctx = ReplayContext(
        cash=1000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: 0.0,
    )
    ctx.place_order(ticker="YES", side="BUY", count=50)
    ctx.update_time("2026-04-03T13:00:00.000Z")
    ctx.place_order(ticker="YES", side="BUY", count=30)

    fills = ctx.get_fills()
    assert len(fills) == 2
    assert fills[0].count == 50
    assert fills[0].ts == "2026-04-03T12:00:00.000Z"
    assert fills[1].count == 30
    assert fills[1].ts == "2026-04-03T13:00:00.000Z"


def test_get_starting_cash():
    """Starting cash is preserved after trades."""
    ctx = ReplayContext(
        cash=1000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda ticker, price, qty: 0.0,
    )
    ctx.place_order(ticker="YES", side="BUY", count=100)
    assert ctx.get_starting_cash() == 1000.0
    assert ctx.get_cash() < 1000.0

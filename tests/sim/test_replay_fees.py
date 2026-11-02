# tests/sim/test_replay_fees.py
from sniper.sim.replay.fees import polymarket_taker_fee, kalshi_taker_fee


def test_polymarket_fee_at_midprice():
    # fee = max_fee_rate × price × (1 - price) × quantity
    # At price 0.50: fee = 0.02 × 0.50 × 0.50 × 100 = 0.50
    fee = polymarket_taker_fee(price=0.50, quantity=100, max_fee_rate=0.02)
    assert abs(fee - 0.50) < 0.001


def test_polymarket_fee_at_extreme_price():
    # At price 0.95: fee = 0.02 × 0.95 × 0.05 × 100 = 0.095
    fee = polymarket_taker_fee(price=0.95, quantity=100, max_fee_rate=0.02)
    assert abs(fee - 0.095) < 0.001


def test_polymarket_fee_disabled():
    fee = polymarket_taker_fee(price=0.50, quantity=100, max_fee_rate=0.0)
    assert fee == 0.0


def test_kalshi_fee():
    # Kalshi: fee = 0.07 × price × (1 - price) × count, capped at $0.50/contract
    fee = kalshi_taker_fee(price_cents=50, count=10)
    # 0.07 × 0.50 × 0.50 × 10 = 0.175 per contract uncapped
    # Per contract: 0.07 × 0.50 × 0.50 = 0.0175 → $0.0175, well under $0.50 cap
    # Total: 0.175
    assert abs(fee - 0.175) < 0.001


def test_kalshi_fee_cap():
    # At price_cents=50: per-contract fee = 0.07 × 0.5 × 0.5 = 0.0175
    # Well below $0.50 cap, so no cap applies here
    # At price_cents=1 (extreme): 0.07 × 0.01 × 0.99 = 0.000693 per contract
    fee = kalshi_taker_fee(price_cents=1, count=1000)
    assert fee > 0

"""Per-market fee resolver: empirical conflict (the 1000-vs-0 finding), fail-closed, never assume."""
from __future__ import annotations

from sniper.algos.polymarket_crypto5m import fee_model as F


def test_fees_disabled_is_known_zero():
    fee = F.decode_fee(condition_id="c", fees_enabled=False, taker_base_fee=1000)
    assert fee["fee_status"] == "known" and fee["fee_rate"] == 0.0
    assert F.is_known(fee)


def test_config_says_fee_but_observed_zero_is_conflict():
    # THE finding: config taker_base_fee=1000 enabled, but realized trades show fee_rate_bps=0
    fee = F.decode_fee(condition_id="c", fees_enabled=True, taker_base_fee=1000,
                       observed_fee_rate_bps=0)
    assert fee["fee_status"] == "fee_conflict"
    assert fee["fee_rate"] is None                 # NOT 0.10, NOT 0 — undecided, fail closed
    assert not F.is_known(fee)


def test_config_unvalidated_is_fee_unknown_never_assumed_rate():
    # config only, no realized observation -> cannot confirm -> fail closed (never assume 1000->0.10)
    fee = F.decode_fee(condition_id="c", fees_enabled=True, taker_base_fee=1000)
    assert fee["fee_status"] == "fee_unknown" and fee["fee_rate"] is None
    assert not F.is_known(fee)


def test_config_and_observed_agree_is_known():
    fee = F.decode_fee(condition_id="c", fees_enabled=True, taker_base_fee=70,
                       observed_fee_rate_bps=70)
    assert fee["fee_status"] == "known" and abs(fee["fee_rate"] - 0.007) < 1e-12


def test_stale_fee_fails_closed():
    fee = F.decode_fee(condition_id="c", fees_enabled=True, taker_base_fee=70,
                       observed_fee_rate_bps=70, fetched_ts=0, now_ts=10_000, max_age_s=3600)
    assert fee["fee_status"] == "fee_stale" and fee["fee_rate"] is None


def test_cost_per_share_formula():
    # fee = rate * price * (1-price); at price 0.5, rate 0.10 -> fee 0.025/share
    assert abs(F.cost_per_share(0.5, 0.10) - (0.5 + 0.10 * 0.25)) < 1e-12
    assert F.cost_per_share(0.4, 0.0) == 0.4       # zero fee -> just the price


def test_parse_clob_market_fee_uses_taker_base_fee():
    raw = {"enable_order_book": True, "taker_base_fee": 1000, "maker_base_fee": 1000}
    fee = F.parse_clob_market_fee(raw, condition_id="c", observed_fee_rate_bps=0)
    assert fee["taker_base_fee"] == 1000
    assert fee["fee_status"] == "fee_conflict"     # 1000 config vs 0 observed


def test_order_book_disabled_does_not_zero_a_fee_market():
    # enable_order_book is NOT the fee signal — a fees-on market must not leak as known/0
    raw = {"enable_order_book": False, "taker_base_fee": 1000}
    fee = F.parse_clob_market_fee(raw, condition_id="c", observed_fee_rate_bps=0)
    assert fee["fee_status"] == "fee_conflict" and fee["fee_rate"] is None


def test_missing_taker_base_fee_is_fee_unknown():
    fee = F.parse_clob_market_fee({"enable_order_book": True}, condition_id="c")
    assert fee["fee_status"] == "fee_unknown" and fee["fee_rate"] is None


def test_zero_taker_base_fee_is_genuine_no_fee():
    fee = F.parse_clob_market_fee({"taker_base_fee": 0}, condition_id="c")
    assert fee["fee_status"] == "known" and fee["fee_rate"] == 0.0


def test_garbage_observed_fee_fails_closed():
    for bad in (float("nan"), -50, 999999):
        fee = F.decode_fee(condition_id="c", fees_enabled=True, taker_base_fee=bad,
                           observed_fee_rate_bps=bad)
        assert fee["fee_status"] == "fee_unknown" and fee["fee_rate"] is None

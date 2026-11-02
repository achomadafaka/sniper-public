"""Tests for taker-fill realism (sniper/hyperlive/fills.py)."""
import math

from sniper.hyperlive import fills as F


def _book():
    # mid = 100. asks: 100.05 (x100sh), 100.15 (x100sh). bids: 99.95 (x100), 99.85 (x100).
    return {
        "mid": 100.0,
        "asks": [{"px": 100.05, "sz": 100.0}, {"px": 100.15, "sz": 100.0}],
        "bids": [{"px": 99.95, "sz": 100.0}, {"px": 99.85, "sz": 100.0}],
    }


def test_taker_slippage_within_top_level_is_half_spread():
    b = _book()
    # buy $1000 -> ~10 shares at 100.05, vwap≈100.05, slip = 5 bps
    slip = F.taker_slippage_bps(b, 1000, buy=True)
    assert math.isclose(slip, 5.0, abs_tol=0.2)


def test_taker_slippage_walks_into_second_level():
    b = _book()
    # buy $15,000: level1 has 100sh*100.05=$10,005, remainder ~$4,995 at 100.15
    slip = F.taker_slippage_bps(b, 15000, buy=True)
    assert slip > 5.0 and slip < 15.0                  # between the two levels' bps


def test_taker_slippage_none_when_book_too_thin():
    b = _book()                                        # total ask notional ~ $20,020
    assert F.taker_slippage_bps(b, 50000, buy=True) is None


def test_taker_slippage_zero_notional():
    assert F.taker_slippage_bps(_book(), 0, buy=True) == 0.0


def test_sell_side_uses_bids():
    b = _book()
    slip = F.taker_slippage_bps(b, 1000, buy=False)    # hit 99.95 -> 5 bps below mid
    assert math.isclose(slip, 5.0, abs_tol=0.2)


def test_round_trip_is_both_legs():
    b = _book()
    rt = F.round_trip_slippage_bps(b, 1000)
    buy = F.taker_slippage_bps(b, 1000, buy=True)
    sell = F.taker_slippage_bps(b, 1000, buy=False)
    assert math.isclose(rt, buy + sell, abs_tol=1e-6)


def test_round_trip_none_if_a_leg_infeasible():
    b = _book()
    assert F.round_trip_slippage_bps(b, 50000) is None


def test_depth_within_bps():
    b = _book()
    # within 5 bps of mid on ask side: only the 100.05 level (5 bps) -> $10,005
    d5 = F.depth_within_bps(b, 5, buy=True)
    assert math.isclose(d5, 100.05 * 100.0, rel_tol=1e-6)
    d25 = F.depth_within_bps(b, 25, buy=True)          # both ask levels within 25 bps
    assert d25 > d5


def test_spread_bps_computed_from_book():
    b = _book()
    assert math.isclose(F.spread_bps(b), 10.0, abs_tol=0.2)   # (100.05-99.95)/100 *1e4


def test_compact_book_roundtrips_for_slippage():
    b = _book()
    cb = F.compact_book(b, levels=15)
    assert cb["bids"][0] == [99.95, 100.0] and cb["asks"][0] == [100.05, 100.0]
    # slippage computed on the compacted book matches the original
    assert F.taker_slippage_bps(cb, 1000, buy=True) == F.taker_slippage_bps(b, 1000, buy=True)


def test_compact_book_default_keeps_every_returned_level_with_scope_metadata():
    book = {
        "mid": 100.0,
        "bids": [{"px": 99.0 - i / 100, "sz": 1.0} for i in range(20)],
        "asks": [{"px": 101.0 + i / 100, "sz": 1.0} for i in range(20)],
    }
    compact = F.compact_book(book)
    assert len(compact["bids"]) == len(compact["asks"]) == 20
    assert compact["returned_bid_levels"] == compact["stored_bid_levels"] == 20
    assert compact["returned_ask_levels"] == compact["stored_ask_levels"] == 20
    assert compact["client_truncated"] is False
    assert compact["depth_scope"] == "provider_returned_snapshot"
    assert compact["returned_bid_notional"] > 0
    assert F.compact_book(book, captured_ts_ms=123)["captured_ts_ms"] == 123


def test_compact_book_defensive_cap_is_explicit():
    book = {
        "mid": 100.0,
        "bids": [{"px": 99.0, "sz": 1.0} for _ in range(101)],
        "asks": [{"px": 101.0, "sz": 1.0} for _ in range(101)],
    }
    compact = F.compact_book(book)
    assert len(compact["bids"]) == len(compact["asks"]) == 100
    assert compact["returned_bid_levels"] == compact["returned_ask_levels"] == 101
    assert compact["client_truncated"] is True


def test_execution_at_size_distinguishes_measured_from_censored_depth():
    book = F.compact_book(_book())
    measured = F.execution_at_size(book, 1000.0, max_rt_bps=25.0)
    assert measured["status"] == "measured" and measured["feasible"] is True
    censored = F.execution_at_size(book, 100_000.0, max_rt_bps=25.0)
    assert censored["status"] == "censored_depth"
    assert censored["feasible"] is None


def test_legacy_fifteen_level_book_is_never_called_complete_capacity():
    legacy = {"mid": 100.0, "bids": [[99.9, 100.0]] * 15,
              "asks": [[100.1, 100.0]] * 15}
    result = F.capacity_with_provenance(legacy, max_rt_bps=25.0)
    assert result["depth_scope"] == "legacy_client_truncated_15"
    assert result["status"] == "lower_bound"
    assert result["capacity_lower_bound_usd"] > 0


def test_book_summary_shape():
    s = F.book_summary(_book())
    assert set(s) == {"mid", "spread_bps", "ask_depth_5bps", "ask_depth_25bps",
                      "bid_depth_5bps", "bid_depth_25bps"}
    assert s["mid"] == 100.0


def test_max_size_within_slippage():
    # book: $10,005 within 5bps each side, then a deep second level within ~50bps
    book = {"mid": 100.0,
            "asks": [{"px": 100.05, "sz": 100.0}, {"px": 100.5, "sz": 10000.0}],
            "bids": [{"px": 99.95, "sz": 100.0}, {"px": 99.5, "sz": 10000.0}]}
    # tiny budget -> only the tight top level fits (~half the $10k level per leg)
    small = F.max_size_within_slippage(book, 12.0)
    big = F.max_size_within_slippage(book, 90.0)
    assert 0 < small < big
    # a book too thin for even a dust round trip under 0.1bps returns ~0
    thin = {"mid": 100.0, "asks": [{"px": 100.5, "sz": 0.001}], "bids": [{"px": 99.5, "sz": 0.001}]}
    assert F.max_size_within_slippage(thin, 0.1) == 0.0

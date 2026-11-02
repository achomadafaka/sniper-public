from sniper.hyperlive import cluster_map as CM


def test_positions_from_clearinghouse_extracts_liq_levels():
    state = {"assetPositions": [
        {"position": {"coin": "BTC", "szi": "0.6", "entryPx": "64000", "liquidationPx": "60000"}},
        {"position": {"coin": "ETH", "szi": "-2", "entryPx": "3000", "liquidationPx": "3300"}},
        {"position": {"coin": "SOL", "szi": "0", "entryPx": "150", "liquidationPx": None}},  # skipped
    ]}
    pos = CM.positions_from_clearinghouse(state)
    assert {p["asset"] for p in pos} == {"BTC", "ETH"}
    btc = next(p for p in pos if p["asset"] == "BTC")
    assert btc["liq_px"] == 60000 and round(btc["notional"]) == round(0.6 * 64000)


def test_cluster_signal_points_toward_nearest_dense_cluster():
    # a dense cluster of shorts liquidating at ~102 (above 100) -> magnet pulls price UP -> long
    levels = [(102.0, 5_000_000.0), (102.05, 4_000_000.0), (95.0, 100_000.0)]
    sig = CM.cluster_signal(levels, mid=100.0)
    assert sig is not None
    side, dist_bps, notional = sig
    assert side == "long" and 180 < dist_bps < 220   # ~200bps above
    assert notional >= 5_000_000.0


def test_cluster_signal_below_price_is_short_and_respects_range():
    levels = [(98.0, 9_000_000.0)]
    side, dist, _ = CM.cluster_signal(levels, mid=100.0)
    assert side == "short" and 180 < dist < 220
    # cluster too far away -> no signal
    assert CM.cluster_signal([(50.0, 9e9)], mid=100.0, max_distance_bps=300.0) is None


def test_poll_clusters_aggregates_across_addresses():
    book = {
        "0xa": {"assetPositions": [{"position": {"coin": "HYPE", "szi": "100", "entryPx": "67", "liquidationPx": "68"}}]},
        "0xb": {"assetPositions": [{"position": {"coin": "HYPE", "szi": "200", "entryPx": "67", "liquidationPx": "68.01"}}]},
    }
    out = CM.poll_clusters(lambda a: book[a], ["0xa", "0xb"], {"HYPE": 67.0})
    assert "HYPE" in out and out["HYPE"][0] == "long"   # cluster above mid -> magnet up


# --- Thesis-1 magnet fix: whale-weighted clusters ---

def test_whale_weight_prefers_single_large_position():
    mid = 100.0
    # Bin A (just above): one whale of 8M. Bin B (also above, farther): 10 dust of 0.5M (5M total).
    levels = [(100.5, 8_000_000.0)] + [(101.0, 500_000.0)] * 10
    side, dist, notl = CM.cluster_signal(levels, mid, min_notional=1_000_000.0, whale_weight=True)
    assert side == "long"                       # cluster above price -> price pulled up -> long bias
    assert abs(dist - 50.0) < 5.0               # picked the 100.5 whale bin (~50bps away)
    assert notl >= 8_000_000.0


def test_equal_weight_picks_max_total_notional():
    mid = 100.0
    # Whale bin total 8M (single whale); dust bin total 12M (24 dust of 0.5M).
    levels = [(100.5, 8_000_000.0)] + [(101.0, 500_000.0)] * 24
    whale = CM.cluster_signal(levels, mid, min_notional=1_000_000.0, whale_weight=True)
    equal = CM.cluster_signal(levels, mid, min_notional=1_000_000.0, whale_weight=False)
    assert abs(whale[1] - 50.0) < 5.0           # whale-weight -> the 8M single-position bin
    assert abs(equal[1] - 100.0) < 5.0          # equal-weight -> the 12M dust-total bin (~100bps)


def test_equal_weight_off_keeps_min_notional_filter():
    mid = 100.0
    levels = [(100.5, 8_000_000.0), (101.0, 500_000.0)]
    out = CM.cluster_signal(levels, mid, min_notional=1_000_000.0, whale_weight=False)
    assert out is not None and out[0] == "long"  # the 0.5M dust bin is below min_notional, filtered

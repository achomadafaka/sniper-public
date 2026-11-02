from sniper.hyperlive.compute import RollingState


def _ctx(oi=1_000_000, funding=0.00001, mark=100, oracle=100):
    return {"openInterest": str(oi), "funding": str(funding), "markPx": str(mark), "oraclePx": str(oracle)}


def test_depth_collapse_ratio_drops_when_book_thins():
    rs = RollingState()
    rs.on_bbo("BTC", 1000, 100.0, 100.1, 5, 5)
    rs.on_ctx("BTC", 1000, _ctx())
    for ts in (100, 200, 300):                       # healthy baseline depth ~1000
        rs.on_l2book("BTC", ts, [(100.0, 5.0)], [(100.1, 5.0)])
    rs.on_l2book("BTC", 1000, [(100.0, 1.0)], [(100.1, 1.0)])  # book collapses to ~200
    s = rs.snapshot("BTC", 1000)
    assert s.features["depth_ratio"] < 0.5           # detected thinning
    assert s.features["depth_total"] > 0


def test_binance_cross_venue_lead_is_signed():
    rs = RollingState()
    rs.on_bbo("BTC", 1000, 100.0, 100.1, 5, 5)       # HL mid 100.05
    rs.on_ctx("BTC", 1000, _ctx())
    rs.binance["BTC"] = 101.0                          # binance leads up
    s = rs.snapshot("BTC", 1000)
    assert s.features["binance_lead_side"] == "long"
    assert s.features["binance_lead_bps"] > 0
    rs.binance["BTC"] = 99.0
    assert rs.snapshot("BTC", 1000).features["binance_lead_side"] == "short"


def test_oi_confirm_distinguishes_fueled_from_covering():
    # price up + OI up = fueled (+1)
    rs = RollingState()
    rs.on_bbo("X", 1000, 99.99, 100.01, 5, 5)         # ~60s ago mid 100
    rs.on_bbo("X", 61000, 100.99, 101.01, 5, 5)       # now mid ~101 (up)
    rs.on_ctx("X", 1000, _ctx(oi=1_000_000))
    rs.on_ctx("X", 61000, _ctx(oi=1_300_000))         # OI up
    assert rs.snapshot("X", 61000).features["oi_confirm"] == 1
    # price up + OI down = covering (-1)
    rs2 = RollingState()
    rs2.on_bbo("Y", 1000, 99.99, 100.01, 5, 5)
    rs2.on_bbo("Y", 61000, 100.99, 101.01, 5, 5)
    rs2.on_ctx("Y", 1000, _ctx(oi=1_000_000))
    rs2.on_ctx("Y", 61000, _ctx(oi=800_000))
    assert rs2.snapshot("Y", 61000).features["oi_confirm"] == -1


def test_mins_to_funding_is_in_range():
    rs = RollingState()
    rs.on_bbo("BTC", 1000, 100.0, 100.1, 5, 5)
    rs.on_ctx("BTC", 1000, _ctx())
    m = rs.snapshot("BTC", 1000).features["mins_to_funding"]
    assert 0.0 <= m <= 60.0

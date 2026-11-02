from sniper.hyperlive import follow_sim as FS


def _state(positions):
    # positions: list of (coin, szi, entry_px)
    return {"assetPositions": [
        {"position": {"coin": c, "szi": str(s), "entryPx": str(e),
                      "liquidationPx": None, "unrealizedPnl": "0"}}
        for c, s, e in positions]}


def test_positions_map_parses_side_and_size():
    m = FS.positions_map(_state([("ETH", 8.0, 2000.0), ("SOL", -50.0, 150.0)]))
    assert m["ETH"]["side"] == "long" and m["ETH"]["szi"] == 8.0
    assert m["SOL"]["side"] == "short" and abs(m["SOL"]["szi"]) == 50.0


def test_detect_entries_finds_new_and_increased_longs_and_shorts():
    prev = FS.positions_map(_state([("ETH", 8.0, 2000.0)]))
    cur = FS.positions_map(_state([("ETH", 12.0, 2050.0),      # increased long
                                   ("SOL", -50.0, 150.0)]))    # new short
    ent = FS.detect_entries(prev, cur)
    by = {e["coin"]: e for e in ent}
    assert by["ETH"]["side"] == "long" and by["ETH"]["kind"] == "increase"
    assert by["SOL"]["side"] == "short" and by["SOL"]["kind"] == "new"


def test_detect_entries_ignores_reductions_and_flat():
    prev = FS.positions_map(_state([("ETH", 12.0, 2000.0), ("BTC", 1.0, 60000.0)]))
    cur = FS.positions_map(_state([("ETH", 8.0, 2000.0)]))     # ETH reduced, BTC closed
    assert FS.detect_entries(prev, cur) == []


def test_detect_entries_treats_side_flip_as_new_entry():
    prev = FS.positions_map(_state([("ETH", 8.0, 2000.0)]))    # long
    cur = FS.positions_map(_state([("ETH", -5.0, 2010.0)]))    # flipped short
    ent = FS.detect_entries(prev, cur)
    assert ent[0]["coin"] == "ETH" and ent[0]["side"] == "short" and ent[0]["kind"] == "flip"


def test_follow_net_bps_long_and_short_net_of_fee():
    # long: +100bps move, minus 9bps round-trip fee = +91
    assert FS.follow_net_bps("long", 100.0, 101.0, fee_bps=9.0) == 91.0
    # short: price falls 100bps -> +100 gross, -9 fee = +91
    assert FS.follow_net_bps("short", 100.0, 99.0, fee_bps=9.0) == 91.0
    # long that goes wrong: -50bps move - 9 fee = -59
    assert FS.follow_net_bps("long", 100.0, 99.5, fee_bps=9.0) == -59.0


def test_follow_net_bps_guards_bad_prices():
    assert FS.follow_net_bps("long", 0.0, 100.0) is None
    assert FS.follow_net_bps("long", 100.0, None) is None

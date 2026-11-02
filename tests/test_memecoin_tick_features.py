"""Tick-tape feature job v0 — wash filter, OFI orientation, arrival accel, liquidity bridge."""
import sqlite3

from sniper.analysis.tick_features import (
    compute_pool_windows, compute_window_features, ensure_feature_tables, resolve_pool_meta,
    store_features, tick_liquidity_usd,
)
from sniper.persistence import db_ticks as t


def _tc():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c)
    ensure_feature_tables(c)
    return c


def _swap(maker, a0, a1):
    return {"maker": maker, "amount0": str(a0), "amount1": str(a1)}


# --- pure window features ---

def test_ofi_counts_buys_and_sells_by_quote_side():
    # quote is token0: buy of the coin = quote INTO pool (a0 > 0)
    swaps = [_swap("m1", 100, -5), _swap("m2", 200, -9), _swap("m3", -150, 7)]
    f = compute_window_features(swaps, quote_side=0)
    assert f["buy_n"] == 2 and f["sell_n"] == 1
    assert f["net_flow_quote"] == "150"
    assert f["wash_n"] == 0 and f["makers_n"] == 3


def test_wash_filter_removes_round_trippers_and_dust():
    swaps = [
        _swap("washer", 100, -5), _swap("washer", -100, 5),   # both sides -> excluded
        _swap("dust", 1, 0),                                   # |quote| <= dust -> excluded
        _swap("real", 500, -20),
    ]
    f = compute_window_features(swaps, quote_side=0, dust_quote_atomic=2)
    assert f["wash_n"] == 3
    assert f["buy_n"] == 1 and f["sell_n"] == 0
    assert f["net_flow_quote"] == "500"
    assert f["makers_n"] == 1


def test_unknown_maker_never_joins_wash_pairing():
    swaps = [_swap(None, 100, -5), _swap(None, -60, 3)]        # no maker attribution
    f = compute_window_features(swaps, quote_side=0)
    assert f["wash_n"] == 0 and f["buy_n"] == 1 and f["sell_n"] == 1
    assert f["makers_n"] == 0


# --- windows + arrival acceleration ---

def _tick(pool, kind, maker, a0, a1, at, sig):
    return {"chain_id": "4663", "pool": pool, "kind": kind, "block_no": 1, "tx_sig": sig,
            "log_index": 0, "maker": maker, "amount0": str(a0), "amount1": str(a1),
            "observed_at": at}


def test_pool_windows_compute_accel_and_latest_reserve():
    tc = _tc()
    rows = []
    # previous window (12:00-12:05): 1 swap; newest (12:05-12:10): 3 swaps + a sync
    rows.append(_tick("0xp", "v3_swap", "a", 10, -1, "2026-07-23T12:03:00+00:00", "s1"))
    for i, ts in enumerate(("12:06:00", "12:07:00", "12:08:00")):
        rows.append(_tick("0xp", "v3_swap", f"m{i}", 10, -1, f"2026-07-23T{ts}+00:00", f"x{i}"))
    rows.append(_tick("0xp", "v2_sync", None, 777, 42, "2026-07-23T12:07:30+00:00", "y1"))
    t.insert_ticks(tc, rows)
    out = compute_pool_windows(tc, "4663", "0xp", window_s=300,
                               now_iso="2026-07-23T12:10:00+00:00", quote_side=0)
    assert len(out) == 2
    assert out[0]["swaps_n"] == 1 and out[1]["swaps_n"] == 3
    assert out[1]["arrival_accel"] == 2.0
    assert out[1]["reserve_quote"] == "777"
    assert store_features(tc, out) == 2
    # idempotent recompute replaces, not duplicates
    assert store_features(tc, out) >= 0
    n = tc.execute("SELECT COUNT(*) FROM pool_features").fetchone()[0]
    assert n == 2


# --- pool orientation ---

def test_resolve_pool_meta_sets_quote_side_and_skips_failures():
    tc = _tc()
    usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    def eth_call(pool, selector):
        if pool == "0xbad":
            return None
        if selector.endswith("1681"):                      # token0()
            return "0x" + "0" * 24 + usdc[2:]
        return "0x" + "0" * 24 + "ab" * 20                 # token1() = the coin

    n = resolve_pool_meta(tc, "8453", ["0xgood", "0xbad"], eth_call, {usdc},
                          "2026-07-23T12:00:00+00:00")
    assert n == 1
    row = tc.execute("SELECT * FROM pool_meta WHERE pool='0xgood'").fetchone()
    assert row["quote_side"] == 0 and row["token0"] == usdc.lower()
    assert tc.execute("SELECT COUNT(*) FROM pool_meta WHERE pool='0xbad'").fetchone()[0] == 0


# --- liquidity bridge ---

def test_tick_liquidity_usd_from_latest_sync():
    tc = _tc()
    t.insert_ticks(tc, [
        _tick("0xp", "v2_sync", None, 5 * 10**18, 999, "2026-07-23T12:00:00+00:00", "a"),
        _tick("0xp", "v2_sync", None, 3 * 10**18, 999, "2026-07-23T12:05:00+00:00", "b"),
    ])
    # latest sync: 3 WETH quote reserve @ $2000 -> $6000 x2 = $12000
    usd = tick_liquidity_usd(tc, "4663", "0xp", quote_side=0, quote_decimals=18, quote_usd=2000.0)
    assert abs(usd - 12000.0) < 1e-6
    assert tick_liquidity_usd(tc, "4663", "0xnone", quote_side=0,
                              quote_decimals=18, quote_usd=2000.0) is None

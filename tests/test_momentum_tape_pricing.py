"""Task 3 — momentum_a prefers the tape price on 4663, falls back, and records attribution.

The position->pool lookup resolves from the position's MINT on the BASE side (spec §4.3b). It must
NOT use quote_route_target: that is the router/settler, not the pool, and has zero ticks by
construction — the original design did, and the first live gate run returned 0/30 aligned pairs.
These fixtures therefore key ticks by a pool address the position does NOT name, so a resolver that
reads quote_route_target cannot pass.
"""
import sqlite3

from sniper.analysis import tick_features as tf
from sniper.persistence import db_ticks as t
from sniper.services.memecoin_exits import tape_mid_for_position

POOL = "0xpool"
MINT = "0xc0in"
QUOTE = "0xweth"
ROUTER = "0x0000000000001ff368"      # what quote_route_target actually holds — never a pool


def _tc(block_ts_iso, *, quote_dec=18, base_dec=9, quote_side=0, extra_pool=False):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    t.ensure_tick_tables(c); t.ensure_block_times(c); tf.ensure_feature_tables(c)
    t.insert_ticks(c, [{"chain_id": "4663", "pool": POOL, "kind": "v2_swap", "block_no": 5,
                        "tx_sig": "0xa", "log_index": 0, "maker": "0xr",
                        "amount0": str(10**18), "amount1": str(-1000 * 10**9),
                        "observed_at": block_ts_iso}])
    t.backfill_block_times(c, "4663", [5], lambda b: 1_700_000_000, block_ts_iso)
    # quote_side=0 -> token0 is quote, token1 is base(mint)
    tok0, tok1 = (QUOTE, MINT) if quote_side == 0 else (MINT, QUOTE)
    d0, d1 = (quote_dec, base_dec) if quote_side == 0 else (base_dec, quote_dec)
    c.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, resolved_at,"
              " token0_decimals, token1_decimals) VALUES (?,?,?,?,?,?,?,?)",
              ("4663", POOL, tok0, tok1, quote_side, block_ts_iso, d0, d1))
    if extra_pool:      # a SECOND usable pool for the same mint -> ambiguous
        c.execute("INSERT INTO pool_meta (chain_id, pool, token0, token1, quote_side, resolved_at,"
                  " token0_decimals, token1_decimals) VALUES (?,?,?,?,?,?,?,?)",
                  ("4663", "0xpool2", tok0, tok1, quote_side, block_ts_iso, d0, d1))
    c.commit()
    return c


def _pos(**kw):
    p = {"id": 1, "network_id": "4663", "mint": MINT, "quote_route_target": ROUTER}
    p.update(kw)
    return p


def test_tape_prices_a_4663_position_resolved_from_its_mint():
    tc = _tc("2026-07-24T00:00:00+00:00")
    price, source = tape_mid_for_position(tc, _pos(), quote_usd=3000.0, now_ts=1_700_000_010)
    assert abs(price - 3.0) < 1e-9
    assert source == "tape"


def test_mint_matching_is_case_insensitive():
    tc = _tc("2026-07-24T00:00:00+00:00")
    price, _ = tape_mid_for_position(tc, _pos(mint=MINT.upper()), quote_usd=3000.0, now_ts=1_700_000_010)
    assert abs(price - 3.0) < 1e-9


def test_quote_side_1_resolves_the_base_side_correctly():
    tc = _tc("2026-07-24T00:00:00+00:00", quote_side=1, quote_dec=18, base_dec=9)
    # quote_side=1 -> amount1 is quote, amount0 is base; the seeded swap is quote-in/base-out
    price, source = tape_mid_for_position(tc, _pos(), quote_usd=3000.0, now_ts=1_700_000_010)
    assert source == "tape" and price > 0


def test_a_mint_that_is_the_quote_asset_never_resolves():
    """WETH is the 4663 quote asset and appears in 168 pools. A token0-OR-token1 match would
    resolve it to all of them; the base-side predicate must yield nothing."""
    tc = _tc("2026-07-24T00:00:00+00:00")
    assert tape_mid_for_position(tc, _pos(mint=QUOTE), quote_usd=3000.0, now_ts=1_700_000_010) == (None, None)


def test_ambiguous_mint_with_two_pools_fails_closed():
    tc = _tc("2026-07-24T00:00:00+00:00", extra_pool=True)
    assert tape_mid_for_position(tc, _pos(), quote_usd=3000.0, now_ts=1_700_000_010) == (None, None)


def test_stale_tape_does_not_price():
    tc = _tc("2026-07-24T00:00:00+00:00")
    price, source = tape_mid_for_position(tc, _pos(), quote_usd=3000.0, now_ts=1_700_009_999)
    assert price is None and source is None      # older than MEMECOIN_TAPE_PRICE_MAX_AGE_S


def test_unresolved_decimals_do_not_price():
    tc = _tc("2026-07-24T00:00:00+00:00")
    tc.execute("UPDATE pool_meta SET token1_decimals = NULL")
    tc.commit()
    assert tape_mid_for_position(tc, _pos(), quote_usd=3000.0, now_ts=1_700_000_010) == (None, None)


def test_non_4663_chain_is_never_tape_priced():
    tc = _tc("2026-07-24T00:00:00+00:00")
    for net in ("solana", "8453", "56", None):
        assert tape_mid_for_position(tc, _pos(network_id=net), quote_usd=3000.0,
                                     now_ts=1_700_000_010) == (None, None)


def test_momentum_a_chain_gate_admits_4663_and_solana_only():
    """Task 5, shipped only because the §5 cross-check PASSED (n=1768, median 1.78%, p90 12.62%)."""
    from sniper.services.memecoin_copy_trader import momentum_a_chain_allowed
    assert momentum_a_chain_allowed("solana") is True
    assert momentum_a_chain_allowed("") is True          # unknown-net legacy path, unchanged
    assert momentum_a_chain_allowed("4663") is True      # NEW: tape-priced
    for blocked in ("8453", "56", "1", "evm-unknown"):
        assert momentum_a_chain_allowed(blocked) is False


def test_sweep_caller_actually_passes_the_ticks_connection():
    """Without this the whole feature is dead code: sweep_exits defaults tc=None, so a caller that
    omits it silently falls back to DexScreener and 4663 positions time-cap at $0 as before."""
    import inspect
    from sniper.services import memecoin_runner
    src = inspect.getsource(memecoin_runner)
    assert "sweep_exits(conn, price_map, now_iso, tc=tc)" in src

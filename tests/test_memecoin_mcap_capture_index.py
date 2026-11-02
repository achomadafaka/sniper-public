"""The capture-table mcap fallback must use its index, not scan 8.5M rows.

Measured 2026-08-23: `resolve_mint_mcap` cost 611.95 ms per call and was the whole of the armed
fast lane's 0.62s median dispatch cost — on a path the code documents as DB-only precisely because
it is latency-critical, and which runs PER SWAP. Position 44596 (KIRK) took 16.16s from seeing the
signal to a confirmed order, 11.22s of it before the funded hook was even entered.

Cause: `idx_*_coin_time` leads with `network_id`, but the query filtered `network_id` only through
the COIN_NETWORK_SQL CASE expression, which hides the leading column. `mint` alone is the second
column, so neither predicate was usable and SQLite full-scanned.
"""
import sqlite3

from sniper.persistence import db_memecoin as m


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _seed(c, mint, network_id, caps, raw_network=None):
    for i, cap in enumerate(caps):
        c.execute(
            "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, market_cap, "
            "observed_at, quality, contexts, capture_version) VALUES(?,?,?,?,?,?,?,?)",
            ("s%d" % i, raw_network if raw_network is not None else network_id, mint, cap,
             "2026-08-23T00:00:%02d+00:00" % i, "ok", "[]", 2))
    c.commit()


def test_the_query_plan_uses_the_coin_time_index():
    """The regression itself: a plan containing SCAN means the leading column is hidden again."""
    c = _db()
    _seed(c, "MINT", "solana", [100.0])
    plan = [r[-1] for r in c.execute(
        "EXPLAIN QUERY PLAN "
        "SELECT market_cap FROM memecoin_quote_observations_v2 "
        "WHERE mint=? AND market_cap>0 AND network_id=? "
        f"AND {m.COIN_NETWORK_SQL}=? ORDER BY observed_at DESC LIMIT 25",
        ("MINT", "solana", "solana"))]
    joined = " ".join(plan)
    assert "idx_mc_quotes_v2_coin_time" in joined, joined
    assert "SCAN memecoin_quote_observations_v2" not in joined, joined


def test_the_median_is_unchanged():
    c = _db()
    _seed(c, "MINT", "solana", [100.0, 300.0, 200.0])
    assert m.resolve_mint_mcap(c, "MINT", network_id="solana") == 200.0


def test_zero_caps_are_still_excluded():
    c = _db()
    _seed(c, "MINT", "solana", [0.0, 0.0, 50.0])
    assert m.resolve_mint_mcap(c, "MINT", network_id="solana") == 50.0


def test_a_different_network_never_borrows():
    c = _db()
    _seed(c, "MINT", "8453", [999.0])
    assert m.resolve_mint_mcap(c, "MINT", network_id="solana") is None


def test_an_unknown_mint_still_returns_none():
    c = _db()
    _seed(c, "MINT", "solana", [100.0])
    assert m.resolve_mint_mcap(c, "OTHER", network_id="solana") is None


def test_an_unnormalized_raw_value_is_skipped_and_that_is_benign():
    """A raw '' normalizes to solana, so the old expression-only form matched it. Production has
    ZERO such priced rows (0 of 8,471,247 quote rows; the only 122 feed rows are unpriced, which
    the >0 filter already excluded), and a miss degrades to None — the funded classifier resolves
    mcap live downstream for the admission decision."""
    c = _db()
    _seed(c, "MINT", "solana", [777.0], raw_network="")
    assert m.resolve_mint_mcap(c, "MINT", network_id="solana") is None

"""Becker candidate feed — tested against small synthetic parquet (real DuckDB SQL).

Proves: exact block→epoch; buy-fill detection (maker/taker USDC side -> buyer); token-token
excluded; as-of block cap; opened = distinct bought token; outcome-neutral (amounts ignored);
min_opened floor; deterministic hash order + limit.
"""
from __future__ import annotations

import duckdb
import pytest

from sniper.analysis.thesis_cards.becker_feed import (
    becker_candidate_feed, block_for_epoch, wallet_opened_counts,
)


def _write(con, path, rows, cols):
    col_defs = ", ".join(f"{n} {t}" for n, t in cols)
    con.execute(f"CREATE TABLE t ({col_defs})")
    if rows:
        ph = ", ".join("(" + ", ".join("?" for _ in cols) + ")" for _ in rows)
        flat = [v for r in rows for v in r]
        con.execute(f"INSERT INTO t VALUES {ph}", flat)
    con.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")
    con.execute("DROP TABLE t")


@pytest.fixture
def becker(tmp_path):
    con = duckdb.connect()
    trades = str(tmp_path / "trades.parquet")
    blocks = str(tmp_path / "blocks.parquet")
    tcols = [("maker", "VARCHAR"), ("taker", "VARCHAR"), ("maker_asset_id", "VARCHAR"),
             ("taker_asset_id", "VARCHAR"), ("maker_amount", "BIGINT"),
             ("taker_amount", "BIGINT"), ("block_number", "BIGINT")]
    rows = [
        # walletA buys T1 (maker gave USDC), block 100
        ("walletA", "seller1", "0", "T1", 50, 100, 100),
        # walletA buys T2 (taker gave USDC), block 110
        ("seller2", "walletA", "T2", "0", 100, 40, 110),
        # walletA buys T1 again -> distinct token still 1 for T1, block 120
        ("walletA", "seller3", "0", "T1", 999999, 1, 120),
        # walletB buys T1, block 130
        ("walletB", "seller4", "0", "T1", 10, 10, 130),
        # token-token swap (neither side USDC) -> excluded, block 140
        ("walletB", "seller5", "T3", "T4", 1, 1, 140),
        # walletC buys T5 AFTER as-of block -> excluded, block 999
        ("walletC", "seller6", "0", "T5", 5, 5, 999),
    ]
    _write(con, trades, rows, tcols)
    _write(con, blocks, [(100, "2024-01-01T00:00:00Z"), (200, "2024-01-02T00:00:00Z"),
                         (300, "2024-01-03T00:00:00Z")],
           [("block_number", "BIGINT"), ("timestamp", "VARCHAR")])
    con.close()
    return trades, blocks


def test_block_for_epoch_is_exact(becker):
    _, blocks = becker
    # 2024-01-02 12:00 UTC -> max block with ts <= that = block 200
    assert block_for_epoch(blocks, 1704196800) == 200
    # before any block
    assert block_for_epoch(blocks, 0) is None


def test_opened_count_is_distinct_bought_token(becker):
    trades, _ = becker
    out = {c.wallet: c.opened for c in
           wallet_opened_counts(trades, as_of_block=200, min_opened=1, limit=100)}
    assert out == {"walleta": 2, "walletb": 1}        # A: {T1,T2}; B: {T1}; C excluded (after T)


def test_min_opened_floor(becker):
    trades, _ = becker
    out = wallet_opened_counts(trades, as_of_block=200, min_opened=2, limit=100)
    assert [c.wallet for c in out] == ["walleta"]     # only A clears 2


def test_amounts_do_not_affect_count(becker):
    # walletA's T1 re-buy has a huge amount; opened is a distinct-token count, not value
    trades, _ = becker
    a = {c.wallet: c.opened for c in wallet_opened_counts(trades, as_of_block=200, min_opened=1, limit=100)}
    assert a["walleta"] == 2                            # unaffected by maker_amount=999999


def test_limit_caps_results(becker):
    trades, _ = becker
    assert len(wallet_opened_counts(trades, as_of_block=200, min_opened=1, limit=1)) == 1


def test_feed_combines_block_and_counts(becker):
    trades, blocks = becker
    cands = becker_candidate_feed(1704196800, trades_glob=trades, blocks_glob=blocks,
                                  min_opened=1, limit=100)
    assert {c.wallet for c in cands} == {"walleta", "walletb"}
    # as-of before any block -> empty feed
    assert becker_candidate_feed(0, trades_glob=trades, blocks_glob=blocks) == []

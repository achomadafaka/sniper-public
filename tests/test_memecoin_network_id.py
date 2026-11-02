"""Item 3 — persist network_id on events + positions so per-chain eval (Solana vs EVM) is possible,
and route the real leg by network. The fomo feed already carries `networkId`; we were dropping it.
Backfill derivable historical rows: an `0x`-prefixed mint is EVM (chain unknown), else Solana."""
import sqlite3

from sniper.feeds.fomo_feed import normalize_feed_item
from sniper.persistence import db_memecoin as m


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _raw_buy(network="solana", mint="MintBase58Addr"):
    return {"type": "large_buy", "id": "e1", "userId": "u1", "tokenAddress": mint,
            "networkId": network, "createdAt": "2026-07-09T00:00:00Z",
            "body": {"userHandle": "h", "ticker": "T", "price": 1.0, "marketCap": 1000.0,
                     "currentSizeUsd": 500.0}}


def test_normalize_feed_item_carries_network_id():
    ev = normalize_feed_item(_raw_buy(network="8453"), "2026-07-09T00:00:01Z", None)
    assert ev is not None
    assert ev.network_id == "8453"


def test_normalize_feed_item_network_id_defaults_empty():
    raw = _raw_buy()
    raw.pop("networkId")
    ev = normalize_feed_item(raw, "2026-07-09T00:00:01Z", None)
    assert ev.network_id == ""


def test_insert_event_persists_network_id():
    c = _c()
    ev = normalize_feed_item(_raw_buy(network="solana"), "2026-07-09T00:00:01Z", None)
    m.insert_event(c, ev.__dict__)
    row = c.execute("SELECT network_id FROM memecoin_events WHERE fomo_event_id='e1'").fetchone()
    assert row["network_id"] == "solana"


def test_open_position_persists_network_id():
    c = _c()
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="Mint", ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t",
                          network_id="solana")
    assert m.get_position(c, pid)["network_id"] == "solana"


def test_backfill_network_id_by_mint_prefix():
    c = _c()
    # legacy rows written before the column existed (network_id NULL)
    evm = m.open_position(c, source_user_id="u", source_handle="h", mint="0xabc123", ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t")
    sol = m.open_position(c, source_user_id="u", source_handle="h", mint="So1anaBase58", ticker="T",
                          entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                          entry_lag_s=0.1, their_size_usd=0.0, opened_at="t")
    c.execute("UPDATE memecoin_positions SET network_id=NULL")  # simulate pre-migration rows
    known = m.open_position(c, source_user_id="u", source_handle="h", mint="0xdef", ticker="T",
                            entry_price=1.0, entry_notional=10.0, entry_fill_source="j",
                            entry_lag_s=0.1, their_size_usd=0.0, opened_at="t", network_id="8453")
    m.backfill_network_id(c)
    assert m.get_position(c, evm)["network_id"] == "evm-unknown"
    assert m.get_position(c, sol)["network_id"] == "solana"
    assert m.get_position(c, known)["network_id"] == "8453"   # never overwrite a known value

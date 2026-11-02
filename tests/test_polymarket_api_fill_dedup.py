"""Regression: fetch_user_activity must NOT collapse distinct fills that share
a transactionHash.

Root cause (verified 2026-06-15 against the live Data API on wallet
0x20ad75e1…, condition 0x252887d8…): Polymarket's /activity endpoint returns
ONE row per fill. A taker order crossing N maker price levels settles in a
SINGLE transaction, so it produces N activity rows that share one
transactionHash but differ in price/size. The old dedup keyed on
transactionHash alone, keeping the first fill and silently dropping the rest —
e.g. a 9-fill, 21,250-share buy ($1,198.75) collapsed to a single 5,000-share
row ($100), understating cost basis, held shares and realized PnL.

The dedup's legitimate job is to drop spurious duplicates re-served across
offset pages / window boundaries. The fix is to key on the full per-fill
identity instead of the transactionHash alone: laddered fills differ in
price/size and survive; byte-identical re-served rows still collapse.
"""
from __future__ import annotations

import httpx
import pytest

import sniper.markets.polymarket_api as polymarket_api


@pytest.fixture(autouse=True)
def _no_sleep_no_cache(monkeypatch):
    # Tests mock the transport; the inter-page sleep is pure harness latency.
    monkeypatch.setattr(polymarket_api.time, "sleep", lambda *_a, **_kw: None)
    # Disable the _request_json response cache so each mocked page is served live.
    monkeypatch.setattr(polymarket_api, "_cache_get", lambda *_a, **_kw: None)
    monkeypatch.setattr(polymarket_api, "_cache_put", lambda *_a, **_kw: None)


def _client(pages: list[list[dict]]) -> httpx.Client:
    """Serve pages[i] on request i; empty list once exhausted (natural end)."""
    calls = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        i = calls[0]
        calls[0] += 1
        return httpx.Response(200, json=pages[i] if i < len(pages) else [])

    return httpx.Client(transport=httpx.MockTransport(handler))


def _fill(tx: str, *, size: float, price: float, usdc: float, ts: int = 1770068250,
          side: str = "BUY", oidx: int = 0,
          cid: str = "0xCOND") -> dict:
    return {
        "type": "TRADE", "side": side, "outcome": "Over", "outcomeIndex": oidx,
        "conditionId": cid, "asset": f"asset-{oidx}", "size": size, "price": price,
        "usdcSize": usdc, "timestamp": ts, "transactionHash": tx,
    }


def test_laddered_fills_sharing_one_tx_are_all_kept():
    """A 3-fill market order in one tx (different price/size) must yield 3 rows."""
    tx = "0x41b8fefbf3a174000398f4022418cc617169e72b18501aaff9ede82dc5453658"
    page = [
        _fill(tx, size=5000, price=0.02, usdc=100),
        _fill(tx, size=5000, price=0.03, usdc=150),
        _fill(tx, size=250, price=0.10, usdc=25),
    ]
    rows = polymarket_api.fetch_user_activity(
        _client([page]), "0xwallet", activity_type=None,
        page_limit=1000, walk_full_history=False)
    assert len(rows) == 3, f"expected all 3 fills, got {len(rows)} (dedup-by-tx dropped fills)"
    assert sum(r["size"] for r in rows) == 10250
    assert sum(r["usdcSize"] for r in rows) == 275


def test_distinct_fills_same_tx_same_price_diff_size_kept():
    tx = "0xabc"
    page = [
        _fill(tx, size=1000, price=0.50, usdc=500),
        _fill(tx, size=2500, price=0.50, usdc=1250),
    ]
    rows = polymarket_api.fetch_user_activity(
        _client([page]), "0xwallet", activity_type=None,
        page_limit=1000, walk_full_history=False)
    assert len(rows) == 2
    assert sum(r["size"] for r in rows) == 3500


def test_byte_identical_row_re_served_across_pages_is_collapsed():
    """The dedup's legitimate purpose is retained: an identical row re-served on a
    later page (offset overlap / retry) is kept exactly once."""
    tx = "0xdup"
    row = _fill(tx, size=1000, price=0.50, usdc=500)
    # page_limit=1 forces multi-page paging; same row served twice, then empty.
    rows = polymarket_api.fetch_user_activity(
        _client([[dict(row)], [dict(row)], []]), "0xwallet", activity_type=None,
        page_limit=1, walk_full_history=False)
    assert len(rows) == 1, "byte-identical re-served row must collapse to one"

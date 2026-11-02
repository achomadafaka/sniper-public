# Thesis Feed Integration Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the native feed foundation for the winning-wallet thesis extractor: feed contracts, append-only raw storage, CLOB WebSocket capture/derivation, Goldsky fill normalization, and analyzer providers.

**Architecture:** Keep raw source capture separate from detector logic. Source adapters append raw `FeedEvent`s; derivers write normalized points/fills; provider classes read normalized artifacts and satisfy the existing thesis extractor interfaces. Missing feeds remain explicit `blocked_missing_feed`.

**Tech Stack:** Python 3, dataclasses, JSONL, parquet when available, `pytest`, existing `sniper.analysis.wallet_protocol.subgraph_reader`, existing `sniper.analysis.winning_wallet_thesis.data` provider contracts.

**Spec:** `docs/superpowers/specs/2026-05-31-thesis-feed-integration-design.md`

---

## File Structure

| File | Responsibility |
|---|---|
| `sniper/feeds/thesis_feed_types.py` | `FeedEvent`, `OrderBookPoint`, `FillEvent`, source helpers. |
| `sniper/feeds/append_store.py` | Append-only JSONL writer/reader with payload-hash idempotency. |
| `sniper/feeds/clob_ws_capture.py` | CLOB WebSocket capture loop and message-to-FeedEvent conversion. |
| `sniper/feeds/orderbook_deriver.py` | Raw CLOB messages -> `OrderBookPoint`s. |
| `sniper/feeds/fill_normalizer.py` | Goldsky/subgraph rows -> `FillEvent`s. |
| `sniper/analysis/winning_wallet_thesis/data/book_price_path.py` | `PricePathProvider` backed by orderbook points. |
| `sniper/analysis/winning_wallet_thesis/data/onchain_fill_identity.py` | `FillIdentityProvider` backed by normalized fills. |
| `scripts/capture_thesis_feeds.py` | Operator-triggered capture CLI, dry-run/sample friendly. |
| `tests/feeds/test_thesis_feed_*.py` | Feed contracts, append store, CLOB conversion, fill normalization. |
| `tests/winning_wallet_thesis/test_feed_providers.py` | Provider behavior against normalized fixtures. |

---

## Task 1: Feed data contracts

**Files:**
- Create: `sniper/feeds/thesis_feed_types.py`
- Test: `tests/feeds/test_thesis_feed_types.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.feeds.thesis_feed_types import FeedEvent, payload_hash


def test_payload_hash_is_stable_for_key_order():
    a = {"b": 2, "a": 1}
    b = {"a": 1, "b": 2}
    assert payload_hash(a) == payload_hash(b)


def test_feed_event_keeps_observed_and_source_time_separate():
    event = FeedEvent(
        source="polymarket_clob_ws",
        source_event_type="price_change",
        observed_ts_ms=1000,
        event_ts_ms=900,
        market_id="m",
        condition_id="c",
        asset_id="a",
        event_slug="slug",
        payload={"x": 1},
        payload_hash=payload_hash({"x": 1}),
    )
    assert event.observed_ts_ms == 1000
    assert event.event_ts_ms == 900
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/feeds/test_thesis_feed_types.py -q`

Expected: fails with missing module.

- [ ] **Step 3: Implement contracts**

```python
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict
from typing import Any


def payload_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class FeedEvent:
    source: str
    source_event_type: str
    observed_ts_ms: int
    event_ts_ms: int | None
    market_id: str | None
    condition_id: str | None
    asset_id: str | None
    event_slug: str | None
    payload: dict[str, Any]
    payload_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OrderBookPoint:
    asset_id: str
    ts_ms: int
    best_bid: float | None
    best_ask: float | None
    mid: float | None
    spread: float | None
    bid_depth_1: float
    ask_depth_1: float
    bid_depth_5: float
    ask_depth_5: float
    source: str


@dataclass(frozen=True)
class FillEvent:
    tx_hash: str
    log_id: str
    ts: int
    maker: str
    taker: str
    maker_asset_id: str
    taker_asset_id: str
    maker_amount: float
    taker_amount: float
    condition_id: str | None = None
    event_slug: str | None = None
    price: float | None = None
    source: str = "goldsky_orderbook_subgraph"
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/feeds/test_thesis_feed_types.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/feeds/thesis_feed_types.py tests/feeds/test_thesis_feed_types.py
git commit -m "feat(feeds): add thesis feed data contracts"
```

---

## Task 2: Append-only raw feed store

**Files:**
- Create: `sniper/feeds/append_store.py`
- Test: `tests/feeds/test_append_store.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.feeds.append_store import JsonlAppendStore
from sniper.feeds.thesis_feed_types import FeedEvent, payload_hash


def _event(payload):
    return FeedEvent("src", "kind", 1, None, None, None, None, None, payload, payload_hash(payload))


def test_append_store_dedupes_by_payload_hash(tmp_path):
    store = JsonlAppendStore(tmp_path)
    assert store.append("src", _event({"x": 1})) is True
    assert store.append("src", _event({"x": 1})) is False
    rows = list(store.read("src"))
    assert len(rows) == 1
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/feeds/test_append_store.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement store**

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

from sniper.feeds.thesis_feed_types import FeedEvent


class JsonlAppendStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._seen: dict[str, set[str]] = {}

    def _path(self, source: str) -> Path:
        return self.root / f"{source}.jsonl"

    def _load_seen(self, source: str) -> set[str]:
        if source in self._seen:
            return self._seen[source]
        seen: set[str] = set()
        path = self._path(source)
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    seen.add(json.loads(line)["payload_hash"])
        self._seen[source] = seen
        return seen

    def append(self, source: str, event: FeedEvent) -> bool:
        seen = self._load_seen(source)
        if event.payload_hash in seen:
            return False
        with self._path(source).open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.as_dict(), sort_keys=True) + "\n")
        seen.add(event.payload_hash)
        return True

    def read(self, source: str) -> Iterator[dict]:
        path = self._path(source)
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/feeds/test_append_store.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/feeds/append_store.py tests/feeds/test_append_store.py
git commit -m "feat(feeds): add append-only raw feed store"
```

---

## Task 3: CLOB WebSocket message normalizer

**Files:**
- Create: `sniper/feeds/clob_ws_capture.py`
- Test: `tests/feeds/test_clob_ws_capture.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.feeds.clob_ws_capture import feed_event_from_clob_message


def test_price_change_message_becomes_feed_event():
    msg = {
        "event_type": "price_change",
        "market": "0xmarket",
        "timestamp": "1757908892351",
        "price_changes": [{"asset_id": "asset1", "price": "0.5"}],
    }
    out = feed_event_from_clob_message(msg, observed_ts_ms=1757908892400)
    assert out.source == "polymarket_clob_ws"
    assert out.source_event_type == "price_change"
    assert out.market_id == "0xmarket"
    assert out.asset_id == "asset1"
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/feeds/test_clob_ws_capture.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement normalizer**

```python
from __future__ import annotations

import time
from typing import Any

from sniper.feeds.thesis_feed_types import FeedEvent, payload_hash

SOURCE = "polymarket_clob_ws"


def _first_asset_id(msg: dict[str, Any]) -> str | None:
    if msg.get("asset_id"):
        return str(msg["asset_id"])
    changes = msg.get("price_changes") or []
    if changes and isinstance(changes[0], dict) and changes[0].get("asset_id"):
        return str(changes[0]["asset_id"])
    return None


def feed_event_from_clob_message(msg: dict[str, Any], *, observed_ts_ms: int | None = None) -> FeedEvent:
    if observed_ts_ms is None:
        observed_ts_ms = int(time.time() * 1000)
    try:
        event_ts_ms = int(msg["timestamp"]) if msg.get("timestamp") is not None else None
    except (TypeError, ValueError):
        event_ts_ms = None
    return FeedEvent(
        source=SOURCE,
        source_event_type=str(msg.get("event_type") or "unknown"),
        observed_ts_ms=observed_ts_ms,
        event_ts_ms=event_ts_ms,
        market_id=str(msg["market"]) if msg.get("market") else None,
        condition_id=None,
        asset_id=_first_asset_id(msg),
        event_slug=None,
        payload=msg,
        payload_hash=payload_hash(msg),
    )
```

Do not implement the live WebSocket loop in this task. This task makes the conversion testable.

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/feeds/test_clob_ws_capture.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/feeds/clob_ws_capture.py tests/feeds/test_clob_ws_capture.py
git commit -m "feat(feeds): normalize clob websocket messages"
```

---

## Task 4: Orderbook point deriver

**Files:**
- Create: `sniper/feeds/orderbook_deriver.py`
- Test: `tests/feeds/test_orderbook_deriver.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.feeds.orderbook_deriver import point_from_book


def test_book_snapshot_derives_mid_spread_and_depth():
    msg = {
        "asset_id": "asset1",
        "timestamp": "1000",
        "bids": [{"price": ".48", "size": "30"}],
        "asks": [{"price": ".52", "size": "25"}],
    }
    p = point_from_book(msg)
    assert p.best_bid == 0.48
    assert p.best_ask == 0.52
    assert round(p.mid, 2) == 0.50
    assert round(p.spread, 2) == 0.04
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/feeds/test_orderbook_deriver.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement deriver**

```python
from __future__ import annotations

from sniper.feeds.thesis_feed_types import OrderBookPoint


def _levels(rows: list[dict], *, reverse: bool) -> list[tuple[float, float]]:
    out = []
    for row in rows or []:
        try:
            out.append((float(row["price"]), float(row["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x[0], reverse=reverse)


def point_from_book(msg: dict) -> OrderBookPoint:
    bids = _levels(msg.get("bids") or [], reverse=True)
    asks = _levels(msg.get("asks") or [], reverse=False)
    best_bid = bids[0][0] if bids else None
    best_ask = asks[0][0] if asks else None
    mid = (best_bid + best_ask) / 2 if best_bid is not None and best_ask is not None else None
    spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
    return OrderBookPoint(
        asset_id=str(msg["asset_id"]),
        ts_ms=int(msg["timestamp"]),
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread=spread,
        bid_depth_1=bids[0][1] if bids else 0.0,
        ask_depth_1=asks[0][1] if asks else 0.0,
        bid_depth_5=sum(size for _, size in bids[:5]),
        ask_depth_5=sum(size for _, size in asks[:5]),
        source="polymarket_clob_ws",
    )
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/feeds/test_orderbook_deriver.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/feeds/orderbook_deriver.py tests/feeds/test_orderbook_deriver.py
git commit -m "feat(feeds): derive orderbook points"
```

---

## Task 5: Fill normalizer from existing subgraph rows

**Files:**
- Create: `sniper/feeds/fill_normalizer.py`
- Test: `tests/feeds/test_fill_normalizer.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.feeds.fill_normalizer import fill_event_from_subgraph_row


def test_subgraph_order_filled_row_normalizes_to_fill_event():
    row = {
        "id": "log1",
        "timestamp": "1700000000",
        "maker": "0xM",
        "taker": "0xT",
        "makerAssetId": "100",
        "takerAssetId": "0",
        "makerAmountFilled": "5000000",
        "takerAmountFilled": "2500000",
        "transactionHash": "0xtx",
    }
    fill = fill_event_from_subgraph_row(row)
    assert fill.maker == "0xm"
    assert fill.taker == "0xt"
    assert fill.maker_asset_id == "100"
    assert fill.tx_hash == "0xtx"
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/feeds/test_fill_normalizer.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement normalizer**

```python
from __future__ import annotations

from sniper.feeds.thesis_feed_types import FillEvent


def _float_amount(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def fill_event_from_subgraph_row(row: dict) -> FillEvent:
    return FillEvent(
        tx_hash=str(row.get("transactionHash") or ""),
        log_id=str(row.get("id") or ""),
        ts=int(row.get("timestamp") or 0),
        maker=str(row.get("maker") or "").lower(),
        taker=str(row.get("taker") or "").lower(),
        maker_asset_id=str(row.get("makerAssetId") or ""),
        taker_asset_id=str(row.get("takerAssetId") or ""),
        maker_amount=_float_amount(row.get("makerAmountFilled")),
        taker_amount=_float_amount(row.get("takerAmountFilled")),
    )
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/feeds/test_fill_normalizer.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/feeds/fill_normalizer.py tests/feeds/test_fill_normalizer.py
git commit -m "feat(feeds): normalize exchange fill events"
```

---

## Task 6: Analyzer providers

**Files:**
- Create: `sniper/analysis/winning_wallet_thesis/data/book_price_path.py`
- Create: `sniper/analysis/winning_wallet_thesis/data/onchain_fill_identity.py`
- Test: `tests/winning_wallet_thesis/test_feed_providers.py`

- [ ] **Step 1: Write failing tests**

```python
from sniper.analysis.winning_wallet_thesis.data.book_price_path import BookPricePathProvider
from sniper.analysis.winning_wallet_thesis.data.onchain_fill_identity import OnchainFillIdentityProvider
from sniper.feeds.thesis_feed_types import FillEvent, OrderBookPoint


def test_book_price_path_provider_returns_mid_path_for_condition():
    points = [OrderBookPoint("asset1", 1000, .4, .6, .5, .2, 1, 1, 1, 1, "test")]
    provider = BookPricePathProvider(points, condition_to_asset={"cond1": "asset1"})
    assert provider.path("cond1", start_ts=1, end_ts=2) == [(1, .5)]


def test_onchain_fill_identity_provider_filters_wallet():
    fill = FillEvent("tx", "log", 10, "0xmaker", "0xtaker", "a", "b", 1, 2)
    provider = OnchainFillIdentityProvider([fill])
    assert provider.available is True
    assert provider.fills_for_wallet("0xmaker", start_ts=0, end_ts=20) == [fill]
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_feed_providers.py -q`

Expected: missing module failure.

- [ ] **Step 3: Implement providers**

```python
# book_price_path.py
from __future__ import annotations

from dataclasses import dataclass

from sniper.feeds.thesis_feed_types import OrderBookPoint


@dataclass
class BookPricePathProvider:
    points: list[OrderBookPoint]
    condition_to_asset: dict[str, str]

    def path(self, condition_id: str, *, start_ts: int, end_ts: int) -> list[tuple[int, float]] | None:
        asset = self.condition_to_asset.get(condition_id)
        if asset is None:
            return None
        out = []
        for point in self.points:
            ts = int(point.ts_ms / 1000)
            if point.asset_id == asset and start_ts <= ts <= end_ts and point.mid is not None:
                out.append((ts, point.mid))
        return out
```

```python
# onchain_fill_identity.py
from __future__ import annotations

from dataclasses import dataclass

from sniper.feeds.thesis_feed_types import FillEvent


@dataclass
class OnchainFillIdentityProvider:
    fills: list[FillEvent]
    available: bool = True

    def fills_for_wallet(self, wallet: str, *, start_ts: int, end_ts: int) -> list[FillEvent]:
        w = wallet.lower()
        return [f for f in self.fills
                if start_ts <= f.ts <= end_ts and (f.maker == w or f.taker == w)]

    def fills_for_market(self, condition_id: str, *, start_ts: int, end_ts: int) -> list[FillEvent]:
        return [f for f in self.fills
                if f.condition_id == condition_id and start_ts <= f.ts <= end_ts]
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/winning_wallet_thesis/test_feed_providers.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add sniper/analysis/winning_wallet_thesis/data/book_price_path.py sniper/analysis/winning_wallet_thesis/data/onchain_fill_identity.py tests/winning_wallet_thesis/test_feed_providers.py
git commit -m "feat(thesis): provide feed-backed analyzer providers"
```

---

## Task 7: Capture CLI skeleton

**Files:**
- Create: `scripts/capture_thesis_feeds.py`
- Test: `tests/feeds/test_capture_thesis_feeds_cli.py`

- [ ] **Step 1: Write failing help test**

```python
import subprocess
import sys


def test_capture_thesis_feeds_help():
    out = subprocess.run(
        [sys.executable, "scripts/capture_thesis_feeds.py", "--help"],
        text=True,
        capture_output=True,
        check=True,
    )
    assert "--source" in out.stdout
    assert "--out" in out.stdout
```

- [ ] **Step 2: Run failing test**

Run: `python3 -m pytest tests/feeds/test_capture_thesis_feeds_cli.py -q`

Expected: missing script failure.

- [ ] **Step 3: Implement dry-run CLI**

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture thesis feeds (operator-triggered).")
    ap.add_argument("--source", choices=["clob_ws", "goldsky_fills", "gamma_universe"], required=True)
    ap.add_argument("--out", default="runs/feed_archive/raw")
    ap.add_argument("--dry-run", action="store_true", default=True)
    args = ap.parse_args()
    print(json.dumps({"source": args.source, "out": args.out, "dry_run": args.dry_run}, sort_keys=True))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Verify**

Run: `python3 -m pytest tests/feeds/test_capture_thesis_feeds_cli.py -q`

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/capture_thesis_feeds.py tests/feeds/test_capture_thesis_feeds_cli.py
git commit -m "feat(feeds): add thesis feed capture CLI skeleton"
```

## Phase 1 Exit Criteria

- Raw feed contract exists.
- Append-only store exists and dedupes.
- CLOB WebSocket messages can be normalized offline.
- Orderbook points can be derived from book snapshots.
- Goldsky fill rows can be normalized.
- Winning-wallet thesis detectors can consume feed-backed providers.
- No live feed loop runs in CI.
- No production DB writes.
- Focused tests pass:

```bash
python3 -m pytest tests/feeds tests/winning_wallet_thesis/test_feed_providers.py -q
python3 scripts/validate_schema_refs.py
```

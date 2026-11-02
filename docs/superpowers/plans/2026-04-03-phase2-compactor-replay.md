# Phase 2: Compactor + Episode Builder + Replay Engine

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A laptop-side pipeline that transforms raw JSONL from the VPS collector into Parquet tables, builds episodes from resolved markets, and replays strategies with honest taker-fill execution.

**Architecture:** Three slices with a dependency chain: 2A (compactor) produces Parquet, 2B (episode builder) reads Parquet and produces episodes, 2C (replay engine) reads episodes and simulates strategies. Each slice is independently testable. The compactor uses test fixtures (synthetic JSONL), not live VPS data. The episode builder uses compactor output or fixtures. The replay engine uses episodes (built or existing Kalshi).

**Tech Stack:** Python 3.12+, pyarrow (Parquet I/O), DuckDB (optional for querying), sim/ package from Phase 0. No external service dependencies — everything runs locally.

**Parallel execution:** Slices 2A and 2C can be developed in parallel (2C uses the existing 4 Kalshi episodes while waiting for 2A output). Slice 2B depends on 2A.

---

## File Structure

### Slice 2A: Compactor
| File | Responsibility |
|------|---------------|
| `sim/compact/__init__.py` | Package marker |
| `sim/compact/router.py` | Route RawEvent → table-specific row dicts |
| `sim/compact/writer.py` | Write row dicts to date-partitioned Parquet files |
| `sim/compact/manifest.py` | Track processed .jsonl.gz files for idempotency |
| `sim/compact/run.py` | CLI entrypoint: scan raw dir, compact unprocessed files |
| `tests/sim/test_compact_router.py` | Router tests per event type |
| `tests/sim/test_compact_writer.py` | Parquet write + read-back tests |
| `tests/sim/test_compact_manifest.py` | Manifest idempotency tests |
| `tests/sim/test_compact_integration.py` | End-to-end: JSONL fixture → Parquet → verify |

### Slice 2B: Episode Builder
| File | Responsibility |
|------|---------------|
| `sim/episodes/__init__.py` | Package marker |
| `sim/episodes/builder.py` | Build one episode from Parquet tables for a resolved market |
| `sim/episodes/outcomes.py` | Resolve asset_id → outcome_index from lifecycle metadata |
| `tests/sim/test_episode_builder.py` | Episode building tests |
| `tests/sim/test_outcomes.py` | Outcome resolution tests |

### Slice 2C: Replay Engine
| File | Responsibility |
|------|---------------|
| `sim/replay/__init__.py` | Package marker |
| `sim/replay/engine.py` | Core replay loop: timeline → agent → fills → settlement |
| `sim/replay/context.py` | ReplayContext: quote lookup, position tracking, order placement |
| `sim/replay/fees.py` | Venue-specific fee calculation |
| `sim/replay/strategy.py` | Strategy base class / protocol |
| `sim/replay/adapters.py` | Venue adapters for Polymarket + Kalshi episode formats |
| `tests/sim/test_replay_engine.py` | Engine determinism + settlement tests |
| `tests/sim/test_replay_context.py` | Quote lookup, fill logic, liquidity cap |
| `tests/sim/test_replay_fees.py` | Fee calculation tests |

---

## Slice 2A: Compactor

### Task 2A.1: Event Router

**Files:**
- Create: `sim/compact/__init__.py`
- Create: `sim/compact/router.py`
- Create: `tests/sim/test_compact_router.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_compact_router.py
from sim.compact.router import route_event
from sim.schema import RawEvent, EventType


def _make_event(event_type: EventType, payload: dict, **kwargs) -> RawEvent:
    defaults = dict(
        venue="polymarket", market_id="pm:0xtest", venue_market_id="0xtest",
        market_type="geopolitical", capture_ts="2026-04-03T12:00:00.000Z",
        ingest_seq=1, asset_id="asset123",
    )
    defaults.update(kwargs)
    return RawEvent(event_type=event_type, payload=payload, **defaults)


def test_route_trade():
    evt = _make_event(EventType.TRADE, {
        "price": "0.65", "size": "100.5", "side": "BUY",
        "fee_rate_bps": "0", "transaction_hash": "0xabc",
    }, exchange_ts="2026-04-03T12:00:00.000Z")
    table, row = route_event(evt)
    assert table == "trades"
    assert row["price"] == 0.65
    assert row["size"] == 100.5
    assert row["side"] == "BUY"
    assert row["fee_rate_bps"] == 0
    assert row["transaction_hash"] == "0xabc"
    assert row["market_id"] == "pm:0xtest"
    assert row["venue"] == "polymarket"


def test_route_trade_nullable_fields():
    evt = _make_event(EventType.TRADE, {
        "price": "0.50", "size": "10", "side": "SELL",
    })
    table, row = route_event(evt)
    assert table == "trades"
    assert row["fee_rate_bps"] is None
    assert row["transaction_hash"] is None


def test_route_book_delta_with_bba():
    evt = _make_event(EventType.BOOK_DELTA, {
        "price": "0.62", "size": "500", "side": "BUY",
        "best_bid": "0.61", "best_ask": "0.64",
    })
    tables = route_event(evt)
    # book_delta produces TWO rows: one for book_deltas table, one for quotes_top
    assert isinstance(tables, list)
    assert len(tables) == 2
    delta_table, delta_row = tables[0]
    quote_table, quote_row = tables[1]
    assert delta_table == "book_deltas"
    assert delta_row["price"] == 0.62
    assert delta_row["best_bid"] == 0.61
    assert delta_row["best_ask"] == 0.64
    assert quote_table == "quotes_top"
    assert quote_row["best_bid"] == 0.61
    assert quote_row["best_ask"] == 0.64
    assert quote_row["source_event"] == "book_delta"
    # bid_size: only set when side=BUY and price=best_bid
    assert quote_row["bid_size"] is None  # price 0.62 != best_bid 0.61


def test_route_book_delta_bid_size_when_at_top():
    evt = _make_event(EventType.BOOK_DELTA, {
        "price": "0.61", "size": "500", "side": "BUY",
        "best_bid": "0.61", "best_ask": "0.64",
    })
    tables = route_event(evt)
    quote_row = tables[1][1]
    assert quote_row["bid_size"] == 500.0  # price matches best_bid


def test_route_best_bba():
    evt = _make_event(EventType.BEST_BBA, {
        "best_bid": "0.60", "best_ask": "0.65",
    })
    table, row = route_event(evt)
    assert table == "quotes_top"
    assert row["best_bid"] == 0.60
    assert row["best_ask"] == 0.65
    assert row["bid_size"] is None
    assert row["ask_size"] is None
    assert row["source_event"] == "best_bba"


def test_route_book_snapshot():
    evt = _make_event(EventType.BOOK_SNAPSHOT, {
        "bids": [{"price": "0.60", "size": "1000"}, {"price": "0.59", "size": "500"}],
        "asks": [{"price": "0.65", "size": "800"}, {"price": "0.66", "size": "200"}],
    })
    tables = route_event(evt)
    assert isinstance(tables, list)
    # Should produce: 1 quotes_top row + 4 book_levels rows (2 bids + 2 asks)
    quote_rows = [(t, r) for t, r in tables if t == "quotes_top"]
    level_rows = [(t, r) for t, r in tables if t == "book_levels"]
    assert len(quote_rows) == 1
    assert len(level_rows) == 4
    assert quote_rows[0][1]["best_bid"] == 0.60
    assert quote_rows[0][1]["best_ask"] == 0.65
    assert quote_rows[0][1]["bid_size"] == 1000.0
    assert quote_rows[0][1]["ask_size"] == 800.0
    assert quote_rows[0][1]["source_event"] == "book_snapshot"
    # Check level ordering
    bid_levels = [r for t, r in level_rows if r["side"] == "bid"]
    assert bid_levels[0]["level_index"] == 0
    assert bid_levels[0]["price"] == 0.60
    assert bid_levels[1]["level_index"] == 1


def test_route_market_metadata():
    evt = _make_event(EventType.MARKET_METADATA, {
        "question": "US strikes Iran?", "outcomes": ["Yes", "No"],
    }, asset_id=None)
    table, row = route_event(evt)
    assert table == "market_lifecycle"
    assert row["event_type"] == "market_metadata"
    assert "question" in row["payload"]


def test_route_market_resolved():
    evt = _make_event(EventType.MARKET_RESOLVED, {
        "outcome_prices": ["0.01", "0.99"], "resolved_ts": "2026-04-15T12:00:00.000Z",
    }, asset_id=None)
    table, row = route_event(evt)
    assert table == "market_lifecycle"
    assert row["event_type"] == "market_resolved"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_compact_router.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/compact/__init__.py
"""Compactor — transforms raw JSONL into typed Parquet tables."""

# sim/compact/router.py
"""Route a RawEvent to one or more (table_name, row_dict) pairs."""
from __future__ import annotations

import json
from typing import Optional

from sim.schema import RawEvent, EventType


def _safe_float(val, default=None) -> Optional[float]:
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val, default=None) -> Optional[int]:
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _common_fields(evt: RawEvent) -> dict:
    return {
        "market_id": evt.market_id,
        "asset_id": evt.asset_id,
        "capture_ts": evt.capture_ts,
        "exchange_ts": evt.exchange_ts,
        "ingest_seq": evt.ingest_seq,
        "venue": evt.venue,
    }


def _route_trade(evt: RawEvent) -> tuple[str, dict]:
    p = evt.payload
    row = {
        **_common_fields(evt),
        "price": _safe_float(p.get("price")),
        "size": _safe_float(p.get("size")),
        "side": p.get("side", ""),
        "fee_rate_bps": _safe_int(p.get("fee_rate_bps")),
        "transaction_hash": p.get("transaction_hash"),
        "market_type": evt.market_type,
    }
    return ("trades", row)


def _route_book_delta(evt: RawEvent) -> list[tuple[str, dict]]:
    p = evt.payload
    common = _common_fields(evt)
    price = _safe_float(p.get("price"))
    size = _safe_float(p.get("size"))
    side = p.get("side", "")
    best_bid = _safe_float(p.get("best_bid"))
    best_ask = _safe_float(p.get("best_ask"))

    delta_row = {
        **common,
        "price": price,
        "size": size,
        "side": side,
        "best_bid": best_bid,
        "best_ask": best_ask,
    }

    # Derive bid_size/ask_size only when delta is at the top price
    bid_size = None
    ask_size = None
    if side == "BUY" and price is not None and best_bid is not None and abs(price - best_bid) < 1e-9:
        bid_size = size
    if side == "SELL" and price is not None and best_ask is not None and abs(price - best_ask) < 1e-9:
        ask_size = size

    quote_row = {
        **common,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size": bid_size,
        "ask_size": ask_size,
        "source_event": "book_delta",
    }

    return [("book_deltas", delta_row), ("quotes_top", quote_row)]


def _route_best_bba(evt: RawEvent) -> tuple[str, dict]:
    p = evt.payload
    return ("quotes_top", {
        **_common_fields(evt),
        "best_bid": _safe_float(p.get("best_bid")),
        "best_ask": _safe_float(p.get("best_ask")),
        "bid_size": None,
        "ask_size": None,
        "source_event": "best_bba",
    })


def _route_book_snapshot(evt: RawEvent) -> list[tuple[str, dict]]:
    p = evt.payload
    common = _common_fields(evt)
    bids = p.get("bids", [])
    asks = p.get("asks", [])

    results: list[tuple[str, dict]] = []

    # Top-of-book quote
    top_bid = _safe_float(bids[0]["price"]) if bids else None
    top_ask = _safe_float(asks[0]["price"]) if asks else None
    top_bid_size = _safe_float(bids[0]["size"]) if bids else None
    top_ask_size = _safe_float(asks[0]["size"]) if asks else None

    results.append(("quotes_top", {
        **common,
        "best_bid": top_bid,
        "best_ask": top_ask,
        "bid_size": top_bid_size,
        "ask_size": top_ask_size,
        "source_event": "book_snapshot",
    }))

    # All levels
    for i, bid in enumerate(bids):
        results.append(("book_levels", {
            "market_id": evt.market_id,
            "asset_id": evt.asset_id,
            "capture_ts": evt.capture_ts,
            "ingest_seq": evt.ingest_seq,
            "side": "bid",
            "price": _safe_float(bid["price"]),
            "size": _safe_float(bid["size"]),
            "level_index": i,
            "venue": evt.venue,
        }))

    for i, ask in enumerate(asks):
        results.append(("book_levels", {
            "market_id": evt.market_id,
            "asset_id": evt.asset_id,
            "capture_ts": evt.capture_ts,
            "ingest_seq": evt.ingest_seq,
            "side": "ask",
            "price": _safe_float(ask["price"]),
            "size": _safe_float(ask["size"]),
            "level_index": i,
            "venue": evt.venue,
        }))

    return results


def _route_lifecycle(evt: RawEvent) -> tuple[str, dict]:
    return ("market_lifecycle", {
        "market_id": evt.market_id,
        "capture_ts": evt.capture_ts,
        "ingest_seq": evt.ingest_seq,
        "event_type": evt.event_type.value,
        "market_type": evt.market_type,
        "payload": json.dumps(evt.payload),
        "venue": evt.venue,
    })


def route_event(evt: RawEvent) -> tuple[str, dict] | list[tuple[str, dict]]:
    """Route a RawEvent to (table_name, row_dict) or list of them.

    Returns a single tuple for events that map 1:1 to a table row.
    Returns a list for events that produce multiple rows (book_delta → delta + quote,
    book_snapshot → quote + N levels).
    """
    if evt.event_type == EventType.TRADE:
        return _route_trade(evt)
    if evt.event_type == EventType.BOOK_DELTA:
        return _route_book_delta(evt)
    if evt.event_type == EventType.BEST_BBA:
        return _route_best_bba(evt)
    if evt.event_type == EventType.BOOK_SNAPSHOT:
        return _route_book_snapshot(evt)
    if evt.event_type in (EventType.MARKET_METADATA, EventType.MARKET_RESOLVED):
        return _route_lifecycle(evt)
    return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_compact_router.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add sim/compact/ tests/sim/test_compact_router.py
git commit -m "sim/compact: event router — routes RawEvent to typed table rows"
```

---

### Task 2A.2: Parquet Writer

**Files:**
- Create: `sim/compact/writer.py`
- Create: `tests/sim/test_compact_writer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_compact_writer.py
import os
import tempfile
import pyarrow.parquet as pq

from sim.compact.writer import ParquetTableWriter


def test_write_and_read_back_trades():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = ParquetTableWriter(base_dir=tmpdir)
        rows = [
            {"market_id": "pm:0x1", "asset_id": "a1", "capture_ts": "2026-04-03T12:00:00.000Z",
             "exchange_ts": "2026-04-03T12:00:00.000Z", "ingest_seq": 1,
             "price": 0.65, "size": 100.0, "side": "BUY",
             "fee_rate_bps": 0, "transaction_hash": "0xabc", "market_type": "geo", "venue": "polymarket"},
            {"market_id": "pm:0x1", "asset_id": "a1", "capture_ts": "2026-04-03T12:00:01.000Z",
             "exchange_ts": None, "ingest_seq": 2,
             "price": 0.70, "size": 50.0, "side": "SELL",
             "fee_rate_bps": None, "transaction_hash": None, "market_type": "geo", "venue": "polymarket"},
        ]
        w.append("trades", "2026-04-03", rows)
        w.flush()

        path = os.path.join(tmpdir, "trades", "2026-04-03.parquet")
        assert os.path.exists(path)
        table = pq.read_table(path)
        assert len(table) == 2
        assert table.column("price").to_pylist() == [0.65, 0.70]
        assert table.column("transaction_hash").to_pylist() == ["0xabc", None]


def test_append_is_additive():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = ParquetTableWriter(base_dir=tmpdir)
        row1 = [{"market_id": "pm:0x1", "capture_ts": "t1", "ingest_seq": 1, "best_bid": 0.5,
                 "best_ask": 0.6, "bid_size": None, "ask_size": None,
                 "source_event": "best_bba", "venue": "polymarket",
                 "asset_id": "a1", "exchange_ts": None}]
        row2 = [{"market_id": "pm:0x1", "capture_ts": "t2", "ingest_seq": 2, "best_bid": 0.51,
                 "best_ask": 0.61, "bid_size": None, "ask_size": None,
                 "source_event": "best_bba", "venue": "polymarket",
                 "asset_id": "a1", "exchange_ts": None}]
        w.append("quotes_top", "2026-04-03", row1)
        w.append("quotes_top", "2026-04-03", row2)
        w.flush()

        path = os.path.join(tmpdir, "quotes_top", "2026-04-03.parquet")
        table = pq.read_table(path)
        assert len(table) == 2


def test_date_partitioning():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = ParquetTableWriter(base_dir=tmpdir)
        w.append("trades", "2026-04-03", [{"market_id": "a", "ingest_seq": 1, "price": 0.5,
                 "size": 10, "side": "BUY", "venue": "pm", "market_type": "x",
                 "asset_id": "a", "capture_ts": "t", "exchange_ts": None,
                 "fee_rate_bps": None, "transaction_hash": None}])
        w.append("trades", "2026-04-04", [{"market_id": "b", "ingest_seq": 2, "price": 0.6,
                 "size": 20, "side": "SELL", "venue": "pm", "market_type": "x",
                 "asset_id": "b", "capture_ts": "t", "exchange_ts": None,
                 "fee_rate_bps": None, "transaction_hash": None}])
        w.flush()

        assert os.path.exists(os.path.join(tmpdir, "trades", "2026-04-03.parquet"))
        assert os.path.exists(os.path.join(tmpdir, "trades", "2026-04-04.parquet"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_compact_writer.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/compact/writer.py
"""Write row dicts to date-partitioned Parquet files."""
from __future__ import annotations

import os
from collections import defaultdict
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


class ParquetTableWriter:
    """Accumulates rows in memory, writes to Parquet on flush().

    Layout: {base_dir}/{table_name}/{YYYY-MM-DD}.parquet
    """

    def __init__(self, base_dir: str):
        self._base_dir = base_dir
        self._buffers: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)

    def append(self, table_name: str, date: str, rows: list[dict[str, Any]]) -> None:
        key = (table_name, date)
        self._buffers[key].extend(rows)

    def flush(self) -> dict[str, int]:
        """Write all buffered rows to Parquet. Returns {table_name: rows_written}."""
        counts: dict[str, int] = defaultdict(int)
        for (table_name, date), rows in self._buffers.items():
            if not rows:
                continue
            dir_path = os.path.join(self._base_dir, table_name)
            os.makedirs(dir_path, exist_ok=True)
            file_path = os.path.join(dir_path, f"{date}.parquet")

            table = pa.Table.from_pylist(rows)

            if os.path.exists(file_path):
                existing = pq.read_table(file_path)
                table = pa.concat_tables([existing, table], promote_options="default")

            pq.write_table(table, file_path)
            counts[table_name] += len(rows)

        self._buffers.clear()
        return dict(counts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_compact_writer.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add sim/compact/writer.py tests/sim/test_compact_writer.py
git commit -m "sim/compact: Parquet writer — date-partitioned, append-safe"
```

---

### Task 2A.3: Manifest for Idempotency

**Files:**
- Create: `sim/compact/manifest.py`
- Create: `tests/sim/test_compact_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_compact_manifest.py
import os
import tempfile

from sim.compact.manifest import CompactManifest


def test_mark_and_check():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "manifest.json")
        m = CompactManifest(path)
        assert not m.is_processed("raw/pm/2026-04-03/12.jsonl.gz")
        m.mark_processed("raw/pm/2026-04-03/12.jsonl.gz")
        assert m.is_processed("raw/pm/2026-04-03/12.jsonl.gz")


def test_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "manifest.json")
        m1 = CompactManifest(path)
        m1.mark_processed("file1.jsonl.gz")
        m1.save()

        m2 = CompactManifest(path)
        assert m2.is_processed("file1.jsonl.gz")
        assert not m2.is_processed("file2.jsonl.gz")


def test_skips_non_gz():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "manifest.json")
        m = CompactManifest(path)
        # Active .jsonl files should not be processable
        assert not m.should_process("raw/pm/2026-04-03/12.jsonl")
        assert m.should_process("raw/pm/2026-04-03/12.jsonl.gz")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_compact_manifest.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/compact/manifest.py
"""Track which raw files have been compacted — prevents double-processing."""
from __future__ import annotations

import json
import os


class CompactManifest:
    """Persistent set of processed .jsonl.gz filenames."""

    def __init__(self, path: str):
        self._path = path
        self._processed: set[str] = set()
        if os.path.exists(path):
            with open(path, "r") as f:
                self._processed = set(json.load(f))

    def is_processed(self, filename: str) -> bool:
        return filename in self._processed

    def should_process(self, filename: str) -> bool:
        """Returns True only for unprocessed .jsonl.gz files."""
        if not filename.endswith(".jsonl.gz"):
            return False
        return not self.is_processed(filename)

    def mark_processed(self, filename: str) -> None:
        self._processed.add(filename)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w") as f:
            json.dump(sorted(self._processed), f)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_compact_manifest.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add sim/compact/manifest.py tests/sim/test_compact_manifest.py
git commit -m "sim/compact: manifest — idempotent tracking of processed files"
```

---

### Task 2A.4: Compactor CLI + Integration Test

**Files:**
- Create: `sim/compact/run.py`
- Create: `tests/sim/test_compact_integration.py`

- [ ] **Step 1: Write the failing integration test**

```python
# tests/sim/test_compact_integration.py
import gzip
import json
import os
import tempfile

import pyarrow.parquet as pq

from sim.compact.run import compact_directory
from sim.schema import RawEvent, EventType
from sim.serialize import event_to_jsonl


def _write_fixture(raw_dir: str) -> str:
    """Write a synthetic .jsonl.gz file with known events."""
    date_dir = os.path.join(raw_dir, "polymarket", "2026-04-03")
    os.makedirs(date_dir)
    gz_path = os.path.join(date_dir, "12.jsonl.gz")

    events = [
        RawEvent(venue="polymarket", market_id="pm:0x1", venue_market_id="0x1",
                 market_type="geopolitical", event_type=EventType.TRADE,
                 capture_ts="2026-04-03T12:00:00.000Z", ingest_seq=1,
                 payload={"price": "0.65", "size": "100", "side": "BUY"},
                 asset_id="asset_yes", exchange_ts="2026-04-03T12:00:00.000Z"),
        RawEvent(venue="polymarket", market_id="pm:0x1", venue_market_id="0x1",
                 market_type="geopolitical", event_type=EventType.BOOK_DELTA,
                 capture_ts="2026-04-03T12:00:01.000Z", ingest_seq=2,
                 payload={"price": "0.62", "size": "500", "side": "BUY",
                          "best_bid": "0.61", "best_ask": "0.64"},
                 asset_id="asset_yes"),
        RawEvent(venue="polymarket", market_id="pm:0x1", venue_market_id="0x1",
                 market_type="geopolitical", event_type=EventType.BOOK_SNAPSHOT,
                 capture_ts="2026-04-03T12:00:02.000Z", ingest_seq=3,
                 payload={"bids": [{"price": "0.60", "size": "1000"}],
                          "asks": [{"price": "0.65", "size": "800"}]},
                 asset_id="asset_yes"),
        RawEvent(venue="polymarket", market_id="pm:0x1", venue_market_id="0x1",
                 market_type="geopolitical", event_type=EventType.MARKET_METADATA,
                 capture_ts="2026-04-03T12:00:03.000Z", ingest_seq=4,
                 payload={"question": "Test?", "outcomes": ["Yes", "No"]}),
    ]

    with gzip.open(gz_path, "wt") as f:
        for evt in events:
            f.write(event_to_jsonl(evt) + "\n")

    return gz_path


def test_compact_produces_all_tables():
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_dir = os.path.join(tmpdir, "raw")
        out_dir = os.path.join(tmpdir, "normalized")
        manifest_path = os.path.join(tmpdir, "manifest.json")

        _write_fixture(raw_dir)
        report = compact_directory(raw_dir, out_dir, manifest_path, venue="polymarket")

        assert report["files_processed"] == 1
        assert report["records_read"] == 4
        assert report["records_rejected"] == 0
        assert "trades" in report["tables"]
        assert "quotes_top" in report["tables"]
        assert "book_levels" in report["tables"]
        assert "book_deltas" in report["tables"]
        assert "market_lifecycle" in report["tables"]

        # Verify trades
        trades = pq.read_table(os.path.join(out_dir, "trades", "2026-04-03.parquet"))
        assert len(trades) == 1
        assert trades.column("price").to_pylist() == [0.65]

        # Verify quotes_top has entries from delta + snapshot
        quotes = pq.read_table(os.path.join(out_dir, "quotes_top", "2026-04-03.parquet"))
        assert len(quotes) >= 2  # at least delta + snapshot

        # Verify book_levels has all levels from snapshot
        levels = pq.read_table(os.path.join(out_dir, "book_levels", "2026-04-03.parquet"))
        assert len(levels) == 2  # 1 bid + 1 ask


def test_compact_idempotent():
    with tempfile.TemporaryDirectory() as tmpdir:
        raw_dir = os.path.join(tmpdir, "raw")
        out_dir = os.path.join(tmpdir, "normalized")
        manifest_path = os.path.join(tmpdir, "manifest.json")

        _write_fixture(raw_dir)

        report1 = compact_directory(raw_dir, out_dir, manifest_path, venue="polymarket")
        assert report1["files_processed"] == 1

        report2 = compact_directory(raw_dir, out_dir, manifest_path, venue="polymarket")
        assert report2["files_processed"] == 0  # already in manifest

        # Data should be the same (not doubled)
        trades = pq.read_table(os.path.join(out_dir, "trades", "2026-04-03.parquet"))
        assert len(trades) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_compact_integration.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/compact/run.py
"""Compactor CLI — scan raw directory, compact unprocessed .jsonl.gz files."""
from __future__ import annotations

import gzip
import logging
import os
from collections import defaultdict
from typing import Any

from sim.compact.manifest import CompactManifest
from sim.compact.router import route_event
from sim.compact.writer import ParquetTableWriter
from sim.serialize import jsonl_to_event

LOG = logging.getLogger(__name__)


def compact_directory(
    raw_dir: str,
    output_dir: str,
    manifest_path: str,
    venue: str = "polymarket",
) -> dict[str, Any]:
    """Compact all unprocessed .jsonl.gz files under raw_dir/{venue}/.

    Returns a report dict with counts.
    """
    manifest = CompactManifest(manifest_path)
    writer = ParquetTableWriter(base_dir=output_dir)

    venue_dir = os.path.join(raw_dir, venue)
    if not os.path.exists(venue_dir):
        return {"files_processed": 0, "records_read": 0, "records_rejected": 0, "tables": {}}

    gz_files = []
    for root, _dirs, files in os.walk(venue_dir):
        for fname in sorted(files):
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, raw_dir)
            if manifest.should_process(rel_path):
                gz_files.append((full_path, rel_path))

    total_read = 0
    total_rejected = 0
    table_counts: dict[str, int] = defaultdict(int)

    for full_path, rel_path in gz_files:
        LOG.info("Compacting %s", rel_path)
        with gzip.open(full_path, "rt") as f:
            for line in f:
                total_read += 1
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = jsonl_to_event(line)
                except Exception:
                    total_rejected += 1
                    continue

                # Extract date from capture_ts for partitioning
                date = evt.capture_ts[:10] if evt.capture_ts else "unknown"

                result = route_event(evt)
                if isinstance(result, tuple):
                    table_name, row = result
                    writer.append(table_name, date, [row])
                    table_counts[table_name] += 1
                elif isinstance(result, list):
                    for table_name, row in result:
                        writer.append(table_name, date, [row])
                        table_counts[table_name] += 1

        manifest.mark_processed(rel_path)

    flush_counts = writer.flush()
    manifest.save()

    return {
        "files_processed": len(gz_files),
        "records_read": total_read,
        "records_rejected": total_rejected,
        "tables": dict(table_counts),
    }


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(description="Compact raw JSONL to Parquet")
    parser.add_argument("--raw-dir", default=os.path.expanduser("~/sniper/raw"))
    parser.add_argument("--output-dir", default=os.path.expanduser("~/Desktop/polymarket-research/normalized"))
    parser.add_argument("--manifest", default=os.path.expanduser("~/Desktop/polymarket-research/compact_manifest.json"))
    parser.add_argument("--venue", default="polymarket")
    args = parser.parse_args()

    report = compact_directory(args.raw_dir, args.output_dir, args.manifest, args.venue)
    print(f"Compacted {report['files_processed']} files, {report['records_read']} records")
    print(f"Rejected: {report['records_rejected']}")
    for table, count in sorted(report["tables"].items()):
        print(f"  {table}: {count}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_compact_integration.py -v`
Expected: 2 passed

- [ ] **Step 5: Run full sim test suite**

Run: `python3 -m pytest tests/sim/ -v`
Expected: All tests pass (62 existing + new compact tests)

- [ ] **Step 6: Commit**

```bash
git add sim/compact/run.py tests/sim/test_compact_integration.py
git commit -m "sim/compact: CLI + integration test — end-to-end JSONL → Parquet"
```

---

## Slice 2C: Replay Engine (can start in parallel with 2A)

### Task 2C.1: Fee Calculation

**Files:**
- Create: `sim/replay/__init__.py`
- Create: `sim/replay/fees.py`
- Create: `tests/sim/test_replay_fees.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_replay_fees.py
from sim.replay.fees import polymarket_taker_fee, kalshi_taker_fee


def test_polymarket_fee_at_midprice():
    # fee = max_fee_rate × price × (1 - price) × quantity
    # At price 0.50: fee = 0.02 × 0.50 × 0.50 × 100 = 0.50
    fee = polymarket_taker_fee(price=0.50, quantity=100, max_fee_rate=0.02)
    assert abs(fee - 0.50) < 0.001


def test_polymarket_fee_at_extreme_price():
    # At price 0.95: fee = 0.02 × 0.95 × 0.05 × 100 = 0.095
    fee = polymarket_taker_fee(price=0.95, quantity=100, max_fee_rate=0.02)
    assert abs(fee - 0.095) < 0.001


def test_polymarket_fee_disabled():
    fee = polymarket_taker_fee(price=0.50, quantity=100, max_fee_rate=0.0)
    assert fee == 0.0


def test_kalshi_fee():
    # Kalshi: fee = 0.07 × price × (1 - price) × count, capped at $0.50/contract
    fee = kalshi_taker_fee(price_cents=50, count=10)
    # 0.07 × 0.50 × 0.50 × 10 = 0.175 per contract uncapped
    # Per contract: 0.07 × 0.50 × 0.50 = 0.0175 → $0.0175, well under $0.50 cap
    # Total: 0.175
    assert abs(fee - 0.175) < 0.001


def test_kalshi_fee_cap():
    # At price_cents=50: per-contract fee = 0.07 × 0.5 × 0.5 = 0.0175
    # Well below $0.50 cap, so no cap applies here
    # At price_cents=1 (extreme): 0.07 × 0.01 × 0.99 = 0.000693 per contract
    fee = kalshi_taker_fee(price_cents=1, count=1000)
    assert fee > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_replay_fees.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/replay/__init__.py
"""Replay engine — honest taker-fill simulation over episodes."""

# sim/replay/fees.py
"""Venue-specific fee calculations."""
from __future__ import annotations


def polymarket_taker_fee(
    price: float,
    quantity: float,
    max_fee_rate: float = 0.02,
) -> float:
    """Polymarket taker fee: max_fee_rate × price × (1 - price) × quantity.

    Args:
        price: probability price in [0, 1]
        quantity: number of shares
        max_fee_rate: per-token fee rate (default 2%, varies by market)

    Returns:
        Fee in USDC.
    """
    if max_fee_rate <= 0 or quantity <= 0:
        return 0.0
    return max_fee_rate * price * (1.0 - price) * quantity


def kalshi_taker_fee(
    price_cents: int,
    count: int,
    taker_rate: float = 0.07,
    cap_per_contract: float = 0.50,
) -> float:
    """Kalshi taker fee: taker_rate × p × (1-p) × count, capped per contract.

    Args:
        price_cents: price in cents (1-99)
        count: number of contracts
        taker_rate: fee rate (default 7%)
        cap_per_contract: max fee per contract in dollars

    Returns:
        Fee in USD.
    """
    p = price_cents / 100.0
    per_contract = min(taker_rate * p * (1.0 - p), cap_per_contract)
    return per_contract * count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_replay_fees.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add sim/replay/ tests/sim/test_replay_fees.py
git commit -m "sim/replay: fee models — Polymarket + Kalshi taker fees"
```

---

### Task 2C.2: Strategy Protocol + ReplayContext

**Files:**
- Create: `sim/replay/strategy.py`
- Create: `sim/replay/context.py`
- Create: `tests/sim/test_replay_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_replay_context.py
from sim.replay.context import ReplayContext, Fill, OrderResult
from sim.replay.fees import polymarket_taker_fee


def test_place_buy_order_fills_at_ask():
    ctx = ReplayContext(
        cash=1000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda price, qty: polymarket_taker_fee(price, qty, 0.02),
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=100)
    assert result.filled == 100
    assert result.fill_price == 0.65
    assert result.fee > 0
    assert ctx.get_cash() < 1000.0
    assert ctx.get_positions()["YES"] == 100


def test_order_rejected_no_quote():
    ctx = ReplayContext(
        cash=1000.0,
        positions={},
        current_quotes={},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda price, qty: 0.0,
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=100)
    assert result.filled == 0
    assert result.rejected


def test_fill_capped_to_liquidity():
    ctx = ReplayContext(
        cash=100000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 50.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda price, qty: 0.0,
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=1000)
    assert result.filled == 50  # capped to ask_size
    assert result.partial


def test_fill_rejected_null_size():
    ctx = ReplayContext(
        cash=100000.0,
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": None}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda price, qty: 0.0,
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=100)
    assert result.filled == 0
    assert result.rejected


def test_sell_fills_at_bid():
    ctx = ReplayContext(
        cash=0.0,
        positions={"YES": 200},
        current_quotes={"YES": {"best_bid": 0.70, "best_ask": 0.75, "bid_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda price, qty: polymarket_taker_fee(price, qty, 0.02),
    )
    result = ctx.place_order(ticker="YES", side="SELL", count=100)
    assert result.filled == 100
    assert result.fill_price == 0.70
    assert ctx.get_positions()["YES"] == 100
    assert ctx.get_cash() > 0


def test_insufficient_cash():
    ctx = ReplayContext(
        cash=10.0,  # not enough for 100 @ 0.65
        positions={},
        current_quotes={"YES": {"best_bid": 0.60, "best_ask": 0.65, "ask_size": 500.0}},
        current_ts="2026-04-03T12:00:00.000Z",
        fee_fn=lambda price, qty: 0.0,
    )
    result = ctx.place_order(ticker="YES", side="BUY", count=100)
    # Should fill what cash allows: 10 / 0.65 ≈ 15 shares
    assert result.filled <= 15
    assert result.partial
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_replay_context.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/replay/strategy.py
"""Strategy protocol — what strategies must implement."""
from __future__ import annotations

from typing import Protocol, Any


class Strategy(Protocol):
    def on_episode_start(self, metadata: dict) -> None: ...
    def act(self, ctx: Any) -> None: ...
    def on_episode_end(self, result: dict) -> None: ...


# sim/replay/context.py
"""ReplayContext — quote lookup, position tracking, order placement."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional
import math


@dataclass
class OrderResult:
    filled: int = 0
    fill_price: float = 0.0
    fee: float = 0.0
    rejected: bool = False
    partial: bool = False
    reason: str = ""


class ReplayContext:
    """Mutable simulation state for one episode. Updated by the replay engine."""

    def __init__(
        self,
        cash: float,
        positions: dict[str, int],
        current_quotes: dict[str, dict],
        current_ts: str,
        fee_fn: Callable[[float, float], float],
    ):
        self._cash = cash
        self._positions = dict(positions)
        self._quotes = current_quotes
        self._ts = current_ts
        self._fee_fn = fee_fn
        self._fills: list[dict] = []

    def get_cash(self) -> float:
        return self._cash

    def get_positions(self) -> dict[str, int]:
        return dict(self._positions)

    def get_time(self) -> str:
        return self._ts

    def get_markets(self) -> dict[str, dict]:
        return dict(self._quotes)

    def update_quotes(self, quotes: dict[str, dict]) -> None:
        self._quotes = quotes

    def update_time(self, ts: str) -> None:
        self._ts = ts

    def place_order(self, ticker: str, side: str, count: int) -> OrderResult:
        if ticker not in self._quotes:
            return OrderResult(rejected=True, reason="no quote")

        quote = self._quotes[ticker]
        if side == "BUY":
            price = quote.get("best_ask")
            available = quote.get("ask_size")
        elif side == "SELL":
            price = quote.get("best_bid")
            available = quote.get("bid_size")
        else:
            return OrderResult(rejected=True, reason=f"invalid side: {side}")

        if price is None:
            return OrderResult(rejected=True, reason="no price")
        if available is None:
            return OrderResult(rejected=True, reason="no liquidity data")

        # Cap to available liquidity
        fill_count = min(count, int(available))
        if fill_count <= 0:
            return OrderResult(rejected=True, reason="no liquidity")

        # For buys: cap to what cash allows
        if side == "BUY":
            max_affordable = int(self._cash / price) if price > 0 else 0
            fill_count = min(fill_count, max_affordable)
            if fill_count <= 0:
                return OrderResult(rejected=True, reason="insufficient cash")

        # For sells: cap to held position
        if side == "SELL":
            held = self._positions.get(ticker, 0)
            fill_count = min(fill_count, held)
            if fill_count <= 0:
                return OrderResult(rejected=True, reason="no position")

        fee = self._fee_fn(price, fill_count)
        cost = price * fill_count + fee

        if side == "BUY":
            self._cash -= cost
            self._positions[ticker] = self._positions.get(ticker, 0) + fill_count
        else:
            proceeds = price * fill_count - fee
            self._cash += proceeds
            self._positions[ticker] = self._positions.get(ticker, 0) - fill_count

        partial = fill_count < count
        self._fills.append({
            "ts": self._ts, "ticker": ticker, "side": side,
            "count": fill_count, "price": price, "fee": fee,
        })

        return OrderResult(
            filled=fill_count,
            fill_price=price,
            fee=fee,
            partial=partial,
        )

    def settle(self, outcomes: dict[str, float]) -> float:
        """Settle all positions. Returns total PnL from settlement.

        outcomes: {ticker: payout_per_share} e.g. {"YES": 1.0, "NO": 0.0}
        """
        settlement_cash = 0.0
        for ticker, shares in self._positions.items():
            payout = outcomes.get(ticker, 0.0)
            settlement_cash += shares * payout
        self._cash += settlement_cash
        self._positions.clear()
        return settlement_cash
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_replay_context.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add sim/replay/strategy.py sim/replay/context.py tests/sim/test_replay_context.py
git commit -m "sim/replay: context + strategy protocol — fills, fees, liquidity cap"
```

---

## Exit Gate Verification

After all slices are implemented:

- [ ] `python3 -m pytest tests/sim/ -v` — all tests pass
- [ ] Compactor runs on synthetic fixture: `python3 -m sim.compact.run --raw-dir /tmp/test_raw --output-dir /tmp/test_out --manifest /tmp/test_manifest.json`
- [ ] All 5 Parquet tables produced and readable
- [ ] Manifest tracks processed files correctly (re-run produces no new output)
- [ ] Fee calculations match manual computation within $0.01

**Note:** Exit gates 2 (episode from resolved market) and 3 (replay on Kalshi episode) require Slice 2B + episode data. Those will be verified after the episode builder is implemented and the VPS collector has captured at least one market resolution.

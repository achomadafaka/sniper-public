# Phase 1: Data Collection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A VPS service that captures Polymarket WebSocket events and metadata into append-only gzipped JSONL files, conforming to the Phase 0 canonical schema, within 100MB RSS.

**Architecture:** Three independent slices that share only the `sim/` package from Phase 0 and a common config module. Slice 1 (WS collector) runs as a long-lived asyncio process. Slice 2 (metadata sync) runs as a periodic poller and writes both raw metadata events and a `markets_snapshot.json` coordination file that Slice 1 reloads. Slice 3 (monitoring) provides a health check script for manual use or a later timer. Only Slices 1 and 2 are deployed as long-running systemd user services on the VPS.

**Tech Stack:** Python 3.12+, websockets, aiohttp (for Gamma API), gzip, sim/ package from Phase 0. No SQLite dependency — raw storage is JSONL only.

**Parallel execution:** Slices 1, 2, and 3 have disjoint file ownership. They can be implemented by separate subagents simultaneously.

---

## File Structure

### Slice 1: WS Collector
| File | Responsibility |
|------|---------------|
| `sim/collector/ws.py` | WebSocket connection, message decoding, RawEvent creation |
| `sim/collector/writer.py` | Gzipped JSONL file rotation and append |
| `sim/collector/ingest.py` | Main collector loop — ties WS to writer |
| `sim/collector/__init__.py` | Package marker |
| `tests/sim/test_ws.py` | WS message parsing tests |
| `tests/sim/test_writer.py` | File rotation and write tests |

### Slice 2: Metadata Sync
| File | Responsibility |
|------|---------------|
| `sim/collector/meta.py` | Gamma API polling, universe filtering, snapshot persistence, metadata/settlement raw events |
| `tests/sim/test_meta.py` | Metadata discovery and settlement tests |

### Slice 3: Monitoring + Ops
| File | Responsibility |
|------|---------------|
| `sim/collector/health.py` | Schema conformance check, gap detection, resource check |
| `sim/collector/config.py` | Shared config (paths, thresholds, env vars) |
| `systemd/sniper-sim-collector.service` | WS collector systemd unit |
| `systemd/sniper-sim-meta.service` | Metadata sync systemd unit |
| `tests/sim/test_health.py` | Health check tests |
| `tests/sim/test_config.py` | Config tests |

---

## Slice 1: WS Collector

### Task 1.1: Collector Config

**Files:**
- Create: `sim/collector/__init__.py`
- Create: `sim/collector/config.py`
- Create: `tests/sim/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_config.py
from sim.collector.config import CollectorConfig


def test_default_config():
    cfg = CollectorConfig()
    assert cfg.ws_url == "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    assert cfg.raw_dir.endswith("raw")
    assert cfg.venue == "polymarket"
    assert cfg.max_markets == 200
    assert cfg.rotation_seconds == 3600
    assert cfg.reconnect_base_delay == 5.0
    assert cfg.reconnect_max_delay == 300.0
    assert cfg.custom_feature_enabled is True
    assert cfg.snapshot_path.endswith("markets_snapshot.json")
    assert cfg.market_reload_seconds == 60
    assert cfg.app_ping_seconds == 10


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("SIM_RAW_DIR", "/tmp/test_raw")
    monkeypatch.setenv("SIM_MAX_MARKETS", "50")
    monkeypatch.setenv("SIM_MARKETS_SNAPSHOT_PATH", "/tmp/sim-markets.json")
    cfg = CollectorConfig.from_env()
    assert cfg.raw_dir == "/tmp/test_raw"
    assert cfg.max_markets == 50
    assert cfg.snapshot_path == "/tmp/sim-markets.json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/collector/__init__.py
"""Simulation platform data collector."""

# sim/collector/config.py
"""Collector configuration — all knobs in one place."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class CollectorConfig:
    """Configuration for the WS collector and metadata sync."""

    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_url: str = "https://gamma-api.polymarket.com"
    raw_dir: str = os.path.expanduser("~/sniper/raw")
    snapshot_path: str = os.path.expanduser("~/sniper/state/markets_snapshot.json")
    venue: str = "polymarket"
    max_markets: int = 200
    rotation_seconds: int = 3600
    reconnect_base_delay: float = 5.0
    reconnect_max_delay: float = 300.0
    meta_poll_interval: int = 1800  # 30 minutes
    market_reload_seconds: int = 60
    app_ping_seconds: int = 10
    custom_feature_enabled: bool = True
    min_volume: float = 10_000.0
    max_days_to_end: int = 90

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def from_env(cls) -> CollectorConfig:
        return cls(
            ws_url=os.getenv("SIM_WS_URL", cls.ws_url),
            gamma_url=os.getenv("SIM_GAMMA_URL", cls.gamma_url),
            raw_dir=os.getenv("SIM_RAW_DIR", os.path.expanduser("~/sniper/raw")),
            snapshot_path=os.getenv("SIM_MARKETS_SNAPSHOT_PATH", os.path.expanduser("~/sniper/state/markets_snapshot.json")),
            venue=os.getenv("SIM_VENUE", cls.venue),
            max_markets=int(os.getenv("SIM_MAX_MARKETS", str(cls.max_markets))),
            rotation_seconds=int(os.getenv("SIM_ROTATION_SECONDS", str(cls.rotation_seconds))),
            meta_poll_interval=int(os.getenv("SIM_META_POLL_INTERVAL", str(cls.meta_poll_interval))),
            market_reload_seconds=int(os.getenv("SIM_MARKET_RELOAD_SECONDS", str(cls.market_reload_seconds))),
            app_ping_seconds=int(os.getenv("SIM_APP_PING_SECONDS", str(cls.app_ping_seconds))),
            custom_feature_enabled=cls._env_bool("SIM_CUSTOM_FEATURE_ENABLED", cls.custom_feature_enabled),
            min_volume=float(os.getenv("SIM_MIN_VOLUME", str(cls.min_volume))),
            max_days_to_end=int(os.getenv("SIM_MAX_DAYS_TO_END", str(cls.max_days_to_end))),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_config.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add sim/collector/ tests/sim/test_config.py
git commit -m "sim/collector: config module — all knobs in one place"
```

---

### Task 1.2: JSONL Writer with Gzip Rotation

**Files:**
- Create: `sim/collector/writer.py`
- Create: `tests/sim/test_writer.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_writer.py
import gzip
import json
import os
import tempfile
from datetime import datetime, timezone

from sim.collector.writer import RawWriter
from sim.schema import RawEvent, EventType
from sim.serialize import jsonl_to_event


def _make_event(seq: int) -> RawEvent:
    return RawEvent(
        venue="polymarket",
        market_id="pm:0xtest",
        venue_market_id="0xtest",
        market_type="geopolitical",
        event_type=EventType.TRADE,
        capture_ts=datetime.now(timezone.utc).isoformat(timespec="milliseconds") + "Z",
        ingest_seq=seq,
        payload={"price": 0.65, "size": 100},
    )


def test_writer_creates_gzipped_file():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = RawWriter(raw_dir=tmpdir, venue="polymarket", rotation_seconds=3600)
        w.write(_make_event(1))
        w.close()

        files = []
        for root, dirs, fnames in os.walk(tmpdir):
            for f in fnames:
                files.append(os.path.join(root, f))

        assert len(files) == 1
        assert files[0].endswith(".jsonl.gz")

        with gzip.open(files[0], "rt") as f:
            line = f.readline()
            evt = jsonl_to_event(line.strip())
            assert evt.venue == "polymarket"
            assert evt.ingest_seq == 1


def test_writer_batches_multiple_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = RawWriter(raw_dir=tmpdir, venue="polymarket", rotation_seconds=3600)
        for i in range(10):
            w.write(_make_event(i))
        w.close()

        files = []
        for root, dirs, fnames in os.walk(tmpdir):
            for f in fnames:
                files.append(os.path.join(root, f))

        assert len(files) == 1
        with gzip.open(files[0], "rt") as f:
            lines = f.readlines()
            assert len(lines) == 10


def test_writer_file_path_format():
    with tempfile.TemporaryDirectory() as tmpdir:
        w = RawWriter(raw_dir=tmpdir, venue="polymarket", rotation_seconds=3600)
        w.write(_make_event(0))
        path = w._current_path
        w.close()

        # Path should be: {raw_dir}/polymarket/YYYY-MM-DD/HH.jsonl.gz
        parts = path.replace(tmpdir + "/", "").split("/")
        assert parts[0] == "polymarket"
        assert len(parts) == 3  # venue/date/hour.jsonl.gz
        assert parts[2].endswith(".jsonl.gz")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_writer.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/collector/writer.py
"""Append-only gzipped JSONL writer with hourly rotation."""
from __future__ import annotations

import gzip
import os
import time
from datetime import datetime, timezone
from typing import Optional

from sim.schema import RawEvent
from sim.serialize import event_to_jsonl


class RawWriter:
    """Writes RawEvents to gzipped JSONL files.

    File layout: {raw_dir}/{venue}/{YYYY-MM-DD}/{HH}.jsonl.gz
    One file per venue per hour. All markets batched together.
    """

    def __init__(self, raw_dir: str, venue: str, rotation_seconds: int = 3600):
        self._raw_dir = raw_dir
        self._venue = venue
        self._rotation_seconds = rotation_seconds
        self._current_hour: Optional[str] = None
        self._current_path: Optional[str] = None
        self._file: Optional[gzip.GzipFile] = None

    def _hour_key(self) -> str:
        now = datetime.now(timezone.utc)
        return now.strftime("%Y-%m-%d/%H")

    def _ensure_file(self) -> gzip.GzipFile:
        hour = self._hour_key()
        if self._file is not None and self._current_hour == hour:
            return self._file

        self.close()
        date_part, hour_part = hour.split("/")
        dir_path = os.path.join(self._raw_dir, self._venue, date_part)
        os.makedirs(dir_path, exist_ok=True)
        self._current_path = os.path.join(dir_path, f"{hour_part}.jsonl.gz")
        self._file = gzip.open(self._current_path, "at", compresslevel=1)
        self._current_hour = hour
        return self._file

    def write(self, event: RawEvent) -> None:
        f = self._ensure_file()
        line = event_to_jsonl(event)
        f.write(line + "\n")

    def flush(self) -> None:
        if self._file is not None:
            self._file.flush()

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
            self._current_hour = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_writer.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add sim/collector/writer.py tests/sim/test_writer.py
git commit -m "sim/collector: gzipped JSONL writer with hourly rotation"
```

---

### Task 1.3: WS Message Parser

**Files:**
- Create: `sim/collector/ws.py`
- Create: `tests/sim/test_ws.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_ws.py
from sim.collector.ws import parse_ws_message
from sim.schema import EventType


def test_parse_last_trade_price():
    raw = {
        "event_type": "last_trade_price",
        "asset_id": "36272374",
        "market": "0xcondition123",
        "price": "0.65",
        "size": "100.5",
        "side": "BUY",
        "timestamp": "1711929600",
    }
    events = parse_ws_message(
        raw,
        market_id="pm:0xcondition123",
        venue_market_id="0xcondition123",
        market_type="geopolitical",
        ingest_seq=42,
        capture_ts="2026-04-01T14:00:00.000Z",
    )
    assert len(events) == 1
    evt = events[0]
    assert evt.event_type == EventType.TRADE
    assert evt.asset_id == "36272374"
    assert evt.venue == "polymarket"
    assert evt.ingest_seq == 42
    assert evt.exchange_ts == "2024-04-01T00:00:00.000Z"
    assert evt.payload["price"] == "0.65"
    assert evt.payload["size"] == "100.5"
    assert evt.payload["side"] == "BUY"


def test_parse_price_change():
    raw = {
        "event_type": "price_change",
        "market": "0xcondition123",
        "price_changes": [
            {
                "asset_id": "36272374",
                "price": "0.70",
                "size": "500",
                "side": "BUY",
                "best_bid": "0.69",
                "best_ask": "0.71",
            }
        ],
        "timestamp": "1711929601",
    }
    events = parse_ws_message(
        raw,
        market_id="pm:0xcondition123",
        venue_market_id="0xcondition123",
        market_type="geopolitical",
        ingest_seq=43,
        capture_ts="2026-04-01T14:00:01.000Z",
    )
    assert len(events) == 1
    assert events[0].event_type == EventType.BOOK_DELTA
    assert events[0].asset_id == "36272374"
    assert events[0].exchange_ts == "2024-04-01T00:00:01.000Z"


def test_parse_book():
    raw = {
        "event_type": "book",
        "asset_id": "36272374",
        "market": "0xcondition123",
        "bids": [{"price": "0.60", "size": "1000"}],
        "asks": [{"price": "0.65", "size": "500"}],
        "timestamp": "1711929602",
    }
    events = parse_ws_message(
        raw,
        market_id="pm:0xcondition123",
        venue_market_id="0xcondition123",
        market_type="geopolitical",
        ingest_seq=44,
        capture_ts="2026-04-01T14:00:02.000Z",
    )
    assert len(events) == 1
    assert events[0].event_type == EventType.BOOK_SNAPSHOT
    assert events[0].exchange_ts == "2024-04-01T00:00:02.000Z"


def test_parse_best_bid_ask():
    raw = {
        "event_type": "best_bid_ask",
        "asset_id": "36272374",
        "market": "0xcondition123",
        "best_bid": "0.61",
        "best_ask": "0.64",
        "spread": "0.03",
        "timestamp": "1711929603",
    }
    events = parse_ws_message(
        raw,
        market_id="pm:0xcondition123",
        venue_market_id="0xcondition123",
        market_type="geopolitical",
        ingest_seq=45,
        capture_ts="2026-04-01T14:00:03.000Z",
    )
    assert len(events) == 1
    assert events[0].event_type == EventType.BEST_BBA
    assert events[0].exchange_ts == "2024-04-01T00:00:03.000Z"


def test_parse_unknown_event_type_returns_empty():
    raw = {"event_type": "unknown_type", "market": "0x123"}
    events = parse_ws_message(
        raw,
        market_id="pm:0x123",
        venue_market_id="0x123",
        market_type="other",
        ingest_seq=0,
        capture_ts="2026-04-01T00:00:00.000Z",
    )
    assert events == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_ws.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/collector/ws.py
"""Polymarket WebSocket message parser.

Converts venue-native WS messages into canonical RawEvents.
Does NOT manage the WS connection — that's in ingest.py.
"""
from __future__ import annotations

from sim.schema import RawEvent, EventType

# Map Polymarket WS event_type to canonical EventType
_EVENT_MAP = {
    "last_trade_price": EventType.TRADE,
    "price_change": EventType.BOOK_DELTA,
    "book": EventType.BOOK_SNAPSHOT,
    "best_bid_ask": EventType.BEST_BBA,
}


def _ms_epoch_to_iso(ts: str | int | None) -> str | None:
    if ts in (None, ""):
        return None
    try:
        ms = int(ts)
    except (TypeError, ValueError):
        return None
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_ws_message(
    raw: dict,
    *,
    market_id: str,
    venue_market_id: str,
    market_type: str,
    ingest_seq: int,
    capture_ts: str,
) -> list[RawEvent]:
    """Parse a single Polymarket WS message into RawEvent(s).

    Returns empty list for unrecognized event types.
    """
    ws_type = raw.get("event_type", "")
    event_type = _EVENT_MAP.get(ws_type)
    if event_type is None:
        return []

    exchange_ts = _ms_epoch_to_iso(raw.get("timestamp"))

    if ws_type == "price_change":
        events: list[RawEvent] = []
        for change in raw.get("price_changes", []):
            asset_id = change.get("asset_id")
            events.append(
                RawEvent(
                    venue="polymarket",
                    market_id=market_id,
                    venue_market_id=venue_market_id,
                    market_type=market_type,
                    event_type=event_type,
                    capture_ts=capture_ts,
                    ingest_seq=ingest_seq,
                    payload=change,
                    asset_id=str(asset_id) if asset_id else None,
                    outcome_index=None,
                    exchange_ts=exchange_ts,
                    venue_seq=None,
                )
            )
        return events

    asset_id = raw.get("asset_id")
    return [
        RawEvent(
            venue="polymarket",
            market_id=market_id,
            venue_market_id=venue_market_id,
            market_type=market_type,
            event_type=event_type,
            capture_ts=capture_ts,
            ingest_seq=ingest_seq,
            payload=raw,
            asset_id=str(asset_id) if asset_id else None,
            outcome_index=None,
            exchange_ts=exchange_ts,
            venue_seq=None,
        )
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_ws.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add sim/collector/ws.py tests/sim/test_ws.py
git commit -m "sim/collector: WS message parser — last_trade_price, price_change, book, best_bid_ask"
```

---

### Task 1.4: Collector Main Loop (ingest.py)

**Files:**
- Create: `sim/collector/ingest.py`

This is the integration point — connects WS, parser, snapshot state, heartbeat, and writer.

- [ ] **Step 1: Write the ingest loop**

```python
# sim/collector/ingest.py
"""Main collector loop — WebSocket → parser → writer."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed

from sim.collector.config import CollectorConfig
from sim.collector.writer import RawWriter
from sim.collector.ws import parse_ws_message
from sim.ids import make_market_id

LOG = logging.getLogger(__name__)


class Collector:
    """Polymarket WebSocket collector.

    Subscribes to market channels, parses messages into RawEvents,
    writes to gzipped JSONL via RawWriter.
    """

    def __init__(self, config: CollectorConfig):
        self._config = config
        self._writer = RawWriter(
            raw_dir=config.raw_dir,
            venue=config.venue,
            rotation_seconds=config.rotation_seconds,
        )
        self._ingest_seq = 0
        self._running = False
        self._markets: dict[str, dict] = {}  # condition_id -> market info
        self._asset_to_market: dict[str, str] = {}  # asset_id -> condition_id
        self._last_snapshot_mtime: float = 0.0
        self._last_snapshot_check: float = 0.0

    def _next_seq(self) -> int:
        self._ingest_seq += 1
        return self._ingest_seq

    def _capture_ts(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def register_markets(self, markets: list[dict]) -> None:
        """Register markets from metadata sync. Called externally."""
        for m in markets:
            cid = m.get("condition_id", "")
            if not cid:
                continue
            self._markets[cid] = m
            for token in m.get("tokens", []):
                tid = token.get("token_id", "")
                if tid:
                    self._asset_to_market[tid] = cid

    def load_markets_snapshot(self) -> bool:
        """Reload the latest market universe snapshot if it changed on disk."""
        now = time.monotonic()
        if now - self._last_snapshot_check < self._config.market_reload_seconds:
            return False
        self._last_snapshot_check = now
        path = self._config.snapshot_path
        if not os.path.exists(path):
            return False
        mtime = os.path.getmtime(path)
        if mtime <= self._last_snapshot_mtime:
            return False
        with open(path, "r", encoding="utf-8") as f:
            markets = json.load(f)
        if not isinstance(markets, list):
            raise ValueError("markets snapshot must be a JSON list")
        self._markets.clear()
        self._asset_to_market.clear()
        self.register_markets(markets)
        self._last_snapshot_mtime = mtime
        LOG.info("Loaded %d markets from %s", len(markets), path)
        return True

    def get_asset_ids(self) -> list[str]:
        """Return all asset IDs to subscribe to."""
        return list(self._asset_to_market.keys())

    async def _heartbeat(self, ws) -> None:
        """Application-layer heartbeat required by the Polymarket market channel."""
        while self._running:
            await asyncio.sleep(self._config.app_ping_seconds)
            await ws.send("{}")

    async def run(self) -> None:
        """Main loop — connect, subscribe, receive, parse, write."""
        self._running = True
        delay = self._config.reconnect_base_delay

        while self._running:
            try:
                await self._connect_and_stream()
                delay = self._config.reconnect_base_delay
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                LOG.warning("WS disconnected: %s. Reconnecting in %.0fs", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._config.reconnect_max_delay)
            except Exception:
                LOG.exception("Unexpected error in collector loop")
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._config.reconnect_max_delay)

    async def _connect_and_stream(self) -> None:
        self.load_markets_snapshot()
        asset_ids = self.get_asset_ids()
        if not asset_ids:
            LOG.info("No markets registered yet. Waiting for snapshot...")
            await asyncio.sleep(30)
            return

        LOG.info("Connecting to %s with %d assets", self._config.ws_url, len(asset_ids))
        async with websockets.connect(
            self._config.ws_url,
            ping_interval=30,
            ping_timeout=60,
            close_timeout=10,
        ) as ws:
            # Subscribe
            subscribe_msg = json.dumps({
                "type": "market",
                "assets_ids": asset_ids,
                "custom_feature_enabled": self._config.custom_feature_enabled,
            })
            await ws.send(subscribe_msg)
            LOG.info("Subscribed to %d assets", len(asset_ids))
            heartbeat_task = asyncio.create_task(self._heartbeat(ws))

            try:
                async for raw_text in ws:
                    if not self._running:
                        break

                    capture_ts = self._capture_ts()
                    self.load_markets_snapshot()

                    try:
                        messages = json.loads(raw_text)
                    except json.JSONDecodeError:
                        LOG.warning("Invalid JSON from WS: %s", raw_text[:200])
                        continue

                    if not isinstance(messages, list):
                        messages = [messages]

                    for msg in messages:
                        asset_id = msg.get("asset_id", "")
                        condition_id = msg.get("market", "") or self._asset_to_market.get(asset_id, "")
                        if not condition_id:
                            continue

                        market_info = self._markets.get(condition_id, {})
                        market_id = make_market_id("polymarket", condition_id)
                        market_type = market_info.get("market_type", "other")

                        events = parse_ws_message(
                            msg,
                            market_id=market_id,
                            venue_market_id=condition_id,
                            market_type=market_type,
                            ingest_seq=self._next_seq(),
                            capture_ts=capture_ts,
                        )

                        for evt in events:
                            self._writer.write(evt)

                    if self._ingest_seq % 100 == 0:
                        self._writer.flush()
            finally:
                heartbeat_task.cancel()

    def stop(self) -> None:
        self._running = False
        self._writer.close()
```

- [ ] **Step 2: Commit**

```bash
git add sim/collector/ingest.py
git commit -m "sim/collector: main ingest loop — WS connect, parse, write"
```

---

## Slice 2: Metadata Sync

### Task 2.1: Gamma API Metadata Poller + Snapshot Writer

**Files:**
- Create: `sim/collector/meta.py`
- Create: `tests/sim/test_meta.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_meta.py
import json
from sim.collector.meta import (
    parse_gamma_market,
    filter_and_classify,
    write_market_snapshot,
    build_metadata_events,
)


def test_parse_gamma_market():
    raw = {
        "conditionId": "0xabc123",
        "question": "US strikes Iran by March 31?",
        "outcomes": json.dumps(["Yes", "No"]),
        "outcomePrices": json.dumps(["0.25", "0.75"]),
        "clobTokenIds": json.dumps(["token_yes_123", "token_no_456"]),
        "volume": "150000",
        "volume24hr": "5000",
        "active": True,
        "closed": False,
        "endDate": "2026-06-01T00:00:00Z",
    }
    result = parse_gamma_market(raw)
    assert result is not None
    assert result["condition_id"] == "0xabc123"
    assert result["question"] == "US strikes Iran by March 31?"
    assert result["outcomes"] == ["Yes", "No"]
    assert result["tokens"] == [
        {"token_id": "token_yes_123", "label": "Yes", "index": 0},
        {"token_id": "token_no_456", "label": "No", "index": 1},
    ]
    assert result["volume"] == 150000.0
    assert result["market_type"] == "geopolitical"
    assert result["market_id"] == "pm:0xabc123"
    assert result["outcome_prices"] == ["0.25", "0.75"]


def test_parse_gamma_market_returns_none_for_missing_condition():
    assert parse_gamma_market({}) is None


def test_filter_and_classify():
    markets = [
        {
            "conditionId": "0x1",
            "question": "BTC above 100K?",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "clobTokenIds": json.dumps(["t1", "t2"]),
            "volume": "50000",
            "active": True,
            "closed": False,
            "endDate": "2026-05-01T00:00:00Z",
        },
        {
            "conditionId": "0x2",
            "question": "Low volume market",
            "outcomes": json.dumps(["Yes", "No"]),
            "outcomePrices": json.dumps(["0.5", "0.5"]),
            "clobTokenIds": json.dumps(["t3", "t4"]),
            "volume": "500",  # below threshold
            "active": True,
            "closed": False,
            "endDate": "2026-05-01T00:00:00Z",
        },
    ]
    result = filter_and_classify(markets, max_markets=10)
    assert len(result) == 1
    assert result[0]["condition_id"] == "0x1"


def test_write_market_snapshot(tmp_path):
    markets = [{"condition_id": "0x1", "tokens": [], "market_type": "crypto_threshold"}]
    path = tmp_path / "markets_snapshot.json"
    write_market_snapshot(str(path), markets)
    assert path.exists()
    assert json.loads(path.read_text()) == markets


def test_build_market_lifecycle_events():
    markets = [
        {
            "market_id": "pm:0x1",
            "condition_id": "0x1",
            "question": "BTC above 100K?",
            "outcomes": ["Yes", "No"],
            "outcome_prices": ["1", "0"],
            "tokens": [{"token_id": "t1", "label": "Yes", "index": 0}],
            "volume": 50000.0,
            "active": True,
            "closed": True,
            "end_date": "2026-05-01T00:00:00Z",
            "market_type": "crypto_threshold",
        }
    ]
    events = build_metadata_events(markets, ingest_seq_start=10, capture_ts="2026-04-01T00:00:00.000Z")
    assert len(events) == 2
    assert events[0].event_type.value == "market_metadata"
    assert events[1].event_type.value == "market_resolved"
    assert events[0].market_id == "pm:0x1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_meta.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/collector/meta.py
"""Metadata sync — Gamma API polling, universe filtering, snapshot persistence, settlement detection."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

import aiohttp

from sim.collector.config import CollectorConfig
from sim.collector.writer import RawWriter
from sim.ids import make_market_id
from sim.market_type import classify
from sim.schema import RawEvent, EventType
from sim.universe import UniverseFilter, MarketCandidate

LOG = logging.getLogger(__name__)


def parse_gamma_market(raw: dict[str, Any]) -> Optional[dict]:
    """Parse a single Gamma API market response into a normalized dict.

    Returns None if the market is missing required fields.
    """
    condition_id = raw.get("conditionId", "")
    if not condition_id:
        return None

    question = raw.get("question", "")

    try:
        outcomes = json.loads(raw.get("outcomes", "[]"))
    except (json.JSONDecodeError, TypeError):
        outcomes = []

    try:
        token_ids = json.loads(raw.get("clobTokenIds", "[]"))
    except (json.JSONDecodeError, TypeError):
        token_ids = []

    try:
        outcome_prices = json.loads(raw.get("outcomePrices", "[]"))
    except (json.JSONDecodeError, TypeError):
        outcome_prices = []

    try:
        volume = float(raw.get("volume", 0))
    except (TypeError, ValueError):
        volume = 0.0

    active = bool(raw.get("active", False))
    closed = bool(raw.get("closed", False))
    end_date = raw.get("endDate", "")

    tokens = []
    for i, (outcome, tid) in enumerate(zip(outcomes, token_ids)):
        tokens.append({"token_id": str(tid), "label": str(outcome), "index": i})

    return {
        "condition_id": condition_id,
        "question": question,
        "outcomes": outcomes,
        "outcome_prices": outcome_prices,
        "tokens": tokens,
        "volume": volume,
        "active": active,
        "closed": closed,
        "end_date": end_date,
        "market_id": make_market_id("polymarket", condition_id),
        "market_type": classify("polymarket", question),
    }


def filter_and_classify(
    raw_markets: list[dict[str, Any]],
    max_markets: int = 200,
    min_volume: float = 10_000.0,
    max_days_to_end: int = 90,
) -> list[dict]:
    """Parse, filter, and classify a batch of Gamma API markets."""
    uf = UniverseFilter(
        min_volume=min_volume,
        max_days_to_end=max_days_to_end,
        max_active=max_markets,
    )

    parsed = []
    for raw in raw_markets:
        m = parse_gamma_market(raw)
        if m is None:
            continue
        candidate = MarketCandidate(
            condition_id=m["condition_id"],
            question=m["question"],
            outcomes=m["outcomes"],
            volume=m["volume"],
            end_date=m["end_date"],
            active=m["active"],
            closed=m["closed"],
        )
        if uf.qualifies(candidate):
            parsed.append(m)

    # Apply cap by volume
    parsed.sort(key=lambda m: m["volume"], reverse=True)
    return parsed[:max_markets]


def write_market_snapshot(path: str, markets: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(markets, f, separators=(",", ":"))


def build_metadata_events(
    markets: list[dict[str, Any]],
    *,
    ingest_seq_start: int,
    capture_ts: str,
) -> list[RawEvent]:
    events: list[RawEvent] = []
    seq = ingest_seq_start
    for market in markets:
        events.append(
            RawEvent(
                venue="polymarket",
                market_id=market["market_id"],
                venue_market_id=market["condition_id"],
                market_type=market["market_type"],
                event_type=EventType.MARKET_METADATA,
                capture_ts=capture_ts,
                ingest_seq=seq,
                payload=market,
            )
        )
        seq += 1
        if market.get("closed") and market.get("outcome_prices"):
            events.append(
                RawEvent(
                    venue="polymarket",
                    market_id=market["market_id"],
                    venue_market_id=market["condition_id"],
                    market_type=market["market_type"],
                    event_type=EventType.MARKET_RESOLVED,
                    capture_ts=capture_ts,
                    ingest_seq=seq,
                    payload={
                        "outcome_prices": market["outcome_prices"],
                        "resolved_ts": capture_ts,
                    },
                )
            )
            seq += 1
    return events


async def fetch_gamma_markets(session: aiohttp.ClientSession, config: CollectorConfig) -> list[dict[str, Any]]:
    async with session.get(f"{config.gamma_url}/markets") as resp:
        resp.raise_for_status()
        data = await resp.json()
    if not isinstance(data, list):
        raise ValueError("Gamma /markets response must be a list")
    return data


async def run_meta_loop(config: CollectorConfig) -> None:
    writer = RawWriter(raw_dir=config.raw_dir, venue=config.venue, rotation_seconds=config.rotation_seconds)
    seq = 0

    async with aiohttp.ClientSession() as session:
        while True:
            raw_markets = await fetch_gamma_markets(session, config)
            markets = filter_and_classify(
                raw_markets,
                max_markets=config.max_markets,
                min_volume=config.min_volume,
                max_days_to_end=config.max_days_to_end,
            )
            write_market_snapshot(config.snapshot_path, markets)

            from datetime import datetime, timezone

            capture_ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            events = build_metadata_events(markets, ingest_seq_start=seq, capture_ts=capture_ts)
            for event in events:
                writer.write(event)
            writer.flush()
            seq += len(events)

            LOG.info("Metadata sync wrote %d markets and refreshed %s", len(markets), config.snapshot_path)
            await asyncio.sleep(config.meta_poll_interval)


if __name__ == "__main__":
    asyncio.run(run_meta_loop(CollectorConfig.from_env()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_meta.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add sim/collector/meta.py tests/sim/test_meta.py
git commit -m "sim/collector: metadata sync — Gamma API polling, snapshot, raw metadata events"
```

---

## Slice 3: Monitoring + Ops

### Task 3.1: Health Check Script

**Files:**
- Create: `sim/collector/health.py`
- Create: `tests/sim/test_health.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_health.py
import gzip
import json
import os
import tempfile
from datetime import datetime, timezone

from sim.collector.health import check_freshness, check_schema_conformance


def _write_sample_raw(tmpdir: str, age_minutes: int = 5) -> str:
    """Write a sample raw JSONL file and return its path."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    hour_str = now.strftime("%H")
    dir_path = os.path.join(tmpdir, "polymarket", date_str)
    os.makedirs(dir_path, exist_ok=True)
    filepath = os.path.join(dir_path, f"{hour_str}.jsonl.gz")

    record = {
        "venue": "polymarket",
        "market_id": "pm:0xtest",
        "venue_market_id": "0xtest",
        "market_type": "geopolitical",
        "event_type": "trade",
        "capture_ts": now.isoformat(timespec="milliseconds") + "Z",
        "ingest_seq": 1,
        "payload": {"price": "0.65"},
    }
    with gzip.open(filepath, "wt") as f:
        f.write(json.dumps(record) + "\n")
    return filepath


def test_freshness_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        _write_sample_raw(tmpdir)
        result = check_freshness(tmpdir, venue="polymarket", max_age_minutes=10)
        assert result["status"] == "PASS"


def test_freshness_fail_no_files():
    with tempfile.TemporaryDirectory() as tmpdir:
        result = check_freshness(tmpdir, venue="polymarket", max_age_minutes=10)
        assert result["status"] == "FAIL"


def test_schema_conformance_pass():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = _write_sample_raw(tmpdir)
        result = check_schema_conformance(path, sample_size=1)
        assert result["status"] == "PASS"
        assert result["checked"] == 1
        assert result["missing_fields"] == 0


def test_schema_conformance_fail_missing_field():
    with tempfile.TemporaryDirectory() as tmpdir:
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        hour_str = datetime.now(timezone.utc).strftime("%H")
        dir_path = os.path.join(tmpdir, "polymarket", date_str)
        os.makedirs(dir_path, exist_ok=True)
        filepath = os.path.join(dir_path, f"{hour_str}.jsonl.gz")

        bad_record = {"venue": "polymarket", "payload": {}}  # missing required fields
        with gzip.open(filepath, "wt") as f:
            f.write(json.dumps(bad_record) + "\n")

        result = check_schema_conformance(filepath, sample_size=1)
        assert result["status"] == "FAIL"
        assert result["missing_fields"] > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_health.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

```python
# sim/collector/health.py
"""Health checks for the data collector."""
from __future__ import annotations

import gzip
import json
import os
from datetime import datetime, timezone
from typing import Any

from sim.serialize import jsonl_to_event


def check_freshness(
    raw_dir: str,
    venue: str = "polymarket",
    max_age_minutes: int = 10,
) -> dict[str, Any]:
    """Check if raw data is fresh enough."""
    venue_dir = os.path.join(raw_dir, venue)
    if not os.path.exists(venue_dir):
        return {"status": "FAIL", "reason": f"No directory: {venue_dir}", "last_file": None}

    latest_file = None
    latest_mtime = 0.0
    for root, dirs, files in os.walk(venue_dir):
        for f in files:
            if f.endswith(".jsonl.gz"):
                path = os.path.join(root, f)
                mtime = os.path.getmtime(path)
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_file = path

    if latest_file is None:
        return {"status": "FAIL", "reason": "No .jsonl.gz files found", "last_file": None}

    age_seconds = (datetime.now(timezone.utc).timestamp() - latest_mtime)
    age_minutes = age_seconds / 60

    if age_minutes > max_age_minutes:
        return {
            "status": "FAIL" if age_minutes > max_age_minutes * 3 else "WARN",
            "reason": f"Latest file is {age_minutes:.1f}min old",
            "last_file": latest_file,
            "age_minutes": round(age_minutes, 1),
        }

    return {
        "status": "PASS",
        "last_file": latest_file,
        "age_minutes": round(age_minutes, 1),
    }


def check_schema_conformance(
    filepath: str,
    sample_size: int = 100,
) -> dict[str, Any]:
    """Check that records in a raw file have all required fields."""
    checked = 0
    missing_count = 0
    missing_details: list[str] = []

    try:
        with gzip.open(filepath, "rt") as f:
            for line in f:
                if checked >= sample_size:
                    break
                try:
                    record = json.loads(line.strip())
                except json.JSONDecodeError:
                    missing_count += 1
                    missing_details.append(f"line {checked}: invalid JSON")
                    checked += 1
                    continue

                required = {"venue", "market_id", "venue_market_id", "market_type", "event_type", "capture_ts", "ingest_seq", "payload"}
                missing = required - set(record.keys())
                if missing:
                    missing_count += 1
                    missing_details.append(f"line {checked}: missing {missing}")
                else:
                    try:
                        jsonl_to_event(json.dumps(record, separators=(",", ":")))
                    except Exception as e:
                        missing_count += 1
                        missing_details.append(f"line {checked}: invalid record ({e})")
                checked += 1
    except (OSError, gzip.BadGzipFile) as e:
        return {"status": "FAIL", "reason": str(e), "checked": 0, "missing_fields": 0}

    status = "PASS" if missing_count == 0 else "FAIL"
    return {
        "status": status,
        "checked": checked,
        "missing_fields": missing_count,
        "details": missing_details[:10],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_health.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add sim/collector/health.py tests/sim/test_health.py
git commit -m "sim/collector: health checks — freshness + schema conformance"
```

---

### Task 3.2: Systemd Units

**Files:**
- Create: `systemd/sniper-sim-collector.service`
- Create: `systemd/sniper-sim-meta.service`

Note: Slice 3 health remains an on-demand script in Phase 1, not a long-running service.

- [ ] **Step 1: Write the service files**

```ini
# systemd/sniper-sim-collector.service
[Unit]
Description=Sniper Sim WS Collector (Polymarket)
After=network.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=/home/sniper/sniper
EnvironmentFile=-/home/sniper/sniper/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/sniper/sniper/.venv/bin/python -m sim.collector
Restart=always
RestartSec=10
MemoryMax=100M
MemorySwapMax=50M
RuntimeMaxSec=86400

[Install]
WantedBy=default.target
```

```ini
# systemd/sniper-sim-meta.service
[Unit]
Description=Sniper Sim Metadata Sync (Polymarket)
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/sniper/sniper
EnvironmentFile=-/home/sniper/sniper/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/sniper/sniper/.venv/bin/python -m sim.collector.meta
Restart=always
RestartSec=30
MemoryMax=80M

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Commit**

```bash
git add systemd/sniper-sim-collector.service systemd/sniper-sim-meta.service
git commit -m "sim/collector: systemd units for WS collector + metadata sync"
```

---

### Task 3.3: Collector Entry Point (`__main__`)

**Files:**
- Create: `sim/collector/__main__.py`

- [ ] **Step 1: Write the entry point**

```python
# sim/collector/__main__.py
"""Entry point: python -m sim.collector"""
import asyncio
import logging
import signal

from sim.collector.config import CollectorConfig
from sim.collector.ingest import Collector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("sim.collector")


async def main() -> None:
    config = CollectorConfig.from_env()
    collector = Collector(config)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, collector.stop)

    LOG.info("Starting collector (raw_dir=%s, max_markets=%d)", config.raw_dir, config.max_markets)
    await collector.run()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify it imports cleanly**

Run: `python3 -c "from sim.collector.__main__ import main; print('OK')"`
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add sim/collector/__main__.py
git commit -m "sim/collector: __main__ entry point for systemd ExecStart"
```

---

## Exit Checks

### Phase 1 Exit Gate Verification

After all slices are implemented, verify:

- [ ] `python3 -m pytest tests/sim/ -v` — all tests pass
- [ ] `python3 -c "from sim.collector.ingest import Collector; print('import OK')"` — collector importable
- [ ] `python3 -c "from sim.collector.meta import filter_and_classify; print('import OK')"` — metadata importable
- [ ] `python3 -c "from sim.collector.health import check_freshness, check_schema_conformance; print('import OK')"` — health importable
- [ ] Systemd unit files have correct paths and memory limits
- [ ] RSS budget: MemoryMax=100M for collector, 80M for metadata

**Full Phase 1 exit gate** (from spec): Collector runs 24/7 for one week on VPS. Gap rate < 5%. Recovery from restart within 60 seconds. All raw events pass schema conformance check. This is verified AFTER deployment, not during implementation.

Implementation must still provide before deployment:

- a metadata snapshot file that the collector can reload
- raw `market_metadata` events written via the same JSONL writer contract
- WS subscription with `custom_feature_enabled: true`
- `exchange_ts` populated from venue timestamps when present

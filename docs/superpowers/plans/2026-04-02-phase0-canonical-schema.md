# Phase 0: Canonical Schema Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Freeze the canonical event schema, venue adapter contract, and market universe policy as tested Python code that all downstream phases depend on.

**Architecture:** A new `sim/` package at the repo root contains the schema dataclasses, venue adapter protocol, market universe filter, and event serialization. It has zero dependencies on the existing Sniper codebase (`db.py`, `replay/`, etc.) — it is a standalone foundation. The existing `sim/market_type.py` classifier is reused as-is.

**Tech Stack:** Python 3.12+, dataclasses, pytest. No external dependencies.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `sim/__init__.py` | Package marker, re-exports public API |
| `sim/schema.py` | Canonical event dataclass + event type enum (outcomes are payload-only, no separate dataclass) |
| `sim/ids.py` | Market ID construction (`pm:{cid}`, `kalshi:{ticker}`) and parsing |
| `sim/venue.py` | Venue adapter protocol (abstract interface for any venue) |
| `sim/universe.py` | Market universe filter (applies policy from spec) |
| `sim/serialize.py` | Event → JSONL line + JSONL line → Event (round-trip) |
| `tests/sim/__init__.py` | Test package marker |
| `tests/sim/test_schema.py` | Schema construction, validation, edge cases |
| `tests/sim/test_ids.py` | ID construction and parsing |
| `tests/sim/test_universe.py` | Universe filter logic |
| `tests/sim/test_serialize.py` | Round-trip serialization |
| `tests/sim/test_classify_integration.py` | market_type classifier integration with schema |

---

### Task 1: Event Schema Dataclass

**Files:**
- Create: `sim/__init__.py`
- Create: `sim/schema.py`
- Create: `tests/sim/__init__.py`
- Create: `tests/sim/test_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_schema.py
from sim.schema import RawEvent, EventType


def test_raw_event_required_fields():
    evt = RawEvent(
        venue="polymarket",
        market_id="pm:0xabc123",
        venue_market_id="0xabc123",
        market_type="geopolitical",
        event_type=EventType.TRADE,
        capture_ts="2026-04-01T14:23:01.482Z",
        ingest_seq=1,
        payload={"asset_id": "123", "price": 0.65, "size": 100, "side": "buy"},
    )
    assert evt.venue == "polymarket"
    assert evt.event_type == EventType.TRADE
    assert evt.ingest_seq == 1
    assert evt.asset_id is None
    assert evt.outcome_index is None
    assert evt.exchange_ts is None
    assert evt.venue_seq is None


def test_raw_event_optional_fields():
    evt = RawEvent(
        venue="polymarket",
        market_id="pm:0xabc123",
        venue_market_id="0xabc123",
        market_type="geopolitical",
        event_type=EventType.BOOK_SNAPSHOT,
        capture_ts="2026-04-01T14:23:01.482Z",
        ingest_seq=2,
        payload={"bids": [], "asks": []},
        asset_id="36272374199536",
        outcome_index=0,
        exchange_ts="2026-04-01T14:23:01.100Z",
        venue_seq=42,
    )
    assert evt.asset_id == "36272374199536"
    assert evt.outcome_index == 0
    assert evt.venue_seq == 42


def test_event_type_enum():
    assert EventType.BOOK_SNAPSHOT.value == "book_snapshot"
    assert EventType.BOOK_DELTA.value == "book_delta"
    assert EventType.TRADE.value == "trade"
    assert EventType.BEST_BBA.value == "best_bba"
    assert EventType.MARKET_METADATA.value == "market_metadata"
    assert EventType.MARKET_RESOLVED.value == "market_resolved"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sim.schema'`

- [ ] **Step 3: Write minimal implementation**

```python
# sim/__init__.py
"""Simulation platform — canonical schema and venue adapters."""

# sim/schema.py
"""Canonical event schema for the simulation platform."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class EventType(str, Enum):
    BOOK_SNAPSHOT = "book_snapshot"
    BOOK_DELTA = "book_delta"
    TRADE = "trade"
    BEST_BBA = "best_bba"
    MARKET_METADATA = "market_metadata"
    MARKET_RESOLVED = "market_resolved"


@dataclass(slots=True)
class RawEvent:
    """A single raw event from any venue.

    Raw events are stored close to venue-native shape.
    The compactor normalizes them into market-wide views.
    """

    venue: str
    market_id: str
    venue_market_id: str
    market_type: str
    event_type: EventType
    capture_ts: str
    ingest_seq: int
    payload: dict[str, Any]
    asset_id: Optional[str] = None
    outcome_index: Optional[int] = None
    exchange_ts: Optional[str] = None
    venue_seq: Optional[int] = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_schema.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add sim/ tests/sim/
git commit -m "sim: canonical event schema — RawEvent dataclass + EventType enum"
```

---

### Task 2: Market ID Construction

**Files:**
- Create: `sim/ids.py`
- Create: `tests/sim/test_ids.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_ids.py
from sim.ids import make_market_id, parse_market_id


def test_polymarket_id():
    mid = make_market_id("polymarket", "0xabc123def456")
    assert mid == "pm:0xabc123def456"


def test_kalshi_id():
    mid = make_market_id("kalshi", "KXBTCD-26JAN2017-T100749.99")
    assert mid == "kalshi:KXBTCD-26JAN2017-T100749.99"


def test_parse_polymarket_id():
    venue, raw_id = parse_market_id("pm:0xabc123def456")
    assert venue == "polymarket"
    assert raw_id == "0xabc123def456"


def test_parse_kalshi_id():
    venue, raw_id = parse_market_id("kalshi:KXBTCD-26JAN2017-T100749.99")
    assert venue == "kalshi"
    assert raw_id == "KXBTCD-26JAN2017-T100749.99"


def test_unknown_venue_raises():
    import pytest
    with pytest.raises(ValueError, match="Unknown venue"):
        make_market_id("binance", "BTCUSDT")


def test_parse_invalid_raises():
    import pytest
    with pytest.raises(ValueError, match="Invalid market_id"):
        parse_market_id("nocolon")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_ids.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# sim/ids.py
"""Canonical market ID construction and parsing."""
from __future__ import annotations

_VENUE_PREFIXES = {
    "polymarket": "pm",
    "kalshi": "kalshi",
}

_PREFIX_TO_VENUE = {v: k for k, v in _VENUE_PREFIXES.items()}


def make_market_id(venue: str, venue_native_id: str) -> str:
    """Build a canonical market_id from venue + native ID."""
    prefix = _VENUE_PREFIXES.get(venue)
    if prefix is None:
        raise ValueError(f"Unknown venue: {venue}")
    return f"{prefix}:{venue_native_id}"


def parse_market_id(market_id: str) -> tuple[str, str]:
    """Parse a canonical market_id into (venue, native_id)."""
    if ":" not in market_id:
        raise ValueError(f"Invalid market_id format (no colon): {market_id}")
    prefix, native_id = market_id.split(":", 1)
    venue = _PREFIX_TO_VENUE.get(prefix)
    if venue is None:
        raise ValueError(f"Invalid market_id prefix: {prefix}")
    return venue, native_id
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_ids.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add sim/ids.py tests/sim/test_ids.py
git commit -m "sim: market ID construction — pm:{cid} and kalshi:{ticker}"
```

---

### Task 3: Venue Adapter Protocol

**Files:**
- Create: `sim/venue.py`
- Modify: `tests/sim/test_schema.py` (add protocol conformance test)

- [ ] **Step 1: Write the failing test**

```python
# append to tests/sim/test_schema.py
from sim.venue import VenueAdapter
from sim.schema import RawEvent, EventType


def test_venue_adapter_is_runtime_checkable_protocol():
    """VenueAdapter must be a runtime-checkable Protocol."""
    assert isinstance(VenueAdapter, type)
    # Verify it's actually a Protocol by checking isinstance works
    class NotAnAdapter:
        pass
    assert not isinstance(NotAnAdapter(), VenueAdapter)


class FakeAdapter:
    """Minimal conformance test — implements all required methods."""
    venue_name = "fake"

    def make_market_id(self, venue_native_id: str) -> str:
        return f"fake:{venue_native_id}"

    def classify_market(self, identifier: str) -> str:
        return "other"

    def normalize_price(self, raw_price: float) -> float:
        return raw_price

    def parse_event(self, channel: str, raw_msg: dict) -> RawEvent | list[RawEvent]:
        return RawEvent(
            venue="fake", market_id="fake:test", venue_market_id="test",
            market_type="other", event_type=EventType.TRADE,
            capture_ts="2026-01-01T00:00:00.000Z", ingest_seq=0,
            payload=raw_msg,
        )


def test_fake_adapter_conforms_to_protocol():
    adapter = FakeAdapter()
    assert isinstance(adapter, VenueAdapter)
    assert adapter.venue_name == "fake"
    assert adapter.make_market_id("abc") == "fake:abc"
    assert adapter.classify_market("something") == "other"
    assert adapter.normalize_price(0.65) == 0.65
    result = adapter.parse_event("trade", {"price": 0.5})
    assert isinstance(result, RawEvent)


def test_incomplete_adapter_does_not_conform():
    class MissingMethods:
        venue_name = "broken"
    assert not isinstance(MissingMethods(), VenueAdapter)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sim.venue'`

- [ ] **Step 3: Write minimal implementation**

```python
# sim/venue.py
"""Venue adapter protocol — interface for any prediction market venue."""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from sim.schema import RawEvent


@runtime_checkable
class VenueAdapter(Protocol):
    """Interface that venue-specific adapters must implement.

    Each venue adapter knows how to:
    1. Construct canonical market IDs from venue-native IDs.
    2. Classify market type from venue-native identifiers.
    3. Normalize prices to 0.0-1.0 probability space.
    4. Parse raw WS/API messages into canonical RawEvent(s).
    """

    venue_name: str

    def make_market_id(self, venue_native_id: str) -> str:
        """Build a canonical market_id from a venue-native ID."""
        ...

    def classify_market(self, identifier: str) -> str:
        """Return canonical market_type from venue-native identifier."""
        ...

    def normalize_price(self, raw_price: float) -> float:
        """Convert venue-native price to 0.0-1.0 probability space."""
        ...

    def parse_event(self, channel: str, raw_msg: dict[str, Any]) -> RawEvent | list[RawEvent]:
        """Parse a raw WS/API message into one or more canonical RawEvents."""
        ...
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_schema.py -v`
Expected: 6 passed (3 original + 3 new)

- [ ] **Step 5: Commit**

```bash
git add sim/venue.py tests/sim/test_schema.py
git commit -m "sim: venue adapter protocol — runtime-checkable interface"
```

---

### Task 4: Market Universe Filter

**Files:**
- Create: `sim/universe.py`
- Create: `tests/sim/test_universe.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_universe.py
from sim.universe import UniverseFilter, MarketCandidate


def _make_candidate(**overrides) -> MarketCandidate:
    defaults = dict(
        condition_id="0xabc",
        question="US strikes Iran by March 31?",
        outcomes=["Yes", "No"],
        volume=50000.0,
        end_date="2026-06-01T00:00:00Z",
        active=True,
        closed=False,
    )
    defaults.update(overrides)
    return MarketCandidate(**defaults)


def test_accepts_qualifying_market():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate()) is True


def test_rejects_non_binary():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(outcomes=["A", "B", "C"])) is False


def test_rejects_low_volume():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(volume=5000.0)) is False


def test_rejects_closed():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(closed=True)) is False


def test_rejects_inactive():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(active=False)) is False


def test_rejects_far_end_date():
    f = UniverseFilter()
    from datetime import datetime, timezone, timedelta
    far_future = (datetime.now(timezone.utc) + timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f.qualifies(_make_candidate(end_date=far_future)) is False


def test_accepts_near_end_date():
    f = UniverseFilter()
    from datetime import datetime, timezone, timedelta
    near_future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f.qualifies(_make_candidate(end_date=near_future)) is True


def test_cap_enforcement():
    f = UniverseFilter(max_active=3)
    candidates = [_make_candidate(condition_id=f"0x{i}") for i in range(5)]
    accepted = [c for c in candidates if f.qualifies(c)]
    # All 5 qualify individually, but cap is 3
    assert len(accepted) <= 5  # qualifies() doesn't enforce cap
    # Cap is enforced by apply_cap()
    capped = f.apply_cap(accepted)
    assert len(capped) == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_universe.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# sim/universe.py
"""Market universe filter — applies collection scope policy."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional


@dataclass(slots=True)
class MarketCandidate:
    """A market being evaluated for collection."""
    condition_id: str
    question: str
    outcomes: list[str]
    volume: float
    end_date: str
    active: bool
    closed: bool


@dataclass
class UniverseFilter:
    """Applies the market universe policy from the spec."""

    min_volume: float = 10_000.0
    max_days_to_end: int = 90
    max_active: int = 200
    required_outcomes: tuple[str, ...] = ("Yes", "No")

    def qualifies(self, candidate: MarketCandidate, now: datetime | None = None) -> bool:
        """Check if a single market passes all filters."""
        if candidate.closed:
            return False
        if not candidate.active:
            return False
        if sorted(candidate.outcomes) != sorted(self.required_outcomes):
            return False
        if candidate.volume < self.min_volume:
            return False
        # 90-day horizon check
        try:
            end = datetime.fromisoformat(candidate.end_date.replace("Z", "+00:00"))
            ref = now or datetime.now(timezone.utc)
            if (end - ref).days > self.max_days_to_end:
                return False
        except (ValueError, TypeError):
            return False  # unparseable end_date → reject
        return True

    def apply_cap(self, candidates: list[MarketCandidate]) -> list[MarketCandidate]:
        """Enforce max_active cap. Keeps highest volume markets."""
        sorted_by_vol = sorted(candidates, key=lambda c: c.volume, reverse=True)
        return sorted_by_vol[: self.max_active]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_universe.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add sim/universe.py tests/sim/test_universe.py
git commit -m "sim: market universe filter — binary YES/NO, volume, 90-day horizon, cap"
```

---

### Task 5: Event Serialization (Round-Trip)

**Files:**
- Create: `sim/serialize.py`
- Create: `tests/sim/test_serialize.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/sim/test_serialize.py
import json
from sim.schema import RawEvent, EventType
from sim.serialize import event_to_jsonl, jsonl_to_event


def _sample_event() -> RawEvent:
    return RawEvent(
        venue="polymarket",
        market_id="pm:0xabc123",
        venue_market_id="0xabc123",
        market_type="geopolitical",
        event_type=EventType.TRADE,
        capture_ts="2026-04-01T14:23:01.482Z",
        ingest_seq=42,
        payload={"asset_id": "123", "price": 0.65, "size": 100.5, "side": "buy"},
        asset_id="123",
        outcome_index=0,
        exchange_ts="2026-04-01T14:23:01.100Z",
    )


def test_round_trip():
    original = _sample_event()
    line = event_to_jsonl(original)
    restored = jsonl_to_event(line)
    assert restored.venue == original.venue
    assert restored.market_id == original.market_id
    assert restored.event_type == original.event_type
    assert restored.capture_ts == original.capture_ts
    assert restored.ingest_seq == original.ingest_seq
    assert restored.asset_id == original.asset_id
    assert restored.outcome_index == original.outcome_index
    assert restored.exchange_ts == original.exchange_ts
    assert restored.venue_seq == original.venue_seq  # None
    assert restored.payload == original.payload


def test_jsonl_is_single_line():
    line = event_to_jsonl(_sample_event())
    assert "\n" not in line


def test_jsonl_is_valid_json():
    line = event_to_jsonl(_sample_event())
    parsed = json.loads(line)
    assert parsed["venue"] == "polymarket"
    assert parsed["event_type"] == "trade"
    assert parsed["ingest_seq"] == 42


def test_nullable_fields_omitted_when_none():
    evt = RawEvent(
        venue="kalshi",
        market_id="kalshi:KXBTC-26MAR31-B85000",
        venue_market_id="KXBTC-26MAR31-B85000",
        market_type="crypto_threshold",
        event_type=EventType.MARKET_METADATA,
        capture_ts="2026-04-01T14:23:01.482Z",
        ingest_seq=1,
        payload={"title": "BTC above 85K"},
    )
    line = event_to_jsonl(evt)
    parsed = json.loads(line)
    assert "asset_id" not in parsed
    assert "exchange_ts" not in parsed
    assert "venue_seq" not in parsed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/sim/test_serialize.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# sim/serialize.py
"""Event serialization — JSONL round-trip for raw events."""
from __future__ import annotations

import json
from typing import Any

from sim.schema import RawEvent, EventType


def event_to_jsonl(event: RawEvent) -> str:
    """Serialize a RawEvent to a single JSONL line (no trailing newline)."""
    d: dict[str, Any] = {
        "venue": event.venue,
        "market_id": event.market_id,
        "venue_market_id": event.venue_market_id,
        "market_type": event.market_type,
        "event_type": event.event_type.value,
        "capture_ts": event.capture_ts,
        "ingest_seq": event.ingest_seq,
        "payload": event.payload,
    }
    if event.asset_id is not None:
        d["asset_id"] = event.asset_id
    if event.outcome_index is not None:
        d["outcome_index"] = event.outcome_index
    if event.exchange_ts is not None:
        d["exchange_ts"] = event.exchange_ts
    if event.venue_seq is not None:
        d["venue_seq"] = event.venue_seq
    return json.dumps(d, separators=(",", ":"))


def jsonl_to_event(line: str) -> RawEvent:
    """Deserialize a JSONL line into a RawEvent."""
    d = json.loads(line)
    return RawEvent(
        venue=d["venue"],
        market_id=d["market_id"],
        venue_market_id=d["venue_market_id"],
        market_type=d["market_type"],
        event_type=EventType(d["event_type"]),
        capture_ts=d["capture_ts"],
        ingest_seq=d["ingest_seq"],
        payload=d["payload"],
        asset_id=d.get("asset_id"),
        outcome_index=d.get("outcome_index"),
        exchange_ts=d.get("exchange_ts"),
        venue_seq=d.get("venue_seq"),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/sim/test_serialize.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add sim/serialize.py tests/sim/test_serialize.py
git commit -m "sim: event serialization — JSONL round-trip with nullable field handling"
```

---

### Task 6: Classifier Integration Test

**Files:**
- Create: `tests/sim/test_classify_integration.py`

- [ ] **Step 1: Write the test**

```python
# tests/sim/test_classify_integration.py
"""Verify sim/market_type.py integrates with sim schema."""
from sim.schema import RawEvent, EventType
from sim.ids import make_market_id
from sim.market_type import classify


def test_polymarket_event_gets_classified():
    question = "US strikes Iran by February 28?"
    market_type = classify("polymarket", question)
    evt = RawEvent(
        venue="polymarket",
        market_id=make_market_id("polymarket", "0xabc123"),
        venue_market_id="0xabc123",
        market_type=market_type,
        event_type=EventType.MARKET_METADATA,
        capture_ts="2026-04-01T14:23:01.482Z",
        ingest_seq=1,
        payload={"question": question},
    )
    assert evt.market_type == "geopolitical"
    assert evt.market_id == "pm:0xabc123"


def test_kalshi_event_gets_classified():
    ticker = "KXNFLGAME-26JAN11BUFJAC-BUF"
    market_type = classify("kalshi", ticker)
    evt = RawEvent(
        venue="kalshi",
        market_id=make_market_id("kalshi", ticker),
        venue_market_id=ticker,
        market_type=market_type,
        event_type=EventType.TRADE,
        capture_ts="2026-04-01T14:23:01.482Z",
        ingest_seq=1,
        payload={"ticker": ticker, "price_cents": 75, "count": 10},
    )
    assert evt.market_type == "sports_nfl"
    assert evt.market_id == f"kalshi:{ticker}"


def test_all_event_types_constructable():
    """Every EventType value can be used in a RawEvent."""
    for et in EventType:
        evt = RawEvent(
            venue="polymarket",
            market_id="pm:test",
            venue_market_id="test",
            market_type="other",
            event_type=et,
            capture_ts="2026-04-01T00:00:00.000Z",
            ingest_seq=0,
            payload={},
        )
        assert evt.event_type == et
```

- [ ] **Step 2: Run full test suite**

Run: `python3 -m pytest tests/sim/ -v`
Expected: All tests pass (schema + ids + universe + serialize + integration)

- [ ] **Step 3: Commit**

```bash
git add tests/sim/test_classify_integration.py
git commit -m "sim: classifier integration test — market_type flows through schema"
```

---

### Task 7: Package Exports + Phase 0 Exit Verification

**Files:**
- Modify: `sim/__init__.py`

- [ ] **Step 1: Update package exports**

```python
# sim/__init__.py
"""Simulation platform — canonical schema and venue adapters."""
from sim.schema import RawEvent, EventType
from sim.ids import make_market_id, parse_market_id
from sim.venue import VenueAdapter
from sim.universe import UniverseFilter, MarketCandidate
from sim.serialize import event_to_jsonl, jsonl_to_event

__all__ = [
    "RawEvent",
    "EventType",
    "make_market_id",
    "parse_market_id",
    "VenueAdapter",
    "UniverseFilter",
    "MarketCandidate",
    "event_to_jsonl",
    "jsonl_to_event",
]
```

- [ ] **Step 2: Run full test suite including existing Sniper tests**

Run: `python3 -m pytest tests/sim/ -v`
Expected: All sim tests pass.

Then separately verify existing tests are unaffected:
Run: `python3 -m pytest tests/ -q --ignore=tests/sim; echo "EXIT: $?"`
Expected: Existing tests pass. Exit code 0.

- [ ] **Step 3: Verify Phase 0 exit criteria**

```
Phase 0 Exit Gate Checklist:
[x] Schema frozen — RawEvent dataclass with all spec fields
[x] Venue adapter contract frozen — VenueAdapter protocol
[x] Classifier tested — 20/20 in sim/market_type.py + integration test
[x] Market universe policy — UniverseFilter with spec thresholds
[x] Serialization round-trip — event_to_jsonl ↔ jsonl_to_event
[x] IDs canonical — pm:{cid}, kalshi:{ticker}
```

- [ ] **Step 4: Commit**

```bash
git add sim/__init__.py
git commit -m "sim: Phase 0 complete — schema frozen, adapter contract frozen, classifier tested"
```

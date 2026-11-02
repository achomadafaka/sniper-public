# Phase 3A: WS Followed-Wallet Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable real-time detection of followed-wallet trades on the Polymarket WS stream, writing to `followed_wallet_activity` with synthetic tx hashes, then reconciling with real tx hashes when sync fetches them later.

**Architecture:** Synthetic `ws:<sha1(...)>` hashes using raw millisecond timestamps for WS-detected trades. DB layer owns reconciliation: `executemany` batch for WS rows, per-row read-modify-write for real-hash rows. Strict matching (same wallet/market/outcome/side/type, +/-2s, price/size at 6 decimals).

**Tech Stack:** SQLite, Python 3.13, hashlib (sha1), existing `db_utils.py`/`db_followed.py` helpers.

**Spec:** `docs/superpowers/specs/2026-03-27-copy-infrastructure-phase3-design.md`

---

## File Map

| File | Responsibility |
|------|---------------|
| `db_utils.py` | `generate_ws_synthetic_hash()`, `is_synthetic_ws_hash()` — pure hash helpers |
| `db_followed.py` | `_reconcile_real_hash_row()` — per-row reconciliation logic, called from `upsert_followed_wallet_activity()` |
| `db.py` | `idx_fwa_reconcile` index in canonical schema |
| `tests/test_ws_reconciliation.py` | All 11 test cases from spec |

---

### Task 1: Synthetic Hash Helpers in db_utils.py

**Files:**
- Modify: `db_utils.py` (after `_followed_wallet_activity_key`, ~line 110)
- Test: `tests/test_ws_reconciliation.py`

- [ ] **Step 1: Write failing tests for hash helpers**

```python
# tests/test_ws_reconciliation.py
import os, sys, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db_utils import generate_ws_synthetic_hash, is_synthetic_ws_hash

class TestSyntheticHash:
    def test_format_starts_with_ws_prefix(self):
        h = generate_ws_synthetic_hash("0xabc", "cond1", "YES", "BUY", "BUY", 1700000000123, 0.45, 100.0)
        assert h.startswith("ws:")
        assert len(h) == 3 + 40  # "ws:" + 40-char sha1 hex

    def test_deterministic(self):
        args = ("0xabc", "cond1", "YES", "BUY", "BUY", 1700000000123, 0.45, 100.0)
        assert generate_ws_synthetic_hash(*args) == generate_ws_synthetic_hash(*args)

    def test_millisecond_precision_matters(self):
        h1 = generate_ws_synthetic_hash("0xabc", "cond1", "YES", "BUY", "BUY", 1700000000000, 0.45, 100.0)
        h2 = generate_ws_synthetic_hash("0xabc", "cond1", "YES", "BUY", "BUY", 1700000000001, 0.45, 100.0)
        assert h1 != h2  # 1ms difference → different hash

    def test_is_synthetic_true(self):
        assert is_synthetic_ws_hash("ws:abc123") is True

    def test_is_synthetic_false_real_hash(self):
        assert is_synthetic_ws_hash("0xdeadbeef") is False

    def test_is_synthetic_false_empty(self):
        assert is_synthetic_ws_hash("") is False
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `python3 -m pytest tests/test_ws_reconciliation.py::TestSyntheticHash -v`
Expected: FAIL — `ImportError: cannot import name 'generate_ws_synthetic_hash'`

- [ ] **Step 3: Implement hash helpers**

In `db_utils.py` after `_followed_wallet_activity_key` (~line 110):

```python
def generate_ws_synthetic_hash(
    wallet: str,
    condition_id: str,
    outcome: str,
    side: str,
    activity_type: str,
    raw_timestamp_ms: int,
    price: float,
    size: float,
) -> str:
    """Generate a synthetic tx hash for WS-detected followed-wallet trades.

    Uses raw millisecond timestamp (NOT second-resolution activity_ts)
    to prevent collisions in burst trades. The result is passed as
    transaction_hash to _followed_wallet_activity_key() for the final
    activity_key computation.
    """
    base = "|".join((
        _normalize_wallet(wallet),
        _normalize_condition_id(condition_id),
        _normalize_outcome(outcome),
        str(side or "").strip().upper(),
        str(activity_type or "").strip().upper(),
        str(int(raw_timestamp_ms)),
        f"{float(price):.10f}",
        f"{float(size):.10f}",
    ))
    return "ws:" + hashlib.sha1(base.encode("utf-8")).hexdigest()


def is_synthetic_ws_hash(tx_hash: str) -> bool:
    """Check if a transaction_hash is a synthetic WS hash."""
    return isinstance(tx_hash, str) and tx_hash.startswith("ws:")
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `python3 -m pytest tests/test_ws_reconciliation.py::TestSyntheticHash -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add db_utils.py tests/test_ws_reconciliation.py
git commit -m "feat(phase3a): synthetic WS hash helpers in db_utils.py"
```

---

### Task 2: Reconciliation Index in db.py

**Files:**
- Modify: `db.py` (~line 1553, after existing `followed_wallet_activity` indexes)

- [ ] **Step 1: Add the reconciliation index**

In `db.py`, after the existing `idx_followed_wallet_activity_condition_ts` index creation:

```python
conn.execute("""
    CREATE INDEX IF NOT EXISTS idx_fwa_reconcile
    ON followed_wallet_activity(wallet, condition_id, outcome, activity_type, side, activity_ts)
""")
```

- [ ] **Step 2: Verify index creates on fresh DB**

Run: `python3 -c "import sqlite3; from db import init_db, get_connection; c = get_connection(':memory:'); init_db(c); print([r[1] for r in c.execute('PRAGMA index_list(followed_wallet_activity)').fetchall()])"`
Expected: list includes `idx_fwa_reconcile`

- [ ] **Step 3: Commit**

```bash
git add db.py
git commit -m "feat(phase3a): add reconciliation index idx_fwa_reconcile"
```

---

### Task 3: Reconciliation Logic in db_followed.py

**Files:**
- Modify: `db_followed.py` (modify `upsert_followed_wallet_activity`, add `_reconcile_real_hash_row`)
- Test: `tests/test_ws_reconciliation.py`

- [ ] **Step 1: Write failing tests for reconciliation**

Add to `tests/test_ws_reconciliation.py`:

```python
from db_utils import generate_ws_synthetic_hash, is_synthetic_ws_hash, _normalize_wallet
from db_followed import upsert_followed_wallet_activity
from db import init_db

def _get_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn

def _ws_activity(wallet, condition_id, outcome, side, price, size, ts_ms):
    """Build a WS-style activity dict with synthetic hash."""
    return {
        "transactionHash": generate_ws_synthetic_hash(
            wallet, condition_id, outcome, side, "BUY", ts_ms, price, size
        ),
        "conditionId": condition_id,
        "type": "BUY",
        "side": side,
        "outcome": outcome,
        "price": price,
        "size": size,
        "timestamp": ts_ms,
    }

def _real_activity(tx_hash, condition_id, outcome, side, price, size, ts_ms, **extra):
    """Build a REST/sync-style activity dict with real hash."""
    d = {
        "transactionHash": tx_hash,
        "conditionId": condition_id,
        "type": "BUY",
        "side": side,
        "outcome": outcome,
        "price": price,
        "size": size,
        "timestamp": ts_ms,
        "title": "Test Market",
        "slug": "test-market",
    }
    d.update(extra)
    return d

def _count_rows(conn, wallet):
    return conn.execute(
        "SELECT COUNT(*) FROM followed_wallet_activity WHERE wallet = ?",
        (_normalize_wallet(wallet),)
    ).fetchone()[0]

def _get_row(conn, wallet):
    return conn.execute(
        "SELECT * FROM followed_wallet_activity WHERE wallet = ? LIMIT 1",
        (_normalize_wallet(wallet),)
    ).fetchone()


class TestWsReconciliation:

    def test_ws_synthetic_inserts(self):
        """Test 1: WS synthetic row inserts with ws: tx hash."""
        conn = _get_conn()
        ws = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[ws], source_endpoint="ws-stream")
        assert _count_rows(conn, "0xabc") == 1
        row = _get_row(conn, "0xabc")
        assert is_synthetic_ws_hash(row["transaction_hash"])

    def test_real_hash_merges_synthetic(self):
        """Test 2: Later real-hash sync row merges, leaving exactly 1 row."""
        conn = _get_conn()
        ws = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[ws], source_endpoint="ws-stream")
        real = _real_activity("0xREAL123", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real], source_endpoint="activity")
        assert _count_rows(conn, "0xabc") == 1

    def test_merged_key_is_canonical(self):
        """Test 3: After merge, stored activity_key is canonical real-hash key."""
        conn = _get_conn()
        ws = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[ws], source_endpoint="ws-stream")
        real = _real_activity("0xREAL123", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real], source_endpoint="activity")
        row = _get_row(conn, "0xabc")
        assert not is_synthetic_ws_hash(row["transaction_hash"])
        assert row["transaction_hash"] == "0xreal123"  # normalized lowercase
        assert row["title"] == "Test Market"  # enriched from real row

    def test_real_hash_upsert_idempotent(self):
        """Test 4: Second real-hash upsert is idempotent."""
        conn = _get_conn()
        real = _real_activity("0xREAL123", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real])
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real])
        assert _count_rows(conn, "0xabc") == 1

    def test_no_match_inserts_new(self):
        """Test 5: No-match real-hash row inserts as new row."""
        conn = _get_conn()
        ws = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[ws], source_endpoint="ws-stream")
        # Different market — no match
        real = _real_activity("0xREAL456", "cond_DIFFERENT", "YES", "BUY", 0.60, 50.0, 1700000005000)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real])
        assert _count_rows(conn, "0xabc") == 2

    def test_ambiguous_no_auto_merge(self):
        """Test 6: Ambiguous synthetic candidates do not auto-merge."""
        conn = _get_conn()
        # Two synthetic rows with same market but different ms timestamps (same second)
        ws1 = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000100)
        ws2 = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000200)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[ws1, ws2], source_endpoint="ws-stream")
        assert _count_rows(conn, "0xabc") == 2
        # Real hash arrives — matches both on criteria (same second, same price/size)
        real = _real_activity("0xREAL789", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real])
        # Should have 3 rows: 2 synthetic + 1 new canonical (no merge)
        assert _count_rows(conn, "0xabc") == 3

    def test_existing_real_hash_unchanged(self):
        """Test 7: Existing pure real-hash behavior unchanged."""
        conn = _get_conn()
        real = _real_activity("0xREAL_A", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real])
        assert _count_rows(conn, "0xabc") == 1
        row = _get_row(conn, "0xabc")
        assert not is_synthetic_ws_hash(row["transaction_hash"])

    def test_ws_reconnect_dedup(self):
        """Test 8: WS reconnect duplicate does not create second row."""
        conn = _get_conn()
        ws = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[ws], source_endpoint="ws-stream")
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[ws], source_endpoint="ws-stream")
        assert _count_rows(conn, "0xabc") == 1

    def test_zero_price_rejected(self):
        """Test 9: Zero-price WS events rejected before insert."""
        conn = _get_conn()
        ws = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.0, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[ws], source_endpoint="ws-stream")
        assert _count_rows(conn, "0xabc") == 0

    def test_canonical_exists_cleans_one_synthetic(self):
        """Test 10: Canonical already exists + one synthetic → synthetic deleted."""
        conn = _get_conn()
        # Insert canonical first
        real = _real_activity("0xREAL_FIRST", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real])
        # Insert synthetic that matches
        ws = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000500)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[ws], source_endpoint="ws-stream")
        assert _count_rows(conn, "0xabc") == 2
        # Re-upsert canonical — should clean up the synthetic
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real])
        assert _count_rows(conn, "0xabc") == 1
        row = _get_row(conn, "0xabc")
        assert not is_synthetic_ws_hash(row["transaction_hash"])

    def test_canonical_exists_multiple_synthetics_no_delete(self):
        """Test 11: Canonical exists + multiple synthetics → none deleted, warning logged."""
        conn = _get_conn()
        real = _real_activity("0xREAL_FIRST", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real])
        ws1 = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000100)
        ws2 = _ws_activity("0xabc", "cond1", "YES", "BUY", 0.45, 100.0, 1700000000200)
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[ws1, ws2], source_endpoint="ws-stream")
        assert _count_rows(conn, "0xabc") == 3
        # Re-upsert canonical — ambiguous synthetics, should NOT delete
        upsert_followed_wallet_activity(conn, wallet="0xabc", activities=[real])
        assert _count_rows(conn, "0xabc") == 3  # all 3 remain
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `python3 -m pytest tests/test_ws_reconciliation.py::TestWsReconciliation -v`
Expected: Most tests FAIL (reconciliation logic not implemented yet; test_ws_synthetic_inserts may pass if synthetic hash is just treated as a normal tx hash by existing code)

- [ ] **Step 3: Implement reconciliation logic**

In `db_followed.py`, add a private reconciliation function and modify `upsert_followed_wallet_activity`:

**Add `_reconcile_real_hash_row()`** (new function before `upsert_followed_wallet_activity`):

```python
def _reconcile_real_hash_row(
    conn: sqlite3.Connection,
    wallet_key: str,
    canonical_activity_key: str,
    row_tuple: tuple,
    incoming: dict,
) -> bool:
    """Attempt to reconcile a real-hash row against existing synthetic WS rows.

    Returns True if reconciliation handled the row (merged or inserted).
    Returns False if the caller should fall through to normal insert.
    """
    # Unpack incoming fields for matching
    tx_hash = row_tuple[2]       # transaction_hash
    condition_id = row_tuple[3]  # condition_id
    activity_type = row_tuple[4] # activity_type
    side = row_tuple[5]          # side
    outcome = row_tuple[6]       # outcome
    price = row_tuple[10]        # price
    size = row_tuple[11]         # size
    usdc_size = row_tuple[12]    # usdc_size
    activity_ts = row_tuple[13]  # activity_ts

    # Step 2: Check if canonical row already exists
    existing = conn.execute(
        "SELECT activity_key FROM followed_wallet_activity WHERE activity_key = ?",
        (canonical_activity_key,),
    ).fetchone()

    if existing:
        # Canonical row exists — upsert into it (idempotent)
        # Then clean up matching synthetic rows
        _cleanup_synthetic_duplicates(
            conn, wallet_key, condition_id, outcome, side,
            activity_type, activity_ts, price, size, usdc_size,
        )
        return False  # let normal upsert handle the canonical row

    # Step 3: Search for reconcilable synthetic candidate
    candidates = conn.execute(
        """
        SELECT activity_key, activity_ts, price, size, usdc_size
        FROM followed_wallet_activity
        WHERE wallet = ?
          AND condition_id = ?
          AND outcome = ?
          AND activity_type = ?
          AND side = ?
          AND activity_ts BETWEEN ? AND ?
          AND transaction_hash LIKE 'ws:%'
        """,
        (wallet_key, condition_id, outcome.upper(), activity_type, side,
         activity_ts - 2, activity_ts + 2),
    ).fetchall()

    # Filter by price/size match
    matched = []
    for c in candidates:
        if round(float(c["price"]), 6) != round(price, 6):
            continue
        if round(float(c["size"]), 6) != round(size, 6):
            continue
        c_usdc = float(c["usdc_size"] or 0)
        if c_usdc > 0 and usdc_size > 0:
            if round(c_usdc, 4) != round(usdc_size, 4):
                continue
        matched.append(c)

    if len(matched) == 1:
        # Step 4: Exactly one match — update in place
        conn.execute(
            """
            UPDATE followed_wallet_activity SET
                activity_key = ?,
                transaction_hash = ?,
                title = ?,
                slug = ?,
                event_slug = ?,
                price = ?,
                size = ?,
                usdc_size = ?,
                activity_ts = ?,
                source_endpoint = ?,
                raw_json = ?,
                inserted_at = CURRENT_TIMESTAMP
            WHERE activity_key = ?
            """,
            (
                canonical_activity_key,
                tx_hash,
                row_tuple[7],   # title
                row_tuple[8],   # slug
                row_tuple[9],   # event_slug
                price, size, usdc_size, activity_ts,
                row_tuple[14],  # source_endpoint
                row_tuple[15],  # raw_json
                matched[0]["activity_key"],  # WHERE old synthetic key
            ),
        )
        return True  # reconciled — skip normal insert

    if len(matched) > 1:
        # Step 5: Ambiguous — insert canonical row, don't merge
        logger.warning(
            "ambiguous_followed_wallet_activity_reconcile: %d synthetic candidates "
            "for wallet=%s cond=%s outcome=%s — inserting canonical row without merge",
            len(matched), wallet_key[:16], condition_id[:16], outcome,
        )

    # Step 6 (or ambiguous): fall through to normal insert
    return False


def _cleanup_synthetic_duplicates(
    conn, wallet_key, condition_id, outcome, side,
    activity_type, activity_ts, price, size, usdc_size,
):
    """After canonical upsert, delete matching synthetic duplicates (Step 2)."""
    candidates = conn.execute(
        """
        SELECT activity_key, price, size, usdc_size
        FROM followed_wallet_activity
        WHERE wallet = ?
          AND condition_id = ?
          AND outcome = ?
          AND activity_type = ?
          AND side = ?
          AND activity_ts BETWEEN ? AND ?
          AND transaction_hash LIKE 'ws:%'
        """,
        (wallet_key, condition_id, outcome.upper(), activity_type, side,
         activity_ts - 2, activity_ts + 2),
    ).fetchall()

    matched = []
    for c in candidates:
        if round(float(c["price"]), 6) != round(price, 6):
            continue
        if round(float(c["size"]), 6) != round(size, 6):
            continue
        c_usdc = float(c["usdc_size"] or 0)
        if c_usdc > 0 and usdc_size > 0:
            if round(c_usdc, 4) != round(usdc_size, 4):
                continue
        matched.append(c)

    if len(matched) == 1:
        conn.execute(
            "DELETE FROM followed_wallet_activity WHERE activity_key = ?",
            (matched[0]["activity_key"],),
        )
    elif len(matched) > 1:
        logger.warning(
            "ambiguous_followed_wallet_activity_reconcile_cleanup: %d synthetic "
            "candidates for wallet=%s cond=%s — leaving all",
            len(matched), wallet_key[:16], condition_id[:16],
        )
```

**Modify `upsert_followed_wallet_activity()`** — split into two paths:

```python
def upsert_followed_wallet_activity(conn, *, wallet, activities, source_endpoint="activity"):
    wallet_key = _normalize_wallet(wallet)
    if not wallet_key:
        return 0

    ws_payload = []     # synthetic hashes — batch via executemany
    real_payload = []   # real hashes — per-row reconciliation

    for row in activities:
        # ... existing normalization (lines 796-827) ...

        # Reject zero-price/zero-size for WS rows
        if is_synthetic_ws_hash(transaction_hash) and (price <= 0 or size <= 0):
            continue

        row_tuple = (activity_key, wallet_key, transaction_hash, ...)

        if is_synthetic_ws_hash(transaction_hash):
            ws_payload.append(row_tuple)
        else:
            real_payload.append((row_tuple, row))  # keep original dict for reconciliation

    inserted = 0

    # Path 1: WS batch (fast, no reads)
    if ws_payload:
        conn.executemany("""INSERT INTO followed_wallet_activity ... ON CONFLICT ...""", ws_payload)
        inserted += len(ws_payload)

    # Path 2: Real-hash per-row reconciliation
    for row_tuple, original in real_payload:
        canonical_key = row_tuple[0]
        reconciled = _reconcile_real_hash_row(conn, wallet_key, canonical_key, row_tuple, original)
        if not reconciled:
            # Normal upsert for this single row
            conn.execute("""INSERT INTO followed_wallet_activity ... ON CONFLICT ...""", row_tuple)
        inserted += 1

    conn.commit()
    return inserted
```

- [ ] **Step 4: Run tests — verify they pass**

Run: `python3 -m pytest tests/test_ws_reconciliation.py -v`
Expected: All 11 + 6 = 17 tests PASS

- [ ] **Step 5: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 1850+ passed, 0 failed

- [ ] **Step 6: Commit**

```bash
git add db_followed.py tests/test_ws_reconciliation.py
git commit -m "feat(phase3a): WS→real-hash reconciliation in upsert_followed_wallet_activity"
```

---

### Task 4: Followed-Wallet Detection in stream_low_buys.py

**Files:**
- Modify: `stream_low_buys.py` (~line 641, in the event processing loop)

This task is the WS-side detection only. The streamer detects, normalizes, writes fast. DB layer owns reconciliation.

- [ ] **Step 1: Add followed_wallets loader**

In `LowPriceIngestor.__init__()`, add loading of followed wallets from `followed_wallets` table (not just `paper_trades`):

```python
def _load_followed_wallets(self) -> set:
    """Load wallets from followed_wallets table for real-time WS detection."""
    try:
        cur = self.conn.execute("SELECT DISTINCT wallet FROM followed_wallets")
        return {row[0].strip().lower() for row in cur.fetchall() if row[0]}
    except Exception:
        return set()
```

Add a refresh mechanism in the metadata refresh task (every 5 minutes).

- [ ] **Step 2: Add WS detection in event handler**

In the event processing loop (~line 641), BEFORE `_trigger_decision()`:

```python
# Phase 3A: Real-time followed-wallet detection
_event_wallet = str(event.get("wallet") or event.get("maker_address") or event.get("taker") or "").strip().lower()
if _event_wallet and _event_wallet in self._followed_wallets:
    try:
        from db_utils import generate_ws_synthetic_hash
        from db_followed import upsert_followed_wallet_activity
        _ws_hash = generate_ws_synthetic_hash(
            _event_wallet, event.get("condition_id", ""),
            event.get("outcome", "YES"), event.get("side", "BUY"), "BUY",
            int(event.get("timestamp_ms") or event.get("timestamp") or 0),
            float(event.get("price") or 0), float(event.get("size") or 0),
        )
        upsert_followed_wallet_activity(
            self.conn,
            wallet=_event_wallet,
            activities=[{
                "transactionHash": _ws_hash,
                "conditionId": event.get("condition_id", ""),
                "type": "BUY",
                "side": event.get("side", "BUY"),
                "outcome": event.get("outcome", ""),
                "price": event.get("price", 0),
                "size": event.get("size", 0),
                "timestamp": event.get("timestamp_ms") or event.get("timestamp") or 0,
            }],
            source_endpoint="ws-stream",
        )
    except Exception:
        logger.debug("followed-wallet WS activity write failed", exc_info=True)
```

- [ ] **Step 3: Verify no change to low_buy_trades path**

Confirm: the `_trigger_decision()` call and everything after it is unchanged. The followed-wallet detection is a separate code path that runs before it.

- [ ] **Step 4: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: All tests pass, no regressions

- [ ] **Step 5: Commit**

```bash
git add stream_low_buys.py
git commit -m "feat(phase3a): real-time followed-wallet detection in WS stream"
```

---

### Task 5: Final Verification & Commit

- [ ] **Step 1: Run all reconciliation tests**

Run: `python3 -m pytest tests/test_ws_reconciliation.py -v`
Expected: 17 PASS

- [ ] **Step 2: Run full test suite**

Run: `python3 -m pytest tests/ -q`
Expected: 1860+ passed, 0 failed

- [ ] **Step 3: Verify spec compliance**

Check against the 11 test cases in the spec. All should map to passing tests.

- [ ] **Step 4: Code review**

Use superpowers:requesting-code-review to review the implementation.

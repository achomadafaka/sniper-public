# Radar v5 Realistic-Fill Paper Adapter Implementation Plan

**Status:** Superseded by `2026-08-06-radar-owned-realistic-paper-lifecycle.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn each qualifying `pump-confirmation-truth-v5` decision into a conservative quote-modeled fill from a fresh executable Jupiter quote, handed to V6 through an append-only receipt ledger, without touching v5's own modeled clock.

**Architecture:** Two immutable Radar tables (`op_paper_fill_requests`, `op_paper_fills`) written by the Radar worker only. The request is appended in the same transaction as the v5 alert; the quote runs on the existing `OpportunitySolanaProbe` worker at lower priority; a terminal receipt is appended exactly once per request. V6 tails `op_paper_fills.receipt_seq` read-only and opens isolated mock positions on `filled` receipts, scoring both legs on the Jupiter slippage floor under a new `route-floor-v1` fill model.

**Tech Stack:** Python 3.13, SQLite (WAL, trigger-enforced immutability), pytest, Jupiter quote API via the existing probe.

**Spec:** `docs/superpowers/specs/2026-08-05-radar-v5-realistic-fill-paper-adapter-design.md`

## Global Constraints

- Activation is **default OFF** (`MEMECOIN_OPPORTUNITY_PAPER_ADAPTER=0`).
- Clip is **`$25` only** — `PRIMARY_CLIP_USD_MICROS = 25_000_000`.
- Fill model version is the literal string `route-floor-v1`. Adapter version is `radar-paper-adapter-v1`.
- Signal source is the literal string `radar_v5`.
- Both legs score `otherAmountThreshold`; `outAmount` is retained for diagnostics only. Never mix.
- The Radar worker never imports any `sniper.services.memecoin_v6_*` module.
- The Radar worker writes only the Radar database; the V6 worker opens the Radar database read-only.
- `op_paper_fill_requests` and `op_paper_fills` are **never** added to `_RETENTION_DELETE_TABLES`.
- `pending` and `expired` are derived states, never stored column values.
- No change to v5's predicate, barriers, evaluator, or scoring.
- Existing fomo-source V6 rows keep expected-amount scoring and are not re-normalized.

---

### Task 1: Radar schema — two immutable, retention-excluded tables

**Files:**
- Modify: `sniper/research/memecoin_opportunity/store.py` (`SCHEMA_VERSION`, `_SCHEMA`, `_migrate_schema`)
- Test: `tests/test_memecoin_opportunity_store.py`

**Interfaces:**
- Produces: tables `op_paper_fill_requests(request_id TEXT PRIMARY KEY, …)` and
  `op_paper_fills(receipt_seq INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE, …)`;
  `SCHEMA_VERSION == 11`.

- [ ] **Step 1: Write the failing tests**

```python
def test_paper_fill_tables_are_immutable_and_retention_excluded(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    conn.execute(
        "INSERT INTO op_paper_fill_requests (request_id,alert_id,launch_id,network_id,"
        "venue_id,token_id,creator_id,launch_mode,state_fingerprint,rule_version,"
        "evaluator_version,adapter_version,fill_model_version,clip_usd_micros,decision_at,"
        "decision_age_ms,intent_at,expires_at,local_qty_atomic,row_fingerprint,inserted_at) "
        "VALUES ('r1','a1','l1','solana','pumpfun_solana','t1','c1','standard','sf',"
        "'pump-confirmation-truth-v5','alert-outcome-v4','radar-paper-adapter-v1',"
        "'route-floor-v1',25000000,'2026-08-05T00:00:00Z',20000,'2026-08-05T00:00:00Z',"
        "'2026-08-05T00:00:30Z',123,'fp','2026-08-05T00:00:00Z')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE op_paper_fill_requests SET alert_id='a2' WHERE request_id='r1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM op_paper_fill_requests WHERE request_id='r1'")
    # retention authority must NOT unlock these tables
    with retention_delete_authority(conn):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM op_paper_fill_requests WHERE request_id='r1'")


def test_paper_fill_receipt_is_append_only_and_unique_per_request(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    def _receipt(request_id):
        conn.execute(
            "INSERT INTO op_paper_fills (request_id,status,receipt_at,row_fingerprint,inserted_at) "
            "VALUES (?,'filled','2026-08-05T00:00:01Z','fp','2026-08-05T00:00:01Z')",
            (request_id,),
        )
    _receipt("r1")
    with pytest.raises(sqlite3.IntegrityError):
        _receipt("r1")
    seq = conn.execute("SELECT receipt_seq FROM op_paper_fills WHERE request_id='r1'").fetchone()[0]
    assert seq >= 1
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE op_paper_fills SET status='entry_unfilled' WHERE request_id='r1'")


def test_paper_fill_tables_are_not_in_the_retention_delete_set():
    from sniper.research.memecoin_opportunity.store import _RETENTION_DELETE_TABLES
    assert "op_paper_fill_requests" not in _RETENTION_DELETE_TABLES
    assert "op_paper_fills" not in _RETENTION_DELETE_TABLES
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_memecoin_opportunity_store.py -q -k paper_fill`
Expected: FAIL — `no such table: op_paper_fill_requests`

- [ ] **Step 3: Implement**

Bump `SCHEMA_VERSION = 11`. Add both `CREATE TABLE` statements to `_SCHEMA` exactly as written in
the spec's schema section. Add unconditional immutability triggers for both tables (no
`op_retention_delete_allowed()` exemption — that is what excludes them from retention):

```sql
CREATE TRIGGER IF NOT EXISTS op_paper_fill_requests_no_update
BEFORE UPDATE ON op_paper_fill_requests
BEGIN SELECT RAISE(ABORT, 'op_paper_fill_requests immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_paper_fill_requests_no_delete
BEFORE DELETE ON op_paper_fill_requests
BEGIN SELECT RAISE(ABORT, 'op_paper_fill_requests immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_paper_fills_no_update
BEFORE UPDATE ON op_paper_fills
BEGIN SELECT RAISE(ABORT, 'op_paper_fills immutable'); END;
CREATE TRIGGER IF NOT EXISTS op_paper_fills_no_delete
BEFORE DELETE ON op_paper_fills
BEGIN SELECT RAISE(ABORT, 'op_paper_fills immutable'); END;
```

In `_migrate_schema`, accept version `10` in the allowed set and add both `CREATE TABLE IF NOT
EXISTS` + the four triggers before the `UPDATE op_schema_version` line.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_memecoin_opportunity_store.py -q -k paper_fill`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/store.py tests/test_memecoin_opportunity_store.py
git commit -m "feat(radar): immutable, retention-excluded paper fill request/receipt tables"
```

---

### Task 2: Request identity and derived states

**Files:**
- Create: `sniper/research/memecoin_opportunity/paper_adapter.py`
- Test: `tests/test_memecoin_opportunity_paper_adapter.py`

**Interfaces:**
- Produces:
  - `ADAPTER_VERSION = "radar-paper-adapter-v1"`, `FILL_MODEL_VERSION = "route-floor-v1"`,
    `SIGNAL_SOURCE = "radar_v5"`
  - `paper_request_id(*, alert_id: str, adapter_version: str, fill_model_version: str, clip_usd_micros: int) -> str`
  - `append_paper_fill_request(conn, request: PaperFillRequest) -> str`
  - `append_paper_fill_receipt(conn, *, request_id, status, unfilled_reason=None, economics=None, clocks=None, now_iso) -> int`
  - `expired_requests(conn, *, now_iso) -> tuple[sqlite3.Row, ...]`
  - `UNFILLED_REASONS: frozenset[str]`

- [ ] **Step 1: Write the failing tests**

```python
def test_request_id_is_deterministic_and_version_scoped():
    a = paper_request_id(alert_id="a1", adapter_version="radar-paper-adapter-v1",
                         fill_model_version="route-floor-v1", clip_usd_micros=25_000_000)
    b = paper_request_id(alert_id="a1", adapter_version="radar-paper-adapter-v1",
                         fill_model_version="route-floor-v1", clip_usd_micros=25_000_000)
    c = paper_request_id(alert_id="a1", adapter_version="radar-paper-adapter-v2",
                         fill_model_version="route-floor-v1", clip_usd_micros=25_000_000)
    assert a == b and a != c


def test_expired_requests_are_derived_not_stored(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_paper_fill_request(conn, _request(request_id="r1", expires_at="2026-08-05T00:00:30Z"))
    assert [row["request_id"] for row in expired_requests(conn, now_iso="2026-08-05T00:00:31Z")] == ["r1"]
    append_paper_fill_receipt(conn, request_id="r1", status="entry_unfilled",
                              unfilled_reason="capacity_expired", now_iso="2026-08-05T00:00:31Z")
    assert expired_requests(conn, now_iso="2026-08-05T00:01:00Z") == ()


def test_receipt_rejects_unknown_unfilled_reason(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_paper_fill_request(conn, _request(request_id="r1"))
    with pytest.raises(ValueError, match="unfilled reason"):
        append_paper_fill_receipt(conn, request_id="r1", status="entry_unfilled",
                                  unfilled_reason="because", now_iso="2026-08-05T00:00:31Z")


def test_filled_receipt_requires_both_expected_and_floor(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_paper_fill_request(conn, _request(request_id="r1"))
    with pytest.raises(ValueError, match="floor"):
        append_paper_fill_receipt(
            conn, request_id="r1", status="filled",
            economics={"expected_out_atomic": 100, "floor_out_atomic": None},
            now_iso="2026-08-05T00:00:31Z")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_memecoin_opportunity_paper_adapter.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

`paper_request_id` is `content_fingerprint({...})` over exactly those four keys.
`append_paper_fill_receipt` validates: `status in {"filled","entry_unfilled"}`;
`unfilled_reason` is `None` iff `filled` and otherwise a member of `UNFILLED_REASONS`;
a `filled` receipt requires both `expected_out_atomic` and `floor_out_atomic` to be positive ints.
`expired_requests` is a `LEFT JOIN op_paper_fills USING(request_id) WHERE receipt_seq IS NULL AND
expires_at <= :now`.

```python
UNFILLED_REASONS = frozenset({
    "capacity_expired", "quote_unavailable", "no_route", "malformed_quote",
    "stale_quote", "identity_invalid", "consumer_unavailable",
})
```

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_memecoin_opportunity_paper_adapter.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/paper_adapter.py tests/test_memecoin_opportunity_paper_adapter.py
git commit -m "feat(radar): deterministic paper request identity, derived pending/expired"
```

---

### Task 3: Settings — default OFF plus consumer heartbeat gate

**Files:**
- Modify: `sniper/settings/memecoin_opportunity.py`
- Test: `tests/test_memecoin_opportunity_settings.py`

**Interfaces:**
- Produces: `settings.paper_adapter_enabled: bool`, `settings.paper_adapter_expiry_s: float`,
  `settings.paper_adapter_consumer_heartbeat_path: str`,
  `settings.paper_adapter_consumer_max_age_s: float`

- [ ] **Step 1: Write the failing test**

```python
def test_paper_adapter_is_default_off():
    settings = load_memecoin_opportunity_settings(env={"MEMECOIN_OPPORTUNITY_CAPTURE": "1",
                                                       "MEMECOIN_OPPORTUNITY_SOLANA_RPC_URL": "https://x"})
    assert settings.paper_adapter_enabled is False


def test_paper_adapter_expiry_must_be_positive():
    with pytest.raises(ValueError, match="PAPER_ADAPTER_EXPIRY_S"):
        load_memecoin_opportunity_settings(env={
            "MEMECOIN_OPPORTUNITY_CAPTURE": "1",
            "MEMECOIN_OPPORTUNITY_SOLANA_RPC_URL": "https://x",
            "MEMECOIN_OPPORTUNITY_PAPER_ADAPTER": "1",
            "MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_EXPIRY_S": "0",
        })
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_memecoin_opportunity_settings.py -q -k paper_adapter`
Expected: FAIL — `AttributeError: paper_adapter_enabled`

- [ ] **Step 3: Implement**

Add the four fields to the settings dataclass and loader, defaulting
`MEMECOIN_OPPORTUNITY_PAPER_ADAPTER=0`, `_EXPIRY_S=30`, `_CONSUMER_MAX_AGE_S=120`,
`_CONSUMER_HEARTBEAT=data/memecoin_v6_paper.heartbeat`. Validate expiry and max-age are `> 0`
using the same guard style as `MEMECOIN_OPPORTUNITY_CAPTURE_WINDOW_S`.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_memecoin_opportunity_settings.py -q -k paper_adapter`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/settings/memecoin_opportunity.py tests/test_memecoin_opportunity_settings.py
git commit -m "feat(radar): default-OFF paper adapter settings with consumer heartbeat gate"
```

---

### Task 4: Append the request in the alert transaction

**Files:**
- Modify: `sniper/research/memecoin_opportunity/engine.py` (forward-alert append path)
- Test: `tests/test_memecoin_opportunity_engine.py`

**Interfaces:**
- Consumes: `append_paper_fill_request`, `paper_request_id`, `ADAPTER_VERSION`, `FILL_MODEL_VERSION`
- Produces: one `op_paper_fill_requests` row per v5 alert when the adapter is enabled

- [ ] **Step 1: Write the failing test**

```python
def test_v5_alert_appends_one_paper_fill_request_when_enabled(tmp_path):
    conn, engine = _engine(tmp_path, paper_adapter_enabled=True)
    _drive_to_v5_alert(conn, engine)
    rows = conn.execute(
        "SELECT rule_version,clip_usd_micros,launch_mode,adapter_version,fill_model_version "
        "FROM op_paper_fill_requests").fetchall()
    assert len(rows) == 1
    assert rows[0]["rule_version"] == "pump-confirmation-truth-v5"
    assert rows[0]["clip_usd_micros"] == 25_000_000
    assert rows[0]["fill_model_version"] == "route-floor-v1"
    assert rows[0]["launch_mode"] in {"standard", "cashback", "mayhem"}


def test_no_paper_fill_request_when_adapter_disabled(tmp_path):
    conn, engine = _engine(tmp_path, paper_adapter_enabled=False)
    _drive_to_v5_alert(conn, engine)
    assert conn.execute("SELECT COUNT(*) FROM op_paper_fill_requests").fetchone()[0] == 0


def test_non_v5_arms_do_not_create_paper_fill_requests(tmp_path):
    conn, engine = _engine(tmp_path, paper_adapter_enabled=True)
    _drive_to_control_alert(conn, engine)
    assert conn.execute("SELECT COUNT(*) FROM op_paper_fill_requests").fetchone()[0] == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_memecoin_opportunity_engine.py -q -k paper_fill_request`
Expected: FAIL — no rows appended

- [ ] **Step 3: Implement**

In the same `with transaction(self.conn)` block that appends the forward alert, when
`self.paper_adapter_enabled` and `rule_version == "pump-confirmation-truth-v5"`, append the request.
`launch_mode` is derived exactly as `_alert_features` does it (`engine.py:265`):
`"cashback" if state.cashback_enabled else "mayhem" if state.mayhem_mode else "standard"`.
`local_qty_atomic` is `primary.tokens_out_atomic`. `decision_age_ms` is the existing value.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_memecoin_opportunity_engine.py -q -k paper_fill_request`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/engine.py tests/test_memecoin_opportunity_engine.py
git commit -m "feat(radar): append the paper fill request atomically with the v5 alert"
```

---

### Task 5: Quote the request on the existing probe and append the receipt

**Files:**
- Modify: `sniper/research/memecoin_opportunity/paper_adapter.py`
- Modify: `scripts/memecoin_opportunity_worker.py` (drive the adapter from the existing sweep)
- Test: `tests/test_memecoin_opportunity_paper_adapter.py`

**Interfaces:**
- Consumes: `OpportunitySolanaProbe`, `parse_jupiter_quote_receipt`
- Produces: `drive_paper_adapter(conn, probe, *, settings, now_iso, consumer_fresh: bool) -> dict[str, int]`

- [ ] **Step 1: Write the failing tests**

```python
def test_filled_receipt_scores_the_floor_not_the_expected(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_paper_fill_request(conn, _request(request_id="r1"))
    probe = _FakeProbe(payload={"outAmount": "1000", "otherAmountThreshold": "940",
                                "slippageBps": 300, "routePlan": []})
    drive_paper_adapter(conn, probe, settings=_settings(), now_iso="2026-08-05T00:00:02Z",
                        consumer_fresh=True)
    row = conn.execute("SELECT * FROM op_paper_fills WHERE request_id='r1'").fetchone()
    assert row["status"] == "filled"
    assert row["floor_out_atomic"] == 940
    assert row["expected_out_atomic"] == 1000


def test_malformed_quote_becomes_typed_unfilled(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_paper_fill_request(conn, _request(request_id="r1"))
    probe = _FakeProbe(payload={"outAmount": "1000"})  # no otherAmountThreshold
    drive_paper_adapter(conn, probe, settings=_settings(), now_iso="2026-08-05T00:00:02Z",
                        consumer_fresh=True)
    row = conn.execute("SELECT status,unfilled_reason FROM op_paper_fills WHERE request_id='r1'").fetchone()
    assert (row["status"], row["unfilled_reason"]) == ("entry_unfilled", "malformed_quote")


def test_stale_consumer_yields_consumer_unavailable_and_no_quote(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_paper_fill_request(conn, _request(request_id="r1"))
    probe = _FakeProbe(payload={"outAmount": "1000", "otherAmountThreshold": "940"})
    drive_paper_adapter(conn, probe, settings=_settings(), now_iso="2026-08-05T00:00:02Z",
                        consumer_fresh=False)
    row = conn.execute("SELECT status,unfilled_reason FROM op_paper_fills WHERE request_id='r1'").fetchone()
    assert (row["status"], row["unfilled_reason"]) == ("entry_unfilled", "consumer_unavailable")
    assert probe.calls == 0


def test_queue_delay_and_quote_latency_are_recorded_separately(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_paper_fill_request(conn, _request(request_id="r1", intent_at="2026-08-05T00:00:00Z"))
    probe = _FakeProbe(payload={"outAmount": "1000", "otherAmountThreshold": "940"},
                       started_at="2026-08-05T00:00:01Z", completed_at="2026-08-05T00:00:02Z")
    drive_paper_adapter(conn, probe, settings=_settings(), now_iso="2026-08-05T00:00:02Z",
                        consumer_fresh=True)
    row = conn.execute("SELECT queue_delay_ms,quote_latency_ms FROM op_paper_fills WHERE request_id='r1'").fetchone()
    assert row["queue_delay_ms"] == 1000
    assert row["quote_latency_ms"] == 1000
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_memecoin_opportunity_paper_adapter.py -q -k receipt or floor or consumer or latency`
Expected: FAIL — `drive_paper_adapter` not defined

- [ ] **Step 3: Implement**

`drive_paper_adapter` selects requests with no receipt, ordered by `intent_at`. For each: if not
`consumer_fresh`, append `consumer_unavailable`; else enqueue on the probe at lower priority than
capture and the route probe. Map exceptions to reasons via `parse_jupiter_quote_receipt`'s
`ValueError` messages (`JUPITER_PROVIDER_UNAVAILABLE` → `quote_unavailable`,
`JUPITER_MALFORMED_QUOTE` → `malformed_quote`). Return per-status counters.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_memecoin_opportunity_paper_adapter.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/paper_adapter.py scripts/memecoin_opportunity_worker.py tests/test_memecoin_opportunity_paper_adapter.py
git commit -m "feat(radar): quote paper requests on the existing probe, append floor-scored receipts"
```

---

### Task 6: Expiry sweep appends, never updates

**Files:**
- Modify: `sniper/research/memecoin_opportunity/paper_adapter.py`
- Test: `tests/test_memecoin_opportunity_paper_adapter.py`

**Interfaces:**
- Produces: `sweep_expired_requests(conn, *, now_iso) -> int`

- [ ] **Step 1: Write the failing test**

```python
def test_expiry_sweep_appends_exactly_one_terminal_receipt(tmp_path):
    conn = connect(tmp_path / "opportunity.sqlite")
    append_paper_fill_request(conn, _request(request_id="r1", expires_at="2026-08-05T00:00:30Z"))
    assert sweep_expired_requests(conn, now_iso="2026-08-05T00:00:31Z") == 1
    assert sweep_expired_requests(conn, now_iso="2026-08-05T00:00:32Z") == 0
    rows = conn.execute("SELECT status,unfilled_reason FROM op_paper_fills WHERE request_id='r1'").fetchall()
    assert len(rows) == 1
    assert (rows[0]["status"], rows[0]["unfilled_reason"]) == ("entry_unfilled", "capacity_expired")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_memecoin_opportunity_paper_adapter.py -q -k expiry_sweep`
Expected: FAIL — `sweep_expired_requests` not defined

- [ ] **Step 3: Implement**

Iterate `expired_requests(conn, now_iso=now_iso)` and append `capacity_expired` receipts. Never
issue `UPDATE` against either table.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_memecoin_opportunity_paper_adapter.py -q -k expiry_sweep`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/research/memecoin_opportunity/paper_adapter.py tests/test_memecoin_opportunity_paper_adapter.py
git commit -m "feat(radar): expiry sweep appends capacity_expired receipts idempotently"
```

---

### Task 7: V6 ledger — provenance columns and measurement censoring

**Files:**
- Modify: `sniper/persistence/db_memecoin_v6_paper.py` (`PRAGMA user_version`, migration, `v6_paper_entries`, `v6_paper_positions.status` CHECK)
- Test: `tests/test_memecoin_v6_paper_db.py`

**Interfaces:**
- Produces: `v6_paper_entries` columns `source_receipt_id`, `adapter_version`,
  `fill_model_version`, `rule_version`, `evaluator_version`, `launch_mode`;
  `v6_paper_positions.status` accepts `censored_migration` and `censored_handoff_stale`.

- [ ] **Step 1: Write the failing tests**

```python
def test_v6_entries_carry_radar_provenance(tmp_path):
    conn = connect_v6(tmp_path / "v6.sqlite")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(v6_paper_entries)")}
    assert {"source_receipt_id", "adapter_version", "fill_model_version",
            "rule_version", "evaluator_version", "launch_mode"} <= cols


def test_measurement_censoring_is_distinct_from_operator_censoring(tmp_path):
    conn = connect_v6(tmp_path / "v6.sqlite")
    _seed_position(conn, position_id="p1")
    conn.execute("UPDATE v6_paper_positions SET status='censored_migration' WHERE position_id='p1'")
    conn.execute("UPDATE v6_paper_positions SET status='censored_handoff_stale' WHERE position_id='p1'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE v6_paper_positions SET status='censored_whatever' WHERE position_id='p1'")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_db.py -q -k provenance or censoring`
Expected: FAIL — missing columns / CHECK rejects the new statuses

- [ ] **Step 3: Implement**

Bump `PRAGMA user_version` to 10 and add `_migrate_v9_to_v10`: six `ALTER TABLE v6_paper_entries
ADD COLUMN … TEXT` (nullable, so existing rows are untouched), and rebuild
`v6_paper_positions` with `CHECK (status IN ('open','closed','censored_operator',
'censored_migration','censored_handoff_stale'))` following the existing table-rebuild pattern used
by earlier migrations in this file.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_db.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/persistence/db_memecoin_v6_paper.py tests/test_memecoin_v6_paper_db.py
git commit -m "feat(v6): radar provenance columns and measurement-censoring statuses"
```

---

### Task 8: `route-floor-v1` normalization

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_quotes.py:584` (entry), `:660` (exit)
- Test: `tests/test_memecoin_v6_paper_quotes.py`

**Interfaces:**
- Produces: `normalize_entry_fill(..., fill_model_version)` and `quote_exit_outcome(...,
  fill_model_version)` scoring `otherAmountThreshold` when `fill_model_version == "route-floor-v1"`,
  and `outAmount` otherwise.

- [ ] **Step 1: Write the failing tests**

```python
def test_route_floor_v1_scores_the_threshold_on_both_legs():
    raw = {"outAmount": "1000", "otherAmountThreshold": "940", "slippageBps": 300, "routePlan": []}
    entry = build_entry_fill(raw, fill_model_version="route-floor-v1")
    assert entry.token_qty_atomic == 940
    assert entry.quote_payload["expected_out_atomic"] == 1000
    exit_fill = build_exit_fill(raw, fill_model_version="route-floor-v1")
    assert exit_fill.quote_out_atomic == 940


def test_legacy_fill_model_is_unchanged():
    raw = {"outAmount": "1000", "otherAmountThreshold": "940", "slippageBps": 300, "routePlan": []}
    entry = build_entry_fill(raw, fill_model_version="modeled_live_quote")
    assert entry.token_qty_atomic == 1000


def test_route_floor_v1_requires_the_threshold():
    with pytest.raises(QuoteUnavailable):
        build_entry_fill({"outAmount": "1000"}, fill_model_version="route-floor-v1")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_quotes.py -q -k route_floor or legacy_fill_model`
Expected: FAIL — parameter not accepted

- [ ] **Step 3: Implement**

Thread `fill_model_version` through both quote paths. Under `route-floor-v1` read
`otherAmountThreshold` for the scored amount and keep `outAmount` in `quote_payload` as
`expected_out_atomic`. Missing threshold raises `QuoteUnavailable`. Default remains the existing
behaviour so fomo-source positions are untouched.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_quotes.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_paper_quotes.py tests/test_memecoin_v6_paper_quotes.py
git commit -m "feat(v6): route-floor-v1 scores the slippage threshold on both legs"
```

---

### Task 9: V6 cursor on `receipt_seq` with authoritative admission

**Files:**
- Create: `sniper/services/memecoin_v6_radar_signals.py`
- Test: `tests/test_memecoin_v6_radar_signals.py`

**Interfaces:**
- Consumes: `op_paper_fills` (read-only)
- Produces: `read_radar_fills(radar_conn, *, after_seq: int, limit: int) -> tuple[RadarFill, ...]`,
  `RadarFill(receipt_seq, request_id, status, …)`

- [ ] **Step 1: Write the failing tests**

```python
def test_cursor_advances_across_unfilled_receipts(tmp_path):
    radar = _radar_with_receipts(tmp_path, [("r1", "entry_unfilled"), ("r2", "filled")])
    fills = read_radar_fills(radar, after_seq=0, limit=10)
    assert [f.status for f in fills] == ["entry_unfilled", "filled"]
    assert fills[-1].receipt_seq == 2


def test_only_filled_receipts_are_admissible(tmp_path):
    radar = _radar_with_receipts(tmp_path, [("r1", "entry_unfilled"), ("r2", "filled")])
    fills = read_radar_fills(radar, after_seq=0, limit=10)
    assert [f.request_id for f in fills if f.admissible] == ["r2"]


def test_radar_connection_is_opened_read_only(tmp_path):
    radar = _radar_with_receipts(tmp_path, [("r1", "filled")])
    with pytest.raises(sqlite3.OperationalError):
        radar.execute("INSERT INTO op_paper_fills (request_id,status,receipt_at,row_fingerprint,"
                      "inserted_at) VALUES ('r9','filled','x','fp','x')")
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_memecoin_v6_radar_signals.py -q`
Expected: FAIL — module not found

- [ ] **Step 3: Implement**

`read_radar_fills` selects `WHERE receipt_seq > :after_seq ORDER BY receipt_seq LIMIT :limit`.
`admissible` is `status == "filled"`. The module opens the Radar database with
`file:…?mode=ro` and never writes it. It imports nothing from
`sniper.research.memecoin_opportunity`.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_memecoin_v6_radar_signals.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_radar_signals.py tests/test_memecoin_v6_radar_signals.py
git commit -m "feat(v6): read-only radar receipt cursor on receipt_seq"
```

---

### Task 10: Structural cohort separation

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_report.py`
- Test: `tests/test_memecoin_v6_paper_report.py`

**Interfaces:**
- Produces: `cohort_filter_sql(*, signal_source, adapter_version, fill_model_version, rule_version,
  evaluator_version, launch_mode, policy_hash) -> tuple[str, tuple]`

- [ ] **Step 1: Write the failing tests**

```python
def test_cohort_filter_binds_every_discriminator():
    sql, params = cohort_filter_sql(
        signal_source="radar_v5", adapter_version="radar-paper-adapter-v1",
        fill_model_version="route-floor-v1", rule_version="pump-confirmation-truth-v5",
        evaluator_version="alert-outcome-v4", launch_mode="standard", policy_hash="h1")
    for column in ("signal_source", "adapter_version", "fill_model_version",
                   "rule_version", "evaluator_version", "launch_mode", "policy_hash"):
        assert column in sql
    assert len(params) == 7


def test_radar_and_fomo_rows_never_share_an_aggregate(tmp_path):
    conn = _v6_with(tmp_path, radar_rows=3, fomo_rows=4)
    radar = aggregate_cohort(conn, signal_source="radar_v5")
    fomo = aggregate_cohort(conn, signal_source="global_feed")
    assert radar["n"] == 3 and fomo["n"] == 4
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_report.py -q -k cohort`
Expected: FAIL — `cohort_filter_sql` not defined

- [ ] **Step 3: Implement**

Build the predicate from all seven discriminators and require every aggregate entry point to take
it. Missing any discriminator raises rather than silently widening.

- [ ] **Step 4: Run to verify pass**

Run: `python3 -m pytest tests/test_memecoin_v6_paper_report.py -q -k cohort`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_v6_paper_report.py tests/test_memecoin_v6_paper_report.py
git commit -m "feat(v6): enforce cohort separation in queries, not layout"
```

---

### Task 11: Current-arm-only operator cockpit

**Files:**
- Modify: `sniper/services/memecoin_v6_paper_report.py`
- Modify: `sniper/services/memecoin_v6_radar_signals.py`
- Modify: `sniper/routes/api_memecoin.py`
- Modify: `static/app.js`
- Test: `tests/test_memecoin_v6_{paper_report,paper_api,paper_ui,radar_signals}.py`

- [x] Filter cockpit positions and counts to the current Radar adapter/fill/rule/evaluator contract
  and current V6 policy hash.
- [x] Remove legacy and terminal position cards from the operator surface without deleting rows.
- [x] Report actual activation/heartbeat state and immutable request/fill/unfilled/pending counts.
- [x] Render an explicit current-arm identity and honest empty state; never substitute older data.

---

## Final verification

```bash
python3 -m pytest tests/ -q -k "opportunity or v6_paper or v6_radar"
python3 scripts/validate_schema_refs.py
python3 scripts/brain_index.py generate
```

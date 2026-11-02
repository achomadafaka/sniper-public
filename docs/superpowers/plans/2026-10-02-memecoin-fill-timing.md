# Memecoin fill timing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo: no subagents (operator rule); Beta executes inline.

**Goal:** Every submitted funded entry (filled or failed) gets its source slot or block, our landing
slot or block, the gap (S+n), the lane, the stage times and a "why not S+1" cause. Everything the
operator reads shows it: the Telegram entry alert, the Real positions row and a Copy-tab table.

**Architecture:**
- Zero-read captures on the fill path add values we already hold to writes and trace marks that
  already happen.
- A bounded in-memory deque records the slot lane's skip reasons. A runner thread drains it every
  second into its own SQLite file.
- A separate cron process, the resolver, writes one `memecoin_fill_timing` row per submitted attempt
  into that same file, and fills gaps with reads behind knobs that default off.
- One pure module computes S+n and the cause for both the resolver and the views.

**Tech Stack:** Python 3.13, sqlite3 (WAL), Flask routes, vanilla JS (`static/app.js`), pytest
with node for JS.

**Spec:** `docs/superpowers/specs/2026-10-02-memecoin-fill-timing-design.md` (revision 2, 5cf3a3501)

## Global Constraints

- Nothing new on the fill path: no new call, lock or retry; measured p50/p90 for every added
  mark or append (CLAUDE.md rule 2).
- `data/memecoin_fill_timing.db` holds both new tables, and the ledger is never written by the drain
  or the resolver. Readers open their OWN read-only connection to the timing file
  (`ft.open_readonly()`, URI `mode=ro`), never `ATTACH`. `get_connection()` is not opened with
  `uri=True`, so an ATTACH URI would become a literal filename: a stray file, writable (Gamma LOW1,
  trap 984bcf0a). The same applies to the rh and BSC lane files in the resolver. A missing timing
  file means "no row yet" and never raises.
- Absent is NULL, final unknown is the state `unknown`; a gap is never 0 for a missing slot.
- Reads default OFF: `MEMECOIN_FILL_TIMING_SOLANA_READS=0`, `MEMECOIN_FILL_TIMING_EVM_READS=0`.
- Solana reads only `MEMECOIN_RESEARCH_HELIUS_RPC_URL`; EVM reads only 4663 research QuickNode and
  8453 `https://base.gateway.tenderly.co` through `verify_axiom_addresses.code_read_url` + an
  `eth_chainId` check; 56 has no host.
- on_time: solana 1 slot, 8453 1 block, 56 2, 5042 2, 4663 8 blocks.
- pytest only as `nice -n 19 ionice -c 3 python3 -m pytest ... -n 6` at most; targeted files.

---

### Task 1: The pure timing module

**Files:**
- Create: `sniper/services/memecoin_fill_timing.py`
- Test: `tests/test_memecoin_fill_timing.py`

**Interfaces:**
- Produces:
  - `ON_TIME: dict[str, int]`;
  - `SCHEMA: str`, `timing_db_path() -> Path`, `connect(path=None) -> sqlite3.Connection` (WAL,
    schema ensured), `open_readonly(path=None) -> sqlite3.Connection|None` (None when the file does
    not exist; never creates it);
  - `facts(attempt: dict, detection: dict|None, slot_attempt: dict|None, skip: dict|None,
    fast: dict|None) -> dict` (the stored inputs; pure);
  - `gap_of(f) -> int|None`, `stages(f) -> dict[str, float|None]`,
    `classify(f) -> tuple[str, str]` (cause, detail), `landed_text(f) -> str`.

- [ ] **Step 1: Write the failing tests.** One test per cause rule:
  - `on_time` at S+1, and S+0 shown as a real 0;
  - `price`: failed with reason containing `slippage` or `price`;
  - `not_landed`;
  - `slot_lane_failed`: carries the slot attempt's S+n and `fallback_wait_ms`;
  - `slot_lane_skipped`: from the attempt reason, from the skip row, and as "never saw it";
  - `fast_lane_skipped` on 4663 and 56;
  - the largest stage as the fallback cause.

  Also: `detail` always ends with the largest stage; `gap_of` is None when either slot is None, never
  0; `landed_text` gives `Landed S+1`, `Landed S+7 · slot lane: pool_unknown · detect 3 slots`,
  `Landed +19 blocks`, `Landed S+? (source slot resolving)` and `Not landed`.

```python
from sniper.services import memecoin_fill_timing as ft

def _a(**kw):
    base = {"attempt_id": "a1", "network_id": "solana", "signal_source": "onchain_watch",
            "delivery_lane": None, "terminal_status": "filled", "terminal_reason": None,
            "observed_at": "2026-10-02T10:00:00.000+00:00", "decision_started_at": "2026-10-02T10:00:00.010+00:00",
            "transaction_submitted_at": "2026-10-02T10:00:00.400+00:00",
            "chain_confirmation_completed_at": "2026-10-02T10:00:01.100+00:00",
            "completed_at": "2026-10-02T10:00:01.200+00:00", "tx_sig": "OURS",
            "origin_event_id": "onchain:SRC", "source_slot": None, "landing_slot": None, "recv_slot": None}
    base.update(kw)
    return base

def test_s_plus_one_is_on_time_and_s_plus_zero_is_a_real_zero():
    f = ft.facts(_a(source_slot=100, landing_slot=101), None, None, None, None)
    assert ft.gap_of(f) == 1 and ft.classify(f)[0] == "on_time" and ft.landed_text(f) == "Landed S+1"
    f0 = ft.facts(_a(source_slot=100, landing_slot=100), None, None, None, None)
    assert ft.gap_of(f0) == 0

def test_a_missing_slot_is_none_never_zero():
    assert ft.gap_of(ft.facts(_a(landing_slot=101), None, None, None, None)) is None

def test_fallback_after_a_failed_slot_attempt_names_it_and_the_wait():
    slot = {"terminal_status": "failed", "terminal_reason": "slippage", "source_slot": 100,
            "landing_slot": 102, "completed_at": "2026-10-02T09:59:59.620+00:00"}
    f = ft.facts(_a(delivery_lane="onchain_fallback_after_slot_lane", source_slot=100, landing_slot=106,
                    decision_started_at="2026-10-02T10:00:00.000+00:00"), None, slot, None, None)
    cause, detail = ft.classify(f)
    assert cause == "slot_lane_failed" and "S+2" in detail and "slippage" in detail and "fallback_wait 380 ms" in detail
```
  (plus the other cases, written the same way)

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError`).
  `nice -n 19 ionice -c 3 python3 -m pytest -q -n 0 tests/test_memecoin_fill_timing.py`
- [ ] **Step 3: Implement** `memecoin_fill_timing.py`:
  - `SCHEMA` holds both tables of spec 2.3, `timing_db_path()` returns
    `state_path("data", "memecoin_fill_timing.db")`, and `connect()` runs `PRAGMA journal_mode=WAL`
    and the schema.
  - `open_readonly` returns `sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=1)`, or None
    when the file is missing. A test covers a missing file and checks that no file was created.
  - `facts` merges the inputs: slot-lane trace slots; detection `source_slot`, `recv_slot` and
    `block_time`; the slot attempt; the skip row; the fast-lane row.
  - `stages`: detect is `recv_slot - source_slot` (slots) on Solana and `detected_at - block_time`
    (s) on EVM; fallback_wait and decide in ms; land in slots (Solana) or blocks (EVM).
  - `classify` applies spec 2.5 in order, and `landed_text` returns the Telegram line.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `feat(fill-timing): the pure timing module: S+n, stages and the why-not-S+1 cause`.

### Task 2: Detection captures (source_slot, recv_slot) with a safe rollout

**Files:**
- Modify: `sniper/persistence/db_memecoin.py` (the `onchain_wallet_detections` column list near
  `_onchain_cols`, and `record_onchain_detection` at :10334);
- Modify: `scripts/research/onchain_wallet_watch.py` (`_DETECTION_INSERT` :59, its params at :654,
  the startup ALTER at :733, and the logs frame's context slot passed down to the detection);
- Modify: `scripts/research/onchain_evm_wallet_watch.py` (`_record` :449, and its startup ensure);
- Test: `tests/test_memecoin_fill_timing_captures.py`.

**Interfaces:** Produces the columns `source_slot INTEGER` and `recv_slot INTEGER`.
`record_onchain_detection(..., source_slot=None, recv_slot=None)`.

- [ ] **Step 1: Failing tests:**
  - `record_onchain_detection` stores both columns;
  - on a table created WITHOUT them (the pre-migration DDL), the call still stores the detection
    (the retry without the new columns);
  - the Solana watcher's `_store_detection` params include `tx["slot"]` and the frame context slot;
  - EVM `_record` stores `int(blockNumber,16)`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - Add `("source_slot","INTEGER"), ("recv_slot","INTEGER")` to the column loop.
  - `record_onchain_detection` adds both to its INSERT. On `sqlite3.OperationalError` containing
    `no such column` it retries once with the old column list, and logs `exception` once.
  - Solana watcher: extend `_DETECTION_INSERT` and its params; the ALTER at startup becomes a loop
    over `source_valuation_json`, `source_slot`, `recv_slot`. Thread the notification's
    `result.context.slot` into the task that classifies the transaction (it is already in the frame).
  - EVM watcher: add `source_slot` to `columns`/`values`, with an idempotent ALTER at startup.
- [ ] **Step 4: Run tests and benchmark.** Insert with and without the two values, 10k loops,
  p50/p90 recorded in the commit message.
- [ ] **Step 5: Commit.**

### Task 3: Landing captures (Solana confirmation slot, EVM ReceiptState)

**Files:**
- Modify: `sniper/execution/solana_swap_executor.py:1413`;
- Modify: `sniper/execution/evm_real_seams.py` (`eth_confirm` :1899, `eth_receipt_state`;
  `confirm_receipt_across_pool` passes the result through unchanged);
- Modify: `sniper/execution/evm_swap_executor.py:1597-1603`;
- Modify: `sniper/services/memecoin_rh_fast_lane.py` (`_receipt` :1368 callers write
  `blockNumber` into the event they already record);
- Modify: the BSC lane, wherever it reads a receipt;
- Test: `tests/test_memecoin_fill_timing_captures.py`.

**Interfaces:**
- `ReceiptState(str)` with `block_number: int|None`, exported from `evm_real_seams`;
- trace substage `evm.confirmation` gains `block`;
- `chain_confirmation` marks gain `landing_slot` (Solana) or `block` (EVM).

- [ ] **Step 1: Failing tests:**
  - `eth_confirm` on a status-1 receipt returns a value `== "confirmed"`, with `.block_number`
    parsed from hex. It binds into sqlite as `'confirmed'` and `json.dumps` gives `"confirmed"`.
  - `eth_receipt_state` does the same; `not_found` has no block.
  - The Solana swap executor's `chain_confirmation` event carries `landing_slot` from a
    `ConfirmState`.
  - The EVM executor's `evm.confirmation` carries `block`.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement**, then the caller audit:
  `grep -rn "eth_confirm(\|eth_receipt_state(\|confirm_receipt_across_pool(" sniper scripts`.
  For each caller, check for `type(...) is str`, `is "`, and `str(`/`.lower()`/format before the
  mark, and list them in the commit. The audit covers `eth_receipt_state`'s callers and
  `confirm_receipt_across_pool`'s, not only `eth_confirm`'s 19 (Gamma LOW2).
- [ ] **Step 4: Run the touched test files** (grep `tests/` for each changed symbol) and benchmark
  the mark with and without the extra field (p50/p90).
- [ ] **Step 5: Commit.**

### Task 4: Slot-lane skip reasons (deque + drain)

**Files:**
- Create: `sniper/services/memecoin_slot_skips.py`: `note(sig, outcome, reason)`,
  `drain(limit=4096) -> list`, `flush(conn) -> int`, `run_drain(stop, interval_s=1.0,
  connect=ft.connect)`;
- Modify: `sniper/services/memecoin_slot_lane.py` `route_routed_buy`/`_route_routed_buy`:
  `_route_routed_buy` returns its outcome (`lane`, `learning`, `handoff`, `shadow`), and the wrapper
  calls `note(...)` once in its `finally`;
- Modify: `sniper/services/memecoin_slot_signal.py`: in the pairer `expire` (`pair_expired`) and
  `_check` (`ambiguous`), `note()` the pair's signatures;
- Modify: `scripts/memecoin_runner_loop.py`: start thread `memecoin-slot-skips` next to the slot
  lane;
- Test: `tests/test_memecoin_slot_skips.py`.

- [ ] **Step 1: Failing tests:**
  - each `_route_routed_buy` exit notes one outcome with its `why`;
  - expiry notes `pair_expired`;
  - `flush` writes `memecoin_slot_lane_skips` in a temp timing DB and never opens the ledger
    (`ft.connect` is pointed at tmp; the test asserts no file named `sniper_tracker.db` was
    opened, via a monkeypatched `sqlite3.connect` that records paths);
  - the deque is bounded (`maxlen` 4096) and a flush error keeps the thread alive.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** The `note` cost is one tuple `append`.
- [ ] **Step 4: Benchmark the append on the lane thread** (p50/p90, 100k loops), plus the existing
  slot-lane tests: `grep -l route_routed_buy tests/` and `grep -l RoutedPairer tests/`.
- [ ] **Step 5: Commit.**

### Task 5: The resolver (cron process)

**Files:**
- Create: `scripts/research/fill_timing_resolver.py`;
- Modify: `sniper/settings/memecoin.py`: `MEMECOIN_FILL_TIMING_SOLANA_READS` and
  `MEMECOIN_FILL_TIMING_EVM_READS` (bool, default 0), and `MEMECOIN_FILL_TIMING_WINDOW_H=24`;
- Test: `tests/test_fill_timing_resolver.py`.

**Interfaces:** `resolve_pass(ledger_ro, timing_rw, *, now, solana_read=None, evm_read=None,
alert=None) -> dict(summary)`. `main(argv)` takes `--max-seconds` and `--summary`.

- [ ] **Step 1: Failing tests.**
  - `test_every_submitted_attempt_gets_a_timing_row`: filled, failed and refused attempts; after
    one pass every filled or failed attempt has a row and no refused one does.
  - The slot-lane trace gives `complete` with no reads.
  - An onchain row with detection slots and a captured landing slot is `complete`.
  - A row needing a read with the knob off is `unread`, and is reopened when the knob turns on.
  - Solana reads batch every pending signature into ONE `getSignatureStatuses` call.
  - `not_landed` after 10 min of null with reads on; `unknown` after 24 h of failures.
  - A J7 row that needed `rpc` for the source sends exactly one follow-up line.
  - The resolver never opens the ledger writable.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
  - Read attempts through `get_readonly_connection` with `INDEXED BY idx_mc_entry_view_completed`
    from a cursor stored in the timing DB.
  - Open the rh and BSC lane files with their own read-only URI connections for `fast` facts (never
    ATTACH), skipping a missing file.
  - Reads go through the host guards: Solana `MEMECOIN_RESEARCH_HELIUS_RPC_URL`; EVM
    `code_read_url` and `eth_chainId`.
  - Follow-ups use `memecoin_telegram.send_telegram`.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit.** The cron line goes to Alpha: `* * * * *` with `--max-seconds 50 --summary`
  under `nice -n 19`.

### Task 6: Entry alert line

**Files:**
- Modify: `sniper/services/memecoin_entry_view.py` (`_entry_view_snapshot` :142): attach
  `fill_timing` from `ft.facts` computed on what is stored, plus the timing row when present (read
  through `ft.open_readonly()`; a missing file or any error gives `S+? (resolving)`, never a raise);
- Modify: `sniper/services/memecoin_telegram.py` `render_entry_notification` (:881): after the
  `Times (UTC)` line, `ft.landed_text(view["fill_timing"])` for filled and failed-on-chain;
- Test: `tests/test_memecoin_fill_timing_views.py`.

- [ ] **Steps 1-5:** Failing tests: a slot-lane fill shows `Landed S+1`; an onchain fill with
  detection slots shows `Landed S+5 · slot lane: <reason> · detect 2 slots`; a J7 fill shows
  `Landed S+? (source slot resolving)`; a refusal shows nothing; with no timing file (first boot) the
  view still renders and degrades. Then implement, PASS, commit.

### Task 7: Real positions chip and the Copy-tab table

**Files:**
- Modify: `sniper/routes/api_memecoin.py` `_real_positions_read` (:2893): add `fill_timing` per
  open position. One query on `ft.open_readonly()`, keyed by the positions' attempt ids, with None
  when the file is missing.
- Add the route `GET /api/memecoin/fill-timing/today`: today's rows joined to attempts (ticker,
  chain, lane, S, landing, gap, cause, stages).
- Modify: `static/app.js`: the Real positions row chip `S+n` with the cause as tooltip; a
  "Fills today" panel in the Copy tab.
- Test: `tests/test_memecoin_fill_timing_views.py` (route JSON shapes; a node test of the chip
  text).

- [ ] **Steps 1-5:** Failing route tests, then implement, PASS, and a browser check on a local web
  preview (read-only), then commit.

---

## Self-review

- Spec 2.1: Tasks 2-3. 2.2: Task 4. 2.3-2.4: Tasks 1 and 5. 2.5: Task 1. 2.6: Tasks 6-7.
- Section 3: the constraints above plus the tests. Section 4: the tests named in Tasks 1-5.
- Names: `ft` = `sniper.services.memecoin_fill_timing` in every task; `ReceiptState` is defined in
  Task 3 and only read afterwards; `note`/`flush` are defined in Task 4.

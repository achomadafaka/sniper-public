# Wallet Universe Paper Observation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Observe explicitly selected verified wallets without funding them, retaining complete forward populations and typed evidence gaps.

**Architecture:** A disabled-by-default Solana research command reads an explicit manifest through a separately budgeted read-only RPC transport. Its own SQLite ledger stores observations/cursors and references the existing quote-measurement ledger. It never writes funded detections, invokes the funded hook or supplies V6 candidates.

**Tech Stack:** Python stdlib SQLite/JSON, existing Solana decoder/read-RPC helpers, existing quote-measurement functions, pytest.

**Spec:** `docs/superpowers/specs/2026-10-02-wallet-universe-design.md`; Map interface in `2026-10-02-wallet-universe-map.md`.

## Global Constraints

- No extra synchronous call/lock/retry is allowed on the entry path.
- No DexScreener prices may enter fill evidence.
- No automatic arming or filter exemptions. Unknown stays unknown.
- Atomic amounts are decimal TEXT. Production ledger/graph handles are read-only.
- Use isolated research RPC capacity and a finite read/provider budget.
- No worker activation/deployment, provider calls or funded policy changes during implementation.
- Start with one observable chain; EVM collection remains typed unsupported.

## Review Focus

1. Paused/excluded or unproved identities cannot silently enter the manifest — Task 1.
2. Exact atomic values above SQLite integer limits and duplicate retries remain correct — Task 1.
3. Pagination/restart gaps, quiet wallets, stale or malformed RPC replies stay distinguishable — Task 2.
4. Native fee-only transfers/multiple token accounts cannot become invented buys — Task 2.
5. Exhausted or shared provider capacity cannot touch funded admission or become zero-return evidence — Task 3.

---

## File responsibilities

- Create `sniper/persistence/db_wallet_observation.py`: own research schema and transaction-bound persistence only.
- Create `sniper/services/wallet_observation.py`: pure manifest validation, decoding adapter, bounded collection, setup envelope and quote-only measurement adapter.
- Create `scripts/research/wallet_observation.py`: explicit CLI, resource validation and finite work invocation; no service/watchdog registration.
- Reuse `sniper/services/memecoin_solana_blocktime.py:build_read_rpc_call`, existing decoded venue parsers used by the Solana tape worker, and `sniper/services/memecoin_signal_copy_quotes.py` quote-only scheduling/normalization. Do not modify the funded watcher or its owner filter.
- Create `tests/test_wallet_observation.py` with small inline recorded transaction fixtures and temporary DBs.

### Task 1: Explicit owner manifest and isolated durable ledger

**Files:** Create persistence/service/test files above.

**Interfaces:** `validate_manifest(manifest: dict, proven: dict) -> list[dict]` returns verified selected owner rows with uid/chain/address and selection cutoff; input includes explicit enabled flag, chain, owners, forward start and cap. `open_observation_db(path: Path) -> sqlite3.Connection`; `record_observation(conn, event: dict) -> bool` inserts one immutable observation and returns false for identical duplicates; a differing payload under the same identity raises `ValueError`. `advance_cursor(conn, *, owner: str, signature: str, observed_at: str) -> None` runs inside caller transaction. Own `observations` table: `origin TEXT PRIMARY KEY`, `uid TEXT`, `chain TEXT`, `address TEXT`, `signature TEXT`, `event_index TEXT`, `mint TEXT`, `action TEXT`, `quantity_atomic TEXT`, `quote_atomic TEXT`, `source_at TEXT`, `received_at TEXT`, `state TEXT`, `setup_json TEXT`, `payload_json TEXT`. Own `cursors(owner TEXT PRIMARY KEY, signature TEXT, observed_at TEXT)` and `collection_status(owner TEXT PRIMARY KEY, state TEXT, detail TEXT, as_of TEXT)`. New schema version `PRAGMA user_version=1`.

<!-- schema:external=observations -->

- [x] **Step 1: Add red persistence and manifest checks.**

```python
def test_exact_quantity_and_duplicate_origin(tmp_path):
    from sniper.persistence.db_wallet_observation import open_observation_db, record_observation
    conn = open_observation_db(tmp_path / 'research.db')
    event = dict(origin='solana:A:sig:0:M', uid='A', chain='solana', address='Owner',
                 signature='sig', event_index='0', mint='M', action='buy',
                 quantity_atomic='184467440737095516160', quote_atomic='5000000',
                 source_at=None, received_at='2026-10-02T00:00:00+00:00', state='decoded',
                 setup_json='{}', payload_json='{}')
    assert record_observation(conn, event)
    assert not record_observation(conn, event)
    assert conn.execute('SELECT quantity_atomic, typeof(quantity_atomic) FROM observations').fetchone() == (
        '184467440737095516160', 'text')
```

Test identical retries with a later receipt clock preserving the first receipt, mismatched duplicates, rollback between observation/cursor writes, unverified owner, wrong chain/address case, disabled manifest, paused/excluded wallet and wallet count above declared cap. Proof uses existing verified address registry, including verified `pump:` profile identity; never treats a relay UUID as an address. Verified watch-only is eligible; effectively armed is not required. Stop newly paused/excluded owners at each manifest refresh.

- [x] **Step 2: Run red.** `python3 -m pytest tests/test_wallet_observation.py::test_exact_quantity_and_duplicate_origin -q`; expected import failure.
- [x] **Step 3: Implement own-schema persistence and validation.** Identity includes canonical chain, owner, transaction, decoded event index and mint. Validate nonnegative digit strings without float conversion. Reject event uid/address combinations absent proof. Identical origin compares canonical decoded identity/amounts/program provenance; retain the first `received_at` and exclude a retry’s later transport clock from this comparison. A conflicting decoded payload fails loudly; do not use conflict-ignore to conceal mismatches. Transaction core:

```python
with conn:
    inserted = record_observation(conn, event)
    advance_cursor(conn, owner=event['uid'], signature=event['signature'],
                   observed_at=event['received_at'])
```

All production reads come from request/worker-owned read-only handles. `record_observation` must not commit internally, so cursor rollback remains atomic. Do not call `ensure_memecoin_tables` on the research DB or any production DB. Manifest selection is frozen before its forward start, including all subsequent qualifying origins.

- [x] **Step 4: Run green.** `python3 -m pytest tests/test_wallet_observation.py -q`; injected production connection is query-only and must remain unchanged.
- [x] **Step 5: Commit.** `git add sniper/persistence/db_wallet_observation.py sniper/services/wallet_observation.py tests/test_wallet_observation.py && git commit -m 'feat(research): persist isolated wallet observations'`.

### Task 2: Bounded Solana forward intake with honest decoding

**Files:** Modify service/test files; create `scripts/research/wallet_observation.py`.

**Interfaces:** `decode_observation(tx: dict, owner: dict, *, received_at: str) -> list[dict]` produces Task 1 events only for a transaction-proven supported swap; otherwise returns a typed diagnostic event with null amounts and state `unsupported|partial|identity_mismatch`. `collect_tick(conn, owners: list[dict], rpc_call, *, now_iso: str, max_calls: int) -> dict` returns `{calls, inserted, duplicates, states}`. The injected callable is `rpc_call(method: str, params: list) -> object`. No websocket claim or subscription ack is synthesized; this is paced pull observation.

- [x] **Step 1: Write red behavioral tests.**

```python
def test_fee_only_native_transfer_is_not_a_buy():
    from sniper.services.wallet_observation import decode_observation
    tx = {'transaction': {'message': {'accountKeys': ['Owner']}},
          'meta': {'fee': 5000, 'preBalances': [100000], 'postBalances': [95000],
                   'preTokenBalances': [], 'postTokenBalances': []}}
    rows = decode_observation(tx, {'uid': 'A', 'chain': 'solana', 'address': 'Owner'},
                              received_at='2026-10-02T00:00:00+00:00')
    assert not any(r['action'] == 'buy' and r['state'] == 'decoded' for r in rows)
```

Add recorded supported Pump/AMM swaps, multiple owner token accounts, native/WSOL/cash quote identities, sell receipt, missing meta, failed transaction, owner mismatch, partial RPC page, provider timeout/429/ban, no new signatures, and restart overlap. Simulate several pages with a cap smaller than the history and verify no skipped oldest-unprocessed forward page. Persist pagination continuation separately from the completed frontier if a page is incomplete; expose a count-unknown gap rather than advance past missing transactions.

- [x] **Step 2: Run red.** `python3 -m pytest tests/test_wallet_observation.py::test_fee_only_native_transfer_is_not_a_buy -q`.
- [x] **Step 3: Reuse existing transaction-proven venue decoders and bounded read transport.** Inspect tape-worker parser interfaces before adapting; retain program/instruction provenance, exact integer balances and event indices. Do not import the funded watcher's writer/wake functions or reuse float-based `classify_buy`. Unrecognized venue is a diagnostic, not a synthesized trade. Build transport with explicit research URL, `min_call_s`, `max_429_retries=0` and task-local pacing state. Count every attempted RPC read against caps, including failures. CLI requires `--manifest`, `--research-rpc-url`, `--max-calls`, `--max-wallets`, `--min-call-s`, `--db`; without `--enabled` it validates and exits without reads. Deny signing/submission RPC method names in the adapter. Freeze setup envelope:

```python
setup = dict(version=1, observed_at=received_at, venue=venue,
             quote_asset=quote_asset, provenance=provenance,
             market_cap=None, age_s=None, quote_depth=None, token_controls=None,
             enrichment_state='not_collected')
```

`venue`, `quote_asset`, `provenance` come from the verified decoder result. Attach existing entry snapshots only when exact origin and timestamp show knowledge at observation; later narrative/enrichment remains separately versioned, never overwrites frozen setup. Callback is local research persistence only.

- [x] **Step 4: Run green.** `python3 -m pytest tests/test_wallet_observation.py -q`; test default CLI with injected fake RPC and assert zero calls. Do not run against a real provider.
- [x] **Step 5: Commit.** `git add sniper/services/wallet_observation.py scripts/research/wallet_observation.py tests/test_wallet_observation.py && git commit -m 'feat(research): collect bounded forward Solana wallet facts'`.

### Task 3: Quote evidence without funded coupling

**Files:** Modify service/CLI/tests; modify `scripts/research/wallet_follow_funnel.py` only to read research coverage into the offline projection.

**Interfaces:** `validate_budget(budget: dict, funded_urls: list[str]) -> None` rejects missing/nonfinite caps and overlapping provider/key/host capacity without explicit proved isolation. `schedule_research_quotes(mdb, event: dict, *, policies: list[str], now_iso: str, budget: dict) -> dict` returns `{origin, state, policy_ids, quote_jobs}`. Use `memecoin_signal_copy_quotes.open_measurement_db(path)`, `schedule_entry(mdb, ep, now=None, cohort='wallet')`, `schedule_exits(mdb, row, anchor, legs=None)` and `normalise_quote(raw, *, input_asset, output_asset)`; adapt the explicit research origin to their existing episode shape. Freeze policies with `resolve_paper_arm_policy`; store measurement references in the research ledger, never in funded candidate tables.

- [x] **Step 1: Write red budget and coupling checks.**

```python
def test_missing_research_capacity_rejects_collection():
    import pytest
    from sniper.services.wallet_observation import validate_budget
    with pytest.raises(ValueError, match='research capacity'):
        validate_budget({}, ['https://funded.example/rpc'])
```

Add finite/negative caps, same-key/different-URL provider overlap, exhausted quota, typed quote rejection/rate limit/timeout, partial entry quote, missing sell quote, duplicate jobs, wrong atomic assets and unsupported policy tests. Spy on funded reservation/hook/signing/broadcast seams: calls must be zero. Fail the spy deliberately once to demonstrate the check catches a coupling defect; do not add source-prose pattern scans.

- [x] **Step 2: Run red.** `python3 -m pytest tests/test_wallet_observation.py::test_missing_research_capacity_rejects_collection -q`.
- [x] **Step 3: Add the quote-only adapter and resource guard.** Reuse existing quote-measurement pacing, claim/dedup and typed normalizer. Research origins carry their own cohort identity and frozen policy set, never become `memecoin_onchain_lane` rows or V6 inputs. Missing acquired atomic quantity prevents exit scheduling; missing exact exits censors modeled outcomes. Supported pure policy evaluators may consume exact measured quotes; unsupported/insufficient cadence remains explicitly insufficient, never reconstructed continuous price paths. Source-follow requires observed source sells. Preserve expected output versus quote floor and fees. The adapter’s episode mapping is `{fomo_event_id: event["origin"], source_user_id: event["uid"], network_id: event["chain"], mint: event["mint"]}`; pass `cohort="wallet-universe-v1"` and a parsed aware datetime to scheduling. Reuse `claim_due(mdb, now=None, limit=32)` and `admission_block` before dispatch; persist typed results with the existing `record` helper. Keep dispatch in this research command and use only existing unsigned quote-provider methods, never the funded executor. Shared Jupiter/provider IP pressure disables research quoting unless separate capacity is established; timeout/rate-limit/ban writes typed status and yields. No public fallback quietly consumes funded capacity. Update Map coverage through the offline producer only.

Implementation guard at the collection boundary:

```python
validate_budget(budget, funded_urls)
if budget['remaining_calls'] <= 0:
    return dict(origin=event['origin'], state='budget_exhausted', policy_ids=[], quote_jobs=0)
```

Once quote/provider isolation is explicitly established, a finite operator-enabled smoke run records calls, failures, observation p50/p90 and quote completeness. Funded fill latency is independently measured from organic traces, not inferred from this worker. Until then all runtime checks use fake providers.

- [x] **Step 4: Run green.** `python3 -m pytest tests/test_wallet_observation.py tests/test_memecoin_signal_copy_quotes.py -q`; assert funded DB unchanged, no write RPC methods, no wake/funded hook and no enabled service registration.
- [x] **Step 5: Commit.** `git add sniper/services/wallet_observation.py scripts/research/wallet_observation.py scripts/research/wallet_follow_funnel.py tests/test_wallet_observation.py && git commit -m 'feat(research): budget isolated wallet quote evidence'`.

## Rollback and limits

Stop only this research command, retaining its immutable ledger and frozen manifests. Existing funded observers/authority are unchanged. Pull coverage does not prove a websocket acknowledgement or recover omitted history. Other chains and unsupported policies show explicit gaps. Observation rollout does not authorize funded policy selection.

## Completion rulings, 2026-10-02

The eight code tasks are delivered with review corrections. Named example tests were adapted to actual repository interfaces; the final focused behavioral union passed 389 tests. Source clocks and paced completion clocks are now explicit. Frozen policies are scoped to the selected setup/window and ambiguous identities disable hypothesis export. Current proof helpers include verified watch-only Pump profiles. Seven existing chain ledgers materialized to scratch without production writes.

Provider isolation is an activation prerequisite, not a completed live test: configured host separation and explicit capacity declaration are checked, but different URLs alone cannot prove independent provider/key/IP capacity. No live provider smoke, service activation, continuous-rail policy verdict, new cash/native route or funded latency improvement is claimed. Actual scratch snapshot size required a shared 16 MiB producer/reader bound.

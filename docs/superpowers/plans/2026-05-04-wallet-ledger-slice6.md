# Wallet Ledger Slice 6 Implementation Plan

> **Status:** v4 — closes Codex's BLOCK on v3. Single residual
> contradiction resolved: Task 8 ledger runner cannot mirror
> `--no-history` simply by inheriting `bundle.to_analyze_wallet_kwargs()`
> because Task 7's loader always rebuilds `price_history_fn`. v4
> adds a harness adapter that nulls the suppressed surfaces before
> splatting. v3's two contradiction fixes (Axis-A/B comparison;
> `MaterializedWalletData` loader shape) and v2's four fixes plus
> the calibration-path correction stay valid and are kept in the
> audit table below. Slice 5 closed at commit `edb18f2`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist analysis results, findings, and artifact pointers to
Supabase from a ledger-materialized run; export fixture v2 from
materialization; and re-baseline the 10 calibration wallets against
ledger-derived inputs. Live capture and the live Supabase write path
are gated behind opt-in env flags so the offline test surface stays
hermetic.

**Architecture:** Spec §15 (Supabase outputs), §16 (fixture exports),
§17 Slice 6. The split between offline and live work is the load-bearing
constraint — every output writer has a no-live unit test that exercises
the full path against a stub HTTP layer, and a separate live test gated
by `CALIBRATION_LIVE_SUPABASE_URL` / `CALIBRATION_LIVE_SUPABASE_KEY`.

**Tech Stack:** Python, Supabase REST (explicit `RestSupabaseHttp`
with injectable `HTTPTransport` — no shared globals with
`bot.supabase_helper`), DuckDB, pytest.

---

## Audit Resolution Log

### v3 → v4 (closed Codex round 3)

| # | v3 issue | v4 fix |
|---|---|---|
| 9 | Task 8 said the ledger runner mirrors the calibration harness flags including `--no-history`, but Task 7's loader always rebuilds `price_history_fn` from persisted bars. A direct `analyze_wallet(..., **bundle.to_analyze_wallet_kwargs())` would let H6/FH6 run, contradicting `--no-history`. The existing fixture path treats `--no-*` flags as advisory under `--from-fixture` and gates closures on fixture-surface availability (`replay.py:303`); the ledger path needs the same suppression discipline. | Task 8 adds an explicit `_apply_calibration_harness_suppression(kwargs)` adapter that nulls every surface the existing harness flags suppress: `price_history_fn=None` (--no-history), `peer_baseline_fn=None` (--no-peers), `leafs_by_cid=None` and `nav_lookup=None` (--no-tags). Slice 5's bundle defaults already null peer/leaf/nav and leave `WalletData.execution_profile=None` and `WalletData.archetype_layer1=None` (covering --no-exec / --no-archetype), so the adapter is the only surface that needs explicit suppression today; the spec calls out exec/archetype overrides explicitly so the adapter stays correct if future bundle defaults change. The direct `analyze_wallet(...)` call passes `seed=1, n_iter=2000` matching the live harness. |

### v2 → v3 (closed Codex round 2)

| # | v2 issue | v3 fix |
|---|---|---|
| 6 | Task 8 asserted detailed parity (`gate_result`, `n_closed_positions`, branch activity, BH-pool verdicts, CIs) against `labels.yaml`, but the YAML only carries `expected.overall_verdict`. The existing harness asserts only `overall_verdict` (`tests/calibration/test_calibration_set.py:96`). | Task 8 now runs a two-axis comparison. **Axis A:** generate the live-fixture protocol report in-process (via the existing `_run_protocol_from_fixture` shape) and compare ledger ↔ live for the detailed fields. **Axis B:** independently assert ledger `overall_verdict == labels.yaml expected`. The two axes are separate `pytest` parameterisations so failures don't mask each other. |
| 7 | Task 7 said `load_calibration_fixture(path)` returns a fresh `WalletData` whose `to_analyze_wallet_kwargs()` splat into `analyze_wallet`. `WalletData` has no such method — only `MaterializedWalletData` does (`materialize.py:53`). | Task 7's loader returns a `MaterializedWalletData` bundle. The exporter serialises every non-callable surface plus the raw price-bar list; the loader re-hydrates `WalletData` and rebuilds `price_history_fn` from the persisted bars (peer / leaf / nav stay `None`, mirroring Slice 5). The bundle's `to_analyze_wallet_kwargs()` is splat-ready. Round-trip parity test is byte-equal `ProtocolReport` (compared via `dataclasses.asdict` to bypass callable identity). |
| 8 (wording) | D3 said artifact_registry is "append-only" but Task 6 allowed PATCH on `paths`. | D3 reworded: "**content rows are append-only; `paths` is mutable metadata**". Each row is keyed by `content_hash`; once written, the row's identity (key + content) never changes. `paths` PATCH is metadata enrichment for the same content. New content always gets a new row. There is no `status` column and no supersede semantics. |

### v1 → v2 (closed Codex round 1, kept for audit)

| # | v1 contradiction | v2 fix |
|---|---|---|
| 1 | D1/D2 said insert-ignore (`ON CONFLICT DO NOTHING`) but Task 2 specified `Prefer: resolution=merge-duplicates` (a PostgREST UPDATE path). | Create paths use `Prefer: resolution=ignore-duplicates`. The `merge-duplicates` header is forbidden anywhere in the slice. Updates go through a separate `mark_superseded(...)` PATCH helper. |
| 2 | D6 said `StubSupabaseHttp` records every call; Task 3 said the second writer call leaves `stub.calls` length unchanged. | Stub `calls` is a request log that grows on every call. The idempotency assertion is "writer return value is `0` AND `stub.rows_by_table[table]` length is unchanged" — `stub.calls` is allowed (and expected) to grow. |
| 3 | Live env gate was `CALIBRATION_LIVE_SUPABASE_URL/_KEY` but `RestSupabaseHttp` was specified to wrap `bot.supabase_helper._request`, which reads `SUPABASE_URL`/`SUPABASE_*KEY` globals at import time. | `RestSupabaseHttp(*, url, key, transport=...)` is a fully explicit constructor with no global reads. Live test fixtures read the calibration env vars and pass them as constructor args. `bot.supabase_helper`'s globals are not touched. |
| 4 | D3 said `artifact_registry` is append-only (no supersede status); Task 9 said cleanup marks every written row as `status='superseded'`. | Artifact rows have NO `status` column. `mark_superseded(table="artifact_registry", ...)` raises `ValueError`. Live test cleanup is per-table: results + findings supersede; artifact rows live in a `pytest-slice6/{run_id}/...` path namespace and remain in the table. |
| 5 | D5 / Task 8 referenced `tests/fixtures/calibration_v1/`; the real layout is `tests/calibration/{fixtures,baselines,labels.yaml}/`. | D5 + Task 0 + Task 8 + the file-structure table point at `tests/calibration/...` paths. Ledger envelopes land under `tests/calibration/fixtures_ledger/<wallet>_2026-04-30.json`. The runner asserts against `labels.yaml`'s `expected.overall_verdict` rather than a parallel JSON baseline file. |

---

## Tail Items From Slice 5

These land first, before any Slice 6 task. They are small, risk-bounded,
and unblock Slice 6's calibration re-baseline (which seeds non-trivial
fixtures that exercise the same code paths).

### TI-1: positions.py price-fallback truthiness

**Problem.** `sniper/analysis/wallet_protocol/ledger/positions.py:155`
uses `_float_or_none(row.get("curPrice") or row.get("current_price"))`.
`0.0` is a perfectly valid resolved-NO price; the `or` short-circuits it
to `None` and the row stops looking held-to-resolution. Slice 5 Task 8
worked around this in the integration fixture (`0.005` / `0.99` instead
of `0.0` / `1.0`); the fix has to land before re-baseline because the
calibration wallets contain real `curPrice == 0.0` resolutions.

The same bug pattern exists for any other field that can legitimately be
`0.0` / `0` / `""`. Audit and fix every site.

**Files:**
- Modify: `sniper/analysis/wallet_protocol/ledger/positions.py`
- Modify: `tests/test_wallet_ledger_positions.py`

**Tasks:**

- [ ] **Audit.** Grep for `or row.get(` inside `positions.py` and any
  other ledger writer (`events.py`, `flips.py`, `probes.py`). For each
  site, decide:
  * Is the alternate field a true alias for the same data (correct to
    use `is not None` precedence)?
  * Or is the chain a cascade of distinct fields (current behaviour
    correct)?
  Record findings inline as a comment on each kept `or` so future
  auditors do not re-flag it.
- [ ] **Failing test:** `test_curPrice_zero_persists_as_zero` — seed a
  position with `curPrice=0.0`, persist via `upsert_position_snapshots`,
  read back via `materialize_wallet_data`, assert
  `closed_position["curPrice"] == 0.0`.
- [ ] **Failing test:** `test_curPrice_falls_back_to_current_price_when_curPrice_absent` —
  seed a position with `current_price=0.42` and no `curPrice`; assert
  the persisted row preserves `0.42`.
- [ ] **Failing test:** `test_avgPrice_zero_persists_as_zero` —
  same pattern for `avgPrice`. Defensive: a `0.0` average price is rare
  but possible in degenerate data.
- [ ] **Implement.** Replace each truthiness fallback with an explicit
  `is not None` selector. Pattern:
  ```python
  cur_price = row.get("curPrice")
  if cur_price is None:
      cur_price = row.get("current_price")
  ```
  Inline-helper acceptable if it improves readability. No `or`
  short-circuits on numeric / bool / string fields.
- [ ] Re-run the Slice 5 integration test with the writer fix in place
  AND the integration fixture flipped back to `curPrice=0.0` /
  `curPrice=1.0`. The existing assertion that H2/H3 carry
  `market_structure_unsupported` MUST still hold; this confirms the
  writer fix does not break held-to-resolution filtering on `0.0` /
  `1.0` data.
- [ ] **Commit:** `fix(wallet-ledger): persist 0.0 prices instead of
  short-circuiting via or`.

### TI-2: closed_at → timestamp alias — read-side or persisted?

**Problem.** Slice 5 added the alias at materializer normalization time
(`materialize.py:_normalize_for_walletdata`) so the protocol reads
`row.get("timestamp")` against ledger-materialized closed positions.
Question: is read-side the right boundary, or should the writer also
expose the alias on the DuckDB row directly?

**Decision (default).** Keep the alias **read-side only**. Rationale:

* The DuckDB schema (`wallet_positions.closed_at`) is the canonical
  ledger column. Mutating the schema to also store a redundant
  `timestamp` column duplicates state and creates a drift surface.
* No current consumer reads `closed_at` from the warehouse and expects
  a `timestamp` field on the same row. Materializer normalization is
  the only translation point.
* Read-side normalization is reversible; a future caller that needs
  the persisted alias can lift the rule into the writer without
  rewriting the warehouse.

**Tasks:**

- [ ] Document the decision in spec §8 as a short paragraph: "The DuckDB
  surface stores `closed_at`. The Polymarket-API-shape `timestamp`
  alias is added at read time by the materializer, not at write time
  by the position writer. Future callers that need the persisted alias
  must propose lifting the rule into the writer."
- [ ] **Failing test:** add `tests/test_wallet_ledger_alias_boundary.py`
  with two narrow assertions — (a) the warehouse row reads back with
  `closed_at` and NOT `timestamp` set, (b) the materialized closed
  position dict has BOTH `closed_at` and `timestamp` populated and
  equal. Pins the contract so a future writer-side fix would be a
  conscious change.
- [ ] **Commit:** `docs(wallet-ledger): document closed_at to timestamp
  alias boundary`.

**Re-evaluate if:** any new caller (Slice 6 Supabase writer, fixture
exporter, calibration re-baseline) needs the persisted alias. If so,
flip to writer-side normalization in a dedicated task and migrate the
warehouse with a `ALTER TABLE … ADD COLUMN` + backfill from `closed_at`.
Slice 6 plan currently assumes none do.

---

## Slice 6 Design Decisions (Locked)

### D1. Output paths — three writers, one signature shape

Three Supabase tables, three writer functions. Create paths use
**insert-ignore** semantics — they never overwrite. Updates / status
flips are a separate `mark_superseded(...)` PATCH helper (D3).

```python
def insert_<output>(
    *,
    wallet: str,
    capture_run_id: str,
    window_id: str,
    payload: <typed dataclass>,
    supabase: SupabaseHttp,
) -> int:
    """Insert if absent, no-op on conflict.

    Returns 1 on insert, 0 on conflict — never raises on conflict.
    Conflict is detected by the table's natural-key primary key
    (D2). The writer never UPDATEs an existing row; corrective
    writes go through `mark_superseded(...)`.
    """
```

* `wallet_analysis_results` — one row per `(wallet, run_id, window_id)`,
  carrying the full `ProtocolReport` JSON, indexed summary columns,
  artifact hashes, local warehouse path, schema/code versions.
* `wallet_findings` — N rows per analysis. Every completed analysis
  writes ≥ 1 finding, even when the finding is `no_edge`,
  `data_insufficient`, or `unsupported_market_structure`. Findings
  derive from the `ProtocolReport`; the deriver is pure and offline.
* `artifact_registry` — content-addressed pointers to local artifacts
  (wallet DuckDB file path + sha256, market warehouse snapshot,
  fixture export, CSV/Parquet exports, per-run report blobs).

REST shape: writers send `Prefer: resolution=ignore-duplicates,
return=representation` — that is the PostgREST insert-ignore
behaviour. **No `merge-duplicates` header on any create path** — that
header silently UPDATEs the existing row, which contradicts the
append-only model in D3.

### D2. Idempotency model — natural keys + content hashes

Every write is idempotent by primary key. Re-running an analysis
with the same `(wallet, run_id, window_id)` produces a no-op insert —
the writer's return value is `0`, the row stays unchanged, and the
caller observes "the row I asked to insert was already present".

`wallet_findings` adds `finding_uid = sha256(wallet | run_id |
window_id | finding_type | source_hypothesis_id)` so re-deriving the
same findings is a no-op. A code-version bump that changes finding
contents requires a new `run_id` (same wallet, fresh capture run);
this matches Slice 1 / Slice 2's invariant that `run_id` is the
immutable identity of a capture cycle.

`artifact_registry` keys on `content_hash`. Two paths to the same
content do not produce duplicate rows.

### D3. Corrective writes — supersede, don't overwrite (results + findings only)

Create paths never overwrite. Corrective writes go through a
**separate** `mark_superseded(...)` PATCH helper that updates
`status` on existing rows. Two tables only:

* `wallet_analysis_results` carries `status: 'active' | 'superseded' |
  'rolled_back'` plus `superseded_by_run_id`.
* `wallet_findings` carries `status: 'active' | 'superseded' |
  'rolled_back'` plus `superseded_by_finding_uid`. Default reads
  (`select_active_findings(...)`) filter to `status='active'`.

`artifact_registry` has **append-only content rows and no `status`
column**. Precise contract:

* Each row is keyed by `content_hash`. Once written, the row's
  identity (key + content) never changes — that is the append-only
  guarantee.
* `paths` is **mutable metadata** on an existing row: a second
  `upsert_artifact_registry_entry(...)` call with the same
  `content_hash` but a different filesystem path PATCHes the row's
  `paths` JSONB array to include the new path. This is metadata
  enrichment, not supersede — the row's logical identity (its
  content) is unchanged. New content always gets a new row.
* There is no `status` column, so there is no supersede semantics
  on artifacts. `mark_superseded(table="artifact_registry", ...)`
  raises `ValueError`. See Task 9 for the live-test cleanup
  strategy that respects this invariant (synthetic-namespace
  identity, no row deletion or status flip).

The supersede helper signature:

```python
def mark_superseded(
    *,
    table: Literal["wallet_analysis_results", "wallet_findings"],
    natural_key: dict,                      # {wallet, run_id, window_id, ...}
    superseded_by: str,                     # new run_id or finding_uid
    supabase: SupabaseHttp,
) -> int:
    """PATCH `status` on every active row matching `natural_key`."""
```

Only those two tables are valid `table` values; `artifact_registry`
raises `ValueError` if passed. Hard delete is reserved for the
migration cleanup window (per the brain-hygiene policy in
`CLAUDE.md`).

### D4. Live vs offline split — exact gate

Three categories:

| Category | Path | Trigger |
|---|---|---|
| **Offline (default)** | Materialize → derive findings → emit Supabase payloads → assert against stub HTTP. | Always-on; runs in CI. |
| **Live Supabase write** | All of offline, then send the payloads to a live project. | `CALIBRATION_LIVE_SUPABASE_URL` AND `CALIBRATION_LIVE_SUPABASE_KEY` set; otherwise tests skip with a clear reason. |
| **Live capture** | Run Slice 1 / Slice 2 / Slice 3 fetchers against Polymarket APIs. | Out of Slice 6 scope — a separate runbook. Calibration re-baseline reads the seeded warehouse only. |

Offline path is the contract. Live tests are smoke tests that do not
gate CI.

### D5. Calibration re-baseline — fixture v3 from ledger, not live capture

Real layout (Task 0 must reconfirm but the v2 plan defaults to it):

* `tests/calibration/fixtures/<wallet>_2026-04-30.json` — 10 v2
  fixtures, captured 2026-04-30 against the live API.
* `tests/calibration/labels.yaml` — `calibration_version: "2"` —
  per-wallet `expected` `overall_verdict` plus `operator_prior`.
  This is the v1/v2 baseline the re-baseline asserts against; there
  is no separate JSON-baseline-per-wallet file.
* `tests/calibration/baselines/swissmiss_phase0_informational.json`
  — narrow Phase-0 baseline for one wallet only; not the general
  baseline format.
* `tests/calibration/test_calibration_set.py` — existing offline
  runner that reads the fixtures + labels and asserts protocol
  output.

The 10 calibration wallets need fresh inputs whose ledger-materialized
verdict matches the captured-via-live-API verdict pinned in
`labels.yaml`. Slice 6 treats this as a deterministic fixture export,
not a live re-fetch:

1. **Ledger fixture emitter** reads ledger materialization for each
   wallet and writes a JSON envelope per wallet under
   `tests/calibration/fixtures_ledger/<wallet>_2026-04-30.json` —
   parallel to the existing `fixtures/` tree, **not** replacing it.
   The envelope shape extends the live-API shape with
   `replay_surface_meta` and `manifest_hashes`.
2. **Re-baseline runner** lives at
   `tests/calibration/test_ledger_calibration_set.py` (sibling of
   the live-API runner). It loads each ledger envelope, runs
   `analyze_wallet` under the calibration harness flags
   (`--no-tags --no-peers --no-exec --no-history --no-archetype`,
   `seed=1`, `n_iter=2000`, exactly matching `labels.yaml`'s
   reproduction recipe), and asserts the resulting
   `ProtocolReport.overall_verdict` matches the `labels.yaml`
   `expected` field for the same wallet.
3. Headline counts (`gate_result`, `n_closed_positions`,
   `held_analyst_branch.is_active`, branch BH-pool verdicts) must
   also match. Bootstrap CI fields are compared to two-decimal
   precision (rationale documented inline next to the assertion).
4. The runner does NOT call live APIs. The DuckDB warehouse for
   each wallet is seeded into the fixture tree as a small
   `<wallet>.duckdb` file alongside the JSON envelope (only the
   rows that fall inside the calibration window).

If a wallet's ledger verdict differs from `labels.yaml`'s `expected`,
the runner records the divergence in
`tests/calibration/fixtures_ledger/divergences.md` with the wallet,
diverging field, expected value, ledger value, and a one-sentence
hypothesis. The diverging assertion is `pytest.mark.skip`-ed with a
reason that cites the divergence file. **Do NOT silently update
`labels.yaml`** — a real divergence is operator-reviewed material.

Naming convention: this is the **third** calibration label set in
practice (label set v2 is the live-API baseline; "v3 / ledger" is
this slice's output). Slice 6 does NOT renumber `calibration_version`
in `labels.yaml`; that is a separate operator decision after
divergence review.

### D6. SupabaseHttp injection — explicit constructor, no shared globals

`bot.supabase_helper` reads `SUPABASE_URL` / `SUPABASE_SERVICE_KEY`
/ `SUPABASE_ANON_KEY` from `os.environ` **at import time** (see
`bot/supabase_helper.py:71`). Reusing that path for Slice 6 would
couple the live writer to whatever globals happen to be set in the
test process — a leak surface that defeats the offline contract.

Slice 6 introduces an explicit constructor with no global reads:

```python
class RestSupabaseHttp:
    def __init__(
        self,
        *,
        url: str,                                 # base REST URL
        key: str,                                 # service-role key
        transport: HTTPTransport | None = None,   # injectable for tests
    ): ...

    def insert_ignore(
        self,
        table: str,
        rows: list[dict],
        *,
        natural_key_columns: tuple[str, ...],
    ) -> int: ...

    def patch(
        self,
        table: str,
        *,
        where: dict,
        set_columns: dict,
    ) -> int: ...

    def select(
        self,
        table: str,
        *,
        where: dict,
        columns: tuple[str, ...] | None = None,
    ) -> list[dict]: ...
```

* The `url` / `key` arguments are **always** passed explicitly. There
  is no fallback to environment variables inside the class.
* Calibration env vars (`CALIBRATION_LIVE_SUPABASE_URL` /
  `CALIBRATION_LIVE_SUPABASE_KEY`) are read **only** by the live
  test fixtures (Task 9). The fixtures construct the
  `RestSupabaseHttp` instance from those values and inject it
  into the writers under test. They do NOT mutate
  `os.environ` and do NOT reach into `bot.supabase_helper`'s globals.
* Production callers (e.g. a future runtime that wants to write
  results from the VPS) must pass the URL / key explicitly the
  same way. If we later need a project-wide convenience constructor,
  it lives in a separate caller module and is gated by an env-name
  contract documented at that callsite, not inside `RestSupabaseHttp`.

`StubSupabaseHttp` mirrors the same surface in memory:

* `calls: list[tuple[str, str, dict]]` — every request recorded as
  `(method, table, payload_or_filter)`. Length grows on every call;
  no de-duplication.
* `rows_by_table: dict[str, list[dict]]` — the simulated row store.
  `insert_ignore` adds a row only when the natural-key tuple is
  not already present, returning `0` in the no-op case.

Output writer modules import `SupabaseHttp` only. They never import
`bot.supabase_helper`, `urllib.request`, `requests`, or `httpx`.
That invariant is enforced by an offline grep test (see Task 2 and
Slice 6 Verification).

### D7. Schema migrations — three new Supabase tables

Three new SQL migrations under `migrations/`:

* `supabase_016_wallet_analysis_results.sql`
* `supabase_017_wallet_findings.sql`
* `supabase_018_artifact_registry.sql`

Each migration:

* Creates the table with `IF NOT EXISTS`.
* Creates the natural-key primary key.
* Adds `created_at` / `updated_at` audit columns.
* Adds the `status` column where the rollback model needs it (D3).
* Enables RLS with the same operator-only policy used by
  `agent_runs` / `memories` (see `supabase_005_rls_scope_all.sql`).
* Adds indexes only for the read patterns the spec calls out;
  speculative indexes are explicitly out of scope.

Migrations are idempotent (`CREATE TABLE IF NOT EXISTS`,
`CREATE INDEX IF NOT EXISTS`, `CREATE POLICY ... IF NOT EXISTS` via
the project's existing migration helper if available, or `DROP
POLICY IF EXISTS ... CREATE POLICY ...` otherwise).

### D8. Findings deriver — pure, offline, exhaustive

`derive_findings_from_report(report: ProtocolReport, *, wallet: str,
window_id: str, run_id: str) -> list[Finding]` is pure. Inputs in,
findings out. No I/O, no Supabase, no DuckDB.

Every analysis emits at least one finding. Coverage matrix:

| ProtocolReport state | Emitted finding type |
|---|---|
| Stage 1 reject (any reason) | `data_insufficient` (severity=info) |
| `overall_verdict='no_edge'` | `no_edge` (severity=info) |
| `overall_verdict='underpowered'` | `underpowered` (severity=info) |
| `overall_verdict='operator_review'` | `operator_review` (severity=warn) |
| `overall_verdict='edge_detected'` | one finding per supported hypothesis (severity=insight) |
| Any branch hypothesis underpowered with `market_structure_unsupported` | `unsupported_market_structure` (severity=warn) |
| Any branch hypothesis underpowered with `outcome_index_missing` | `outcome_index_missing` (severity=warn) |

Finding payload fields are spec'd in §15 of the design doc; the
deriver populates `numeric_evidence_json` with the indexed summary
fields the LLM-input layer (Phase 2D) will read.

---

## File Structure

| Path | Slice | Responsibility |
|---|---|---|
| `sniper/analysis/wallet_protocol/ledger/positions.py` | modify (TI-1) | Replace truthiness fallbacks with explicit `is not None`. |
| `sniper/analysis/wallet_protocol/ledger/supabase_io.py` | create | `SupabaseHttp` protocol + `RestSupabaseHttp` + `StubSupabaseHttp`. |
| `sniper/analysis/wallet_protocol/ledger/results_writer.py` | create | `upsert_wallet_analysis_result(...)`. |
| `sniper/analysis/wallet_protocol/ledger/findings.py` | create | `derive_findings_from_report(...)` + `Finding` dataclass + `upsert_findings(...)`. |
| `sniper/analysis/wallet_protocol/ledger/artifacts.py` | create | `upsert_artifact_registry_entry(...)` + content-hashing helpers. |
| `sniper/analysis/wallet_protocol/ledger/fixture_export.py` | create | `export_calibration_fixture(...)`. Materializes a wallet, dumps a JSON envelope, copies a trimmed DuckDB snapshot. |
| `migrations/supabase_016_wallet_analysis_results.sql` | create | Table + PK + RLS. |
| `migrations/supabase_017_wallet_findings.sql` | create | Table + PK + RLS + status column + index on `(wallet, run_id, window_id, status)`. |
| `migrations/supabase_018_artifact_registry.sql` | create | Table + PK on `content_hash` + RLS. |
| `tests/test_wallet_ledger_positions.py` | modify (TI-1) | Add zero-price persistence regressions. |
| `tests/test_wallet_ledger_alias_boundary.py` | create (TI-2) | Pin the read-side-only alias contract. |
| `tests/test_wallet_ledger_supabase_io.py` | create | StubSupabaseHttp behaviour + `RestSupabaseHttp` request shape (no network). |
| `tests/test_wallet_ledger_results_writer.py` | create | `wallet_analysis_results` payload shape + idempotency under stub. |
| `tests/test_wallet_ledger_findings_deriver.py` | create | Pure-function tests over canned `ProtocolReport` shapes; full coverage matrix from D8. |
| `tests/test_wallet_ledger_findings_writer.py` | create | `upsert_findings` idempotency + supersede flow. |
| `tests/test_wallet_ledger_artifacts.py` | create | content-hash dedup + idempotency. |
| `tests/test_wallet_ledger_fixture_export.py` | create | Round-trip: ledger → fixture → re-load → analyze produces equal `ProtocolReport`. |
| `tests/calibration/test_ledger_calibration_set.py` | create | Ledger-materialized verdicts vs `labels.yaml` for the 10 calibration wallets (offline). |
| `tests/test_wallet_ledger_supabase_live.py` | create | LIVE-only smoke writes. Skipped unless both env vars set. |
| `tests/calibration/fixtures_ledger/<wallet>_2026-04-30.json` × 10 | create | Ledger-materialized envelopes — checked in with each wallet's trimmed `<wallet>.duckdb`. |
| `tests/calibration/fixtures_ledger/<wallet>_2026-04-30.duckdb` × 10 | create | Trimmed warehouse snapshots, kept small. |
| `tests/calibration/fixtures_ledger/divergences.md` | create-on-demand | Operator-review log when a ledger verdict diverges from `labels.yaml`. |
| `docs/superpowers/specs/2026-05-02-wallet-fact-ledger-design.md` | modify | Document TI-2 boundary in §8; document D2/D3 idempotency + rollback in §15. |

---

## Tasks

### Task 0: Pre-flight — confirm calibration baseline shape

**Files:** none (read-only).

- [ ] Re-read `tests/calibration/labels.yaml` and confirm
  `calibration_version: "2"`, the 10 wallets listed, and the
  reproduction recipe (`--no-tags --no-peers --no-exec --no-history
  --no-archetype`, seed, `n_iter`).
- [ ] Confirm 10 fixture files under `tests/calibration/fixtures/`
  named `<wallet>_2026-04-30.json`. Note the exact wallet addresses
  for the implementation tasks.
- [ ] Re-read `tests/calibration/test_calibration_set.py` to capture
  the harness pattern (analyze-wallet kwargs, assertion loop). The
  ledger runner mirrors that pattern.
- [ ] If any of the above is missing or has changed shape since the
  plan was written, surface as a BLOCK and stop — do NOT skip ahead
  to Task 1.
- [ ] No code changes. No commit.

### Task TI-1: Fix positions.py price-fallback truthiness

See **TI-1** above. Single commit, three failing tests, audit comment
added next to every kept `or` chain.

### Task TI-2: Document closed_at → timestamp alias boundary

See **TI-2** above. Single commit, spec amendment + one alias-boundary
test.

### Task 1: Supabase migrations

**Files:**
- Create: `migrations/supabase_016_wallet_analysis_results.sql`
- Create: `migrations/supabase_017_wallet_findings.sql`
- Create: `migrations/supabase_018_artifact_registry.sql`

Schema invariants per D3:

* `wallet_analysis_results` has columns `status TEXT NOT NULL DEFAULT
  'active'` (CHECK in `('active','superseded','rolled_back')`) and
  `superseded_by_run_id TEXT`.
* `wallet_findings` has `status` (same CHECK) and
  `superseded_by_finding_uid TEXT`. Plus
  `finding_schema_version INTEGER NOT NULL DEFAULT 1` so future
  matrix changes don't silently break the LLM-input layer.
* `artifact_registry` has **NO `status` column** — append-only.
  Carries `paths JSONB NOT NULL DEFAULT '[]'::jsonb` so the path-set
  enrichment in Task 6 has a column to write to.
* All three carry `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
* The two with `status` carry `updated_at TIMESTAMPTZ NOT NULL
  DEFAULT now()` because the supersede helper PATCHes them.

- [ ] Write the three SQL files. Each is `IF NOT EXISTS`-safe and
  isolated (no cross-table FKs in this slice).
- [ ] Add a small `tests/test_supabase_migrations_lint.py` that
  parses the SQL with `sqlparse` (already a dep) and asserts each
  migration:
  * Contains exactly one `CREATE TABLE IF NOT EXISTS`.
  * Has a `PRIMARY KEY` clause.
  * Has at least one `ENABLE ROW LEVEL SECURITY` line.
  * `wallet_analysis_results` and `wallet_findings` define a
    `status` column with the three-value CHECK constraint.
  * `artifact_registry` does NOT define a `status` column.
  Pure offline; no Supabase round-trip.
- [ ] Apply the migrations via `mcp__supabase__apply_migration` against
  the project's branch (NOT against main yet) and capture the apply
  output in the plan amendment for audit.
- [ ] **Commit:** `feat(wallet-ledger): add Supabase migrations for
  analysis results, findings, artifacts`.

### Task 2: SupabaseHttp protocol + Stub + REST adapter

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/supabase_io.py`
- Create: `tests/test_wallet_ledger_supabase_io.py`

- [ ] Failing tests for `StubSupabaseHttp`:
  * Every method appends to `calls: list[tuple[str, str, dict]]` —
    `("insert_ignore", table, payload)`, `("patch", table, {"where":
    where, "set": set_columns})`, etc. **The list grows on every
    call**; `calls` is a request log, not a deduplicated state
    store. A second `insert_ignore` call with the same natural key
    appends another `("insert_ignore", ...)` entry to `calls`.
  * `insert_ignore` returns `1` when the natural-key tuple is
    absent from `rows_by_table[table]` and the row was added; `0`
    when present (no-op insert). On the no-op return, the row store
    is unchanged.
  * `select` reads from `rows_by_table[table]` filtered by `where`
    and returns rows previously inserted via `insert_ignore` or
    written via `patch`.
  * `patch` updates `rows_by_table[table]` in place and returns the
    number of rows touched.
- [ ] Failing tests for `RestSupabaseHttp`:
  * `insert_ignore` builds the correct REST URL, sends `Prefer:
    resolution=ignore-duplicates,return=representation`, and posts
    a JSON body — verified against an in-process fake
    `HTTPTransport` (no real network).
  * `patch` builds the correct REST URL with PostgREST `?col=eq.value`
    filters, sends `PATCH` with the `set_columns` body, and uses
    no `merge-duplicates` header anywhere.
  * `select` builds GET with `?col=eq.value` filters and an optional
    `?select=col1,col2` projection.
  * Constructor reads `url` and `key` from arguments only — no
    `os.environ` read inside the class. Verified by passing
    placeholder env values that would mismatch the constructor args
    and asserting the constructor args win.
- [ ] Module grep: writer modules MUST NOT import
  `bot.supabase_helper`, `urllib.request`, `requests`, or `httpx`.
  Only `supabase_io.py` is allowed those imports — verified by a
  grep test on every other ledger module added in this slice.
- [ ] Implement.
- [ ] **Commit:** `feat(wallet-ledger): add SupabaseHttp injection
  surface`.

### Task 3: Results writer (insert-ignore)

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/results_writer.py`
- Create: `tests/test_wallet_ledger_results_writer.py`

- [ ] Failing tests:
  * `insert_wallet_analysis_result` with a synthetic
    `ProtocolReport` against a `StubSupabaseHttp` produces one
    `("insert_ignore", "wallet_analysis_results", payload)` entry
    in `stub.calls`. Returns `1`. The row appears in
    `stub.rows_by_table["wallet_analysis_results"]`.
  * **Idempotency contract.** Re-running with the same `(wallet,
    run_id, window_id)`:
    - `stub.calls` length grows by exactly one (the second request
      is recorded).
    - The writer's return value is `0` (no-op insert).
    - `stub.rows_by_table["wallet_analysis_results"]` length is
      unchanged — the row store is exactly one row.
  * Indexed summary columns (`overall_verdict`, `gate_result`,
    `trading_mode`, `n_closed_positions`, `dw_roi_point` etc.) are
    populated correctly from the report.
  * `replay_surface_meta` from the materialized bundle is included
    on the row so the durable record carries provenance.
  * The writer DOES NOT call `patch`; corrective writes are
    `mark_superseded(...)`'s job (Task 5).
- [ ] Implement.
- [ ] **Commit:** `feat(wallet-ledger): add wallet_analysis_results
  insert-ignore writer`.

### Task 4: Findings deriver

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/findings.py` (deriver
  half only; writer in Task 5).
- Create: `tests/test_wallet_ledger_findings_deriver.py`

- [ ] Failing tests, one per row of the D8 coverage matrix. Drive
  with hand-built `ProtocolReport` objects so the test does not
  depend on `analyze_wallet`. Every matrix row asserts:
  * Finding count is correct (≥ 1 per analysis).
  * Each finding's `finding_type` matches the matrix.
  * Each finding's `severity` matches the matrix.
  * Each finding's `numeric_evidence_json` is a JSON-serialisable dict
    populated from the report's indexed summary fields.
  * `finding_uid` is a sha256 hex string and stable across re-runs.
- [ ] Failing test: re-deriving from the same report yields findings
  with byte-equal `finding_uid`s in the same order.
- [ ] Implement.
- [ ] **Commit:** `feat(wallet-ledger): add findings deriver`.

### Task 5: Findings writer + supersede helper

**Files:**
- Modify: `sniper/analysis/wallet_protocol/ledger/findings.py`
- Create: `tests/test_wallet_ledger_findings_writer.py`

- [ ] Failing tests for the create path:
  * `insert_findings(report, wallet, run_id, window_id, supabase)`
    against a stub appends N `insert_ignore` entries to
    `stub.calls`, one per finding the deriver returned. Returns
    the count of NEW rows inserted.
  * Idempotency: re-running with the same `(wallet, run_id,
    window_id)` appends another N `insert_ignore` entries to
    `stub.calls`, returns `0` (no new rows), and leaves
    `stub.rows_by_table["wallet_findings"]` length unchanged.
- [ ] Failing tests for the supersede path:
  * `mark_superseded(table="wallet_findings", natural_key={"wallet":
    ..., "run_id": ..., "window_id": ...}, superseded_by=...,
    supabase=...)` issues exactly one `patch` call against the stub
    with the expected `where` filter and `set_columns =
    {"status": "superseded", "superseded_by_finding_uid": ...,
    "updated_at": <now>}`.
  * Returns the number of rows touched.
  * `mark_superseded(table="artifact_registry", ...)` raises
    `ValueError` — artifact rows have no supersede status (D3).
  * Default reads (`select_active_findings(...)`) emit a `select`
    call with `where={"wallet": ..., "status": "active"}`.
- [ ] Implement.
- [ ] **Commit:** `feat(wallet-ledger): add findings writer and
  supersede helper`.

### Task 6: Artifact registry writer (insert-ignore, append-only)

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/artifacts.py`
- Create: `tests/test_wallet_ledger_artifacts.py`

- [ ] Failing tests:
  * `upsert_artifact_registry_entry(path, *, kind, supabase,
    content_hash=None)` computes a sha256 over the file contents
    when `content_hash` is None.
  * Idempotent on `content_hash`: same content → second
    `insert_ignore` call returns 0 and the row store length is
    unchanged. `stub.calls` length grows (request recorded).
  * Path-set semantics: when a second call lands with the SAME
    `content_hash` but a different `path`, the existing row is
    PATCHed to append the new path to its `paths` JSONB array. This
    is NOT a status flip and is NOT a supersede — it is a benign
    metadata enrichment. Implementation detail: this is the only
    place in Slice 6 where artifact rows accept a write after
    insert; pin the rule in the test.
  * Allowed `kind` set: `wallet_warehouse`, `market_warehouse`,
    `fixture_export`, `report_blob`, `csv_export`. Unknown kind
    raises `ValueError`.
  * `mark_superseded(table="artifact_registry", ...)` raises
    `ValueError` (D3 — append-only, no status flip).
- [ ] Implement. The artifact table has no `status` column; the
  migration in Task 1 reflects this.
- [ ] **Commit:** `feat(wallet-ledger): add artifact registry writer`.

### Task 7: Fixture v2 exporter / loader (MaterializedWalletData round-trip)

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/fixture_export.py`
- Create: `tests/test_wallet_ledger_fixture_export.py`

#### Round-trip surface (audit fix)

The audit caught that v2's loader claim was wrong: `WalletData` has
no `to_analyze_wallet_kwargs()`; only `MaterializedWalletData`
does (`sniper/analysis/wallet_protocol/ledger/materialize.py:53`).
Slice 6's exporter / loader operate on the bundle, not the inner
dataclass:

* `export_calibration_fixture(bundle: MaterializedWalletData,
  output_path: Path) -> Path` — serialises every non-callable
  surface (`wallet_data` via `dataclasses.asdict`, `window`,
  `replay_surface_meta`, `manifest_hashes`, `row_counts`) plus the
  raw price-bar list needed to rebuild `price_history_fn`. Output
  is one JSON file.
* `load_calibration_fixture(path: Path) -> MaterializedWalletData`
  — reverses the export. Re-hydrates `WalletData`, re-builds
  `price_history_fn` from the persisted bar list using
  `price_history.make_ledger_price_history_fn`-equivalent in-memory
  closure, leaves `peer_baseline_fn` / `leafs_by_cid` / `nav_lookup`
  as `None` (Slice 5 design). Returns a bundle whose
  `to_analyze_wallet_kwargs()` is splat-ready.

#### Tasks

- [ ] Failing tests:
  * `export_calibration_fixture(bundle, output_path)` writes the
    JSON envelope. Required keys: `wallet_data`, `window`,
    `replay_surface_meta`, `manifest_hashes`, `row_counts`,
    `price_bars` (list of `{token_id, ts, price, source_version}`
    rows trimmed to the window plus the forward pad).
  * `load_calibration_fixture(path)` returns
    `isinstance(bundle, MaterializedWalletData)`.
  * `bundle.to_analyze_wallet_kwargs()` returns the same five keys
    the materializer produces (`pre_fetched_data`,
    `price_history_fn`, `peer_baseline_fn`, `leafs_by_cid`,
    `nav_lookup`); peer / leaf / nav stay `None`.
  * Round-trip: starting bundle → export → load → analyze_wallet.
    The resulting `ProtocolReport` is `==` to running
    `analyze_wallet` directly on the original bundle. Compare via
    `dataclasses.asdict` so callable surfaces don't trip `==`.
  * `bundle.price_history_fn(token_id, ts)` after re-load returns
    the same `float | None` as the pre-export closure for every
    token / ts pair on the bar surface. Pinned with at least one
    token's full bar list and one out-of-window probe.
  * The exporter and loader call no live APIs — module grep for
    `import requests`, `import httpx`, `PolymarketClient`,
    `bot.supabase_helper`, `urllib.request`. Empty match required.
  * `output_path.parent` is created if absent. Existing files at
    the same path are overwritten only when content has changed
    (sha256 compare on the JSON bytes); identical content is a
    no-op (idempotency contract).
- [ ] Implement.
- [ ] **Commit:** `feat(wallet-ledger): export and load
  MaterializedWalletData fixtures`.

### Task 8: Calibration re-baseline (offline, ledger fixtures)

**Files:**
- Create: `tests/calibration/test_ledger_calibration_set.py`
- Create: `tests/calibration/fixtures_ledger/<wallet>_2026-04-30.json` × 10
- Create: `tests/calibration/fixtures_ledger/<wallet>_2026-04-30.duckdb` × 10
  (trimmed warehouse snapshots, kept small)
- Create-on-demand: `tests/calibration/fixtures_ledger/divergences.md`

Pre-requisite: Task 0 confirmed `tests/calibration/labels.yaml` and
the existing live-API fixture set. The 10 wallets and the harness
flags are the source of truth for both the existing
`tests/calibration/test_calibration_set.py` and this new ledger
runner.

#### Two-axis comparison (audit fix)

`labels.yaml` only carries `expected.overall_verdict`. Detailed
fields (`gate_result`, `n_closed_positions`, branch activity,
BH-pool verdicts, CIs) have **no baseline source** in the YAML —
asserting them against the YAML is a category error. Task 8
therefore performs two independent comparisons per wallet:

* **Axis A — Ledger vs live fixture (in-process).** Generate a live
  reference report by running the protocol against the existing
  `tests/calibration/fixtures/<wallet>_2026-04-30.json` in the
  same test process via the existing
  `_run_protocol_from_fixture(...)` helper (or an exact-shape
  re-implementation if importing it across packages adds a
  dependency edge we don't want). Run the same protocol against
  the ledger envelope at
  `tests/calibration/fixtures_ledger/<wallet>_2026-04-30.json`.
  Assert detailed fields ledger vs live:
  - `gate_result`
  - `n_closed_positions`
  - `held_analyst_branch.is_active`
  - per-hypothesis verdicts in the BH pool (`H2`, `H3`, `H4`,
    `H5` grain pool, `H6`, plus `FH1`–`FH4` per-grain pool / `FH6`
    for active flipper branches)
  - bootstrap CI fields to two decimal places (rationale documented
    inline next to the assertion)
* **Axis B — Ledger vs YAML.** Independently assert
  `ledger_report["overall_verdict"] == labels.yaml `
  `expected.overall_verdict` for the same wallet. The live-fixture
  report is also asserted against the same YAML row by the existing
  `test_calibration_set.py`, so both fixture sources stay anchored
  to the same static contract.

The two-axis structure means a divergence between ledger and live
(Axis A) is a different finding than a divergence between ledger
and the static expected verdict (Axis B). Both are worth logging.

#### Tasks

- [ ] Pin the 10 wallets in the test file as a list parsed from
  `tests/calibration/labels.yaml` at collection time. Treat the
  YAML as the contract; if it grows or shrinks, the test sees it.
- [ ] Add a small helper `_run_protocol_from_ledger_envelope(wallet,
  envelope_path) -> dict` that loads the ledger bundle via Task 7's
  loader, applies a **calibration-harness adapter** (see below),
  and calls `analyze_wallet(...)` with the adapted kwargs. Returns
  the parsed `ProtocolReport` as a dict via `dataclasses.asdict`.
  The CLI surface (`run_wallet_protocol`) is unchanged — keeping
  the harness in-process is the cleaner option and avoids adding a
  `--from-ledger-envelope` mode.

- [ ] **Calibration-harness adapter (audit fix #9).** The existing
  harness uses `--no-tags --no-peers --no-exec --no-history
  --no-archetype` plus `--seed 1 --n-iter 2000`. Direct splatting
  of `bundle.to_analyze_wallet_kwargs()` would let `price_history_fn`
  reach `analyze_wallet` (Task 7's loader rebuilds it from persisted
  bars) and H6/FH6 would run, contradicting `--no-history`. Add a
  small helper that mirrors the live harness suppression rules
  before the splat:

  ```python
  def _apply_calibration_harness_suppression(
      bundle: MaterializedWalletData,
  ) -> dict:
      """Mirror tests/calibration/test_calibration_set.py harness flags.

      Returns kwargs ready for `analyze_wallet(wallet, **kwargs)` with
      every surface suppressed by `--no-tags --no-peers --no-exec
      --no-history --no-archetype` set to None. Slice 5's bundle
      defaults already cover peer/leaf/nav, plus
      WalletData.execution_profile=None and
      WalletData.archetype_layer1=None for the exec/archetype
      surfaces — so today only price_history_fn needs explicit
      override. The exec/archetype overrides stay in the helper so
      a future bundle-default change cannot silently re-enable a
      suppressed surface.
      """
      kwargs = bundle.to_analyze_wallet_kwargs()
      kwargs["price_history_fn"] = None       # --no-history
      kwargs["peer_baseline_fn"] = None       # --no-peers
      kwargs["leafs_by_cid"] = None           # --no-tags
      kwargs["nav_lookup"] = None             # --no-tags
      # --no-exec / --no-archetype are enforced by the materializer
      # leaving WalletData.execution_profile / archetype_layer1 as
      # None. Pin the contract with a defensive assert so a future
      # materializer change doesn't smuggle them in.
      wd = kwargs["pre_fetched_data"]
      assert wd.execution_profile is None, (
          "calibration harness expects --no-exec; bundle smuggled in "
          "execution_profile — fix the materializer or the harness "
          "adapter, not the assertion."
      )
      assert wd.archetype_layer1 is None, (
          "calibration harness expects --no-archetype; bundle smuggled "
          "in archetype_layer1 — fix the materializer or the harness "
          "adapter, not the assertion."
      )
      return kwargs
  ```

  The direct call site is then:

  ```python
  kwargs = _apply_calibration_harness_suppression(bundle)
  report = analyze_wallet(
      bundle.wallet_data.wallet,
      seed=1,
      n_iter=2000,
      **kwargs,
  )
  ```

- [ ] **Failing adapter test.** Add a focused unit test
  (`test_calibration_harness_adapter_suppresses_history_surface`)
  that constructs a bundle with `price_history_fn` populated and
  asserts that `_apply_calibration_harness_suppression(bundle)`
  returns kwargs with `price_history_fn is None`,
  `peer_baseline_fn is None`, `leafs_by_cid is None`,
  `nav_lookup is None`. Defensive: also assert the helper raises
  `AssertionError` if `WalletData.execution_profile` or
  `archetype_layer1` is non-None (covers the exec / archetype
  surfaces against future drift).
- [ ] Failing **Axis A** test: for each wallet, run the protocol
  against both fixtures (live-API + ledger) in the same process and
  assert ledger vs live for every detailed field listed above.
- [ ] Failing **Axis B** test: for each wallet, assert
  `ledger_report["overall_verdict"] == labels.yaml expected`.
  This is a separate `pytest` parameterisation so an Axis A
  failure doesn't mask an Axis B mismatch (and vice versa).
- [ ] Implement the ledger fixtures: for each wallet, run
  `export_calibration_fixture(...)` against a seeded warehouse and
  commit both the JSON envelope and the trimmed
  `<wallet>_2026-04-30.duckdb` snapshot. Live API capture is NOT
  required at this step — the warehouse seed is the only input the
  exporter consumes.
- [ ] **Divergence log.** If any Axis A field or Axis B verdict
  diverges, append to
  `tests/calibration/fixtures_ledger/divergences.md` with the wallet,
  axis, diverging field, live / yaml value, ledger value, and a
  one-sentence hypothesis (e.g. "ledger picked a different
  post-regime subset because CUSUM signal index moved with the new
  closed_at sort"). Mark the assertion `pytest.mark.skip` with a
  reason citing the divergence file. Do NOT silently update
  `labels.yaml` and do NOT silently update the live fixture.
- [ ] **Commit:** `test(wallet-ledger): re-baseline 10 calibration
  wallets against ledger fixtures`.

### Task 9: Live writer smoke test (gated)

**Files:**
- Create: `tests/test_wallet_ledger_supabase_live.py`

- [ ] Test reads `CALIBRATION_LIVE_SUPABASE_URL` and
  `CALIBRATION_LIVE_SUPABASE_KEY` directly; SKIPs with a clear reason
  if either is missing. Constructs `RestSupabaseHttp(url=..., key=...)`
  from those values and injects it into the writers under test. Does
  NOT mutate `os.environ`. Does NOT touch
  `bot.supabase_helper`'s globals.
- [ ] **Test-namespace identity.** All live writes use a synthetic
  identity that cannot collide with real analyses:
  * Wallet: `0x0000000000000000000000000000000000005006` (the literal
    `'5006'` low byte makes the address recognisable in audits).
  * `run_id`: `pytest-slice6-{uuid4().hex}` per test run — unique
    every time, so re-running the test never hits the natural-key
    conflict path on rows from a prior run.
  * `window_id`: `pytest-slice6-window-v1`.
  * `artifact_registry.kind`: any of the spec-allowed kinds; the
    `paths` array entries are prefixed with
    `pytest-slice6/{run_id}/...` so they are visually identifiable
    and the content_hash is deterministic per run.
- [ ] Drive one synthetic minimal payload through the full Slice 6
  pipeline end-to-end:
  * `insert_wallet_analysis_result(...)` → 1 row.
  * `insert_findings(...)` → ≥ 1 row.
  * `upsert_artifact_registry_entry(...)` → 1 row per artifact kind
    exercised.
  * Read back via `RestSupabaseHttp.select(where={"wallet":
    SYNTHETIC_WALLET, "run_id": run_id})` and assert the round-trip
    matches the payload shape.
- [ ] **Cleanup is per-table, not blanket-supersede.** The cleanup
  block must respect D3:
  * `wallet_analysis_results`: `mark_superseded(table=...,
    natural_key={"wallet": SYNTHETIC_WALLET, "run_id": run_id, ...},
    superseded_by="pytest-slice6-cleanup", supabase=...)` flips the
    one row to `status='superseded'`.
  * `wallet_findings`: same pattern — supersede the run's findings
    with `superseded_by="pytest-slice6-cleanup"`.
  * `artifact_registry`: NO supersede call. Append-only invariant
    holds. The synthetic-namespace `paths` prefix is the only
    cleanup signal — operators can identify and prune
    `pytest-slice6/...` artifact rows out-of-band if quota becomes
    a concern. The test claim is "rows are identifiable", not "rows
    are removed".
  * Cleanup runs in a `finally` block so a mid-test failure still
    flips the analysis-result and findings rows to superseded.
- [ ] Final assertion (after cleanup): a `select(where={"wallet":
  SYNTHETIC_WALLET, "status": "active"})` against
  `wallet_analysis_results` AND `wallet_findings` returns zero rows
  for this run's `run_id`. The artifact_registry `select` may still
  return rows (expected — append-only).
- [ ] **Commit:** `test(wallet-ledger): add gated live Supabase
  smoke test`.

### Task 10: Wire materialize → analyze → emit pipeline

**Files:**
- Create: `sniper/analysis/wallet_protocol/ledger/run_pipeline.py`
- Create: `tests/test_wallet_ledger_run_pipeline.py`

- [ ] Failing test: `run_analysis_pipeline(wallet, window, ledger_root,
  *, supabase: SupabaseHttp = None) -> RunResult` materializes the
  bundle, runs `analyze_wallet`, derives findings, and writes them
  to `supabase` when provided. When `supabase` is None, the pipeline
  runs in dry-run mode (returns the result envelope without writing).
- [ ] Failing test: re-running the pipeline twice with the same inputs
  is a full no-op against the stub.
- [ ] Failing test: the pipeline returns a `RunResult` with
  * `report: ProtocolReport`
  * `findings: list[Finding]`
  * `artifact_paths: list[Path]`
  * `replay_surface_meta: dict`
  so callers can inspect without re-materializing.
- [ ] Implement.
- [ ] **Commit:** `feat(wallet-ledger): add ledger analysis pipeline
  driver`.

---

## Slice 6 Verification

- [ ] `python3 -m pytest tests/ -q` — green.
- [ ] `python3 scripts/validate_schema_refs.py` — clean (Supabase
  tables stay external — flag with `<!-- schema:external=<table> -->`
  in any markdown that references columns).
- [ ] `python3 scripts/brain_index.py generate` — no diff.
- [ ] No live HTTP imports in offline modules. Grep:
  ```
  grep -lE 'import requests|import httpx|from requests|from httpx|bot\.supabase_helper|urllib\.request' \
    sniper/analysis/wallet_protocol/ledger/findings.py \
    sniper/analysis/wallet_protocol/ledger/results_writer.py \
    sniper/analysis/wallet_protocol/ledger/artifacts.py \
    sniper/analysis/wallet_protocol/ledger/fixture_export.py \
    sniper/analysis/wallet_protocol/ledger/run_pipeline.py
  ```
  Returns empty. `supabase_io.py` is the single permitted touchpoint
  for `urllib`/`requests`/`httpx`-style imports.
- [ ] No `merge-duplicates` Prefer header anywhere. Grep:
  ```
  grep -rE 'resolution=merge-duplicates' \
    sniper/analysis/wallet_protocol/ledger/
  ```
  Returns empty. Insert paths use `resolution=ignore-duplicates`;
  updates go through PATCH (D1, D3).
- [ ] Idempotency contract: every writer test re-runs at least once
  and asserts:
  * The writer's return value drops to `0` on the second pass.
  * `stub.rows_by_table[table]` length is unchanged.
  * `stub.calls` length grows by exactly the number of requests the
    re-run made (the request log is not deduplicated).
- [ ] Append-only contract: artifact-registry tests assert
  `mark_superseded(table="artifact_registry", ...)` raises
  `ValueError`.
- [ ] Calibration re-baseline: 10 wallets pass OR are explicitly
  skipped with documented divergence in
  `tests/calibration/fixtures_ledger/divergences.md` (no silent
  `labels.yaml` mutation).
- [ ] Live test (Task 9): runs green when both
  `CALIBRATION_LIVE_SUPABASE_URL` and `CALIBRATION_LIVE_SUPABASE_KEY`
  are set; SKIPs cleanly otherwise. The synthetic wallet
  `0x000…5006` and per-run `pytest-slice6-{uuid}` `run_id` keep
  test rows out of the real-analysis namespace.

---

## Out Of Scope

- LLM interpretation of findings (Phase 2D, separate spec).
- Live capture infra changes — Slice 1 / Slice 2 fetchers stay frozen.
- Writer support for new finding types beyond the D8 matrix.
- Cross-wallet aggregation views (`wallet_findings_rollup`-style
  materialized views).
- Migration of the existing `agent_runs` / `agent_predictions` tables;
  Slice 6 adds new tables only.
- Multi-tenant RLS — single-operator project, same policy as existing
  Supabase tables.
- Backfilling pre-Slice-6 analyses into Supabase. The first row
  written is the first analysis run after Slice 6 lands.

---

## Risk Notes

* **Calibration v1 baseline missing.** Task 0 must confirm this. If
  absent, the prerequisite "capture v1" micro-task is the only live-API
  call in the slice and must be operator-supervised.
* **Bootstrap CI drift.** v1 vs v2 numerical comparison at two-decimal
  precision is a guess; if the H1 / H2 / H3 bootstraps land in
  different basins for any wallet, the threshold may need to widen
  per-hypothesis. Mitigation: log raw numerics to the divergences
  file even when the test passes, so we can tighten later.
* **Supabase quota.** The live smoke test writes 10 rows × 3 tables
  per run (worst case). Stays well within the project's 500 MB free
  tier even at 100x the volume.
* **Finding-deriver shape stability.** Once findings are written to
  Supabase, the deriver's payload shape becomes load-bearing for the
  Phase 2D LLM input. D8 pins the matrix now; later additions go
  through a versioned `finding_schema_version` field on
  `wallet_findings`. Plan to add that column in Task 1's migration.

# Fixture v2 Schema — Replay Surfaces (Phase 2B-capture spec)

**Date:** 2026-05-01
**Phase:** 2B-capture (between 2B-fix and 2C richer-input calibration).
**Companion audit:** `docs/audits/2026-05-01-wallet-data-completeness.md`.
**Status:** spec for operator review; implementation plan follows.

## Goal

Extend the wallet-protocol fixture format so `--from-fixture` replay can
exercise every hypothesis the live runner exercises, without any side-
channel API or DB lookup. Bump `FIXTURE_SCHEMA_VERSION` 1 → 2. Add five
optional **replay-surface** fields to `WalletData`. Add provenance
metadata for each surface. Make the runner reject any live fallback
when a fixture is loaded.

Out of scope: re-recording the 10 calibration fixtures (Phase 2C);
changes to hypothesis math (already settled by 2B-fix); changes to
`run_held_branch` / `run_flipper_branch` external kwargs (kept stable).

## Replay surfaces — definition

A **replay surface** is any input the protocol consumes that is NOT a
raw Polymarket fetch result. The five surfaces identified by the 2A
audit:

| surface | producer (live) | consumers |
|---|---|---|
| `archetype_layer1` | `scripts.wallet_archetype_extractor.classify_wallet_archetype_from_snapshot` (NEW — see Archetype capture below) | H3 hedge-archetype skip; capture-time gating for `execution_profile` |
| `execution_profile` | `exec_profile_adapter.build_execution_profile` → `scripts.execution_profile.compute_execution_profile` | H4 entry timing, FH3 spread capture (post-2B-fix: returns `underpowered (no_execution_profile)` when absent) |
| `price_history_lookups` | `price_history_adapter.build_price_history_fn` (post-2B-fix: raw-timestamp closure) | H6 (sell_ts + 24h), FH6 (raw buy_ts and sell_ts) |
| `leafs_by_cid` + `nav_lookup` | `db.fetch_market_tags_for_cids` + `db.fetch_tag_nav_category_map` | H5 / FH4 tag-grain enumeration via `tag_inference.enumerate_tag_grains` |
| `peer_baselines` | `db.peer_baseline_for_grain(conn, grain, tag, exclude_wallet=wallet)` | H5 (FH4 currently suppressed per 2B-fix Bug 1; capture for forward-compat) |

Surfaces are NOT raw Polymarket data (`closed_positions`,
`open_positions`, `activity_rows`, `capital_ledger` stay where they
are). Surfaces ARE optional — their absence means "this surface was
not captured for this wallet."

## WalletData v2

Add five optional fields to `WalletData` (`sniper/analysis/wallet_protocol/types.py:186`):

```python
@dataclass
class WalletData:
    wallet: str
    closed_positions: list[dict]
    open_positions: list[dict]
    activity_rows: list[dict]
    activity_cap_hit: bool
    archetype_layer1: str | None                 # already present
    execution_profile: dict | None               # already present
    fetched_at: int
    capital_ledger: CapitalLedger | None = None
    portfolio_value_usdc: float | None = None
    fetch_quality_flags: list[str] = field(default_factory=list)
    subgraph_fallback_used: bool = False

    # --- v2 replay surfaces ---
    price_history_lookups: dict[str, float | None] | None = None
    leafs_by_cid: dict[str, list[str]] | None = None
    nav_lookup: dict[str, str] | None = None
    peer_baselines: dict[str, list[float]] | None = None  # see schema below
    replay_surface_meta: dict[str, dict] | None = None
```

`price_history_lookups`, `leafs_by_cid`, `nav_lookup`, `peer_baselines`
default to `None` (not `{}`) so loaders can distinguish "surface not
captured" from "surface captured but empty for this wallet." All five
fields are JSON-serializable; the existing
`replay.serialize_wallet_data` / `deserialize_wallet_data` round-trip
must round-trip them too.

## FIXTURE_SCHEMA_VERSION = 2 contract

`replay.FIXTURE_SCHEMA_VERSION = 2`. The fixture JSON's
`schema_version` field gets the new value.

Backward-compat rule (operator-locked): the loader SHALL accept v1
fixtures (the 10 calibration fixtures recorded under v1) by treating
every v2 replay-surface field as `None`. v1 fixtures will be re-recorded
in 2C; we do not block 2B-capture on that.

Forward-compat rule: the loader rejects any `schema_version >
FIXTURE_SCHEMA_VERSION` with a clear error
(`f"unsupported fixture schema_version {n}; this build supports up to {FIXTURE_SCHEMA_VERSION}"`).
This already exists for v1; the rule extends.

## Rule 1 — No live fallback under `--from-fixture`

When `run_wallet_protocol.py` runs with `--from-fixture <path>`:

1. The loader hydrates a `WalletData` from the fixture.
2. The runner constructs ALL hypothesis inputs from the loaded
   `WalletData`'s replay-surface fields. Specifically:
   * `archetype_layer1`: read from `WalletData.archetype_layer1`. If
     `None`, archetype-dependent gates skip as if `--no-archetype`.
   * `execution_profile`: read from `WalletData.execution_profile`. If
     `None`, H4 / FH3 return `underpowered (no_execution_profile)`
     (FH3 fix from 2B-fix Bug 3 already does this).
   * `price_history_fn`: built from `WalletData.price_history_lookups`
     (a fixture-backed closure that reads the dict, returns `None` for
     missing keys). If `WalletData.price_history_lookups is None`,
     `price_history_fn` is also `None`; H6/FH6 return
     `underpowered (no_price_history_fn)`.
   * `leafs_by_cid` / `nav_lookup`: read directly. If either is `None`,
     `leafs_by_cid` is passed as `None` to `run_held_branch` /
     `run_flipper_branch` and tag-grain enumeration is skipped per
     existing semantics.
   * `peer_baseline_fn`: built from `WalletData.peer_baselines` (a
     fixture-backed closure with the same `(grain, tag, wallet) ->
     (mean_wr, peer_count) | None` signature). If
     `WalletData.peer_baselines is None`, `peer_baseline_fn` is `None`;
     every grain returns `inference_not_run`.
3. The runner does NOT call `build_execution_profile`,
   `build_price_history_fn`, `sqlite3.connect(args.db)`,
   `fetch_tag_lookups_from_db`, or `db.peer_baseline_for_grain` when
   `--from-fixture` is set. No exceptions.
4. The `--no-tags` / `--no-peers` / `--no-exec` / `--no-history` /
   `--no-archetype` flags become NO-OPs under `--from-fixture` (they
   already mean "don't fetch live"; under fixture, nothing fetches
   live anyway). The flags remain valid for live runs (no `--from-fixture`).

The fixture-backed closures that feed `price_history_fn` and
`peer_baseline_fn` MUST have stable signatures matching the existing
type aliases (`PriceHistoryFn`, `PeerBaselineFn`); they are constructed
at runner level (or in a new `replay.build_fixture_closures` helper)
and threaded in unchanged.

## Rule 2 — Surface provenance

`WalletData.replay_surface_meta` is a top-level dict keyed by surface
name. Each entry records how that surface was captured:

```python
replay_surface_meta = {
    "archetype_layer1": {
        "source": "scripts.wallet_archetype_extractor.classify_wallet_archetype_from_snapshot",
        "producer": "wallet_archetype_extractor (offline classifier — same snapshot as raw fields)",
        "captured_at": 1714521600,                  # unix epoch
        "status": "captured",                        # see status taxonomy below
        "input_activity_rows": 500,                  # truncated mirror of live 500-row limit
    },
    "execution_profile": {
        "source": "scripts.execution_profile.compute_execution_profile",
        "producer": "exec_profile_adapter.build_execution_profile",
        "captured_at": 1714521600,
        "status": "captured",
        "inner_status": "populated",                 # mirrors execution_profile.status
    },
    "price_history_lookups": {
        "source": "polymarket clob /prices-history",
        "producer": "price_history_adapter.build_price_history_fn",
        "captured_at": 1714521600,
        "status": "captured",
        "n_entries": 47,
        "n_missing": 3,
    },
    "leafs_by_cid": {
        "source": "db.fetch_market_tags_for_cids",
        "producer": "tag_inference.fetch_tag_lookups_from_db",
        "captured_at": 1714521600,
        "status": "captured",
        "db_path": "sniper_tracker.db",
        "n_entries": 96,
    },
    "nav_lookup": {
        "source": "db.fetch_tag_nav_category_map",
        "producer": "tag_inference.fetch_tag_lookups_from_db",
        "captured_at": 1714521600,
        "status": "captured",
        "db_path": "sniper_tracker.db",
        "n_entries": 142,
    },
    "peer_baselines": {
        "source": "db.peer_baseline_for_grain (exclude_wallet=<wallet>)",
        "producer": "db.peer_baseline_for_grain",
        "captured_at": 1714521600,
        "status": "captured",
        "db_path": "sniper_tracker.db",
        "exclude_wallet": "0xdbade4c8…",
        "n_entries": 28,
    },
}
```

**Status taxonomy** (locked):

* `captured` — capture script attempted the producer and stored the
  result, including the legitimate-zero case (e.g. zero SELLs → empty
  `price_history_lookups` dict). Field value is the populated dict
  or `{}`, NEVER `None`.
* `skipped` — capture script was instructed by an explicit `--no-*`
  CLI flag NOT to attempt the producer. Field value is `None`.
* `partial` — attempted; producer returned None for some keys (e.g.
  CLOB returned empty history for a particular `(asset_id, ts)`).
  Replay still uses what's there; missing keys behave as
  `closure → None`. Field value is the partially-populated dict.
* `failed` — capture attempted, producer raised. Field value is
  `None`. Downstream hypothesis returns underpowered/
  inference_not_run as if the surface were absent.

**Empty ≠ skipped.** A wallet with zero SELLs gets
`price_history_lookups = {}` with `status: "captured"` and
`n_entries: 0`. A wallet recorded under `--no-price-history` gets
`price_history_lookups = None` with `status: "skipped"`. Same for
`peer_baselines` and `leafs_by_cid`.

The replay closure construction in `replay.build_fixture_closures`
(see Loader changes below) keys off the field value: `None` ⇒
closure is `None` ⇒ hypothesis returns `no_*` underpowered;
`{}` ⇒ closure exists, returns None for every key ⇒ hypothesis
returns `n_below_threshold` underpowered. The error reason is the
meaningful difference: zero-SELL wallets are underpowered for "not
enough samples," not for "no closure available."

The capture script writes one `replay_surface_meta` entry per
surface, including skipped ones, so the provenance log is
exhaustive.

`replay_surface_meta` is required for every v2 fixture (the loader
rejects v2 fixtures missing this field). For v1 fixtures, it is
absent — loader treats every surface as "not captured."

## Per-surface JSON schema

### `archetype_layer1`

* JSON shape: existing `string | null`. No change.
* Producer requires `WalletData.activity_rows` and
  `WalletData.open_positions` from the same fetch snapshot.
* **Archetype capture must NOT re-fetch.** The current live entry
  point (`extract_wallet_archetype` at
  `scripts/wallet_archetype_extractor.py:1756`) calls
  `_fetch_activity_dicts(wallet, limit=500)` and
  `_fetch_positions_dicts(wallet)` internally — invoking it from the
  capture script would issue a SECOND live fetch, producing an
  archetype tied to a different snapshot moment than the one stored
  in `WalletData.activity_rows`. That breaks replay determinism.
* **Required refactor (plan-level task):** factor
  `extract_wallet_archetype` into a snapshot-input variant —
  `classify_wallet_archetype_from_snapshot(activity, positions, *,
  tag_map=None, tenure_metadata=None, wallet="", skip_gate0=True,
  with_execution=False) -> dict[str, Any]` — that takes already-fetched
  dicts and returns the same `features` shape (with
  `classification.archetype` populated). The live entry point becomes a
  thin wrapper that fetches `(activity, positions)` and forwards.
* The snapshot variant is pure (no DB, no HTTP) only when ALL FOUR
  offline-mode conditions hold:
    1. `tag_map` is a dict (e.g. `{}` for explicit no-DB). `tag_map=None`
       falls back to `_fetch_tag_map(condition_ids)`, which opens SQLite
       against `db.get_db_path()`.
    2. `tenure_metadata` is a dict (pre-fetched tenure block).
       `tenure_metadata=None` falls back to
       `_fetch_wallet_tenure_metadata(wallet)`, which makes live HTTP
       calls (`get_activity_page` + `build_session` +
       `fetch_wallet_profile_enrichment`).
    3. `skip_gate0=True` — otherwise `fetch_wallet_lifetime_stats(wallet)`
       hits live HTTP for the gate0 field.
    4. `with_execution=False` — otherwise `compute_execution_profile`
       builds a live `PolymarketClient()` and hits Polymarket HTTP.
  Replay paths (capture script + parity test + runner under
  `--from-fixture`) MUST satisfy all four. The legacy
  `extract_wallet_archetype` wrapper relies on the live fallbacks.
* The capture script invokes the snapshot variant on
  `data.activity_rows[:500]` (truncated to mirror the live extractor's
  500-row limit, so live and replay archetype labels match for the
  same wallet at the same moment) and `data.open_positions`. Replay
  reads `data.archetype_layer1` directly — no recomputation.
* Live runner (`scripts/run_wallet_protocol.py:316`) keeps its current
  call site for non-fixture runs; under `--from-fixture` it reads
  `data.archetype_layer1` and skips the call (per Rule 1).

### `execution_profile`

* JSON shape: existing `dict | null` with internal `status` field.
  No change.
* Capture script invokes `exec_profile_adapter.build_execution_profile(
  wallet, activity, closed_positions, archetype=archetype_layer1)`.
* `force=False` (default) so the legacy gate fires correctly for
  LAYER1_TERMINAL archetypes (per `exec_profile_adapter.py:8-27` C4
  amendment).

### `price_history_lookups`

* JSON shape:

```json
{
  "asset0xabc...|1700000000": 0.42,
  "asset0xabc...|1700086400": null,
  "asset0xdef...|1700050000": 0.61
}
```

* Key: `f"{asset_id}|{ts}"` (raw integer ts as decimal string,
  pipe-delimited). The `|` is forbidden in valid asset_ids and CLOB
  timestamp formats, so collision-free.
* Value: `float | null` (mid price at the bar covering `ts`, or
  `null` if the producer returned None — empty CLOB history, HTTP
  error, etc.).
* Capture-time enumeration: capture script computes the required
  `(asset_id, ts)` pairs deterministically from `activity_rows` +
  reconstructed flips:
  * For every SELL row in `activity_rows`: add `(asset, sell_ts + 24*3600)`
    (H6 input).
  * For every Flip from `reconstruct_flips`: add `(asset_id, buy_ts)`
    and `(asset_id, sell_ts)` (FH6 inputs after Bug 4 fix).
  * Dedup. Call the post-fix `price_history_fn` once per pair. Record
    every result (including `None`) into the dict.
* Reconstruction note: the capture script imports
  `flip_reconstruction.reconstruct_flips` directly so the enumerated
  pairs are exactly what the protocol will request at replay time.

### `leafs_by_cid`

* JSON shape: existing `dict[str, list[str]]`.

```json
{
  "0xcid_a": ["politics", "geopolitics"],
  "0xcid_b": ["sports", "ufc"]
}
```

* Capture-time enumeration: capture script collects every
  `condition_id` from
  `closed_positions ∪ open_positions ∪ {f.condition_id for f in reconstruct_flips(data).flips}`,
  then calls `db.fetch_market_tags_for_cids(conn, cids)` once. The
  full result is stored. Flip-only conditions matter for FH4 — even
  though FH4 is suppressed under 2B-fix Bug 1, the captured surface
  must be complete so a future flip-specific peer baseline can
  exercise FH4 without re-recording fixtures.

### `nav_lookup`

* JSON shape: existing `dict[str, str]`. Small (one entry per leaf
  tag in `tag_nav_category`); ship the whole table.

```json
{
  "geopolitics": "politics",
  "ufc": "sports"
}
```

* Capture-time: `db.fetch_tag_nav_category_map(conn)` once;
  `nav_lookup` is wallet-independent so it can be deduped across
  fixtures by future tooling, but per-fixture inclusion is the v2
  contract.

### `peer_baselines`

* JSON shape:

```json
{
  "leaf|politics": [0.612, 47],
  "leaf|sports": [0.581, 23],
  "nav|politics": [0.598, 88]
}
```

* Key: `f"{grain}|{tag}"` (composite string, pipe-delimited).
  Both `grain` and `tag` are well-formed identifiers; no `|`.
* Value: `[mean_wr, peer_count]` — list (JSON has no tuples) of
  exactly two elements. Loader converts back to tuple at deserialize
  time.
* `null` value (encoded as `null` in JSON) means the live producer
  returned `None` — peer pool empty after self-exclusion. The
  fixture-backed closure passes that through.
* **Self-exclusion is baked into the fixture.** The capture script
  passes `exclude_wallet=wallet` when invoking
  `db.peer_baseline_for_grain`. A peer_baselines map captured for
  wallet A is NOT reusable for wallet B (and the loader does not
  attempt to reuse — each fixture has its own per-wallet snapshot).
* Capture-time enumeration: for every (grain, tag) pair the wallet's
  `(closed_positions ∪ open_positions ∪ flip-derived condition_ids)`
  resolves into via `tag_inference.enumerate_tag_grains(positions,
  leafs_by_cid, nav_lookup)` — same condition-id universe as
  `leafs_by_cid` capture. Dedup. Call `db.peer_baseline_for_grain`
  once per (grain, tag) with `exclude_wallet=wallet`.

## Capture script changes (`scripts/record_fetch_snapshot.py`)

After the existing `fetch_wallet_data` call, the script:

1. Pre-fetches the snapshot classifier's two live-producer inputs ONCE
   so the classifier itself never falls back to its `tag_map=None` /
   `tenure_metadata=None` paths:
   * `archetype_tag_map = _fetch_tag_map(cids)` (SQLite)
   * `archetype_tenure  = _fetch_wallet_tenure_metadata(wallet)` (live
     HTTP — fine in capture, never in replay)

   Then invokes
   `wallet_archetype_extractor.classify_wallet_archetype_from_snapshot(
   data.activity_rows[:500], data.open_positions,
   tag_map=archetype_tag_map, tenure_metadata=archetype_tenure,
   wallet=wallet, skip_gate0=True, with_execution=False)` and stores
   `archetype_layer1` on `WalletData`. CLI flag `--no-archetype` skips
   with `status: "skipped"`.
2. Invokes `exec_profile_adapter.build_execution_profile(wallet,
   data.activity_rows, data.closed_positions,
   archetype=data.archetype_layer1)` and stores the result on
   `WalletData.execution_profile`. CLI flag
   `--no-execution-profile` skips with `status: "skipped"`.
3. Builds `price_history_fn` from the post-Bug-4 adapter, enumerates
   the required `(asset_id, ts)` pairs (per the schema above), calls
   the closure for each, populates
   `WalletData.price_history_lookups` (always a dict — `{}` when no
   pairs to capture, never `None` unless skipped). CLI flag
   `--no-price-history` skips with `status: "skipped"`.
4. Connects to `args.db` (default `sniper_tracker.db`), calls
   `fetch_tag_lookups_from_db` for `leafs_by_cid` and `nav_lookup`,
   then enumerates (grain, tag) pairs and calls
   `peer_baseline_for_grain(conn, grain, tag,
   exclude_wallet=wallet)` for `peer_baselines`. Three independent
   CLI skip flags — `--no-tags` (leafs_by_cid), `--no-nav`
   (nav_lookup), `--no-peers` (peer_baselines). Each skipped surface
   becomes `None` with `status: "skipped"`; un-skipped but
   legitimately-empty surfaces become `{}` with `status: "captured"`.
5. Populates `WalletData.replay_surface_meta` per the Rule 2 schema.
6. Calls `replay.save_fetch_snapshot(data, path)` — which round-trips
   the new fields via the updated serializer.

CLI default: ALL surfaces are captured. The six skip flags
(`--no-archetype`, `--no-execution-profile`, `--no-price-history`,
`--no-tags`, `--no-nav`, `--no-peers`) exist so 2C's re-record can be
done incrementally if a producer fails (e.g. CLOB rate-limited).
Per-surface only — no `--no-db-surfaces` mega-flag, no
`--full`/`--minimal`.

Naming overlap with the runner: `scripts/run_wallet_protocol.py`
already defines `--no-tags` / `--no-peers` / `--no-archetype` etc.
The recorder's flags share the names because they share the intent
("don't include this surface") at their respective layer; there is
no shared-flag namespace, only shared meaning.

## Loader changes (`sniper/analysis/wallet_protocol/replay.py`)

`serialize_wallet_data` adds the five replay-surface fields and
`replay_surface_meta` to the JSON payload.

`deserialize_wallet_data`:

1. If `schema_version > FIXTURE_SCHEMA_VERSION (= 2)`: raise
   `ValueError`.
2. If `schema_version == 1`: existing v1 fields populate; replay-
   surface fields default to `None`; `replay_surface_meta` is `None`.
3. If `schema_version == 2`: load v1 fields + replay-surface fields
   from the JSON. `replay_surface_meta` is REQUIRED for v2; absence
   raises `ValueError`. `peer_baselines` list values are converted
   back to tuples internally if the protocol code expects tuples
   (verify against actual consumer signature).

New helper `replay.build_fixture_closures(data: WalletData) ->
tuple[PriceHistoryFn | None, PeerBaselineFn | None]`:

* `PriceHistoryFn`: returns `None` (closure itself) when
  `data.price_history_lookups is None` (status `skipped` or
  `failed`). When the field is a dict (status `captured` or
  `partial` — including `{}`), returns a closure that looks up
  `lookups[f"{asset_id}|{ts}"]` and returns the value (which may
  itself be `None` for a captured-but-null lookup), or `None` for
  unknown keys. Empty dict ⇒ closure exists, returns None for
  every lookup ⇒ H6/FH6 underpower with `n_below_threshold`, NOT
  `no_price_history_fn`.
* `PeerBaselineFn`: returns `None` (closure itself) when
  `data.peer_baselines is None`. When the field is a dict,
  returns a closure that looks up `f"{grain}|{tag}"`; if present,
  returns the stored `(mean_wr, peer_count)` tuple (or `None` for
  captured-but-null entries). Unknown keys → `None`.

This is the operator-locked "empty ≠ skipped" rule made concrete:
loader builds the closure iff the field is a dict; the field is a
dict iff status ∈ `{captured, partial}`; status is `skipped`/`failed`
iff the field is `None`.

## Runner changes (`scripts/run_wallet_protocol.py`)

When `--from-fixture` is set, the runner:

1. Calls `load_fetch_snapshot(args.from_fixture)` to get a
   `WalletData`.
2. Builds `(price_history_fn, peer_baseline_fn) =
   replay.build_fixture_closures(data)`.
3. Reads `leafs_by_cid = data.leafs_by_cid`, `nav_lookup =
   data.nav_lookup` directly.
4. Skips ALL live calls: no `build_execution_profile`, no
   `build_price_history_fn`, no `sqlite3.connect`, no
   `fetch_tag_lookups_from_db`, no `peer_baseline_for_grain`. Comment
   block at the skip site cites Rule 1.
5. Logs each replay surface's `status` from
   `data.replay_surface_meta` to stderr so the operator can see at a
   glance what's covered.

The `--no-tags` / `--no-peers` / `--no-exec` / `--no-history` /
`--no-archetype` flags STAY but become advisory under
`--from-fixture` — the loader's surface availability is what governs.
The runner emits a stderr warning if any `--no-*` flag is set together
with `--from-fixture` (deprecated combination).

## Test surface (drives the 2B-capture plan)

Plan-level scaffolding the implementation must add:

1. `tests/test_fixture_v2_schema.py` — round-trip tests for each new
   surface; v1 → v2 backward compat; `replay_surface_meta` required
   for v2; rejection of unsupported `schema_version`.
2. `tests/test_replay_no_live_fallback.py` — assert that a fixture-
   driven run does NOT call `build_execution_profile`,
   `build_price_history_fn`, `sqlite3.connect`,
   `fetch_tag_lookups_from_db`, or `peer_baseline_for_grain`. Use
   monkeypatch to fail any of those if invoked.
3. `tests/test_record_fetch_snapshot_v2.py` — capture script writes
   v2 fixture; replay-surface fields populated for a synthetic wallet;
   skip flags work; `replay_surface_meta` is well-formed.
4. `tests/test_fixture_closures.py` — `build_fixture_closures`
   constructs price/peer closures whose lookups match the underlying
   dict, and which return `None` for missing keys.

## Migration

* v1 fixtures (the 10 calibration fixtures recorded 2026-05-01)
  remain valid. Loader treats their replay surfaces as `None`. The v2
  calibration baseline (10/10 underpowered) does NOT change because
  the harness flags already suppress every replay-surface-dependent
  hypothesis.
* Phase 2C re-records the 10 calibration fixtures as v2 with all
  surfaces captured, then re-runs the harness; the protocol may
  produce different verdicts (some wallets may lift to
  `edge_detected` if H4/H5/H6 fire). Per path-2 reframe, those new
  verdicts become the v2 calibration baseline labels (with
  `operator_prior` blocks preserved).
* Plan 2B-capture does NOT re-record. It only enables the capability.

## Out of scope

* Re-recording calibration fixtures (Phase 2C).
* New DB tables (e.g. `wallet_tag_flip_pnl` for FH4) — separate
  line item; FH4 stays suppressed per 2B-fix Bug 1.
* Adapter rewrites beyond what 2B-fix already landed.
* Protocol semantic changes — none (2B-fix already absorbed all four).
* VPS deployment of any of this — local replay capability only.

## Decisions made (no further input required unless flagged)

1. WalletData v2 (Option A) — extend WalletData, not sibling dataclass.
2. `FIXTURE_SCHEMA_VERSION = 2`.
3. v1 fixtures still load (treat replay surfaces as `None`).
4. `replay_surface_meta` is REQUIRED for v2.
5. Status taxonomy: `captured` / `skipped` / `partial` / `failed`.
   **Empty captured surface ≠ skipped.** Field value is `{}` with
   `status: "captured"` for legitimate-zero (e.g. wallet has no
   SELLs); `None` with `status: "skipped"` only for explicit `--no-*`
   skip; `None` with `status: "failed"` when the producer raised.
6. Composite-string keys for `price_history_lookups` and
   `peer_baselines` (`"asset|ts"` and `"grain|tag"`).
7. Per-surface skip flags in capture script:
   `--no-archetype`, `--no-execution-profile`,
   `--no-price-history`, `--no-tags`, `--no-nav`, `--no-peers`.
   No mega-flags. No `--no-db-surfaces`.
8. Fixture-backed closures via `replay.build_fixture_closures`.
   Closure is `None` iff the field is `None`; closure exists for
   `{}` and returns None per lookup.
9. `--no-*` flags become advisory under `--from-fixture`.
10. Self-excluded peer baselines are per-wallet; not shared. Tag /
    peer enumeration covers
    `closed_positions ∪ open_positions ∪ flip-derived condition_ids`
    so FH4 has a complete surface waiting when its baseline source
    lands.
11. Archetype is captured offline from the same snapshot. Plan adds
    `wallet_archetype_extractor.classify_wallet_archetype_from_snapshot(
    activity, positions, *, tag_map, tenure_metadata, ...)` — pure when
    both `tag_map` and `tenure_metadata` are passed explicitly. The
    live entry point (`extract_wallet_archetype`) becomes a thin
    wrapper that fetches `(activity, positions)` and forwards. Capture
    script pre-fetches `tag_map` and `tenure_metadata` once, then
    passes them to the classifier; never re-fetches inside replay.

## Open questions (for operator review)

None at draft time. If operator wants to push back on any
"Decisions made" item, do it before the implementation plan is
written — those choices propagate into task-level tests and code.

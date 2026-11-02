# Robinhood Pons Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Recover verified Pons venue metadata and resolve Robinhood market cap without waiting on RPC during entry.

**Architecture:** The existing volume resolver thread verifies curve/factory/token/counter identity and writes an additive supply cache. The funded hook reads one indexed, read-only cache row before its existing blind-chain fallback.

**Tech Stack:** Python, SQLite WAL, existing pipelined WebSocket RPC, pytest.

**Spec:** `docs/superpowers/specs/2026-10-03-robinhood-pons-metadata.md`

<!-- schema:external=pool_meta,volume_token_supply -->

## Global Constraints

- Bound each pass to 16 curves.
- Refresh after 300 seconds; entry accepts supply for at most 600 seconds.
- Retry unknown curves after 30 seconds.
- Supply is atomic decimal TEXT; entry lookup is read-only with zero busy timeout.
- No database transaction spans an RPC call; no new entry RPC or wait.
- Preserve cap/floor/fallback and frozen contracts; no stale buy replay; no deployment backup.

## Review Focus

- Canonical-looking spoof curves: factory and registry info must agree, not just the curve's own answers.
- ERC-20 counter with absent price/decimals: retain unknown volume rather than native ETH valuation.
- Partial/429/malformed responses: never become permanent absence or fresh supply.
- Supply refresh stalled or timestamps invalid: expire the cache and preserve entry fallback.
- Repeated launch event after verification: must not erase correct counter identity.

### Task 1: Background venue metadata and supply cache

**Files:** Create `sniper/services/memecoin_pons_metadata.py`, `tests/test_memecoin_pons_metadata.py`; modify `sniper/services/memecoin_volume_writer.py`, `sniper/services/memecoin_tick_stream.py`, `tests/test_memecoin_volume_writer.py`.

**Interfaces:** `resolve_pons_curves(conn, chain, rpc_call, now, limit=16) -> int`; `read_cached_supply(chain, mint, *, path=None, now=None) -> dict` containing `supply`, `outcome`, `observed`; `VolumeTape.register_pons_curve(curve, mint, now, *, counter=None)`.

- [x] Write behavioral tests using SPORE's verified token, curve, factory and ERC-20 counter. Pin native counters, failed/malformed/partial replies, wrong identity, refresh expiry, TEXT atomic units, and launch re-delivery.

```python
assert resolve_pons_curves(conn, '4663', fixture_rpc, 2000) == 1
assert tuple(conn.execute('SELECT token0, token1, quote_side FROM pool_meta WHERE pool=?', (curve,)).fetchone()) == (counter, mint, 0)
assert read_cached_supply('4663', mint, path=path, now=2001)['supply'] == 1e9
```

- [x] Run `nice -n19 ionice -c3 python3 -m pytest tests/test_memecoin_pons_metadata.py tests/test_memecoin_volume_writer.py -q -n2`; capture the failing output.
- [x] Create the side-store table, strict ABI decoding and registry verification, bounded background resolution, and cache reader. Change launch-only registration to unknown counter; route Pons frames to the existing resolver thread. Correct native-only decoder comments without changing amount order.
- [x] Run those tests plus `tests/test_memecoin_pons_v2_volume.py`; expect all pass. Verify actual-counter USD arithmetic and transaction exclusion.
- [x] Commit only Task 1 files and the spec/plan with `fix: recover verified Pons curve metadata`.

### Task 2: Cached Robinhood source market cap

**Files:** Modify `sniper/services/memecoin_real_runner.py`; create `tests/test_memecoin_robinhood_cached_mcap.py`; create `docs/audits/2026-10-03-robinhood-pons-metadata.md`.

**Interfaces:** Consume `read_cached_supply`; produce `_cached_evm_source_mcap(chain, mint, price, trace=None) -> float | None`, recording `evm_cached_supply` with typed outcome and read time.

- [x] Write funded-hook tests: SPORE's fresh supply resolves $15,952.47 and basis `evm_source_cached`; stale/absent/malformed rows preserve fallback; known event cap bypasses lookup; source price absent/nonfinite stays unknown; an above-ceiling cached cap remains refused. Assert no provider read on cache hit.

```python
assert hook(book, event)['status'] == 'filled'
assert seen['classification_context']['effective_mcap'] == pytest.approx(15952.46538195589)
assert seen['classification_context']['mcap_basis'] == 'evm_source_cached'
```

- [x] Run the new tests with two workers; capture the failure before editing the hook.
- [x] Add finite positive source-price multiplication and trace evidence; prefer it in the existing unresolved blind-chain branch, leaving the established fallback on misses.
- [x] Run focused cache, writer, Pons volume, EVM mcap, classifier and floor regressions; expect all pass. Benchmark indexed local reads and record p50/p90.
- [x] Commit Task 2; push main and deploy exact SHA with `scripts/deploy_memecoin_runtime.sh "$task_release_sha" --restart --require-flat --defer-backup` and two test workers.
- [x] Verify authenticated status, runner/web process release, fresh observer release/feed, verified curve/counter mappings and supply cache. Record actual measurements in the audit and user-authorized brain evidence, leaving independent verification pending.

## Rulings and completion

User explicitly requested continuous fixes and supplied full deployment permission. Execute inline without a new approval round. Existing tracked checkout is clean; foreign untracked files are outside this change. Inspect the final diff and request one fresh-context code review as required by executing-plans. Any review fix gets a failing behavioral regression before implementation.

# G5 — `--analyst-fast` Preset for `run_wallet_protocol.py`

**Date:** 2026-05-17
**Status:** spec → tests → implementation
**Origin:** `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` §"Gaps / mistakes" → G5
**Scope:** CLI preset + report stamp. No analyzer-API changes. No fetch-rebuild. No persistence schema changes.

## 0. Doctrine

The wallet analyzer is a strategy-intelligence system, not a copy bot. This change is operator iteration speed only — it does not weaken any gate, mutate any verdict, or introduce a new "fast path" code path inside the analyzer. The analyzer still refuses to fabricate signal; the preset just disables expensive surfaces and records that they were disabled.

## 1. Problem

Live-sampling review (2026-05-10) found:

- The default live run with price history enabled "did not produce JSON after several minutes" and was abandoned.
- The bounded review path (`--no-history --no-exec`) still took 1–3 minutes per high-volume wallet.
- Operators must currently remember which combination of `--no-*` flags constitutes "fast review mode", and there is no machine-readable record on the report that the operator chose a bounded path. That makes downstream consumers (Strategy Pattern Audit, calibration replay, dossier writer) unable to differentiate "this wallet was reviewed fast" from "this surface genuinely had no data."

## 2. What ships

### 2.1 New CLI flag

Add to `scripts/run_wallet_protocol.py` argparse:

```text
--analyst-fast
  Bounded review preset. Equivalent to passing --no-history --no-exec and
  forcing --capital-ledger off. Use for first-pass triage on high-volume
  wallets when you only need Position Builder + Completeness + branches.
  Cannot be combined with --capital-ledger (the preset wins with a stderr
  warning).
```

### 2.2 Effective resolution semantics

Resolution happens in `main()` immediately after `args = _build_parser().parse_args(...)`. Order matters:

1. `args.no_persist + args.persist` mutual-exclusion stays first (unchanged).
2. New: if `args.analyst_fast` is True:
   - if `args.capital_ledger`: warn to stderr that capital_ledger is disabled by the preset, set `args.capital_ledger = False`.
   - `args.no_history = True`
   - `args.no_exec = True`
3. The rest of `main()` proceeds with the resolved args.

`--analyst-fast` is a preset only. It composes additively with `--no-tags`, `--no-peers`, `--no-archetype`, `--no-history`, `--no-exec`, and `--bounded-history`. If the operator passes `--analyst-fast --no-tags`, the analyzer omits tags **and** history and exec attribution. No warning needed for that case.

### 2.3 Report stamp

After `analyze_wallet(...)` returns successfully, the CLI appends one machine-readable flag onto `report.data_quality_flags`:

```text
ANALYST_FAST_MODE_OMITTED:price_history,exec_attribution,capital_ledger
```

The omitted-surface list is fixed for this slice: those three are the surfaces the preset always disables. We do not enumerate per-run which surfaces were *separately* omitted by `--no-tags` or `--no-peers`; those have their own existing flags (`SUBGRAPH_FALLBACK_USED`, etc.).

The stamp goes through `report.data_quality_flags.append(...)`, the same channel every other CLI-side flag uses today. No new field on `ProtocolReport`.

### 2.4 Replay-args inclusion

`_replay_args_json` must record the preset so `scripts/replay_wallet_run.py` can reconstruct argv faithfully. Add `"analyst_fast": bool(getattr(args, "analyst_fast", False))` to the dict, preserved by `sort_keys=True`.

### 2.5 Non-changes

- `analyze_wallet(...)` signature unchanged.
- `ProtocolReport` schema unchanged.
- `_replay_args_json` keeps all existing keys (only adds `analyst_fast`).
- Fixture mode (`--from-fixture`): the existing "advisory" warning is unchanged. `--analyst-fast` under fixture mode prints the same advisory if any `--no-*` flags it forces are also operator-passed; otherwise it sets them silently. Fixture surface availability still governs.

## 3. Acceptance tests

All tests live in a new file `tests/test_run_wallet_protocol_analyst_fast.py`. The test-writer agent owns this file; no implementation lives in it.

### 3.1 Argparse resolution

```text
test_analyst_fast_flag_parses
  Parse [wallet, "--analyst-fast"]; assert args.analyst_fast is True.

test_analyst_fast_alone_does_not_set_no_flags_at_parse_time
  Argparse parse only — main() resolves the preset. Asserts args.no_history
  and args.no_exec are still False at parse time when only --analyst-fast
  is passed. This pins where resolution happens (in main(), not argparse).
```

### 3.2 Resolution semantics inside main()

These tests invoke `main([...])` with a fully mocked fetch+analyze layer so they only exercise the CLI resolution + stamp path. Use existing patterns from `tests/test_run_wallet_protocol_persist_flipper_grains.py` if helpful.

```text
test_analyst_fast_forces_no_history_and_no_exec
  Invoke main([wallet, "--analyst-fast"]) with mocked fetch_wallet_data and
  analyze_wallet capturing the kwargs. Assert the captured args reflect
  no_history=True, no_exec=True (i.e. the analyzer was called as if those
  flags were set).

test_analyst_fast_forces_capital_ledger_off_with_warning
  Invoke main([wallet, "--analyst-fast", "--capital-ledger"]); assert
  stderr contains "capital_ledger" and "analyst-fast", and the captured
  fetch_wallet_data call was made with with_capital_ledger=False.

test_analyst_fast_composes_additively_with_no_tags
  Invoke main([wallet, "--analyst-fast", "--no-tags"]); assert both
  tag-grain lookups AND price_history are disabled; no extra warning.
```

### 3.3 Report stamp

```text
test_analyst_fast_stamps_omitted_flag_on_data_quality_flags
  Mock analyze_wallet to return a ProtocolReport with data_quality_flags=[].
  Invoke main([wallet, "--analyst-fast"]); assert the returned report (via
  the json output path or a captured intercept) has
  "ANALYST_FAST_MODE_OMITTED:price_history,exec_attribution,capital_ledger"
  in data_quality_flags exactly once.

test_no_stamp_when_analyst_fast_absent
  Mock analyze_wallet identically. Invoke main([wallet]) (no flag); assert
  no ANALYST_FAST_MODE_OMITTED entry appears in data_quality_flags.
```

### 3.4 Replay args

```text
test_replay_args_json_includes_analyst_fast_true_when_set
  Build args via _build_parser().parse_args([wallet, "--analyst-fast"]);
  assert json.loads(_replay_args_json(args))["analyst_fast"] is True.

test_replay_args_json_includes_analyst_fast_false_by_default
  Same builder, no flag; assert the field is False (present, not omitted —
  replay correctness needs the explicit absence).
```

### 3.5 Acceptance ceiling

Nine tests total (2 + 3 + 2 + 2 per §3.1–§3.4). The test-writer adds them. The implementer greens them without modifying any test. Each test must be red on `feat/g5-analyst-fast` before implementation lands.

## 4. Implementation surface

| File | Change |
|---|---|
| `scripts/run_wallet_protocol.py` | argparse add `--analyst-fast` (action="store_true"); `main()` resolves preset between mutex check and main fetch; `_replay_args_json` adds `analyst_fast` key; CLI stamps `ANALYST_FAST_MODE_OMITTED:...` after `analyze_wallet` returns. |

No other file touched.

## 5. Risks / what could go wrong

1. **The stamp lands twice.** A duplicate-append would polute downstream consumers. Mitigated by stamping in exactly one place: immediately after `analyze_wallet` returns successfully, gated on `args.analyst_fast`.
2. **A reviewer/consumer parses `ANALYST_FAST_MODE_OMITTED:` as a structured field.** The format is `KEY:csv-of-surface-names`. Existing data_quality_flags use the same `KEY` or `KEY:value` shape (`BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY`, `SUBGRAPH_FALLBACK_USED`, etc.), so this is consistent.
3. **Operators expect the preset to also disable `--bounded-history`.** Out of scope. `--bounded-history` is an opt-out of full-history walk; that's a fetch decision the operator owns separately. We can revisit if review feedback shows operators are confused.
4. **Persistence row's `args_json`.** Already captured via `_replay_args_json` for both success and failure paths; the new key flows through automatically.
5. **Fixture replay determinism.** `--analyst-fast` under fixture mode is advisory by the existing rule. We do NOT skip the report stamp under fixture mode; the stamp is purely about analyst intent, not surface availability. Test does not need to cover fixture mode explicitly.

## 6. Out of scope (explicit)

- Adding `closed_positions_fetched` / `closed_positions_cap_hit` to `data_completeness` (G2 — separate slice).
- Machine-readable `tag_lookup_state` (G4 — separate slice).
- Leaderboard `triage_context` (G3 — separate slice).
- Strategy Pattern Audit `category_clusters` (G6 — separate slice).
- Any new analyzer config object (e.g. `AnalystProfile`). The preset is CLI-only by design.

## 7. Knowledge sync expectations

At branch close:

- AGENTS.md mention of the CLI flag list in the §"Wallet analyzer focused commands" pointer becomes stale if it enumerates `--no-history --no-exec` as the canonical fast path. Replace with `--analyst-fast` recommendation.
- `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` G5 status → resolved with commit ref.
- No Supabase memory write needed (this is implementation, not doctrine).

## 8. Done definition

- 9 acceptance tests red on the branch, then green after implementation.
- `python3 -m pytest tests/test_run_wallet_protocol_analyst_fast.py -q` passes.
- `python3 -m pytest tests/test_run_wallet_protocol_persist_flipper_grains.py -q` still passes (regression safety on the same file's stamping behavior).
- `python3 scripts/validate_schema_refs.py` passes (no schema-affecting changes expected).
- `git diff --stat` shows only `scripts/run_wallet_protocol.py` and the new test file.

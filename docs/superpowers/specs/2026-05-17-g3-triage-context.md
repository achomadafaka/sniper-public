# G3 — `triage_context` on `ProtocolReport`

**Date:** 2026-05-17
**Status:** spec → tests → implementation
**Origin:** `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` §"Gaps / mistakes" → G3
**Scope:** New optional `triage_context` dict on `ProtocolReport`, populated from a CLI-supplied JSON when the operator's reason for analyzing this wallet is leaderboard triage. Prevents apples-to-oranges comparisons between analyzer net_pnl and unlabeled leaderboard PnL.

## 0. Doctrine

Wallet analyzer is strategy-intelligence, not a copy bot. Triage context is operator-supplied evidence about WHY this wallet was analyzed (e.g., "monthly rank 3 with $1.76M PnL"). The analyzer doesn't validate or interpret it; the field exists so any consumer reading the report can know the period/source that prompted the analysis. Without this, a MONTH-period leaderboard ranking gets silently compared to all-time analyzer net_pnl and the operator wonders why they don't match.

## 1. Problem

From the audit (rank1 wallet case):

```text
MONTH leaderboard:  +$2.33M PnL  → looked very high
analyzer net_pnl:   -$1.255M     → matches ALL-time leaderboard
```

The two numbers are not comparable. Once the auditor fetched ALL-time leaderboard separately, they matched the analyzer exactly. But the original prompt to analyze the wallet came from a MONTH ranking — and the report carried no record of that context, so a downstream reader sees "wallet rank 1, net_pnl -$1.25M" and concludes the leaderboard is broken (it isn't; the periods are misaligned).

This is a transparency gap. The right surface is an operator-supplied "why I'm analyzing this wallet" stamp.

## 2. What ships

### 2.1 New `ProtocolReport.triage_context` field

```python
@dataclass
class ProtocolReport:
    ...
    triage_context: dict | None = None
```

Default `None`. When operator supplies context, the field holds the parsed JSON dict as-is (no validation, no schema enforcement). Recommended shape (operator convention, not enforced):

```python
{
    "source": "polymarket_public_leaderboard",
    "period": "MONTH",                  # or "ALL_TIME", "WEEK", etc.
    "fetched_at": 1747700000,           # unix epoch
    "rank": 3,                          # leaderboard rank at fetched_at
    "lb_pnl_at_capture": 1760000.0,     # PnL value the leaderboard showed
    "note": "optional free-text",
}
```

The analyzer never reads or interprets the dict. JSON consumers (UIs, audits, dossier writer) can inspect it.

### 2.2 New `analyze_wallet` kwarg

```python
def analyze_wallet(
    ...,
    triage_context: dict | None = None,
) -> ProtocolReport: ...
```

When non-None, copied verbatim onto `report.triage_context`. When None, the field stays None.

### 2.3 New CLI flag

```text
--triage-context-json JSON_STRING
  Operator-supplied JSON describing why this wallet was analyzed
  (e.g. {"source":"polymarket_public_leaderboard","period":"MONTH",
  "rank":3,"lb_pnl_at_capture":1760000.0,"fetched_at":1747700000}).
  Parsed and stamped onto report.triage_context. Malformed JSON
  exits 2 before any analysis runs.
```

JSON-string-on-argv is appropriate here because the typical case is a small JSON object the operator hand-types or pastes from a wrapper script. A file-based variant can be added later if needed.

### 2.4 Replay round-trip

`_replay_args_json` records `triage_context_json` (raw string, not parsed dict) so `replay_wallet_run.py` reconstructs argv with the same `--triage-context-json '...'`. The replay must produce a byte-identical `triage_context` field on the replayed report.

### 2.5 Non-changes

- No new fields on `WalletData`.
- No fetcher changes.
- No analyzer logic uses `triage_context` for any decision (verdict, gate, copyability, primary_edge_type, branches). Purely advisory metadata.
- `data_completeness` is NOT the host — `triage_context` is operator-supplied, not analyzer-computed coverage.
- No renderer change required in this slice. JSON consumers see the field; a markdown "## Triage context" block can land in a follow-up if operator demand exists.

## 3. Acceptance tests

`tests/test_wallet_protocol_triage_context.py` (new file). 10 tests.

### 3.1 Field default

```text
test_triage_context_defaults_to_none_on_bare_protocolreport
  ProtocolReport(wallet="0x...").triage_context is None.

test_triage_context_defaults_to_none_after_analyze_wallet_without_kwarg
  Run analyze_wallet without the kwarg; assert report.triage_context is None.
```

### 3.2 Analyzer kwarg passthrough

```text
test_analyze_wallet_stamps_triage_context_when_kwarg_passed
  analyze_wallet(..., triage_context={"source":"x"}) →
  report.triage_context == {"source":"x"}.

test_analyze_wallet_preserves_arbitrary_keys
  Pass a complex dict (mix of strings, ints, floats, nested); assert
  identity preservation (verbatim stamp).

test_analyze_wallet_does_not_mutate_provided_dict
  Pass a dict, mutate it post-analyze; assert report.triage_context
  reflects the original state (analyzer should copy or not retain a
  shared reference — pin the safer behaviour).
```

### 3.3 CLI argparse

```text
test_cli_parses_triage_context_json_flag
  _build_parser().parse_args([wallet, "--triage-context-json", '{"a":1}'])
  → args.triage_context_json == '{"a":1}'.

test_cli_defaults_triage_context_json_to_none
  Without the flag, args.triage_context_json is None.
```

### 3.4 CLI stamping behavior

```text
test_cli_passes_parsed_dict_to_analyze_wallet
  main([wallet, "--triage-context-json", '{"source":"lb","period":"MONTH"}'])
  with mocked fetch/analyze; assert the captured analyze_wallet kwarg
  triage_context == {"source":"lb","period":"MONTH"}.

test_cli_no_triage_context_kwarg_when_flag_absent
  main([wallet]); analyze_wallet called with triage_context=None.

test_cli_rejects_malformed_json_with_exit_2
  main([wallet, "--triage-context-json", "not-json"]) → returns 2
  and writes a stderr error mentioning "triage-context-json".
```

### 3.5 Replay round-trip

```text
test_replay_args_json_records_triage_context_json_raw_string
  _build_parser().parse_args([wallet, "--triage-context-json", '{"a":1}'])
  → json.loads(_replay_args_json(args))["triage_context_json"] == '{"a":1}'.

test_replay_argv_reconstructs_flag_when_set
  _argv_from_args_json with "triage_context_json": '{"a":1}' →
  reconstructed argv contains both "--triage-context-json" and the
  exact JSON string '{"a":1}'.
```

### 3.6 Acceptance ceiling

12 tests total (2 + 3 + 2 + 3 + 2). Single new test file. No existing test widening required (triage_context is a brand-new field on ProtocolReport).

## 4. Implementation surface

| File | Change |
|---|---|
| `sniper/analysis/wallet_protocol/types.py` | Add `triage_context: dict \| None = None` to `ProtocolReport`. |
| `sniper/analysis/wallet_protocol/analyze_wallet.py` | New `triage_context: dict \| None = None` kwarg. Verbatim stamp onto `report.triage_context`. |
| `scripts/run_wallet_protocol.py` | New `--triage-context-json` argparse arg. Parse to dict in `main()`, error on malformed JSON before fetching. Pass dict to `analyze_wallet`. Add `triage_context_json` raw string to `_replay_args_json`. |
| `scripts/replay_wallet_run.py` | New entry in argv-reconstruction logic for `triage_context_json` (paired arg, not bool flag). |

## 5. Risks

1. **Schema-less dict on ProtocolReport.** No validation means consumers can't trust the shape. That's the design: the analyzer doesn't curate operator metadata. Documented in §2.1 with a recommended shape. Future slices can add validation if a clear convention emerges.
2. **Shell escaping pain.** `--triage-context-json '{"a":1}'` requires single-quote escaping in shell. Operator might prefer `--triage-context @file.json`. Out of scope for this slice; future enhancement.
3. **Replay argv reconstruction.** `_argv_from_args_json` currently uses `_BOOL_FLAGS` and a few explicit `if/then` for paired args. Adding a paired-arg entry must preserve the JSON string EXACTLY (no re-serialization, which would lose key order in older Python). The reconstruction path already handles strings via `str(payload["key"])`.
4. **Length / typo / argv length.** Long JSON strings as argv entries are fine on Linux. Operator-error JSON is caught at CLI parse time and returns exit 2.

## 6. Out of scope

- Auto-fetching leaderboard data inside the CLI.
- Storing `triage_context` in a persistence row (e.g. `analyzer_runs.triage_context_json`). Persistence schema changes are a separate slice if/when the operator-tooling pipeline matures.
- Renderer (markdown) integration.
- Stamping cross-references between `triage_context.lb_pnl_at_capture` and `report.net_pnl` (the audit recommendation against silent comparison is enforced by transparency, not by validation).
- A `--triage-context @file.json` file-based variant.

## 7. Knowledge sync expectations

- `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` G3 status → resolved with commit ref.
- No AGENTS.md surface change required.

## 8. Done definition

- 12 acceptance tests in `tests/test_wallet_protocol_triage_context.py` red, then green.
- `python3 -m pytest tests/test_wallet_protocol_*.py tests/test_run_wallet_protocol_*.py tests/test_replay_wallet_run_*.py -q` passes.
- `python3 scripts/validate_schema_refs.py` passes.
- `git diff --stat` shows only the 4 implementation files + spec + test.

# G4 — Machine-readable `tag_lookup_state` on `data_completeness`

**Date:** 2026-05-17
**Status:** spec → tests → implementation
**Origin:** `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` §"Gaps / mistakes" → G4
**Scope:** Surface tag-lookup availability as a typed field on `data_completeness` so downstream consumers (and operators reading the JSON) can tell apart `tag_lookup failed` from `--no-tags requested` from `lookup succeeded but no tags`. Today the only signal is a stderr log line; the report is silent.

## 0. Doctrine

Wallet analyzer is strategy-intelligence, not a copy bot. Tag-grain coverage is one of the highest-leverage strategy-extraction axes; "tags are unavailable locally" is a different state than "this wallet has no tags." Both should be visible and distinguishable in the report.

## 1. Problem

Live sampling on 2026-05-10:

```text
$ python3 scripts/run_wallet_protocol.py 0xpestle --json ...
# tag-grain lookups failed: OperationalError('no such table: market_tags')
... (report continues with leafs_by_cid=None, nav_lookup=None)
```

The report carries no machine-readable signal that tag lookups were unavailable. `data_completeness` has activity coverage and attribution coverage, but no tag coverage. Strategy Pattern Audit's `category_specialist_v1` rule then proceeds against an Edge Surface with empty nav data — it correctly rejects, but it doesn't know whether to reject because the wallet has no category edge or because we couldn't look up the tags.

This is a transparency gap, not a tag-data-collection gap. The market_tags table provisioning is a separate piece of operational work.

## 2. What ships

### 2.1 New `data_completeness.tag_lookup_state` field

A new top-level key on the `data_completeness` dict:

```python
data_completeness["tag_lookup_state"] = "ok" | "empty" | "unavailable" | "skipped"
```

Values:

- `"ok"` — tag lookup ran and returned rows for at least one condition_id.
- `"empty"` — tag lookup ran successfully but returned no rows for any condition_id in the wallet's universe (e.g. the wallet only traded markets without category tags).
- `"unavailable"` — tag lookup could not run (missing `market_tags` table, DB error, network error). This is the load-bearing case — it tells consumers "we don't know the tag picture for this wallet."
- `"skipped"` — operator passed `--no-tags`; we never attempted the lookup. Distinct from `"unavailable"` because the operator chose to skip.

### 2.2 `data_quality_flags` propagation

When `tag_lookup_state == "unavailable"`, append `TAG_LOOKUP_UNAVAILABLE` to `report.data_quality_flags`. This makes the state discoverable by audit consumers that already inspect `data_quality_flags`. Other states (`ok`/`empty`/`skipped`) do NOT append a flag — `unavailable` is the only "this should affect downstream behavior" state.

### 2.3 CLI → `analyze_wallet` plumbing

`analyze_wallet` gains a new optional kwarg:

```python
def analyze_wallet(
    ...,
    tag_lookup_state: str | None = None,
    ...,
) -> ProtocolReport: ...
```

When `None`, the analyzer infers from `leafs_by_cid`: `None` → `"unavailable"`, `{}` → `"empty"`, populated → `"ok"`. The CLI passes the explicit state when it knows operator intent:

- `args.no_tags` → CLI passes `"skipped"`.
- Lookup exception caught → CLI passes `"unavailable"`.
- Lookup OK + rows → CLI passes `"ok"`.
- Lookup OK + no rows → CLI passes `"empty"`.

Fixture mode (`--from-fixture`): the fixture carries `leafs_by_cid` (or doesn't). The CLI infers:
- Fixture has populated `leafs_by_cid` → `"ok"`
- Fixture has empty `leafs_by_cid` → `"empty"`
- Fixture has no `leafs_by_cid` → `"unavailable"` (we don't have a way to distinguish "operator skipped at capture time" from "lookup failed at capture time"; conservative interpretation)

### 2.4 Non-changes

- `ProtocolReport` dataclass schema unchanged.
- `WalletData` schema unchanged.
- Existing `data_completeness` keys (`activity_capture`, `attribution_coverage`, `closed_positions_capture`, `caveats`) unchanged.
- Strategy Pattern Audit rule consumption of the new field is out of scope (separate slice; G6 may consume it).

## 3. Acceptance tests

`tests/test_wallet_protocol_tag_lookup_state.py` (new file). 10 tests.

### 3.1 Field presence + type

```text
test_tag_lookup_state_key_always_present
  Build WalletData with no tag-related fields; assert
  data_completeness["tag_lookup_state"] is one of the 4 valid strings.

test_tag_lookup_state_is_string_not_none
  Even when nothing is passed, the field is a string (default "unavailable"
  for safety — absence of signal is read conservatively).
```

### 3.2 Explicit state passthrough

```text
test_explicit_state_ok
  analyze_wallet(..., tag_lookup_state="ok") → data_completeness["tag_lookup_state"] == "ok".

test_explicit_state_empty
  analyze_wallet(..., tag_lookup_state="empty") → "empty".

test_explicit_state_unavailable
  analyze_wallet(..., tag_lookup_state="unavailable") → "unavailable".
  Also asserts "TAG_LOOKUP_UNAVAILABLE" in report.data_quality_flags exactly once.

test_explicit_state_skipped
  analyze_wallet(..., tag_lookup_state="skipped") → "skipped".
  Does NOT append TAG_LOOKUP_UNAVAILABLE flag.
```

### 3.3 Inference from leafs_by_cid when state not explicit

```text
test_inferred_unavailable_when_leafs_by_cid_none
  analyze_wallet(..., leafs_by_cid=None, tag_lookup_state=None) →
  data_completeness["tag_lookup_state"] == "unavailable" + flag set.

test_inferred_empty_when_leafs_by_cid_empty_dict
  analyze_wallet(..., leafs_by_cid={}, tag_lookup_state=None) →
  data_completeness["tag_lookup_state"] == "empty".

test_inferred_ok_when_leafs_by_cid_populated
  analyze_wallet(..., leafs_by_cid={"cid": ["tag"]}, tag_lookup_state=None) →
  data_completeness["tag_lookup_state"] == "ok".
```

### 3.4 Flag not double-stamped

```text
test_unavailable_flag_appended_exactly_once
  Run analyze_wallet twice on the same report (e.g. defensively re-invoke);
  assert flag count is 1 (not 2). Counts via
  report.data_quality_flags.count("TAG_LOOKUP_UNAVAILABLE").

  Edge note: analyze_wallet is called per-wallet, not idempotently in
  production. But the CLI may re-invoke during retries; we want a defensive
  guard. If analyze_wallet always creates a fresh report this is a no-op,
  but the test pins the contract.
```

### 3.5 Acceptance ceiling

Two tests in §3.1, four in §3.2, three in §3.3, one in §3.4 = 10 tests.

## 4. Implementation surface

| File | Change |
|---|---|
| `sniper/analysis/wallet_protocol/analyze_wallet.py` | New `tag_lookup_state: str \| None = None` kwarg. Resolve to one of 4 strings (use explicit value if passed, else infer from `leafs_by_cid`). Write into `data_completeness["tag_lookup_state"]`. When `"unavailable"`, append `TAG_LOOKUP_UNAVAILABLE` to `report.data_quality_flags` (idempotent). |
| `scripts/run_wallet_protocol.py` | Compute the state at the existing tag-lookup site (`try: fetch_tag_lookups_from_db / except: ...`). Pass it through to `analyze_wallet`. Handle `--no-tags` and fixture mode. |
| `sniper/analysis/wallet_protocol/types.py` | Update inline `data_completeness` shape comment to include the new key. |

## 5. Risks

1. **Strategy Pattern Audit known-caveats blind spot.** Unlike G2 which added a new caveat key, G4 adds a top-level `data_completeness` key, not a caveat. The `known_caveats` set in `_data_completeness_quality_label` won't be triggered. ✓ no consumer change needed there.
2. **Spec change interaction with fixtures.** Existing v2 fixtures don't carry tag_lookup_state. Replay must infer from the fixture's `leafs_by_cid` shape (per §2.3). No fixture re-record required.
3. **Audit consumers that grep `data_quality_flags`.** Adding `TAG_LOOKUP_UNAVAILABLE` to that list is consistent with existing flag conventions (`BRANCH_PROMOTED_ON_TAG_GRAIN_ONLY:`, `NEGATIVE_PNL_EDGE_UNEXPLAINED`, etc.). Audit consumers won't choke on the new flag.
4. **The CLI's existing log line `# tag-grain lookups failed: {e!r}` is preserved.** This G4 change ADDS structured signal; it doesn't remove the existing stderr breadcrumb. Operators tailing logs still see the message.

## 6. Out of scope

- Provisioning `market_tags` in local DBs (operational, not transparency).
- Strategy Pattern Audit rule changes to consume the new field (G6 territory).
- Renderer (markdown) changes — the JSON field is the contract; render is a niceness.
- Per-condition-id tag coverage breakdowns (a richer field could expose how many cids had tags, but that's beyond the audit's recommendation).

## 7. Knowledge sync expectations

- `docs/audits/2026-05-10-wallet-analyzer-live-sampling.md` G4 status → resolved with commit ref.
- No AGENTS.md surface change.

## 8. Done definition

- 10 acceptance tests in `tests/test_wallet_protocol_tag_lookup_state.py` red, then green.
- `python3 -m pytest tests/test_wallet_protocol_*.py -q` passes.
- `python3 scripts/validate_schema_refs.py` passes.
- `git diff --stat` shows only the analyzer + CLI + types.py doc + spec + test file.

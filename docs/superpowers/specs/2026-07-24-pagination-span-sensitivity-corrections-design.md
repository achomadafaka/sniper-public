# Pagination and Span-Sensitivity Corrections

**Date:** 2026-07-24

## Scope

Correct two defects found in `b065d639` without enabling the exit worker, changing funded behavior,
calling live APIs, reading production databases, or consuming the queued replay output.

## Solana exact-bound pagination

`_new_signatures()` cannot infer incompleteness merely because its last allowed page is full. An
exactly-full remainder has reached the cursor but looks identical to a larger backlog until one more
request is made.

After `_MAX_PAGES` full pages, issue one **probe** request with the same `until` cursor and `before`
the oldest enumerated signature:

- empty probe: the enumerated region is complete, return it with `reached_cursor=True`;
- non-empty probe: signatures remain, return the bounded region with `reached_cursor=False`;
- RPC failure: return `(None, False)` and hold all state.

The probe is evidence only. Its rows are not returned or decoded during that call, so the normal
page bound remains `_MAX_PAGES * _SIG_LIMIT`. The regression must drive 5,300 signatures through
`tape_pass_for_vault()` until the cursor reaches the newest signature, assert every signature was
decoded exactly once, and assert `frontier_before` is null only at completion.

## Span-floor sensitivity

`build_pool_block_features()` gains an explicit `apply_span_floor: bool = True` keyword. It always
computes `span_s` and `clears_span_floor`; the flag controls only whether an under-floor pool is
excluded.

The study loads and classifies the source data once, then runs the full analysis twice when
`REPORT_SPAN_FLOOR_SENSITIVITY` is true:

- `with_span_floor`: current 1,800-second cohort rule;
- `without_span_floor`: density floor remains, span floor does not filter.

Every CSV filename and printed result is labeled by arm. The two arms use the same source rows,
cadence assertion, feature definitions, temporal split logic, models, and random seeds. No result is
read or interpreted as part of this change.

## Failure handling

- Empty qualifying frames produce an explicit arm-level error instead of an unlabeled concat crash.
- Existing RPC failures remain fail-closed and do not advance the cursor.
- The research script writes no production state; outputs remain under `docs/research/assets/`.

## Verification

- Red/green regression for the 5,300-signature exact-bound drain.
- Red/green tests proving both research arms are invoked and labeled.
- Existing Solana tape, materializer, and predictive-floor tests.
- Broader exit/research-safe pytest selection, schema validation, compile, and `git diff --check`.

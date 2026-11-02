# Solana Exact-Quote Transient Retry Design

**Date:** 2026-08-05

## Problem

The funded Solana exit preview pages on the first `rpc_transport` failure. Position `27035`
produced two outage/recovery pairs nine minutes apart; each recovered on the next eligible sweep
about one to two seconds later. The preview performs two independent reads—an exact token-balance
JSON-RPC call and a Jupiter quote—but both failures are currently labeled `solana:jupiter`, so the
stored alert cannot identify the failing boundary.

## Contract

- Retry a failed exact-balance read once after 200 ms only for `rpc_transport` or
  `provider_timeout`.
- Retry a failed Jupiter exit quote once after 200 ms under the same failure classes.
- Retry only the failed read. Never sign, submit, mutate a position, or consume another sweep-level
  quote-demand token during the retry.
- A successful retry returns the normal exact preview and produces no infrastructure transition.
- Two failed balance attempts return the existing typed failure with provider
  `solana:balance_rpc`.
- Two failed Jupiter attempts return the existing typed failure with provider
  `solana:jupiter`.
- All three outcomes share provider group `solana:exact_quote`. Sweep-wide outage defense and
  recovery aggregation use that group, while outage copy names the failed balance/Jupiter boundary.
  This keeps successful previews in the same safety cohort as token-specific Jupiter failures and
  prevents a partial recovery from closing an episode while another boundary still fails.
- A successful full preview returns provider `solana:exact_quote`.
- Rate limits, malformed responses, and token-specific failures are not retried. Existing
  unsellable-counter, outage-milestone, recovery, and executable-exit rules remain unchanged.

## Scope

Change the typed preview result, funded Solana seam, funded sweep grouping, alert copy, and their
focused tests. Add no setting, dependency, schema, or funded policy change.

## Verification

Tests must prove both boundaries recover after one transport failure, both boundaries fail closed
with the correct boundary after two failures, non-retryable failures remain single-attempt,
successful previews prevent false provider-outage classification of token failures, and a partial
recovery cannot close an ongoing exact-quote episode. Deploy only a merged SHA through the pinned
runtime script. The operator explicitly requires focused tests instead of the broad deploy suite and
explicitly forbids backups for this rollout.

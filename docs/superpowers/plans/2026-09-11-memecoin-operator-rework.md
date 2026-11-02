# Memecoin Operator Rework Implementation Plan

> **For agentic workers:** Use the parallel-agent and test-driven-development skills. Each owner implements and tests its assigned files.

**Goal:** Make Memecoin entry results, position values, notifications, and links agree across the browser and Telegram.

**Architecture:** One read-only entry projection reconciles existing event, attempt, reservation, and fill evidence. Full and stream position responses share price fields. The existing notification dispatcher gains durable delivery ownership.

**Tech Stack:** Python, SQLite, Flask, plain JavaScript, existing CSS tokens, pytest, Node, and Playwright.

**Spec:** `docs/superpowers/specs/2026-09-11-memecoin-operator-rework-design.md`.

## Global constraints

- Keep eligibility, sizes, thresholds, exit policy, authority, and transaction transport unchanged.
- Use the existing isolated worktree. Preserve other agents' changes.
- Use short read-only ledger handles for observation. Keep SQL in persistence modules.
- Do not send manual Telegram messages or submit trades during tests.
- Deploy with `--defer-backup`; the operator requested no new backup.
- Each owner confirms a failing behavioral test before implementation and reports the focused result.

## Task 1: Shared entry and position projections

Owner: backend agent. Files: `sniper/persistence/db_memecoin.py`, `sniper/services/memecoin_entry_view.py`,
`sniper/services/memecoin_manual_intents.py`, `sniper/routes/api_memecoin.py`, and their named tests.

Interfaces:

```python
def entry_view(conn, *, origin_event_id=None, attempt_id=None, event=None):
    """Return one reconciled entry result, or None when identity is absent."""

def recent_entry_views(conn, *, limit=100, chain=None, origin_event_id=None):
    """Return bounded entry results with exact origin and chain identity."""
```

`GET /api/memecoin/entries` returns `entries`, `as_of`, and `revision`.
It accepts `limit`, `chain`, and `origin_event_id`. A detail result retains all attributed retry attempts.
Each row exposes `origin_event_id`, `attempt_id`, `network_id`, `mint`, `ticker`, `source_handle`,
`status`, `label`, `reason`, `source_created_at`, `observed_at`, `completed_at`, `requested_usd`,
`position_id`, `tx_sig`, `paper_action`, `authority`, `links`, and structured recorded gate evidence.
Status values are `source_received`, `evaluating`, `retry_pending`, `transaction_unresolved`,
`submitted_unknown`, `filled`, `refused`, `failed_before_submission`, `transaction_failed`, `needs_review`, and `not_evaluated`.
Consumers display `label`; they do not invent a second status mapping.

The full and ticker position rows share the existing market, runner-decision, and selected-exit fields.
They also expose `remaining_value_usd`, `recovered_usd`, `total_pnl_usd`, and `total_pnl_pct` with unknown values preserved.
The backend owner sends exact field additions to the UI owner before the UI binds them.

- [x] Add regressions for manual unknown, recovered fill, retry ownership, paper-only open, exact chain, absent event, and stale responses.
- [x] Confirm those tests fail on the current implementation.
- [x] Implement bounded ledger helpers and the shared entry view. Reuse `entry_attempt_for_origin` and current accounting helpers.
- [x] Add the entries route, align full/stream price schemas, and include entry revisions in stream change detection.
- [x] Reconcile manual status through the same entry view; preserve durable submission ownership.
- [x] Run the entry-view, manual-intent, ticker, snapshot, and route tests. Report the exact command and result.

Required regression assertions include:

```python
assert view['status'] == 'filled'  # A linked recovered fill overrides an old unknown verdict.
assert manual_status['status'] != 'refused'  # Signed unresolved work remains unresolved.
assert full_row['runner_decision_price'] == ticker_row['runner_decision_price']
```

## Task 2: Browser replacement

Owner: UI agent. Files: `static/app.js`, Memecoin CSS and small renderer files as necessary, template asset includes, and UI tests.
Consume Task 1's entry and position contracts and Task 4's link dictionaries.

- [x] Add behavioral tests for open settings during refresh, old-response rejection, complete observation tuples, small prices, partial-sale accounting, and deep links.
- [x] Confirm the regressions fail.
- [x] Replace primary Memecoin navigation with Live, Entries, Wallets, and Research; preserve every supported action and research view.
- [x] Put positions and recent entry results first. Move manual entry into a reachable labeled panel.
- [x] Render shared result labels and exact-event details. Keep paper actions separate.
- [x] Use one merge path for full and stream responses, with observation timestamps and preserved edit/focus state.
- [x] Separate recovered cash, remaining value, market price, sell quote, and total return. Use significant digits.
- [x] Use real chart timestamps, gaps, exact fill times, and effective exit levels.
- [x] Add keyboard-accessible controls and correct loading, stale, empty, and failed states.
- [x] Run Node behavior tests and focused pytest. Parent verifies authenticated desktop and mobile views.

```javascript
assert.equal(formatPrice(0.00000004).includes('0.00000004'), true);
assert.equal(merged.market_mid_at, oldObservation.market_mid_at); // A retained old value keeps its old time.
```

## Task 3: Notification lifecycle

Owner: notification agent. Files: `sniper/services/memecoin_telegram.py`, `memecoin_alerts.py`,
`memecoin_runner.py`, `memecoin_real_runner.py`, `scripts/memecoin_runner_loop.py`,
notification-specific persistence files, and their named tests. Do not edit `db_memecoin.py` or `api_memecoin.py`.
Consume `entry_view` from Task 1 and `operator_links` from Task 4.

- [x] Add failing tests for unknown results, final retries, enrichment failure, missing source IDs, misleading failure text, and queue failure.
- [x] Use one dispatcher for source and result delivery. Preserve source receipt time and exact origin identity in feed snapshots.
- [x] Use atomic snapshot replacement and expose snapshot time/error instead of silently returning an empty feed.
- [x] Persist one notification delivery record per stable transition key. Preserve the existing completed delivery audit.
- [x] Establish the first-activation cutoff, bounded restart reconciliation, five-minute individual catch-up, and explicit older-result summary.
- [x] Keep ambiguous sends unknown. Supersede unsent old results with the current entry projection.
- [x] Report every applicable armed-source result, including unresolved submissions and final retry refusals.
- [x] Correct effective gate evidence, chain authority labels, close/coin counts, failure claims, and position-policy text.
- [x] Run notification, retry, source-feed, and trace tests. No live sends.

```python
assert unresolved_message['status'] == 'transaction_unresolved'
assert first_transition_key != recovered_fill_transition_key
assert repeated_transition_key == first_transition_key
```

## Task 4: Shared links

Owner: parent. Files: `sniper/services/memecoin_links.py` and `tests/test_memecoin_operator_links.py`.

```python
def operator_links(network_id, mint=None, *, handle=None, tx_sig=None,
                   origin_event_id=None, position_id=None, pair_url=None):
    """Return available token, trader, chart, transaction, entry, and position URLs."""
```

The returned keys are `token`, `trader`, `chart`, `transaction`, `entry`, `position`, and `explorer`.
Internal paths start with `/?tab=memecoin` and encode `mc_view`, `mc_chain`, `mc_origin`, or `mc_position`.
Use existing chain resolution and provider mappings. Preserve Solana mint case. Encode path segments and validate provider pair URLs.
Omit unknown destinations. Never append an address to an unrelated chain explorer.

- [x] Add failing alias, BSC explorer, Solana case, encoded handle, unsupported-chain, and internal identity tests.
- [x] Implement the helper with the existing registry and `urllib.parse`.
- [x] Verify public destination routes with read-only requests and browser evidence.
- [x] Run focused tests and send the stable interface to other owners. Nineteen tests passed, including placeholder-handle omissions.

```python
assert operator_links('bsc', tx_sig='0x' + 'a' * 64)['transaction'].startswith('https://bscscan.com/tx/')
assert operator_links('robin', '0x' + 'b' * 40) == operator_links('4663', '0x' + 'b' * 40)
```

Ruling: Fomo routes retain their current paths and gain login-required labels. The live manifest confirms the authenticated route parent.
Public explorer links provide an alternative. DexScreener's HTTP 403 is a browser challenge, not evidence of an incorrect path.

## Task 5: Integration and deployment

Owner: parent. Review each owner's diff against the spec before integration.

- [x] Run focused combined tests, schema checks, and the bounded funded-path suite.
- [x] Use fixtures to reproduce open, partial, refused, retry, unresolved, stale, and recovered states in a browser.
- [x] Check keyboard controls, 390-pixel mobile layout, real timestamps, deep links, and refresh during an open settings panel.
- [x] Review notification keys, cutoff, ambiguous delivery, and funded state invariants independently.
- [ ] Commit logical changes, merge to main, push, and check CI for the exact SHA.
- [ ] Deploy the pinned runner with `--restart --defer-backup`, then the web release.
- [ ] Verify both releases, one runner, authenticated APIs, served assets, reservation ownership, and unchanged policy values.
- [ ] Record fixture limits and any organic evidence still absent. Mark the spec complete only when every required task passes.


## Additional incident evidence

FLYBOOK attempt `cedd73c3106a4681848a490af6006779` reserved $19.50 as row 2564 at 12:29:35 UTC.
The watchdog stopped the runner at 12:31:02 UTC for `sweep_stale` and released the unsigned reservation.
No signature or broadcast exists. The underlying blocked call is unknown because the old watchdog captured no thread dump.
The entry projection now reports `Failed before submission` from `runner_stopped_before_signing`.
The watchdog now requests the existing SIGUSR2 thread dump before a stale restart. A shell behavior test checks the signal order.

The Axiom migration request remains separate. See `docs/audits/2026-09-11-axiom-migration-access.md`.

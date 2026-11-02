# Hyperlive Analyzer Console And Cleaner Design

## Goal

Make the `HL Analyzer` page usable as an operations console: analyzer list, open paper positions, closed paper positions, and wallet cleanup each get their own workspace without changing paper-only execution semantics.

## UX Contract

- Keep one top-level navigation item: `HL Analyzer`.
- Add internal tabs: `Analyzer`, `Open`, `Closed`, `Cleaner`.
- `Analyzer` keeps the wallet address input, screen summary, compact filters, screened-wallet list, and detail pane.
- The screened-wallet list must scroll inside the analyzer rail instead of forcing the whole page to the last wallet.
- `Open` and `Closed` render separate paper-copy tables. No mixed signal tape.
- `Cleaner` exposes:
  - manual cull: operator enters a tracked wallet and reason;
  - system cleanup: run the existing lifecycle manager and report culled/selected counts.

## Data Contract

- `/api/hyperlive/analyzer` includes lifecycle fields for screened rows when the wallet is tracked: `tracked`, `lifecycle_status`, `lifecycle_reason`, `lifecycle_updated_ts_ms`, `culled_ts_ms`.
- `/api/hyperlive/cleaner/manual` accepts `{ wallet, reason, confirm: true }`, requires an existing tracked wallet, marks it `culled`, and disables any copy target.
- `/api/hyperlive/cleaner/system` accepts `{ max_watch_targets, confirm: true }`, runs `watch_lifecycle.maintain_watchlist`, then returns the current eligible and blocked copy targets.
- Cleaner POST routes reject missing confirmation before opening the database, so accidental UI/API calls do not mutate lifecycle state.
- Cleaner actions touch only `hl_intel.sqlite` lifecycle/copy-target state. They do not place orders, close copy-book rows directly, or call live exchange APIs.

## Edge Cases

- Unknown manual wallet returns `404` and does not create a tracked row.
- Missing confirmation returns `400` and does not mutate wallet lifecycle or copy-target state.
- Blank manual reason is normalized to `manual cull: operator cleanup`.
- Culled wallets remain visible in analyzer evidence with explicit lifecycle state.
- Open paper-copy rows remain in the book; disabling a target only prevents new mirror opens.
- System cleanup is idempotent and uses the same lifecycle thresholds as the paper runner.

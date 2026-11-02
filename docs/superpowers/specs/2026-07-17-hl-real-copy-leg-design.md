# HL Real Copy Leg — small-size live execution test (design)

Date: 2026-07-17
Status: approved (operator + Codex cross-review reconciled)
Owner surfaces: `scripts/run_hl_real.py`, `sniper/hyperlive/real_exec.py`, `sniper/hyperlive/hl_exchange.py`, `data/hl_intel.sqlite`

## 1. Objective and honest evidence base

Deploy a **$100-funded, $15/position** real-money mirror of the HL paper copy book to
measure real-vs-paper execution drift. This is a measurement deployment, not a scale-up.

Evidence, stated precisely (2026-07-17):
- Full clean copy cohort for target `0xc9c78160120cd6ecf3dea892e9ae5af4115bf5f8`:
  n=24 closes, +336 bps mean, CI [227, 446], 91.7% WR. **This is the full cohort, not OOS.**
- True holdout (post-freeze): n=11, +408 bps mean, 11/11 wins.
- Closes since the target was promoted `proven` (2026-07-17T16:19Z): **0**. There is no
  post-promote track record yet.
- Independent forward-follow ledger: n=44, +198 bps mean, 77.3% WR, clustered t=3.99,
  survives BH-FDR across 33 wallets.
- Risk: single-wallet concentration. If 0xc9c7 decays or goes dormant, the strategy has
  no fallback. The phase-2 gate (n≥30 follow closes, t≥2) guards future promotes.

## 2. Scope

In scope: real execution leg that tails the paper book; order-intent ledger; risk rails;
reconciliation; heartbeat/watchdog/Telegram observability; dry-run mode; testnet smoke.

Out of scope (explicitly): funding (separate operator action after dry-run + testnet
checks pass); multi-wallet copying; sizing above $15/pos; any change to paper-book
decision logic; any memecoin-runner change.

## 3. Architecture — real leg tails the paper book

`run_hl_copy.py` (paper) remains the single decision engine and control arm — untouched
except a cadence flag change (§9). A new process `scripts/run_hl_real.py` tails
`hl_copy_book` every ~5 s (SQLite WAL read):

- new eligible paper **open** → real market order on HL
- paper **close** on a leg-owned position → reduce-only real close
- every real fill references its paper twin (`copy_id`) so drift is measured
  per-trade by construction.

Rejected alternatives: (A) wiring orders into the paper loop — contaminates the
validated control; (C) standalone detector — decision drift between books.

Module layout:
- `sniper/hyperlive/hl_exchange.py` — thin adapter over the official
  `hyperliquid-python-sdk` `Exchange`/`Info`: market_open/market_close with **explicit
  slippage** (SDK default is 5% — never used), leverage set, order-status-by-cloid,
  clearinghouse positions. All calls injectable/fakeable for tests.
- `sniper/hyperlive/real_exec.py` — pure decision core: eligibility, sizing, rails,
  intent construction, reconcile diffing. No network.
- `scripts/run_hl_real.py` — loop: tail → decide → submit → record → reconcile →
  heartbeat.

## 4. Signing — dedicated API wallet

Funded account stays the existing EVM address `0x898B…3A1d` (master key on disk, 0600).
The leg signs with a **dedicated HL API wallet** (agent key): fresh keypair generated to
`data/hl_api_wallet.json` (0600), approved once by the master key (ApproveAgent), path
in `.env` as `HL_REAL_API_WALLET_FILE`. Rationale: HL nonce state is per signer; one API
wallet per trading process; master key never held by the runner.

## 5. Order-intent ledger (write-ahead) + fills

`hl_real_fills` alone is insufficient for crash recovery. Two tables in
`data/hl_intel.sqlite` (created by `intel_store.ensure_schema`; not part of the
sniper_tracker.db registry):

<!-- schema:external=hl_real_intents -->
<!-- schema:external=hl_real_fills -->

| table | purpose |
|---|---|
| `hl_real_intents` | write-ahead record of every decision: `cloid` (PK, deterministic), `deployment_id`, `copy_id`, `leg` (open/close), `coin`, `side`, `requested_usd`, `state`, `reason`, `created_ts_ms`, `updated_ts_ms` |
| `hl_real_fills` | execution results keyed to intents: `cloid`, `copy_id`, `fill_px`, `fill_sz`, `fee_usd`, `closed_ts_ms`, `realized_usd`, `paper_twin_pnl_bps`, `real_net_bps`, `drift_bps` |

Intent states: `intended → submitted → filled | partial | rejected | ambiguous`, plus
`skipped` (rail/eligibility refusal, with reason) recorded without submission.

**Deterministic CLOID** = 128-bit hex derived from
`sha256(deployment_id | copy_id | leg)[:16]`. Recovery after crash: for every intent in
`submitted`/`ambiguous`, query order status by cloid and settle the state. Ambiguity
(unknown after query) alerts and blocks new entries until resolved via CLI
(`scripts/hl_real_resolve.py`).

## 6. Eligibility — startup ignores the existing book

A paper row is copyable only if ALL hold:
- `target` in the proven allowlist (env `HL_REAL_TARGETS`, seeded `0xc9c7…`)
- `copy_tier = 'proven'` on the row
- `is_cold_start = 0`
- `detect_ts_ms >= deployment_epoch_ms` — the epoch is written to `hl_meta` at first
  arm; the 127 pre-existing open paper rows (incl. 5 current proven-target rows) are
  **never** adopted.

## 7. Risk rails (all env-config, `.env`)

| knob | default | semantics |
|---|---|---|
| `HL_REAL_ENABLED` | 0 | **Entries-only** kill switch. When 0: no new opens. Exits and reconciliation ALWAYS run — disabling can never strand a position. |
| `HL_REAL_DRY_RUN` | 1 | Full path, intents recorded, nothing submitted. |
| `HL_REAL_PER_POS_USD` | 15 | Notional per position (HL min order $10; skip+record if szDecimals rounding lands below). |
| `HL_REAL_MAX_CONCURRENT` | 5 | Leg-owned open positions. |
| `HL_REAL_MAX_TOTAL_NOTIONAL` | 75 | Sum of leg-owned entry notionals. |
| `HL_REAL_MAX_CHASE_BPS` | 100 | Skip entry if current mark is >100 bps worse than the paper row's entry mark. |
| `HL_REAL_DAY_LOSS_BREAKER_USD` | 20 | **Latched** (persisted in `hl_meta`): once day realized ≤ −$20, entries stop until an operator clears the latch. Software-soft — not a guaranteed loss cap; the guaranteed cap is total notional. |
| entry slippage | 0.5% | explicit on every market open |
| exit slippage | 1.0% | explicit, reduce-only, on every close |
| leverage | 1x isolated | set per coin before first order on that coin |

All closes are reduce-only and touch only leg-owned positions (matched via intent
ledger), never anything else on the account.

## 8. Reconciliation

Every cycle: HL clearinghouse positions vs leg-owned open book.
- position on HL not in book, or book position not on HL, or size mismatch beyond
  rounding → mark `ambiguous`, Telegram alert, block new entries until resolved.
- Never auto-open to repair. Auto-close only when the book says closed but HL still
  holds the position (reduce-only, idempotent by cloid).

## 9. Cadence

Measured: paper loop cycle ≈ 19 s + `sleep(120)` → ~139 s cadence. Change the watchdog
launch flag to `--interval-seconds 30` → ~50 s cadence (no code change; sleep-after-cycle
scheduling kept). Real leg tails at 5 s, so worst-case detection→submission lag is
dominated by the paper cadence (~50 s median).

## 10. Observability

- Heartbeat `data/hl_real.heartbeat` (ts, enabled, dry_run, open_n, intents_pending,
  breaker state) every cycle.
- Watchdog cron `scripts/hl_real_watchdog.sh` (modeled on `hl_copy_watchdog.sh`).
- Telegram: fills, rejects, breaker latch, reconcile faults, kill-switch state changes.
- `hl_real_status` block added to `/api/hyperlive/copy-scorecard` payload (read-only).

## 11. Pre-registered read (sealed until trigger)

Read at **20 filled real closes**. Definitions:
- `real_net_bps` = realized bps net of fees + slippage on the real fills.
- `drift_bps` = `real_net_bps − paper_twin_net_bps` per close (signed).

PASS requires ALL: real mean net > 0; mean drift > **−100 bps**; zero unresolved
reconciliation faults; skip-rate and partial-fill-rate disclosed alongside the verdict.
KILL if at n≥20 the real mean ≤ 0 or mean drift ≤ −100 bps.
No sizing-up discussion before the read fires. No peeking.

## 12. Funding (separate explicit operator action — NOT this build)

**Ordering amended 2026-07-17 (testnet-first was impossible):** HL's testnet faucet and
ApproveAgent require a PRIOR MAINNET DEPOSIT on the same address ("Must deposit before
performing actions"; faucet docs). The account has never deposited, so the ladder is:

1. dry-run soak read (≥24 h);
2. **fund $100**: bridge from the 221 USDC (Solana) via the established Relay path →
   Arbitrum → HL deposit to `0x898B…3A1d`;
3. mainnet agent approval (`hl_real_setup_agent.py`);
4. **mainnet smoke** (`hl_real_testnet_smoke.py --mainnet`, one $15 ETH round trip,
   ~$0.05 cost — more probative than testnet and now the primary gate; the testnet path
   remains available post-deposit if wanted);
5. re-epoch (`hl_real_resolve.py --new-deployment --yes`) so soak-era rows are never
   adopted; then flip `HL_REAL_DRY_RUN=0`.

$121 remains on Solana — above the memecoin $50 min clip, so **memecoin Solana entries
keep running**; the 4663 leg ($166) is untouched. Nothing irreversible: HL withdrawal
back to Arbitrum ≈ 1 min.

## 13. Testing

- TDD throughout. Pure-core tests: cloid determinism, eligibility filter (epoch,
  cold-start, tier, target), sizing/rounding + min-notional skip, chase guard, breaker
  latch persistence, entries-only kill-switch semantics, reconcile diffing, intent state
  machine, crash-recovery settlement (fake order-status).
- Adapter tests with a fake SDK exchange (records calls; asserts explicit slippage +
  reduce-only on every close).
- Testnet smoke script (`scripts/hl_real_testnet_smoke.py`) against
  `api.hyperliquid-testnet.xyz`: place + close one $15 order end-to-end.
- Dry-run soak on mainnet data before arming: intents recorded, zero submissions.

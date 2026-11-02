# Memecoin real-vs-shadow uplift — design spec (2026-07-13)

Branch: `worktree-memecoin-real-shadow-featurestore` (off origin/main @ 3c7a6e5f).
Author: main-loop analysis session. Merge target: `main` (operator-gated deploy for anything touching the live real runner).

## Problem (established, data-backed 2026-07-13)

The paper `real_shadow` arm (mirrors the live real exit rails across 60 wallets) shows
+$1048 mark-to-market / 193 positions; the live `alpha_b`/real arm shows −$77 / 7 positions.
Root causes, in order of $ impact (all from `sniper_tracker.db`, see
`scripts/research/memecoin_real_vs_shadow.py`):

1. **Chain-execution coverage.** Real has been Solana-dominant in practice; 4663 (EVM) only went
   live 2026-07-13 and Base is unfunded. Shadow lifetime PnL by chain: evm-unknown +$335, 4663 +$189,
   solana +$251, bsc +$76. Addressable-but-never-attempted (approved-wallet, post-real-live):
   evm-unknown 20 mints/+$335, 4663 10/+$158, solana 8/+$121. NOTE (corrected in B3): the EVM
   executor + chain-resolution already exist — the gap is funding/allowlist/decimals, not missing code.
2. **Sample + fat tail.** 97% of shadow PnL is 9 `real_tp` hits / 152 closes (avg +$67). At
   N=6 real fills the tail cannot have been sampled; real drew 2 stop-outs + a manual.
3. **Tx-landing reliability.** SCAM: 3 failed swaps → missed a +$181 3×-TP winner → chased in at
   2.5× price → −$25. Retry-chase cost more than every safety gate saved.
4. **Entry slippage.** Mean real +15.4% vs shadow twin; clean same-second fills 0.4–7%, the mean
   dragged by the retry-chase. Not the primary driver, but a measurable drag.
5. **Roster < shadow.** 86 shadow entries (+$92) sit on non-approved wallets — out of scope by design.

Safety-gate scorecard: `token_safety` = net save (blocked 3 dead tokens + 1 marginal), `slippage_cap`
= net positive (saved a −$51 loser, cost TESTIBULL's early +$40), `MAX_CONCURRENT=7` never hit (peak 4).
Gates are second-order; coverage + reliability + N are first-order.

## Design principle

Everything that touches the **live real runner's entry/exit behavior** is DEPLOY-GATED
(operator go + SQLite backup + zero pending reservations + reconciliation, per
`scripts/deploy_memecoin_runtime.sh`). This session BUILDS and TESTS those, DEFAULT OFF, and does
not deploy them. Reversible data/tooling changes (allowlist edits, additive tables, read-only
scripts, brain findings) ship this session.

---

## Tier A — reversible, ship this session

### A1 — Real-vs-shadow diagnostic (DONE)
`scripts/research/memecoin_real_vs_shadow.py` — entry-slippage (real vs shadow twin vs DEX quote),
per-wallet copy-PnL promotion ranking, safety-gate counterfactual. Read-only. Add a smoke test, commit.

### A2 — Wallet promote / demote
Operator-authorized. Write `memecoin_wallets.real_approved` (and leave `real_chains` correct).
Guardrails before writing each wallet:
- **Chain must have a live executor.** Promote only wallets whose dominant shadow chain is `solana`
  (or `4663` once its executor is confirmed landing). Do NOT promote an `evm-unknown` wallet — its
  entries can't route yet; it would only produce `token_safety`/no-route failures.
- **PnL not a single lucky TP.** Require ≥ 2 closes AND positive copy-PnL AND not carried by one
  outlier (inspect trade-by-trade).
- Demote approved wallets ≤ −$40 shadow copy-PnL with repeat stops.
Reversible (flip the bit back). Auto-demote rule remains the live backstop.

### A3 — Signal feature-store  <!-- schema:external=memecoin_signal_features -->
New additive table `memecoin_signal_features`: one row per signal (keyed on the source feed event),
joining **signal-time features** to **forward-return labels** already collected. Idempotent rebuild.

Signal-time features (all computable at entry, from `memecoin_events` / `memecoin_firehose` /
`memecoin_feed_observations_v2`): `their_market_cap`, `their_size_usd`, `is_first_buy`,
`chase_gap_pct`, `lag_s`, `network_id`, `source_user_id`, `was_curated`, `was_real_approved`,
plus derived `mcap_band`, `size_band`, and a `consensus_60m` count (distinct buyers on the same mint
within 60 min before the signal).

Forward labels (from `memecoin_signal_outcomes` / `memecoin_horizon_marks_v2`): `max_gain_pct`,
`peak_horizon_s`, `n_marks`, and thresholded `hit20` / `hit50` booleans. Positions-derived realized
outcome (`real_shadow` twin exit_reason/PnL) joined when present.

The table is the substrate for A4 + B1. It reads only existing tables; dropping it is a no-op.
Register in brain_index; validate schema refs.

### A4 — Feature→outcome scan / report
`scripts/research/memecoin_feature_scan.py` — read-only. Two modes:
- `--bands`: hit-rate + avg-gain by each feature band (mcap, size, first-buy, consensus, chain),
  with N per cell, over the feature-store. Reproduces and refreshes the exploratory separation.
- `--rank`: score recent open signals by their band-implied hit-probability (a transparent lookup,
  not a black-box model) so the operator can see which live signals the data favors.
Honest-N labelling: print the window span and per-cell N; never present a <30-N cell as decisive.

---

## Tier B — live-money path; build + test, DEPLOY operator-gated

### B1 — Entry conditioner (config-gated, DEFAULT OFF)
A pure function `evaluate_entry_features(signal) -> (allow: bool, reason, score)` using feature-store
band lookups. Wire into the real entry path behind `MEMECOIN_ENTRY_CONDITIONER=off|shadow|on`:
- `off` (default): no behavior change. Ships safe.
- `shadow`: log the verdict alongside every real entry decision; do not block. Lets us measure the
  conditioner's would-be effect on live fills before it gates money.
- `on`: block entries whose band-implied hit-prob is below a configured floor.
Unit-tested against fixtures. NOT deployed. Deploy = operator go after ≥ N shadow-mode observations.

### B2 — Solana tx-landing reliability (spec; deploy-gated)
Submit path: `memecoin_real_leg.real_entry` → injected `execute_fn` (Jupiter quote → sign →
write-ahead sig → send → confirm → chain-delta fill); per-stage latency already captured via
`record_exec_latency`. SCAM failure mode: 3 sends failed (no `resolution_reason` → send/confirm
miss), the retry loop then re-entered at 2.5× the original quote and stopped out. Proposed fix
(all behind config, deploy-gated):
- Bounded resubmit with a **fresh blockhash + escalating priority fee** under contention.
- **Chase guard (the actual SCAM fix):** a resubmit must NEVER fill above `original_quote ×
  (1 + slippage_cap)`. If price has run past that, abort the signal instead of chasing the top.
- Emit the abort as a first-class `chase_abort` reason so it shows up in the safety scorecard.

### B3 — EVM coverage (CORRECTED — resolution + executor already exist)
Earlier framing ("no EVM executor") was wrong on inspection. Reality in `memecoin_real_leg.py` +
`memecoin_evm_leg.py` + `settings/memecoin.py`:
- **Chain resolution EXISTS:** `route_real_entry` recovers the chain for `''`/`evm-unknown`
  mints via `resolve_evm_chain` (DexScreener) before allowlist-gating. ~27% of curated EVM buys.
- **EVM executor EXISTS:** `evm_real_entry` (0x router, WETH-denominated, gas-reserve aware);
  4663 is live (funded 2026-07-13, smoke passed) — the 3 `token_safety` 4663 rejections prove the
  path reaches token-safety, i.e. it IS wired.
- **Allowlist = `{8453 (Base), 4663 (Robinhood)}`.** Held out deliberately: **BSC (56)** — 18-dec
  Binance-Peg USDC vs the 6-dec assumption (garbage quotes + fail-open honeypot check); **Eth (1)** —
  gas structurally beats a $10 clip; **Monad (143)** — no confirmed routing.
So the `evm-unknown` +$335 shadow pool splits: the **Base** slice is capturable once Base is funded
+ token-safety is tuned; the **BSC** slice (feature scan: BSC hit20 **29%**, the best EVM band) is
blocked purely by the **18-decimal USDC bug** — the highest-ROI unblock here. Reframed work:
1. Fund Base bankroll + confirm Base real routing end-to-end (executor exists).
2. **Fix per-chain USDC decimals → enable BSC** (unblocks the best-hit-rate EVM band). Needs a BSC
   smoke.
3. Tune 4663 token-safety (3/3 recent 4663 attempts died on it — verify it's not over-blocking;
   counterfactual showed those specific tokens were dead, so it may be correct — measure more).

---

## Verification plan
- Focused pytest for every new/changed module (feature-store builder, entry conditioner).
- `python3 scripts/validate_schema_refs.py` + `python3 scripts/brain_index.py generate` after the new table.
- Re-run the diagnostic + feature scan against the live DB; eyeball cell Ns.
- research-methodologist audit of the feature→outcome claims BEFORE any brain finding is persisted.
- All writes to the live DB (allowlist, new table) are additive/reversible and verified with a read-back query.

## Explicitly out of scope this session
- Deploying anything to `.runtime/memecoin/current` (operator-gated).
- The X-signal feed (owned by `feat/x-source-graph-design` in a separate worktree — no overlap).
- Any exit-rail change (TP/stop/derisk/mirror logic unchanged).

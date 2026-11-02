# Memecoin quant-engine brief — builder handoff (2026-07-24)

Unified assessment + ranked work queue. Sources: live DB reads (2026-07-24), three code sweeps
(entry path, rug surface, wallet curation), exit-brain spec/prereg. Every claim below is measured
or file:line-cited — verify refs before editing, they drift.

## 1. Verdict

Plumbing ~70% done, brain ~30%. Execution/measurement layer is industrial (attempt tracing,
frozen funded contracts, pre-registered exit-brain gates, shadow arms). Every "smart" component
is display-only or shadow-only; the funded path is a binary-allowlist copy mirror with fixed
exit rails, and it is losing money in a specific, fixable shape.

## 2. Ground truth (as of 2026-07-24)

**Real money (88 closes since 07-10):** −$285 total, 55.7% WR, avg win $12.18 vs avg loss
−$22.62 → expectancy −$3.24/trade. `b_stop` bucket −$547/19 dominates losses; TP+trail +$301/26;
manual +$53/24. Zero real losers ever reached +20% (losers were losers at entry; winners got
capped — best win ever $40.13). No open positions; heartbeat healthy.

**Feed signal decay (n≈12k marks, `memecoin_signal_outcomes_v2` + `memecoin_horizon_marks_v2`,
since 07-10):** up-fraction 64% @60s, 59% @5m, 53% @15m, **46% @1h, 39% @6h, 31% @24h** (avg 24h
return −9%). hit20 @1h = 12%. The "signals go up" assumption holds only <15min; the edge is a
latency race with a fat right tail, not a hold.

**Shape proof in our own shadow data:** `momentum_a` (same signal stream, momentum-only exits,
never mirrors sells, 15-min fast-ride) is **+$3,387 all-time on 1,445 closes at 31% WR** and was
the only positive arm in the 07-17→24 week (+$1,987) while control ran −$20k. Low hit rate +
uncapped winners + fast momentum-loss cuts is the winning shape on this stream.

**Funnel:** 3,593 of 3,648 attempts refused in 14d (2,541 `not_approved`), 55 filled — and still
negative. Selectivity is not the problem; exit shape is.

## 3. Architecture reality map

Triggers (only two, both wallet-keyed): fomo global feed → `real_hook`
(`memecoin_runner.py:704-706`), follow-activity slow+fast lanes
(`memecoin_follow_activity.py:305-308`, `memecoin_runner_loop.py:311-362`).

Gate chain (execution order): chain resolution → venue context → per-wallet allowlist
(`memecoin_real_runner.py:1929-1935`) → mcap ceiling w/ resolution cascade (`:1936-2004`) →
consensus-above-mcap veto (`:2016-2036`) → mcap-tiered impact cap → micro concurrency →
staleness → chase guards (`:649-696`) → arm/kill → router/EVM allowlist → atomic
`reserve_entry`/`memecoin_entry_allowed` (`memecoin_risk.py:111-219`: daily-loss latch,
one-per-coin, cooldown, gas reserve, concurrency/exposure/bankroll) → token safety
(`memecoin_real_leg.py:163-170`) → executor. First-come; sizing varies (regime `risk_scale`,
`memecoin_market_state.py:397`), order/priority does not.

Built but NOT wired to money:
- **Exit brain** — propose-only shadow (`memecoin_exit_brain.py:1-13`), swept via
  `candidate_engine.sweep()` (`memecoin_runner_loop.py:620`). Solana permanently
  `DEFER_TO_LEGACY` (prereg §2.1). Monotonic risk-reduction only.
- **Entry conditioner** (`memecoin_entry_conditioner.py:21-41`) — p(hit20) LR gate, default OFF,
  integration point not applied.
- **Radar** — post-hoc rating only (`memecoin_entry_radar.py:71-72`, called at
  `memecoin_real_runner.py:1788`); radar_view/triage/consensus setups are web/display; no
  radar-originated row can become an entry.
- **X intel** — isolated DB + notifications only (`memecoin_x_signal_builder.py:38-47`).
- **Holder-consensus hold** — shadow only, `funded_stop_suppressed: False`
  (`memecoin_real_runner.py:1196-1238`).
- Naming trap: `memecoin_entry_classifier.py` is a post-entry EXIT-overlay tool; the actual
  entry-time classifier is `classify_funded_entry` (`memecoin_market_state.py:306-425`).

Wallet curation: arm = manual, copy-PnL ≥ $25 over ≥ 5 alpha_b closes
(`db_memecoin.py:1691`, thresholds `sniper/settings/memecoin.py:644-645`); auto-demote = −$100
cum real or 4 straight real losses (`memecoin_real_runner.py:1538-1560`). No honest-negative-
alpha demote, no inactivity demote. **No early-vs-connected attribution anywhere in the memecoin
path** — the funding-graph engine exists (`sniper/analysis/winning_wallet_thesis/funding_graph.py`)
but is Polymarket-scoped, imported by zero memecoin modules.

Rug surface: strong pre-buy (Solana `solana_token_safety.py:41-61` mint/freeze/token-2022/sell-
route; EVM `evm_token_safety.py:119-179` code/sell-sim/tax). Holes: no LP-lock or holder-
concentration check on either chain; `depth_pull_flag` permanently None
(`memecoin_exit_market_state.py:139-141`); **catastrophe only fires while quotable**
(`memecoin_real_runner.py:244-257`) — a straight-to-unpriceable rug skips it and sits in the
24h unquotable window (`:273-280`); no post-entry sell-sim recheck; stop worst-case latency
≈ 30s (15s sweep × 2-sweep debounce).

## 4. Work queue (ranked EV-per-effort)

### P0 — Fix the funded exit shape (the whole live-PnL problem)
- n=88 closes is past the registered n≥30 gate: run the post-cutover exit read
  (`docs/superpowers/specs/2026-07-21-memecoin-postcutover-exit-read.md`,
  `scripts/memecoin_postcutover_exit_read.py`) and follow its registered branches.
- Candidate policy change for Solana: momentum_a shape (no win cap, momentum-loss cut, no
  mirror) — already implemented in `memecoin_exits.py:363` rollover machine; the ask is a
  funded-policy A/B proposal through the existing policy-version machinery, NOT a hand edit of
  live rails. See also `2026-07-22-memecoin-stop-derisk-redesign-proposal.md`.
- Hard constraint: no clip-size increase until expectancy is positive over a registered window.

### P1 — Rug hardening (cheap pieces first)
1. Re-run existing EVM sell-sim on OPEN positions each exit sweep — call-site change, function
   exists (`evm_token_safety.py:145-169`). Sell-sim fail → emergency-exit attempt, flagged.
2. Quote-failure on a young position (< N h, not stuck-bagged) → emergency exit attempt, not the
   24h `MEMECOIN_REAL_UNQUOTABLE_EXIT_S` timer.
3. Decode V2 `Burn`/`Mint` events in the tick stream → make `depth_pull_flag` real. This is THE
   direct LP-pull signal; `v2_sync` reserves are already captured (`db_ticks.py` pool_ticks).
4. Solana holder-concentration entry gate: `getTokenLargestAccounts` (one RPC) → top-10 share
   threshold in `token_safety_ok`.

### P2 — Wallet provenance: early vs connected (arming prior, not a verdict)
- Zero-new-capture first: aggregate per wallet from existing signal facts — median `token_age_s`
  at their buys, % first-buy (`fomo_feed.py:48,189`), % sub-60s-of-pool entries. Store on
  `memecoin_trader_analytics` or a small side table.
- Then port the funding-graph channel (deployer-funder linkage, synchronized entries) from
  `winning_wallet_thesis/funding_graph.py` to Solana/EVM funders.
- Use as arming PRIOR: connected-profile ⇒ higher copy-PnL bar (their edge doesn't transfer —
  we are their exit liquidity); early-skilled ⇒ normal bar. Copy-PnL stays the decider; this
  cuts the ~$100/wallet demote tuition.

### P3 — Wire the entry conditioner, shadow-first
- Use the zero-authority shadow-decision-logger pattern just shipped (`241d69b9`): log
  would-gate/would-scale decisions, read at n≥30, then arm. No direct wiring to money.

### P4 — Continuous signal unification (one number sizes money)
- Fold already-computed facts — wallet copy-alpha, `consensus_n_60m`, `token_age_s`, mcap band,
  momentum-since-first-seen, regime — into the existing `risk_scale` multiplier
  (`memecoin_market_state.py:397` → `memecoin_risk.py:83-87,178-181`) instead of adding binary
  vetoes. Shadow-log the proposed scale vs actual first (P3 pattern).

### P5 — Radar as a synthetic wallet
- Radar/consensus-originated setups run as a PAPER arm and must pass the same arming bar as any
  wallet (≥5 closes, ≥$25 copy-PnL) before any funded lane exists. No new risk framework.

### P6 — Demotion upgrade
- Add honest-negative-alpha demote to `check_wallet_demotion` (alert audit: 11/40 armed wallets
  honest-negative; today only real losses demote).

## 5. Solana and the exit brain — why deferred, and the unlock

Not a priority call — a data-honesty gap. The brain's loop is "structure nominates, verified
FLOW confirms"; flow needs per-swap amounts in true event time.

- EVM has it: `eth_getLogs` = complete tape (amounts, V2 Sync reserves, block→time map).
- Solana capture is `logsSubscribe mentions=[mint]` per open position: **slot + signature only,
  no amounts** ("deferred enrichment"), and slots are absent from the EVM-only `block_times`
  map, so even arrival rate can't be honestly windowed (ingest-time windows banned, spec §3.5).
  Fail-closed ⇒ `SOURCE_UNTRUSTED` ⇒ forced legacy (design spec line 55; prereg §1.1/§2.1).

Solana funded exits are NOT unprotected meanwhile — full legacy rails run there (derisk 20%/
bank-50%, 30% trail on `executable_peak_price`, stop 60%, catastrophe 80%, timecap, mirror).
The brain is only a tightening overlay. P0's policy fix needs no tick tape.

Unlock path (in order):
1. **Slot→time:** `getBlockTime` per seen slot, bounded-backfill pattern like the EVM block-time
   worker (64/tick cap). Unlocks honest arrival rate — the exact gate the spec names.
2. **Amounts without per-DEX decoders:** enrich each signature via `getTransaction`; compute
   pre/post token-balance deltas on pool vaults — program-agnostic (Raydium, Pump AMM, Meteora)
   vs decoding each AMM's instruction layout. Unlocks flow + vol ⇒ candidate-capability.
3. **New dated prereg + `feature_version` bump** — required by change control (prereg §5);
   editing v1 in place is a protocol violation.

Scope is already right: the subscription covers exactly open real positions — all the brain ever
reads. Open question for the operator: `getTransaction` RPC spend (Helius key in .env; Geyser
stays a non-goal).

## 6. Hard constraints (builder must not violate)

- Exit-brain candidates: monotonic risk-reduction ONLY (never widen a stop/raise TP/add size/
  reset peak/re-enter); catastrophe 80% untouchable; Solana defers until §5 lands.
- No gate read below n≥30 (prereg §3/§5). No re-slicing after a read.
- Funded behaviour changes ship via policy versions / deploy script, never hand-edits to the
  release-pinned runtime (`.runtime/memecoin/current`).
- No clip-size or exposure increase while live expectancy is negative.
- Funded-contract invariants, regime-is-risk-only, exact-balance exit preview: per CLAUDE.md
  runtime-ops section.
- New thresholds go in `sniper/settings/memecoin.py`, not service files.

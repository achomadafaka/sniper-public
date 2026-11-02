# Memecoin Copy-Mirror — Real-Money Launch Readiness (decomposition / roadmap)

**Date:** 2026-07-09 · **Rev 2** (operator review folded in: corrected arm math, split S0 evidence
streams, added RPC / feed-death / single-instance-lock / paper-real-coexistence subsystems, added
token-safety + shadow economic criterion + fee-inclusive PnL).
**Status:** MAP ONLY — no sub-project specced or approved yet. This document decomposes the
real-money launch into independent sub-projects, their dependencies, the gates that must pass,
and a build order. Each sub-project below gets its own `brainstorm → spec → plan → build` cycle.
**Not** an implementation plan and **not** approval to write execution code.

Related: [re-entry study NO-GO](../../audits/2026-07-09-memecoin-reentry-signal-study.md) ·
brain findings `finding-memecoin-deep-review-2026-07-08`,
`finding-memecoin-reentry-signal-study-2026-07-09`.

---

## 1. Where we are

The memecoin copy-mirror is **100% paper**. The decision logic (copy engine, 3 exit arms,
chase guard, exits) is solid and reviewed. Everything downstream of the *decision* is modeled:

- Fills are DexScreener mid ± modeled slippage (`sniper/execution/solana_quote.py`). No real quote.
- `sniper/execution/solana_swap_executor.py` is a **stub** — `armed=True` raises
  `NotImplementedError`; it never signs or submits.
- No wallet, no keypair, no transaction signing/submission, no on-chain confirmation.
- No memecoin risk caps or kill switch wired (the shared risk framework exists but has no
  `solana_memecoin` venue).
- PnL, cost basis, exits — all computed against modeled fills, never reconciled to chain.

**"Launch real money" is therefore a from-scratch execution + custody + risk build, not a flag
flip.** This is not a reason to slow down — the paper edge looks promising and this is the right
next build — but "very soon" should mean "very soon we *start* the execution layer."

## 2. Two independent gates — BOTH must pass before real capital

These are orthogonal. Building execution does not prove the edge; proving the edge does not build
execution. Do not let excitement about one substitute for the other.

### Gate A — Edge proven (data, not code)
- The arm A/B is gated by **control's close count**, not by momentum_a. Live (2026-07-09):
  momentum_a **77** closed (past the 30–50 gate), alpha_b **33** (entering), control **16** (the
  binding constraint). Control lags **structurally** — 30 open vs momentum_a's 0 — because it
  closes on slow timecaps while momentum_a scalps fast. So "watch control's close accrual" is the
  true Gate-A clock for the arm verdict. Reading before all three clear ~30–50 is reading noise
  (per `project-hl-memecoin-eval-gates`).
- **Two evidence streams, gated differently** (do not conflate — this was the rev-1 error):
  - The **arm A/B verdict** reads `memecoin_positions`/`memecoin_fills`, which record *every* close
    with `realized_pnl`. It is **NOT** gated by capture coverage — only by close counts (above).
  - The **pump / hold-band-forward** reads use `memecoin_signal_outcomes` (forward price paths),
    which is at **55.4%** buy coverage. That hole gates *those* reads; fix it before leaning on
    them, but it does not block the arm verdict.
- The re-entry study already closed one sub-question: **no averaging edge exists** → one entry per
  coin is the evidence-backed policy (see §5).

### Gate B — Execution ready (code)
- The subsystems in §4, validated in **shadow mode** (build real txs, never send, diff intended-vs-
  paper), then **armed on a tiny bankroll** with clean reconciliation, then scaled.

Real money = Gate A ∧ Gate B. Neither alone is sufficient.

## 3. Real-money invariants (decided; carry into every sub-project)

1. **One position per coin, globally.** First curated signal wins; ignore later buys from any
   trader, any arm. The multi-arm, per-(uid,mint) dedup is a paper-A/B artifact. Real money runs
   ONE policy (the A/B winner), one position per mint. Backed by the re-entry NO-GO **and** the
   secondary CONVERGENCE-underperforms-SOLO finding.
2. **No averaging / no re-entry.** Evidence-backed (re-entry study). Removes blended cost basis,
   falling-knife averaging, and makes per-coin exposure trivially one base size.
3. **Fail-closed everywhere.** Any ambiguity (unknown balance, failed quote, stale price, unconfirmed
   tx, reconciliation drift) → do not trade / do not double-submit; alert.
4. **Exit is more dangerous than entry.** A rug/illiquid coin means a real sell that fails or eats
   catastrophic slippage. The paper `_noquote`/`_stale` closes become real stuck bags. Exit
   execution gets the most design and test attention.
5. **Everything armed is operator-gated** behind an explicit flag + a global kill switch, and
   graduates dry-run → shadow → armed-tiny → scale.
6. **Exits must be self-sufficient with the feed dead.** The signal source is an *unofficial* API
   (fomo) that can ban, rotate, or `auth_dead` at any time — and post-review the watchdog is
   deliberately log-only on `auth_dead` (it won't restart a spent token). If the feed dies while we
   hold real positions, the mirror-sell arm goes **blind** — only price-based brackets (SL/TP/
   timecap) still fire. So real-money exits must be fully driven by on-chain price brackets
   independent of the feed, and feed death must page the operator immediately. Treat total loss of
   the signal source as a first-class, expected failure mode, not an edge case.

## 4. Subsystem decomposition

Each is an independent sub-project (own spec/plan). Effort is qualitative — **no time estimates**
(measure, don't guess).

### S0 — Evidence-gate closeout (Gate A prerequisite; data work)
- **Purpose:** make the paper edge readable so Gate A can actually be evaluated. Three concrete
  items, deliberately NOT conflated:
  1. **Watch control-arm close accrual** — the true Gate-A clock for the arm A/B verdict (control
     at 16, needs ~30–50; momentum_a/alpha_b already there). Reads `positions`/`fills`; not blocked
     by capture coverage.
  2. **Root-cause + fix the `memecoin_signal_outcomes` coverage gap (55.4%)** — this gates the
     pump / hold-band-forward reads only, not the arm verdict.
  3. **Then take the pre-registered reads** — arm A/B once control clears; pump/hold-band once
     coverage is fixed.
- **Exists:** capture pipeline (`memecoin_signal_outcomes`), eval gates defined; the arm ledger
  (`positions`/`fills`) already records every close.
- **Missing:** coverage root-cause; the actual verdict reads.
- **Depends on:** nothing (runs in parallel with S1). **Blocks:** the *decision* to spend on Gate B
  at all — if the arm edge is null, execution is moot. (Does NOT block on coverage — see item 2.)

### S1 — Risk / safety envelope (recommended FIRST code piece)
- **Purpose:** the gate every real order passes through; the thing that makes S2+ safe to test.
- **Work:** add a `solana_memecoin` venue to the existing framework
  (`sniper/risk/policy.py`, `models.py`): per-coin cap (`max_asset_concentration_pct` /
  explicit per-mint USD cap), total bankroll cap (`max_total_exposure_usdc`), max concurrent
  positions, min trade size (gas/slippage floor — below which a trade is −EV on fees), daily-loss
  limit, global kill switch, and a real-money `InstanceProfile` (env-driven). Build a
  `RiskContext` from the memecoin DB (bankroll, exposure, concurrent, daily realized PnL) and call
  `evaluate_entry_risk(..., execution_mode="real", venue_limits=...)` before every real entry.
- **Exists:** `evaluate_entry_risk` already implements paper/real guards, bankroll floor, daily-loss,
  concurrency, total + venue exposure, leverage, asset-concentration, market clamp. `RiskContext`,
  `VenueRiskLimits`, `RiskDecision` dataclasses. Mostly a wiring + venue-profile job, not new logic.
- **Missing:** the memecoin `RiskContext` builder; the `solana_memecoin` `VenueRiskLimits` profile;
  the kill switch; wiring the call into the copy engine's real path.
- **Edge cases:** stale exposure snapshot vs a just-submitted tx (double-spend of headroom);
  bankroll = SOL-for-gas vs USDC/token — two balances, both must clear; per-coin cap vs the
  one-position-per-coin rule (belt + suspenders); daily-loss uses real realized PnL (needs S5).
- **Soft dependency:** the real `RiskContext.bankroll_usdc` ultimately comes from S4 on-chain
  balance reads, not the memecoin DB. An env-configured bankroll is a fine interim for design/test;
  note the handoff so S1 doesn't hardcode a DB-derived bankroll that S4 later has to unpick.
- **Depends on:** nothing to design; the daily-loss check needs S5's real PnL to be *accurate*
  (can launch with realized-from-fills interim). **Blocks:** S2/S3 arming.

### S2 — Solana execution: ENTRY (the hard core)
- **Purpose:** turn an approved buy decision into a confirmed on-chain swap.
- **Work:** real quote (Jupiter/Raydium — note our Jupiter quote endpoint was dead; re-select a
  live route source with a real per-token decimals lookup, see the removed-leg caveat in
  `solana_quote.py`), build swap tx, set slippage bps + priority fee / compute units, sign
  (Keypair), submit, confirm (poll signature status), capture the **actual** fill price/qty/tx sig.
  Retry/timeout policy. Replace the `build_swap_tx` stub.
- **Pre-entry token safety (owned work, not an edge-case bullet):** before buying, probe
  mint-authority + freeze-authority (revoked?), token-2022 transfer-fee config, and that a **sell
  route actually exists**. This converts S3's scariest case (a honeypot/no-exit stuck bag) from
  *handling* into *prevention* — don't enter what fails the safety probe.
- **Paper/real coexistence (owner: S2 or S6):** the paper A/B keeps running (evidence keeps
  accruing) alongside the real one-policy loop. They share the fomo poll + the WAF budget that
  already 429s under load, so this is **one poller fanning out** to two consumers, with **separate
  ledgers / tables / instance profiles** (never a shared ledger) and a split rate budget. Design
  the fan-out here so real and paper don't contend or cross-contaminate.
- **RPC:** all of the above needs S4's dedicated RPC (submission/confirmation) — public endpoints
  won't carry an armed loop; see S4.
- **HARD REQUIREMENT — honor the write-ahead-sig protocol.** S1 built the primitive
  (`db_memecoin.set_pending_sig` + `broadcast_at` column) but only S2 can honor it: the S2 entry
  path MUST call `set_pending_sig(pending_id, tx_sig, broadcast_at)` to persist the signed
  signature onto the reservation **before broadcasting**, and `confirm_pending` / `release_pending`
  on the outcome. Without this, S1 §5's stale-reservation reconciliation rule (has-sig → resolve
  on chain; no-sig + old → provably never broadcast → releasable) becomes **undecidable** — a
  crashed reservation can be neither safely released nor resolved. Do not skip; it is the only
  thing that makes crash recovery of in-flight entries possible.
- **Exists:** `solana_swap_executor.build_swap_tx` stub (dry-run contract only); an EVM signing
  pattern to model from in `real_position_redeemer.py` (`account.sign_transaction`).
- **Missing:** everything real — route/quote, signing (`solders`/web3), submission, confirmation,
  priority-fee strategy, slippage config, retry.
- **Edge cases (tackle all):** partial fill; slippage blowout (fill far from quote → abort/limit);
  stuck/unconfirmed tx (submitted but not confirmed → **must not double-submit**); blockhash
  expiry; insufficient SOL for gas mid-flight; MEV/sandwich on entry; token has transfer
  tax/honeypot (can buy, can't sell); wrong decimals (the 1000× trap we already hit); price-source
  manipulation between quote and fill; route unavailable.
- **Depends on:** S1 (risk gate) + S4 (wallet/balance). **Blocks:** live entries.

### S3 — Solana execution: EXIT (the scariest piece)
- **Purpose:** turn mirror-sell / SL / TP / timecap decisions into confirmed on-chain sells,
  honestly, including when the market is against us.
- **Work:** real sell swap for each exit reason; **handle the un-sellable case** — illiquid/rug/
  honeypot where the paper model would book `_stale`/`_noquote`. Define real semantics: retry with
  widening slippage up to a cap, then mark as a stuck bag (real, un-exited) rather than a clean
  paper close. Partial-exit support if we ever mirror scale-outs (currently full-exit).
- **Exists:** paper exit logic (`memecoin_exits.py`) — the decisions are right; only the *fill* is
  modeled.
- **Missing:** real sell submission + confirmation; stuck-bag accounting; slippage-widening exit
  policy; the "cannot exit" state machine.
- **Edge cases:** can't sell (no route/liquidity) → real loss, not a paper 0-PnL; catastrophic
  slippage on exit; sell reverts (tax token); racing a rug (the trader dumps first — our mirror is
  always behind); timecap firing on a coin we can't actually sell; SL that can't execute in a
  crash.
- **Depends on:** S2 (shares the swap/sign/submit machinery), S1, S4. **Blocks:** live — you must
  never enter what you can't exit.

### S4 — Custody / wallet / balance ops / RPC
- **Purpose:** hold keys safely, know our balances, fund/defund, and have a real RPC to talk to.
- **Work:** secure key storage (env/keystore, never in DB/logs), SOL-for-gas + USDC/token balance
  reads, funding + withdrawal runbook, per-run balance preflight. **RPC infrastructure:** a
  dedicated Solana RPC provider (Helius / Triton-class), failover to a second provider, and a rate
  budget — public endpoints will not carry an armed submit/confirm/balance-read loop.
- **Exists:** nothing memecoin-specific.
- **Missing:** all of it, including the RPC layer.
- **Edge cases:** key exposure (the single worst failure); running out of gas SOL mid-session;
  balance vs on-chain drift; wrong-network sends; **RPC outage / rate-limit / lag** (a stale or
  throttled RPC mid-swap is a real-money hazard — must failover or fail-closed, never guess).
- **Depends on:** nothing to design. **Blocks:** S2/S3 (can't sign without a key, can't submit/
  confirm without RPC, can't size without balances).

### S5 — Real-fill accounting + reconciliation
- **Purpose:** the ledger becomes truthful — real fills, real PnL, position == chain.
- **Work:** persist actual fill price/qty/tx sig (extend `memecoin_fills`/`memecoin_positions`);
  compute **fee-inclusive** PnL from real fills — priority fees + tips + swap fees, which the paper
  model captures *none* of (so real PnL is strictly worse than paper by the fee load); reconcile
  open positions against on-chain token balances **and SOL/USDC cash** (not just token balances);
  alert on drift; feed real realized PnL into S1's daily-loss check.
- **Exists:** paper fill/position schema (`memecoin_fills`, `memecoin_positions`), MTM plumbing.
- **Missing:** real-fill capture, fee accounting, cash reconciliation loop, drift alerting.
- **Edge cases:** position says open but wallet shows zero (missed sell / manual move); dust
  balances; a fill we submitted but never recorded (crash between submit and write — ties to S2's
  no-double-submit); decimals in balance math; fees silently eroding a "profitable" paper trade.
- **Depends on:** S2/S3 (produces the real fills). **Blocks:** trustworthy daily-loss limit + any
  real PnL claim.

### S6 — Armed gating + graduation + kill switch + single-instance lock
- **Purpose:** a safe on-ramp; the ability to stop instantly; a guarantee that only one armed loop
  runs.
- **Work:** modes `dry-run → shadow (build real tx, don't send, diff vs paper) → armed-tiny →
  scale`; global kill switch (env + a file/DB flag the loop checks each cycle); operator
  confirmation to arm; per-mode caps.
- **Single-instance lock (HARD requirement before any armed mode):** an **exclusive OS/file lock**
  (e.g. `flock`), not a best-effort PID file. This repo has documented history of watchdog + manual
  **double-runners** — harmless PK-idempotent duplicate rows in paper, but in real money the same
  race is a **double-submitted swap** (double the position, double the loss). The armed loop must
  refuse to start if another holds the lock.
- **Shadow hygiene (structural, not procedural):** shadow mode signs with an **unfunded key** (or
  does not sign at all), so "must not accidentally send" is enforced by there being nothing to
  spend — not by a code path we hope never fires.
- **Exists:** the stub's `armed` flag boundary; paper/real guards in the risk layer.
- **Missing:** the mode ladder, shadow-diff harness, kill switch wiring, confirmation, the exclusive
  lock.
- **Edge cases:** kill switch must halt **entries and be honored mid-exit-sweep**; a partially-armed
  state; two loops racing the lock; shadow-mode sending because the key was funded.
- **Depends on:** S1, S2, S3. **Blocks:** the actual go-live sequence.

### S7 — Monitoring / alerting / failure recovery
- **Purpose:** know immediately when real money misbehaves.
- **Work:** alert on failed tx, reconciliation drift, stuck bag, gas-low, kill-switch trip, daily-
  loss hit; a real-money dashboard; a crash-recovery runbook (what to do with an unconfirmed tx on
  restart).
- **Exists:** heartbeat + watchdog (paper); Telegram alert plumbing.
- **Missing:** real-money alert set, reconciliation surface, recovery runbook.
- **Depends on:** S5 (reconciliation), S6 (kill switch). **Blocks:** unattended operation.

## 5. Settled by prior work (do NOT re-open)

- **Re-entry / averaging:** NO-GO (2026-07-09 study). One position per coin. No blended basis.
- **Clock domain:** position hold-stamps unified on real-UTC; pump path stays host-clock island.
- **Fill honesty:** Jupiter leg removed (dead endpoint + 1000× decimals trap); a real route source
  for S2 must include a real decimals lookup and should start a fresh fill cohort.

## 6. Dependency graph & recommended order

```
S0 (evidence)  ─ parallel ─┐                 (Gate A: decides if Gate B is worth building)
                           │
S4 (wallet/RPC) ─┐         │
S1 (risk) ───────┼─► S2 (entry) ─► S3 (exit) ─► S6 (arming+lock) ─► S7 (monitoring)
                 │         └───────► S5 (reconciliation) ──────────────┘
```

**Recommended first code sub-project: S1 (risk envelope)** — cheapest high-value, extends existing
framework, and is the gate S2/S3 plug into. Run **S0 in parallel** (data work, no code coupling) so
we don't build execution for an edge that turns out null.

## 7. Go-live graduation ladder (Gate B)

1. **Shadow** — S2/S3 build real txs, never send; diff intended fill vs paper model; run days.
   **Economic criterion (this is the bridge between Gate A and Gate B — make it explicit):** shadow
   must output a *number* — the delta between the **real-achievable fill** (real quote + real
   priority-fee + measured quote→sign→land **latency** under congestion, which is *seconds*) and the
   **paper-modeled fill** the edge was measured on. **Graduation requires that delta < the measured
   paper edge margin.** Without this, Gate B only validates plumbing — and real-world latency/fees
   could silently eat the entire edge while every plumbing check still passes.
2. **Armed-tiny** — smallest viable size ($ small), 1–2 coins, full reconciliation each trade.
3. **Scale** — raise per-coin + bankroll caps only on clean reconciliation and a positive
   **fee-inclusive** real PnL sample.

Any reconciliation drift, unexplained failure, or shadow delta ≥ the edge margin → kill, back to
shadow.

## 8. Cross-cutting edge-case register (the "all edge cases" ask)

Consolidated so no sub-project drops one: partial fill · slippage blowout · stuck/unconfirmed tx +
no-double-submit on restart · blockhash expiry · gas-SOL exhaustion · honeypot/transfer-tax token
(can buy, can't sell) · wrong decimals · MEV/sandwich · price-source manipulation between quote and
fill · illiquid/rug exit (real stuck bag) · sell revert · reconciliation drift (position ≠ chain) ·
key exposure · wrong-network send · kill switch honored mid-sweep · daily-loss tripped mid-session ·
runner crash between submit and ledger write · **feed death mid-position (mirror arm blind, brackets
only) + unofficial-API ban/rotation** · **double-runner → double-submitted swap (needs exclusive
lock)** · **RPC outage / rate-limit / lag mid-swap** · **latency (quote→sign→land, seconds) eating
the edge** · **priority-fee/tip load turning a paper-profitable trade real-negative** · **shadow key
accidentally funded → real send**.

## 9. Out of scope (YAGNI for launch)

Averaging/re-entry (settled NO-GO); multi-arm real capital (paper A/B only — real runs one policy);
cross-chain (Solana first); limit orders / advanced routing; auto-compounding.

## 10. Open questions to resolve per sub-project (not now)

- S1: per-coin cap as fixed USD or % of bankroll? starting bankroll + daily-loss numbers?
- S2: route source (Jupiter API vs Raydium SDK vs aggregator) + decimals-lookup source? token-safety
  probe provider (RugCheck-class API vs on-chain reads)? paper/real fan-out owned here or in S6?
- S3: slippage-widening ladder + the "declare stuck bag" threshold?
- S4: key storage mechanism (env vs keystore vs signer service)? RPC provider + failover pair + rate
  budget?
- S6: what's the armed-tiny bankroll and the scale-up trigger? lock mechanism (flock vs advisory DB
  lock)?
- §7: what *is* the measured paper edge margin the shadow delta must beat (comes out of S0's arm
  read)?

# S1 — Memecoin Real-Money Risk / Safety Envelope (design spec)

**Date:** 2026-07-09
**Parent:** [real-money launch-readiness map](2026-07-09-memecoin-real-money-launch-readiness.md) §4 S1.
**Status:** DESIGN — for operator review before an implementation plan. No execution code.
**Scope:** the risk envelope every real memecoin order passes through. It is the gate S2/S3 plug
into and can be **built and tested standalone now** (pure-ish checks over a context + a reservation
ledger); it cannot be **armed** until Gate A names the winning arm (§ policy-agnostic, below).

---

## 1. Purpose

Make it structurally impossible for the real memecoin loop to exceed its risk envelope, to hold a
position it can't afford to exit, to double-spend a cap under concurrent signals, or to keep trading
after a bad day. Everything downstream (S2 entry, S3 exit) calls S1 first and obeys its verdict.

Builds on the **existing** shared risk framework (`sniper/risk/policy.py` `evaluate_entry_risk`,
`sniper/risk/models.py` `RiskContext`/`VenueRiskLimits`/`RiskDecision`) — which already implements
paper/real guards, bankroll floor, daily-loss, concurrency, total + venue exposure, and market
clamp. S1 adds a `solana_memecoin` venue context builder plus three memecoin-specific semantics the
generic evaluator doesn't know about: **SOL full-book exit reserve**, **pessimistic exposure
reservation**, and the **latched daily-loss circuit breaker**.

## 2. Decided shape (from operator review — do not re-open)

- **Fixed USD sizing, not %-of-bankroll.** Percentage sizing auto-scales up after wins (an automatic
  promotion that bypasses the graduation ladder's operator gate) and shrinks toward the fee floor
  after losses. %-sizing is a deliberate **scale-phase** upgrade, not v1.
- **Per-coin cap is an invariant assertion, not a knob.** Under one-position-per-coin + no-averaging
  (map §3), per-coin exposure ≡ per-trade size. Assert `position_notional == trade_size` fail-closed;
  do not expose a second cap.
- **Wallet = bankroll.** A dedicated wallet is funded to exactly the bankroll; `bankroll_usdc ≡ live
  USDC balance`. No virtual-bankroll accounting. The risk check is a **preflight against real
  balances**, not a computed denominator. (Interim for design/test: env-configured balances; real
  reads are S4.)
- **Config home:** `sniper/settings/memecoin.py`, loaded from `.env` (where all `MEMECOIN_*` settings
  live — not `web_settings.py`).

## 3. Configuration (`sniper/settings/memecoin.py`, `.env`-driven)

| Setting | Default | Meaning |
|---|---|---|
| `MEMECOIN_REAL_ENABLED` | `False` | the `real_trading_enabled` flag; real orders rejected unless true |
| `MEMECOIN_TRADE_SIZE_USD` | `10.0` | fixed per-trade clip (= per-coin exposure by invariant) |
| `MEMECOIN_MAX_CONCURRENT` | `6` | max simultaneously-held coins |
| `MEMECOIN_MAX_TOTAL_EXPOSURE_USD` | `60.0` | portfolio cap (redundant with 6×$10 by construction; kept, fail-closed) |
| `MEMECOIN_DAILY_LOSS_LIMIT_USD` | `30.0` | latched daily **realized**-loss circuit breaker (10% of bankroll) |
| `MEMECOIN_MIN_TRADE_FLOOR_USD` | `5.0` *(provisional)* | measured floor (§7 pin 2); also the launch-viability gate |
| `MEMECOIN_MAX_ROUNDTRIP_COST_PCT` | `3.0` | `X` in the floor formula (§7 pin 2) |
| `MEMECOIN_GAS_ENTRY_SOL` | *(provisional)* | SOL reserved for one entry tx |
| `MEMECOIN_GAS_EXIT_SOL` | *(provisional)* | SOL reserved for one exit tx |
| `MEMECOIN_GAS_RETRY_FACTOR` | `2.0` | retry multiplier on exit-gas reserve |

Bankroll is **not** a setting — it is the wallet's live USDC balance. `$300` is the funding amount,
not a config knob. (The `$200–500 / $5–50` posture in `CLAUDE.md` is March-era Polymarket and
historical per repo doctrine; these numbers are chosen fresh, coincidentally in that range.)

## 4. The entry risk check (fail-closed, strict order)

`memecoin_entry_allowed(ctx, balances, pending) -> RiskDecision`. Given a proposed entry for `mint`
at `trade_size = MEMECOIN_TRADE_SIZE_USD`, evaluate in order; first failure wins:

1. **Mode** — `MEMECOIN_REAL_ENABLED` and `execution_mode == "real"`; else reject (paper).
2. **Kill switch** — the S6-owned global flag is not tripped; else reject. (S1 only *honors* it.)
3. **Daily-loss latch** — not tripped for the current real-UTC day (§7 pin 1); else reject.
4. **Size invariant** — `trade_size == MEMECOIN_TRADE_SIZE_USD`; else reject (assertion, not a knob).
5. **Min-floor viability** — `trade_size ≥ MEMECOIN_MIN_TRADE_FLOOR_USD`; else reject (−EV at this
   clip → don't arm). See §7 pin 2.
6. **One position per coin** — reject if `mint ∈ confirmed_open ∪ pending_reservations` (map
   invariant #1: one position per mint globally, first signal wins). The real path builds its own
   gate here, so it must carry this invariant explicitly — the paper engine's dup-open UNIQUE does
   not cover it. Backed belt-and-suspenders by a partial `UNIQUE(mint) WHERE status='reserved'` on
   the ledger (§5) so a check-then-reserve race still can't double-open a coin.
7. **Concurrent cap** — `effective_concurrent < MEMECOIN_MAX_CONCURRENT`, where
   `effective_concurrent = confirmed_open + pending_reservations` (§5).
8. **Total exposure** — `effective_exposure + trade_size ≤ MEMECOIN_MAX_TOTAL_EXPOSURE_USD`, where
   `effective_exposure = confirmed_open_notional + pending_reserved_notional` (§5).
9. **USDC preflight** — `usdc_balance ≥ trade_size + pending_reserved_notional`. (During the window
   where a tx has landed but confirmation hasn't been polled, the balance is already reduced *and*
   the reservation still counts — an **intended** double-count, conservative in the safe direction;
   do not "fix" it.)
10. **SOL full-book exit reserve** (§6) — require `sol_balance ≥ MEMECOIN_GAS_ENTRY_SOL +
    (effective_concurrent + 1) × MEMECOIN_GAS_EXIT_SOL × MEMECOIN_GAS_RETRY_FACTOR`, using
    **effective** (confirmed + reserved) concurrency — NOT confirmed-only, or two in-flight entries
    under-reserve exit gas exactly when the ledger says more positions are landing. Else reject:
    **never hold a position you lack gas to sell.**
11. **Approve + reserve** — on pass, INSERT a pending reservation (§5) and return approved. Checks
    6–9 (the effective-value reads) + this INSERT run in **one SQLite transaction**, re-verifying the
    caps inside it, so check-then-reserve is not a TOCTOU window (S6's single-instance lock is the
    real guarantor; the transaction makes the ledger self-defending). The reservation is written
    **sig-less** here; S2 must persist the signed `tx_sig` onto it **before broadcasting** (§5).

Checks 3, 7, 8 (with effective values) and the mode/bankroll guards map onto the existing
`evaluate_entry_risk`; checks 5, 6, 10 and the latch/reservation semantics are memecoin-specific
wrappers around it. The pure evaluator stays pure — the memecoin builder computes effective/latched
inputs.

## 5. Pessimistic exposure reservation (new semantic)

**Problem:** the map's own S1 edge case — a submitted-but-unconfirmed entry doesn't show up in
`confirmed_open` yet, so two signals in quick succession can both pass checks 6–8 and **double-spend
the cap** (over-concurrency / over-exposure).

**Design:** a persisted **pending-reservation ledger**. At **approve** time an entry reserves its
`trade_size` against exposure and `+1` against concurrency; the checks above use `effective_* =
confirmed + reserved`. Reconcile on confirmation:
- **Confirmed success** → reservation becomes a real open position; pending row cleared.
- **Confirmed failure** (tx failed / blockhash expired / explicit revert) → release the reservation.
- **Unknown / timeout** → **keep it reserved (fail-closed).** Do not release on timeout alone — the
  tx may still land; releasing then re-entering is the double-spend. Resolve via the write-ahead-sig
  rule below, not a timer.

**Write-ahead signature (makes stale reservations decidable).** The reservation is INSERTed
**sig-less** at approve time (§4 step 11), *before* the tx exists. S2 MUST persist the signed
`tx_sig` onto the row (`set_pending_sig`) **before broadcasting**. This makes restart reconciliation
decidable rather than a promise S5 can't keep:
- **no-sig row older than `T`** → the tx was provably never broadcast (crash before sign/broadcast)
  → safe to **release**.
- **has-sig row** → a tx may have landed → **resolve against the chain**; fail-closed (keep reserved)
  until resolved.

Persisted (survives a crash) so a restart doesn't lose track of an in-flight tx. Proposed table +
the one-position-per-coin guard index:

<!-- schema:external=memecoin_pending_entries -->
```sql
CREATE TABLE IF NOT EXISTS memecoin_pending_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mint TEXT, source_user_id TEXT, strategy TEXT,
    reserved_notional_usd REAL,
    reserved_at TEXT, broadcast_at TEXT, tx_sig TEXT,
    status TEXT DEFAULT 'reserved'   -- reserved | confirmed | failed
);
-- self-enforces one-position-per-coin even if the §4 check races
CREATE UNIQUE INDEX IF NOT EXISTS idx_mc_pending_mint_uniq
    ON memecoin_pending_entries(mint) WHERE status='reserved';
```
API: `reserve_pending(...)` (sig-less), `set_pending_sig(id, tx_sig)` (S2 write-ahead, pre-broadcast),
`confirm_pending(id)`, `release_pending(id, reason)`, `pending_totals()` → (count, notional). Restart
reconciliation of stale `reserved` rows against the chain is an **S5/S7 dependency** (noted, not built
here) but is now **decidable** via the write-ahead-sig rule above; S1 defines the ledger + the always-
fail-closed-on-unknown rule.

## 6. SOL floor = entry gas + full-book exit reserve (adjustment)

A per-trade "SOL ≥ gas for the next buy" floor is wrong: it lets us buy until we can't afford to
**sell what we hold**. "Exit is scarier than entry" applies to the gas balance. The floor is
**dynamic**, scaling with open positions:

```
required_sol(N) = MEMECOIN_GAS_ENTRY_SOL + (N + 1) × MEMECOIN_GAS_EXIT_SOL × MEMECOIN_GAS_RETRY_FACTOR
```
where **`N = effective_concurrent` (confirmed + reserved)**, consistent with checks 7–8 — NOT
confirmed-only. Confirmed-only N under-reserves with entries in flight: two in-flight reservations
mean the post-entry book is larger than `confirmed+1`, so the exit reserve would be short exactly
when the ledger says more positions are landing. Check 10 blocks any new entry that would leave us
unable to exit the **entire** post-entry book (with a retry allowance). At the cap (`N = 6`) this
reserves gas for 7 exits × retry — a hard guarantee that every position we hold is always sellable on
gas.

## 7. Spec-pinned semantics

**Pin 1 — Daily-loss latch.** Track realized PnL per **real-UTC calendar day** (matches the runner's
existing `now_real[:10]` boundary). When `daily_realized_pnl ≤ -MEMECOIN_DAILY_LOSS_LIMIT_USD`, set
`tripped_day = today`. **Latched:** once tripped for a day it stays tripped for that whole day, even
if a late fill revises realized PnL back above the line — no un-trip, no flapping breaker. Resets
only when the real-UTC day rolls over. **Halts new entries only**; open positions keep riding their
SL/TP/timecap brackets (panic-flattening the book would realize slippage everywhere at once; the
brackets already bound each position).

**Real-money PnL only.** `daily_realized_pnl` sums **real-money** positions exclusively — never the
paper A/B running alongside (map coexistence item). A mixed ledger would let paper losses trip the
real breaker, or paper wins mask real losses. This requires a mode/instance discriminator on
positions (a column or a separate real table); the **mechanism** is resolved in S5, but the
**requirement is S1's** — the latch is defined over real PnL from day one.

**Pin 2 — Min-trade-floor formula (not a dollar guess).** The floor is the **smallest clip where
measured round-trip cost (priority fees + tips + slippage, both legs) ≤ `X`% of notional**, with
`X = MEMECOIN_MAX_ROUNDTRIP_COST_PCT = 3%` (chosen against the bracket widths). Round-trip cost has a
~fixed per-tx component (fees/tips) that dominates at small clips, so cost% falls as clip grows —
there is a minimum viable clip. The **value** of `MEMECOIN_MIN_TRADE_FLOOR_USD` is a shadow-phase
measurement (provisional `$5`); the **formula and `X` are fixed here.** Launch-viability gate: if the
measured floor `≥` our `$10` clip, real money is −EV at this scale → **do not arm** (or raise the
clip). This is the same economic bar as the map's §7 shadow criterion, applied to trade size.

**Accepted trade-off (stated, not hidden):** a calendar-day reset means a losing streak straddling
midnight gets a fresh daily budget. Acceptable for a circuit breaker at this scale; a rolling-24h
window is a deliberate scale-phase upgrade, not v1.

## 8. Policy-agnostic; what arms it

The envelope caps **size and exposure regardless of which exit arm wins Gate A** — it never reads the
exit policy. So S1 is buildable and testable now against the paper ledger; it only becomes *armed*
when (a) Gate A names the winning arm, (b) `MEMECOIN_REAL_ENABLED=1`, and (c) the S6 kill switch is
clear. S1 owns the caps + honors the real-enable flag; **S6 owns the kill-switch mechanism and the
mode ladder.**

## 9. Interfaces & units delivered by S1

- `build_memecoin_risk_context(conn, balances) -> RiskContext` — effective (reservation-inclusive)
  exposure + concurrency, today's **real-money** realized PnL (latched; real positions only, §7 pin
  1), caps from settings, `venue="solana_memecoin"`.
- `memecoin_entry_allowed(ctx, balances, pending) -> RiskDecision` — the §4 check (coin-dedup + SOL
  effective-N reserve + reservation + latch + delegate to `evaluate_entry_risk`).
- pending-reservation ledger (§5): table + partial `UNIQUE(mint) WHERE status='reserved'` +
  `reserve` (sig-less) / `set_pending_sig` (S2 write-ahead) / `confirm` / `release` / `pending_totals`.
- daily-loss latch state + real-UTC reset (§7 pin 1).
- config in `sniper/settings/memecoin.py`.
- Each unit independently testable; no real execution or real balance read required (balances
  injected; real reads are S4).

## 10. Acceptance criteria (drive TDD)

1. Real order rejected when `MEMECOIN_REAL_ENABLED=0` (paper guard).
2. Two rapid entries: the second is rejected on concurrency/exposure because the first's **pending
   reservation** counts, even though neither is confirmed yet (no double-spend).
3. Pending reservation released on confirmed failure; retained on timeout/unknown (fail-closed).
4. Entry blocked when `sol_balance` can't cover entry gas + `(effective_concurrent+1)×exit_gas×retry`
   — even with USDC and exposure headroom to spare. Pin the in-flight variant: the SOL check fails
   because a **pending (unconfirmed) reservation** pushes the post-entry book over the reserve, even
   though confirmed-only N would have passed.
5. Daily-loss latch trips at the limit, blocks entries, and **stays tripped** after a later fill
   revises realized PnL back above the line; resets on the real-UTC day rollover; open positions are
   untouched (brackets still fire).
6. Size-invariant assertion rejects any proposed notional ≠ `MEMECOIN_TRADE_SIZE_USD`.
7. Min-floor gate rejects a clip below the floor and (as a viability check) flags don't-arm when
   floor ≥ clip.
8. Concurrent/exposure/USDC checks use effective (confirmed + reserved) values throughout.
9. Duplicate-mint entry rejected while the first is merely **reserved** (not yet confirmed) —
   one-position-per-coin holds across the confirmed ∪ pending set, and the ledger's partial
   `UNIQUE(mint) WHERE status='reserved'` backs the check under a race.

## 11. Out of scope (owned elsewhere)

Real swap build/sign/submit + the write-ahead `set_pending_sig` call before broadcast (S2); real
sell + stuck-bag (S3); real balance reads + RPC + custody (S4 — S1 injects balances); real-fill PnL +
the real-vs-paper PnL discriminator **mechanism** + restart reconciliation of stale reservations
(S5 — S1 fixes the *requirement* and the decidability rule, not the mechanism); kill-switch mechanism
+ mode ladder + single-instance lock (S6); alerting (S7); %-of-bankroll sizing and rolling-24h daily
window (scale-phase upgrades).

## 12. Open questions (resolve during planning, not now)

- Exact `MEMECOIN_GAS_ENTRY_SOL` / `MEMECOIN_GAS_EXIT_SOL` provisional values (shadow measures true
  fees; pick a conservative placeholder that over-reserves).
- Latch + reservation state: dedicated tables vs columns on an existing runtime-state row.
- Where `memecoin_entry_allowed` is invoked from once S2 exists (the real copy path seam).

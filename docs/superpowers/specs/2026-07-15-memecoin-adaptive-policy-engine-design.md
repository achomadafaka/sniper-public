# Adaptive Copy Policy Engine — design (2026-07-15)

Status: DESIGN. Operator-directed. Supersedes the fixed-rail + hard-gate model.

## The problem, stated correctly

Today the real leg asks **"is this coin allowed?"** — a binary hard gate (mcap ≤ $30M, else refuse)
followed by ONE fixed exit rail for everything that survives (TP +60% / stop −50%).

That is the wrong question, and the 2026-07-14 gate audit proved it while appearing to prove the
opposite. It measured >$30M coins as worthless (−$1.09 counterfactual) — but it held the exit rail
FIXED at TP+60. **A $50M coin will never hand you +60%.** Under a +15% scalp it may be reliably
positive. The audit measured "big caps don't moon", not "big caps aren't tradeable".

> **The entry verdict is CONDITIONAL on the exit policy.** Asking "is this coin good?" without
> naming the exit is a category error.

The right question: **"which policy, if any, makes this coin +EV — and what is its EV?"** Refuse
only when NO policy is +EV. Everything else gets the policy that fits it.

Corroborating evidence that the fixed rail is the binding constraint, not entry selection:
* Losses are an EXIT problem. Entry duds are cheap (−$7.27 avg). All book profit lives in 25
  positions that ran >+100%, and the −50% stop cut **9 of 24** positions that later traded back
  above entry (real money: 2 of 3; `febu` stopped at −50%, then ran to **+311%**).
* Paper runs with NO safety gates at all and is comfortably profitable on bad fixed rails.
* The optimal fixed TP is already not +60% (+30% yields $386 vs $270) — and a single global optimum
  is itself just the average of policies that should have been conditioned.

## Core design

```
signal (fomo event)
   │
   ├─ features: ZERO extra latency  ──────────────────────────────┐
   │                                                              │
   ▼                                                              ▼
build_signal_context(ev, stats, archetypes, recent)      (pure, no I/O)
   │
   ▼
select_policy(features, POLICY_TABLE)  ── pure O(1) dict lookup ──▶ Decision
   │                                                                 ├ enter: bool
   │                                                                 ├ size_mult: float
   │                                                                 ├ policy: ExitPolicy
   │                                                                 └ cell_id + expected_ev
   ▼
entry executes ──▶ policy PINNED to the position (memecoin_policy_assignments)
   │
   ▼
exit sweep reads the position's ASSIGNED policy, not a global rail
```

Two invariants:

1. **The decision path makes no network calls.** It is a dict lookup over features we already have.
2. **The policy is pinned at entry and is immutable.** A position exits under the policy its entry
   was underwritten with. Changing the table must never re-write the rails of an open position —
   that would make every backtest a lie.

## Why this can be instant

The copy path **already fetches DexScreener stats at entry** (`honest_fill` → `dexscreener_stats`)
to model the fill. The full feature vector is therefore FREE — we are currently discarding most of
the payload.

| Feature | Source | Extra latency |
|---|---|---|
| chain | fomo event | 0 |
| mcap at signal | event `their_market_cap` + dex `marketCap` | 0 |
| **size at signal** (conviction) | event `their_size_usd` | 0 |
| is_first_buy, lag_s, chase_gap | fomo event | 0 |
| liquidity | dex `liquidity.usd` (already parsed) | 0 |
| **volume at signal** | dex `volume.m5 / h1 / h24` — *only h24 parsed today* | 0 |
| **volatility at signal** | dex `priceChange.m5 / h1 / h6` — *not parsed today* | 0 |
| **buy pressure** | dex `txns.m5.buys / sells` — *not parsed today* | 0 |
| token age | dex `pairCreatedAt` (already parsed) | 0 |
| **consensus_60m** | local: distinct curated buyers on this mint / 60m | sub-ms (in-memory ring) |
| **trader archetype** | `memecoin_trader_analytics`, cached in-process | sub-ms (dict) |

Only three parser additions (`priceChange`, short-horizon `volume`, `txns`) stand between us and the
whole vector. **No new HTTP call anywhere on the decision path.**

### Trader archetype
Derived offline from `memecoin_trader_analytics` and cached in-process (refreshed on the existing
follow-sync cadence). The operator's ask — "what type of strategy does the wallet we follow run" —
maps to computable columns already present:

| Archetype | Signature (existing columns) | Implication for OUR exit |
|---|---|---|
| scalper | `median_hold_s` low, `pct_held_under_60s` high | tight TP; their sell comes fast — mirror is live |
| swing | `median_hold_s` hours, moderate `re_entry_rate` | mid TP, wider stop |
| bagholder | `pct_held_over_1d` high | **do not mirror their sell** — they hold losers; use our own rails |
| size-conviction | high `avg_buy_usd`, low `size_cv` | size up; their large buys are the signal |

This is a hypothesis to be FITTED, not asserted. Archetype is a conditioning feature like any other
and must earn its place out-of-sample.

## The policy table

`ExitPolicy` already exists (`memecoin_policy.py`): `tp_pct`, `stop_pct`, `stop_debounce_sweeps`,
`derisk_trigger_pct`, `derisk_fraction`, `trail_mode`, `max_hold_s`, `mirror_sell_mode`.

The table maps **feature cell → ExitPolicy + expected EV + n**. It is DATA, not code:
* fitted offline by the policy-search harness (grid over TP × stop × hold × trail, replayed through
  `sniper/replay/memecoin.py` so the backtest cannot drift from the runner);
* written to `memecoin_policy_versions` (`immutable_hash`, `parameters_json`, `code_commit`);
* loaded once at boot; the runner does a pure lookup.

**Fallback ladder (never a hard refuse on missing data):**
1. exact cell, if `n ≥ N_MIN` → its policy
2. else parent cell (drop the weakest conditioning feature) → its policy
3. else the global best policy
4. **refuse ONLY if the cell's expected EV is negative with confidence** — not because a feature was
   missing. Fail-closed on *data* is what produced the mcap bug (a feed gap dropped sub-$1M winners)
   and the honeypot bug (a check that always failed looked like a working gate).

## Anti-overfitting (this is the whole risk)

We have **4 days** and ~100 coins. A deep table will memorize noise and I will not ship one.

* **Mint-grain dedup is mandatory.** One pumping coin re-signalled by 20 wallets is ONE outcome, not
  20. Row-grain stats on this dataset are pseudo-replication and have already burned this project
  (2026-07-13 methodology audit killed most raw band effects for exactly this reason).
* **Out-of-sample or it doesn't exist.** Fit on 07-11..07-13, test on 07-14; k-fold by coin.
* **Pre-registered n floor per cell**; below it, fall back — do not fit.
* **The bar is: the conditioned table must beat a single global best policy OUT-OF-SAMPLE.** If it
  does not, the honest answer is "one global policy, tuned" and we ship that instead. A shallow
  table that survives beats a deep one that dazzles.
* Shadow mode before money: log what the engine WOULD have chosen, compare to what the fixed rail
  did, for a pre-registered window.

## Phases

| # | Deliverable | Money at risk |
|---|---|---|
| 1 | Parse `priceChange` / short-horizon `volume` / `txns`; persist the full vector to `memecoin_entry_snapshots.features_json` on every signal. Archetype cache. | none — pure capture |
| 2 | Policy-search harness → fitted table + OOS report. Verdict may be "conditioning doesn't survive". | none — offline |
| 3 | `select_policy()` + pin to `memecoin_policy_assignments`; exit sweep honors the per-position policy. **SHADOW mode** (log only). | none |
| 4 | Arm it: policy decides entry AND exit. **Delete the mcap hard gate.** | real |

Phase 1 is unblocked and unconditionally correct — we are discarding free data today, and every day
without it is a day we cannot fit anything. It ships regardless of what Phase 2 concludes.

## Known blocker

**Chain 4663 has no price history** — 323,801/323,801 DexScreener observations are `missing_pair`.
`cd8bd7f3` + `cbb15a80` start filling this via 0x quotes, but a 4663 policy cannot be fitted until
that tape accumulates. Until then 4663 inherits the global policy, and we say so out loud rather
than fabricating a chain-specific rule from nothing.

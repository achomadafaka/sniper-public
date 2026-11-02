# Desk RISK seat: design (Beta, 2026-10-21)

Status: **design only, for the operator before any code.** This is the desk's money exit path.
**Shadow first** (Gamma's ruling, §9): Phase 0 records decisions and never applies them. Phase 1
(live, on a hash half of F) comes only after a pre-registered shadow read.

Seat table (`2026-10-21-llm-trading-desk-design.md` §2): the engine enforces on ticks the rails the
seat set. The seat re-reads at boundaries (fill, rail hit, 1 s structure break, timecap − N) and
may `TIGHTEN(rails)` or `EXIT`. It never adds size and never widens a stop. LP-pull and source-sell
protections stay deterministic and cannot be postponed. The hand-off mechanics are already fixed in
`2026-10-21-desk-execution-plumbing-design.md` §2.3–2.5 (R2 override authority, TIGHTEN in one
transaction, EXIT through the exit intent queue, failure = HOLD). This document adds where the seat
hooks in, what it sees, when it runs, and the full rule-5 edge map.

## 1. Principle: deterministic rails always win

The funded exit sweep (`memecoin_real_runner.real_exit_sweep`) keeps running the position's
effective policy on every tick, with or without the seat. The seat can only make the next tick
stricter: a lower stop distance, a tighter trail, a lower take-profit, or a close. Every seat
failure (slow, down, malformed, refused) leaves the rails exactly as they were. A seat answer is
applied by the **runner**, never by the seat process, and only if it still tightens at apply time.

## 2. Where it hooks in

| Piece | Existing code | Change |
|---|---|---|
| Rails on ticks | `real_exit_sweep` → `effective_exit_override` + `effective_funded_policy` | none |
| Seat's rails | `memecoin_position_exit_overrides` source `desk` (rank 2, after operator; `_EXIT_OVERRIDE_PRIORITY`) | none |
| Catastrophe floor, LP-pull rail, max hold, unquotable exit | frozen contract / deterministic rails | none; the seat cannot reach them |
| TIGHTEN | new: a `desk_risk_intents` row (desk DB) drained by the runner, which calls `set_position_exit_override(conn, pid, 'desk', …, actor='desk-risk:<ticket_id>')` | runner-side apply with the tighten-only check in the same transaction |
| EXIT | `memecoin_manual_exit_intents.enqueue(position_id, action='close', actor='desk-risk:<ticket>')` | none (already runner-owned, not gated by the kill switch) |
| Boundary detection + seat call | new worker `scripts/memecoin_desk_risk_worker.py`, its own process | new |
| Audit | `desk_tickets` stage `risk` (one row per boundary decision) | new stage |

The seat process never writes the funded ledger. Its only outputs are a desk DB row, plus an exit
intent row that goes through the existing queue the runner drains. (The JUDGE worker's courier
already writes manual intents with a short-lived writer. The exit intent follows the same pattern.)

## 3. Boundaries (what wakes the seat)

The worker polls open desk positions (`signal_source='desk'`, `is_open=1`) read-only, every 1 s.

| Boundary | Detected from | Notes |
|---|---|---|
| fill | a new open desk position (fill stage on the ticket) | the first re-read, T seconds after the fill |
| rail hit | a new partial/de-risk fill row for the position | the rail already executed; the seat may tighten the rest |
| 1 s structure break | U tape `candles_1s` for the pool: close below the low of the prior N bars, or N red bars | code-defined, versioned (`structure_rule_version`); the trigger itself is code |
| timecap − N | `opened_at + max_hold_s − N` from the frozen contract | one wake per position |

At most one call per boundary event and per-position cooldown `MEMECOIN_DESK_RISK_COOLDOWN_S`.
Identical inputs are never re-asked (`UNIQUE(ticket_id, stage, inputs_hash)`).

## 4. What the seat sees and returns

Inputs are code-built facts only, the same rule as the JUDGE and CHECKER. They are frozen at the
boundary with `known_at`:
- the frozen rails and the active effective policy;
- fill price, current exact-quote value and its age;
- peak (`executable_peak_price`), PnL %, hold time, time to timecap;
- the last 60 1 s bars (OHLC, buy/sell USD, makers);
- boundary kind;
- remaining distance to each rail.

No ticket free text.

Output, validated in code: `{k, action: HOLD | TIGHTEN | EXIT, rails?: {sl_pct?, trail_pct?, tp_pct?}, reason_code}`.
- **TIGHTEN** names only the rails it changes. Each named rail must be inside
  `MEMECOIN_DESK_RAIL_BOUNDS` and strictly tighter than the active value:
  - smaller `sl_pct` magnitude;
  - smaller `trail_pct`, or a trail added where none was;
  - smaller `tp_pct`.
  Setting a rail to 0 or null is a loosening (off) and is refused `tighten_loosens:<rail>`.
  `derisk_pct`, mcap targets and the catastrophe floor are not in the output.
- **EXIT** is a full close.
- Anything else, including a late answer, is HOLD.

Model: Haiku 5.5 through the JUDGE's Agent SDK adapter (subscription), 5 s deadline. Billing proof
as the JUDGE's.

## 5. Timing: slow or down

- The seat is **off the fill path** and off the exit sweep. A slow call delays only that boundary's
  decision. The rails keep firing on ticks meanwhile.
- **Decision expiry.** A decision carries `expires_at = boundary known_at + MEMECOIN_DESK_RISK_TTL_S`
  (proposal: 10 s). The runner refuses to apply an expired decision (`risk_expired`).
- **Apply-time recheck.** The runner re-reads the active override and the position inside the
  apply transaction:
  - the position is closed → refuse `position_closed`;
  - the TIGHTEN no longer tightens against the active row (another TIGHTEN or the operator moved
    it) → refuse `tighten_loosens`;
  - an operator row is active → refuse `operator_owns` (the operator outranks the seat).
- **A TIGHTEN that crosses the current price** (a new stop above the current loss) makes the next
  sweep close the position. That is allowed (it is a stricter rule) and recorded as
  `tighten_crossed`.
- **Seat down for the whole hold:** the position runs its entry rails to the end, exactly as
  today's desk path does.

## 6. Edge map (CLAUDE.md rule 5, in order)

1. **Money path: YES.** It can close a funded position and change its stop. Every item below
   applies, and the operator sees this before code.
2. **Fill speed.**
   - **Entry path: untouched.** No call, lock or read is added to entry.
   - **Exit sweep:** the runner-side drain adds one indexed read of pending `desk_risk_intents` per
     pass. The read only happens when a desk position is open and an intent is pending; otherwise
     it is a single empty `SELECT`.
   - p50/p90 of that read and of the apply transaction are measured on fixtures before merge and on
     the first live passes, then reported. It goes in only if p90 < 1 ms.
   - The seat call itself runs in its own process.
3. **Concurrency.**
   - The runner is threaded. The apply runs on the exit-sweep thread under the existing exit lock,
     so the read, the tighten check and the `set_position_exit_override` write are one transaction.
     This closes the two-boundaries race (§2.4 item 4).
   - The seat worker holds a flock singleton.
   - A seat EXIT and a deterministic rail exit can race. The exit intent queue already serialises
     closes (`claim_close`), so the loser is refused `already_closing`.
4. **Absent vs zero vs unknown.**
   - `sl_pct` absent in a TIGHTEN means unchanged, never 0. A 0 or null is refused as a loosening.
   - A missing exact quote, or one staler than the TTL, means the seat sees `value: null`. The seat
     may still TIGHTEN or EXIT, and the runner applies only on the tighten rule.
   - Missing bars (tape gap) means no structure-break boundary. A gap never counts as a red bar.
   - An unknown PnL is never 0 in the facts.
5. **Dependency failures.**
   - Model timeout, 429, a dead login (`is_error` success), malformed output or a wrong model echo
     all give HOLD with the cause recorded. HOLD never triggers EXIT and never loosens anything.
   - Desk DB unwritable: no decision, so HOLD.
   - Ledger read failure in the worker: no boundary, so HOLD.
   - Runner apply failure: logged at WARNING, the intent stays pending until expiry, then refused.
   - An answer that is simply wrong can only tighten. Its worst case is an early close at the
     current price.
6. **Units and identity.**
   - Rail percentages are relative to the fill price (stop, tp) and to `executable_peak_price`
     (trail), exactly as `apply_desk_rails` freezes them.
   - Prices are outcome-native.
   - Seconds everywhere, UTC.
   - Identity is `(network_id, mint, position_id, ticket_id)`, checked at apply against the ticket's
     `desk_*` identity, like `reconcile_intents`.
7. **The sample.**
   - RISK changes exits, so it is a new regime for F/SJ. Either RISK is live before cohort U is
     provisioned (with `risk_version` frozen in the policy), or turning it on is a new cohort.
     Never mid-cohort.
   - The SR arms run without RISK, so SJ vs SR includes RISK's effect. To separate it, RISK could
     run on a hash-drawn half of F (`desk-risk-arm-v1`). Gamma to rule.
   - The no-tuning rule applies: H1/H2 outcomes never tune the running RISK.
8. **Rollback.**
   - `MEMECOIN_DESK_RISK_ENABLED=0` stops the worker.
   - The runner drain refuses all pending intents (`risk_disabled`).
   - Active `desk` overrides written by RISK stay, because they are tighter and therefore safe. The
     operator can clear them with `clear_position_exit_override(pid, 'desk')`, which returns the
     position to its frozen contract rails.
   - Half-applied state is impossible: the apply is one transaction.

## 7. The one runnable check per edge case (to build with the code)

- A TIGHTEN that loosens any rail, or sets a rail to 0 or null, is refused, and the active row is
  unchanged.
- An expired decision is refused. A decision on a closed position is refused. An operator row
  blocks the seat.
- Two TIGHTENs at once: the second is re-checked against the first inside the transaction.
- Seat timeout or dead login gives HOLD, no override row, and no exit intent.
- A seat EXIT racing a stop exit: exactly one close.
- The catastrophe floor and the LP rail fire with RISK down. A RISK override cannot zero the
  catastrophe floor (the plumbing test).
- The drain read p90 < 1 ms on a fixture with 5 open desk positions and 50 pending intents.

## 8. Decisions for the operator

1. Approve the model and the 5 s deadline (Haiku 5.5, the JUDGE's subscription seat).
2. The boundary set and the structure-break rule (N bars). Gamma pre-registers N.
3. TTL 10 s and cooldown per position (proposal: 30 s).
4. Phasing (§9): Phase 0 shadow on every desk position, then Phase 1 live on a hash half of F only
   after the shadow read passes. Gamma's ruling, for the operator's yes.
5. Phase 1 needs `risk_version` and `risk_p = 0.5` in the policy before provisioning, or a new cohort.
   Phase 0 changes nothing for F/SJ and needs neither.

## 9. Phasing (Gamma, on ccad8495f)

Tighten-only is safe for the mechanics, not for money. Its worst case, an early close at the current
price, is exactly the cost of cutting a runner, and nothing has measured that yet. An exit change with
no evidence on our own fills goes to shadow before live (lesson 2026-10-01: the Degen exit trial lost
-33.9 % on its first live trade).

**Phase 0, shadow.**
- The seat runs on every desk position at every boundary.
- Each decision (HOLD, TIGHTEN or EXIT) is recorded with its inputs, `inputs_hash` and `decided_at`.
  It is NEVER applied: no `desk_risk_intents` drain, no override, no exit intent.
- Every decision is recorded, including HOLD and refusals, so the read has its denominator.
- The shadow writer is its own process with its own DB (`data/desk/risk_shadow.db`). It never touches
  the runner, so nothing is added to the exit sweep. The drain p90 < 1 ms gate applies at Phase 1.
- The counterfactual comes from the U tape and the position's own marks: for each position, the exit
  price and time the decision would have produced, against the actual rail exit.
- Zero money risk and the full n. `risk_version` stays out of the cohort policy, since F/SJ are
  unchanged.
- The read is pre-registered by Gamma before Phase 0 starts: the structure-break N, then the median
  exit-price ratio and P(Y2 kept), with a UTC-day bootstrap.

**Phase 1, live.** This phase starts only after that read shows the decisions help on OUR desk fills
(median, not mean).
- RISK applies on a hash half of F: `u = sha256("desk-risk-arm-v1|" + ticket_id) / 2^256 < 0.5`. That
  is the only clean estimate of RISK's effect. SJ vs SR cannot isolate it, and turning it on for
  every position would confound it with the JUDGE.
- It needs a new cohort, or `risk_version` + `risk_p` written into the policy before provisioning.
- Power: at most 5 open at a $25 clip, F fills per half are tens, not hundreds. Phase 1 is
  under-powered for small effects; the shadow phase is where the n comes from.

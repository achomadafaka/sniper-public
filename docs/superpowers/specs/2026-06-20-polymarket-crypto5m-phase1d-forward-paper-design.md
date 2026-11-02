# Polymarket crypto5m — Phase 1D: Frozen-Rule Live Forward Paper Harness v0

**Status:** spec. **Approved by GPT Pro** (build + smoke + launch). Shadow/paper only.
**Mode:** no live orders, no private keys, no authenticated user channels, no production DB, no service
install, no model training, no Price-to-Beat rule, no retrospective backfill, no UI. Runs CONCURRENTLY with
the existing capture campaign (do not stop it).

## Purpose

Begin an **immutable, pre-registered, out-of-sample** forward paper record NOW — not to claim edge today,
but so the rules are frozen BEFORE we see outcomes (the only thing that makes a forward test trustworthy).
New ideas create new FUTURE cohorts; they never rewrite the running test.

## Instrument rule (LOCKED — corrected)

- Final settlement = the Polymarket winning token (the real resolved outcome).
- Underlying rule: **Up when Chainlink end ≥ Chainlink start; Down otherwise** (≥, not strict >).
- Binance is a PREDICTOR only — no paper result may be labeled from Binance.

## Frozen v0 configuration (`btc5m-forward-paper-v0`)

| param | value |
|---|---|
| primary checkpoint | 240 s after window start (60 s before resolution) |
| modeled decision→fill latency | 500 ms |
| paper gross notional | $10 USDC per rule per entered market (FIXED gross; fee computed separately, shares unchanged) |
| virtual bankroll | $400 per rule, independent ledgers |
| position limit | one entry per rule per market |
| exit | hold to final Polymarket resolution |
| compounding | disabled |
| maker fills | disabled |

The 30 s-before-close checkpoint (270 s) may be recorded **diagnostically** but is NOT part of v0 pass/fail
and must never be pooled with the 240 s checkpoint.

## Rules (frozen; manifest-hashed; immutable after launch)

Controls (not candidate discoveries, same execution path):
- **C0 random-side** — deterministic side from `sha256(campaign_id + condition_id)` (reproducible).
- **C1 favorite** — higher independent midpoint side; tie/unavailable → no trade; fill at that side's ask.

Candidates (exactly three; no more added this campaign):
- **R1 Binance 30 s momentum** — `r30 = log(binance_price_now / binance_price_30s_ago)`; `>0`→Up, `<0`→Down,
  unavailable/stale→no trade. No magnitude threshold.
- **R2 book imbalance** — per token `imbalance = (bid_notional_near_top − ask_notional_near_top) /
  (bid+ask near top)`; `delta = up_imbalance − down_imbalance`; `≥0.20`→Up, `≤−0.20`→Down, else no trade.
- **R3 agreement** — R1 and R2 both Up→Up, both Down→Down, else no trade.

**Deferred (NOT activated):** any Price-to-Beat / distance / threshold-diffusion rule — only after the
boundary-selection rule locks, as a NEW rule version. Their absence must not block C0/C1/R1/R2/R3.

## Execution (paper)

At the 240 s checkpoint: freeze the known-at-time snapshot → apply the frozen rule → if a side is chosen,
use the first reconstructed book at/after `decision_ts + 500 ms`, walk that token's ask ladder ascending,
acquire up to $10 gross, require complete visible-depth fill (else `paper_no_trade: insufficient_depth`),
record exact shares + levels + book lineage, hold to resolution. NEVER infer the ask from `1 − opposite`.
Every (market × rule) ends as exactly one of `paper_entry` / `paper_no_trade` / `blocked`.

## Fee scenarios (separate PnL tracks over the SAME frozen gross fill)

- **F0 gross** (`fee=0`) — diagnostic only; cannot pass.
- **F1 documented_crypto_fee_scenario** — `fee = shares · 0.07 · price · (1−price)` per ask level, 5-dp
  rounding. Primary v0 cost scenario until canonical fee truth resolves. NOT labeled `observed_actual_fee`.
- **F2 canonical_market_fee** — recomputed over the frozen gross fill when the canonical descriptor
  resolves; never alters prior selections/fills/shares. A rule may not pass on F0 alone.

PnL per track: win → `shares − gross_trade_cost − fee`; lose → `−gross_trade_cost − fee`.

## Persistence (dedicated forward tables; append-only; versioned)

`crypto5m_forward_campaigns`, `_rule_versions`, `_decisions`, `_entries`, `_skips`, `_fee_scenarios`,
`_settlements`, `_rule_results`, `_milestone_reports`. Manifest records campaign_id, start_ts, git_commit,
rule-config hash, rule defs, checkpoint, latency, notional, bankroll, exec/fee/feature/collector versions,
host. Any change to logic/threshold/checkpoint/feature/latency/stake/fill/fee/skip → NEW rule version with a
fresh start_ts; never blend versions; no historical market inserted after freeze.

## Evaluation discipline

Live reporting allowed but DESCRIPTIVE. Formal milestones: 200 (data-health), 500 (preliminary), 1000
(first formal go/no-go). NO naive z-test (optional-stopping invalidates fixed-sample intervals) — use
day-block bootstrap, paired comparison on common eligible windows, and time-uniform confidence sequences for
live monitoring. At 1000: evaluate candidates as a FAMILY (Holm correction), compare each vs both controls,
report gross + fee tracks separately, no checkpoint-shopping. `proceed_candidate` only if documented-fee net
PnL > 0, uncertainty supports a positive mean, beats the random control, beats/adds-value over the favorite
control, survives multiple-rule correction, no single day > 25% of PnL, not concentrated in one vol episode.
Never "production-ready".

## Quarantine (excluded from formal eval)

settlement conflict · missing winner · token-mapping conflict · rule-contract change.

## Harness + launch

`scripts/run_polymarket_crypto5m_forward_paper.py --capture-db <db> --paper-db <db>
--campaign-id btc5m-forward-paper-v0 --continuous`. Reuses the capture spine + feature engine (no duplicate
collector). Per market: detect → schedule 240 s → materialize one frozen snapshot → evaluate C0/C1/R1/R2/R3
→ persist → reconcile settlement later → update descriptive results → restart-idempotent. Launch as an
operator-controlled background/tmux process, not a service.

## Deliverable / commit sequence (to main)

- [x] #1 spec
- [ ] #2 frozen rules + manifest hash
- [ ] #3 fee scenarios + settlement PnL
- [ ] #4 forward persistence (tables + writers)
- [ ] #5 forward harness (decision/settlement loop) + idempotency
- [ ] #6 tests + smoke
- [ ] #7 launch + manifest report

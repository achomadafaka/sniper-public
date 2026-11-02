# Algorithm Discovery v2 — paper-first (decision record)

**Date:** 2026-05-29
**Status:** direction locked (operator-approved); evolves `algo_discovery_v1`
**Supersedes (in part):** the v1 backtest-as-gate (`docs/superpowers/specs/2026-05-29-algorithm-discovery-workflow-design.md` §4.2 backtest stage)

## The pivot

Validate replication by **forward paper-trading**, not historical replay. Backed by the 2026-05-29 feasibility study (four investigations):

- **No liquidity / order-book data exists** in any historical source → a historical "match" proves a trade happened, never that *we* would have filled at that price. This is the exact wall that stalled stink-bid. So replay can never prove executability.
- **Becker trade timestamps are 100% null** (time inferred from block number, ±1–2 weeks absolute) → no minute-precise alignment to events; coarse for selection timing.
- The repo **already has the parallel paper-strategy platform** (`strategy_runner` + plugins + `strategy_positions`; `shadow_copy_v1` is a paper-mirror). Forward paper is both the honest test and already-built plumbing.

**Therefore:** drop historical replay as a *validation gate*. The v1 `backtest_harness` is demoted to a research/diagnostic tool, not a go/no-go.

## What we KEEP from history

Historical profiling/classification stays — it's how we pick *which* wallet to deploy and *what* hypothesis:

- The dossier descriptors (cadence/burst, sizing CV, hour coverage, hold duration, fragmentation) + edge-type hypotheses.
- The new **behavioral classifier** (`classifier.py`) turns those signals into: `algorithmic | discretionary | ambiguous | insufficient_data` + confidence + a route hint. It proposes; it never decides. (External-intel dependence is a *separate* detector that needs live event timestamps — not decided offline.)

## Routed architecture

`profile (dossier) → classify → route → deploy as a paper experiment → forward decision gate`

| Class | Route | Paper experiment |
|---|---|---|
| discretionary / domain-knowledge | `copy_mirror` | mirror the wallet's actual fills (`shadow_copy_v1`); only viable if copy-latency shows edge survives the ~15-min window (slow wallets) |
| algorithmic (self-contained) | `rebuild_rule` | deploy an independent rule strategy that selects its own markets by our guessed parameters; success = its live selections overlap the wallet's |
| external-intel-driven | `flag` | cannot replicate the consumed feed; hand to human+AI |
| ambiguous / insufficient | `human_review` | — |

## Forward-paper rigor (so we don't trade one trap for another)

- **Pre-register** each hypothesis + kill-criteria *before* deploying (no p-hacking the live results).
- Evaluate forward PnL against the **price-implied null** (WR baseline = mean entry price), not raw win-rate — the favorite-buyer trap reappears live (the repurposed `decision_gate` logic is the rubric).
- **Slow truth:** held-to-resolution wallets take weeks to resolve → need parallel slots + interim reads (copy-latency edge-retention, entry quality) before resolution.
- **Multiple-testing:** deploying N and crowning the winner is overfitting-by-selection → pre-register and correct for N.

## Dependencies / open

- Live data path (Polymarket API) for paper deployment is **region-restricted** on the current VPS → runtime stand-up needs the permitted-region migration. Offline classify/route/profile work is unblocked.
- `copy_latency` is **live-capture only** (no historical path) — it's a fetch-time step in the copy-mirror route.

## Build order

1. ✅ behavioral classifier (`classifier.py`) — turns computed signals into a route.
2. Wire classify→route into the orchestrator; replace the backtest-gate tail with route→paper-enrollment.
3. `rebuild_rule` paper strategy (independent-selection plugin from the extracted rule) — the algo route's experiment.
4. Repurpose `decision_gate` as the forward-paper evaluation rubric (price-null + pre-registered kill-criteria).
5. (runtime, gated on geo) stand up parallel paper deployment.

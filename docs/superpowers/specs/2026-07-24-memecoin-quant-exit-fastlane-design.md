# Quant exit fastlane — per-position real-time engine (design)

**Date:** 2026-07-24 · **Status:** DRAFT for build · **Goal:** from the moment a real position
opens, a per-token quant engine reads that pool's live tape (structure, vol, flow, depth,
edge-duration prior) and makes every exit decision — ride, de-risk, take profit, cut — with the
static rails demoted to a wide outer seatbelt (catastrophe / LP-pull / unsellable / data-dark).

## 0. Hardware truth (measured 2026-07-24)

32 cores, 30 GB RAM, machine essentially idle. **Compute is NOT the bottleneck and never will
be**: incremental feature updates (rolling per-slot VWAP, pivot confirm, windowed vol/flow) are
<1 ms per tick for 7 concurrent positions. The latency budget lives entirely in (a) how fast
ticks reach us, (b) chain cadence, (c) exit submission. "Full hardware power" therefore means:
dedicated always-hot processes, WebSocket push instead of polling, zero sleeps on the decision
path, pre-warmed exit routes — not more CPU.

## 1. Latency budget (Solana, the money chain)

| Stage | Today | Target | How |
|---|---|---|---|
| tick arrival | 30 s pull (worker pass) | **~0.4 s push, FREE, NO KEY** | `accountSubscribe` on BOTH pool vaults over `wss://api.mainnet-beta.solana.com` — MEASURED 2026-07-24: notification every slot, median 404 ms, p90 790 ms, keyless. Every other keyless endpoint fails it (dRPC paywalls accountSubscribe; Ankr/1RPC/BlockEden reject the socket; PublicNode ignores it) → single free feed, backed by the always-running pull enumerator (30 s) as verifier + fallback; WS loss degrades to pull + seatbelt, never to blindness |
| amounts decode | batched getTransaction | **0 ms — eliminated** | vault notifications carry the balance directly: per-slot deltas of (coin vault, quote vault) ARE price+flow+depth, no getTransaction on the hot path. Per-slot netting matches the frozen per-slot VWAP grain (S1) exactly — zero fidelity loss vs the registered features. Same-sign per-slot delta = net LP event (depth_pull), opposite = swap flow |
| state update | full recompute per pass | **<5 ms** | incremental: ring buffers per position for price path / flow / arrival windows; pivots confirm append-only |
| decision | 30 s cadence | **same tick** | event-driven `propose()` on every new tick for a held pool |
| exit execution | quote→sign→send on trigger (~2-4 s) | **≤1.5 s** | pre-warmed: refresh exact whole-bag preview every 10 s per open position so a fire is sign+broadcast only; priority fee on exits (speed-roadmap tiering, each rung gated on measured fill delta) |
| **detect→broadcast total** | ~30-45 s | **~1.5-2.5 s** | physical floor without co-location; slot time 400 ms, confirm 1-2 slots |

4663/EVM — CORRECTED 2026-07-24: real chain headers measure ~10 blocks/s (median inter-block dt
~0 s at 1 s stamp resolution, p90 0.5 s) — the earlier "2.3 s blocks" was off ~23×, and 4663 is
FASTER than Solana's 400 ms slots. The per-block `eth_getLogs` poll (~3 s tick) is therefore the
lane's own latency, not the chain's; 4663 has the most tick headroom of any chain we run. Same
event-driven decision + pre-warmed exit applies; a faster 4663 poll is a measured-gain question.
NOTE: the host system clock is +7 h vs chain truth (`data/clock_offset.json` corrects it) —
never compare chain/DB timestamps against the raw system clock.

## 2. Architecture (build list, in order)

1. **Hot tape (Solana WS).** `memecoin_solana_ws.py`: asyncio `accountSubscribe` on the two
   resolved pool vaults per open position (2 subs/position, ≤14 total at MAX_CONCURRENT=7) over
   the free public WS (measured: slot-cadence push, 404 ms median). Per-slot vault deltas →
   in-memory ring + `sol_sync`/flow rows. Pull enumerator (already built, running) stays as
   verifier/gap-filler — push can throttle, pull cannot; divergence is logged and graded.
   NEW dependency: `websockets` (already installed). NEW cost: **zero — no key, no provider.**
   Redundancy upgrade later = a free Helius key as a second subscription, optional.
2. **Incremental engine.** `exit_engine_hot.py`: per-position state object updated per tick
   (O(1)); calls `propose()` on every update. Same pure feature code (`exit_features`) — ring
   buffer feeds it the same series replay uses (one-code-path refusal preserved).
3. **v3 quant policy (new prereg, the actual trader).** Inputs already computed: confirmed
   prior highs/lows, higher-low/lower-high state, realized vol, signed net flow, arrival accel,
   depth + LP-pull, entry mcap band, consensus_n, token age, hold clock. Decision set:
   - **RIDE** while higher-lows hold AND net flow ≥ 0 (no cap on winners);
   - **DERISK** (bank fraction) on first lower-high with vol expansion, or at the edge-duration
     prior (≥50% of winners peak <3 h; 15-min momentum half-life on scalps);
   - **TAKE-PROFIT into strength** on arrival-accel spike + vol blowoff (sell to the bid while
     it exists), not at a fixed +X%;
   - **CUT** on confirmed break below prior low + negative flow (exists today), or LP-pull
     (`depth_pull_flag`) → immediate full exit, or unsellable streak → emergency;
   - every threshold pre-registered from priors already in the brain (edge-duration study,
     translation gates), NOT fitted on the cohort.
4. **Authority inversion (the "seatbelt" deploy).** Funded policy per coin-band becomes the wide
   envelope only: catastrophe 80, LP-pull, unsellable, data-dark fallback. The engine owns every
   decision inside it. Ships as a normal policy version via `deploy_memecoin_runtime.sh`;
   per-position contract stays immutable.
5. **Canary protocol.** First N=5 positions: engine drives exits live, envelope armed, every
   decision logged with full state; sealed read after. Shadow comparison continues on all others.

## 3. What the operator must provide

- ~~API key~~ **Nothing for data** — the measured free WS covers Phase B; a free Helius key is an
  optional later redundancy add.
- Approve the priority-fee budget for exit submission (rung 1 of the speed roadmap).
- Approve the v3 prereg + the envelope policy version (this is the "rails are just a seatbelt"
  sign-off), then the N=5 canary.

## 4. What this does NOT change

Entry path, sizing, risk reservations, funded-contract invariants, kill switch, the read-only /
custody RPC separation, chain-split gate reads. The pull tape + 30 s worker keep running as the
verification layer the WS lane is graded against.

## 5. Phasing honesty

Phase A (buildable now): items 2+3 event-driven off the PULL tape — decision latency drops from
30 s to tape freshness (~30 s Solana pull / ~3 s EVM). Phase B (buildable now — the key
requirement DIED 2026-07-24, measured free WS works): ~0.4 s ticks, ≤2.5 s detect→broadcast on
Solana. Phase C: authority inversion + canary (operator sign-off). Nothing waits on a purchase.

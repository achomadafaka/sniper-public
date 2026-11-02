---
memory_name: plan/wallet-intelligence-rearchitecture-2026-04-24
memory_type: reference
memory_description: Staged plan for replacing insider_score pipeline with archetype + tag analytics + conditional microstructure anomaly detector. 18 tasks across 7 lanes. Opening move A1+A2+A3+B1 parallel.
memory_tags: [wallet, architecture, plan, discovery, curation, anomaly, deprecation]
---

# Wallet Intelligence Rearchitecture — Staged Plan

**Date:** 2026-04-24
**Context:** Archetype system (5-layer wallet brain, tag-based classifier) now
ships. Old `insider_score` / `user_analysis` / `flagged_users` / Scout pipeline
is obsolete. Time to rewire.

Goal: **discover → analyze → curate → act (copy / ignore / derive) → validate →
deploy**, with the archetype pipeline as the analytical core and a
microstructure anomaly detector as the real-time trigger. Human-in-the-loop at
curation. LLM agents + automated workers continue to do dossier summarization,
spec drafting, edge scoring.

---

## Architecture (target)

```
                 +-------------------+
                 |  Discovery crawl  |  (lb_pnl + volume, crude)
                 +---------+---------+
                           v
              +------------+-------------+
              |   Archetype extractor    |  (5-layer brain, tag analytics)
              |   → wallet_archetypes    |
              +------------+-------------+
                           v
              +------------+-------------+
              |      Curation (CLI)      |  (human-in-the-loop)
              |   → followed_wallets     |
              |     + curation_log       |
              +---+--------------------+-+
                  |                    |
           copy   |    derive          |   ignore
                  v                    v
        +---------+--------+   +-------+---------+
        | followed_wallets |   |   strategies/   |
        |  (archetype gate)|   | derived/<slug>  |
        +--------+---------+   +--------+--------+
                 |                      |
                 v                      v
   +-------------+------------+   +-----+------+
   |  anomaly_signals (opt)   |-->| paper/real |
   |  microstructure detector |   |  trader    |
   +--------------------------+   +------------+
```

Legacy pipeline (`user_analysis`, `flagged_users`, `position_signals`,
`candidate_users`, `analyze_users.py`, `db_discovery.py`, `insider_copy_v1`,
Scout, insider-score alerter bands) sunsets per stage D.

---

## Task table

Effort: **S** ≈ one session, **M** ≈ a ship, **L** ≈ multi-ship with its own plan.

| ID | Task | Output artifact | Deps | Effort |
|----|------|-----------------|------|--------|
| **A1** | **Deprecation audit** — grep every reader of `user_analysis`, `flagged_users`, `position_signals`, `candidate_users`, `analyze_users.py`, `db_discovery.py`, `insider_copy_v1`, Scout, and insider-score bands; produce a (legacy → new source) matrix per reader. | `docs/audits/2026-04-24-legacy-taxonomy-readers.md` | — | S |
| **A2** | **Anomaly feature study** — pick 4 candidate pre-trade features (slope, notional/vol ratio, time-to-resolution, market-news gate). Backfill over last 90 days of fills. Correlate each with post-trade drift-to-resolution. Output go/no-go for Option B. | `docs/research/2026-04-24-microstructure-anomaly-viability.md` + notebook / CSV | — | M |
| **A3** | **UI redesign design doc** — page map (review queue, wallet dossier, anomaly stream, strategy authoring, legacy-panel retirement), component recyclability matrix from current dashboard, flow diagrams. No frontend code. | `docs/design/2026-04-24-operator-surface-redesign.md` | — | M |
| **B1** | **`tag_wallet_pnl`** (tag analytics Phase 2). New table + builder + tests. Per (wallet, tag): n_positions, realized_pnl, win_rate, avg_hold_days, conviction_concentration. Min sample ≥ 3. | `sniper/analysis/tag_wallet_pnl.py` + `scripts/tag_analytics.py` extended + `tests/test_tag_wallet_pnl.py` | — | M |
| **B2** | **`tag_archetype_fit`** (tag analytics Phase 3). Per (primary_tag, archetype): n_wallets, median_realized_pnl. | same modules extended + tests | B1 | S |
| **C1** | **`wallet_archetypes` schema + writer** — new table with envelope_json + typed projection columns (archetype, primary_tag, confidence, updated_at). Upsert from `extract_wallet_archetype`. | migration + `db_wallet_archetypes.py` + tests | B1 | S |
| **C2** | **Discovery crawl** — pulls Polymarket leaderboard (top N by lb_pnl), dedupes against known wallets, queues for archetype extraction, writes to `wallet_archetypes`. Daily cron. | `scripts/discover_wallets.py` + systemd timer + tests | C1 | M |
| **C3** | **Curation CLI** — `scripts/curate.py next/approve/reject/list/show`. Renders envelope, prompts action (copy / ignore / derive), writes `followed_wallets` + new `curation_log` table with reason. | `scripts/curate.py` + `curation_log` migration + tests | C1 | M |
| **D1** | **Migrate alerter.py** — replace insider-score bands with archetype-gated signals from `followed_wallets` + (when live) `anomaly_signals`. Dual-track logging for 4 weeks. | PR on alerter.py + regression tests | A1, C3 | M |
| **D2** | **Migrate paper_trader.py** — replace `position_signals` / insider-score gate with archetype-gated signals. Dual-track the insider-score gate alongside for A/B. | PR on paper_trader.py + tests | A1, C3 | M |
| **D3** | **Freeze `user_analysis` / `analyze_users.py`** — stop writes, retire service, leave reads until D4. | systemd unit disable + commit | A1, D1, D2 | S |
| **D4** | **Delete `flagged_users`, `candidate_users`, `position_signals`, `analyze_users.py`, `db_discovery.py`, `insider_copy_v1` plugin, Scout strategy config.** All readers already migrated per D1/D2. | one or more PRs | D1, D2, D3 | M |
| **E1** | **Anomaly detector** — rewrite of `stream_low_buys.py` fire logic around the features validated in A2. Fire rate <10/day. Writes `anomaly_signals`. **Only if A2 returns go.** | rewrite of `stream_low_buys.py` + `anomaly_signals` table + tests | A2 (go) | L |
| **E2** | **Wallet-context lookup** — when anomaly fires, look up wallet in `wallet_archetypes`. Context only, not gate. Confidence boost if known + positive archetype. | extension of E1 | E1, C1 | S |
| **F1** | **Execution rewire** — paper/real trader consumes `anomaly_signals` + `followed_wallets` archetype gate. Size by confidence. 4-week dual-track vs legacy. | PR on paper_trader.py + real_trader.py | D2, E2 | M |
| **G1** | **Strategy candidate surfacer** — query `wallet_archetypes` + `tag_wallet_pnl` for wallets with (high conviction_concentration) × (positive realized_pnl on primary_tag) × (confident archetype). | `scripts/find_strategy_candidates.py` + tests | B1, C1 | S |
| **G2** | **Draft-spec generator** — takes a candidate + its brain data → produces a strategy-spec template in `docs/superpowers/specs/derived/<wallet-slug>.md`. Human fills in entry/exit logic. | `scripts/draft_strategy_spec.py` + template + tests | G1 | M |
| **G3** | **Strategy plugin skeleton** — once human approves the spec, scaffolds `strategies/derived/<slug>_v1.py` with registry wiring + empty on-signal method. | `scripts/scaffold_strategy_plugin.py` + template + tests | G2 | S |
| **H** | **UI build** — per A3's design. Multiple ships. Out of scope for this plan. | — | A3 + F1 | L |

---

## Dependency graph (parallel lanes)

```
Lane 1 (research, all parallel, no blockers):
  A1 ───────────────┐
  A2 ───────────────┤
  A3 ───────────────┤
                    │
Lane 2 (tag analytics):
  B1 ─> B2          │
                    │
Lane 3 (core pipeline, converges):
  B1 ─> C1 ─> C2    │
            └─> C3 ─┤
                    │
Lane 4 (deprecation):
  A1 + C3 ─> D1, D2 (parallel) ─> D3 ─> D4
                    │
Lane 5 (anomaly, only if A2 go):
  A2 ─> E1 ─> E2    │
                    │
Lane 6 (execution):
  D2 + E2 ─> F1     │
                    │
Lane 7 (strategy derivation):
  B1 + C1 ─> G1 ─> G2 ─> G3

Lane 8 (UI):  A3 + F1 ─> H  [deferred]
```

**Opening move**: A1, A2, A3, B1 all parallel. First results unblock C-lane
and D-lane. E-lane is conditional — starts only after A2 returns go.

---

## Explicit non-goals (this plan)

- No frontend code. UI design doc only (A3); build is H, out of scope.
- No real-money trading changes until F1 dual-track shows lift for ≥ 4 weeks.
- No fully-automated strategy derivation. G produces candidates + drafts;
  human writes strategy logic.
- No deletion of anything in D4 until D1/D2 ship and sit for 2 weeks green.
- No changes to existing active strategies (#21 Political NO Value, #25 Scout)
  during A/B/C. Scout freeze is part of D4.

---

## Real-money gate (tighter than before)

Under the new system, real-money requires:
- N ≥ 300 archetype-gated closed paper trades (up from 150)
- Slip-adjusted positive PnL for 90 consecutive days (not just cumulative)
- Statistically significant lift over random-baseline archetype selection
- Holdout-wallet validation: train on 80%, verify WR holds on 20%

The prior bar (N=150, WR≥60%) is retired with the insider_score model.

---

## What this plan does NOT decide

- **Exact anomaly features** — deferred to A2 output
- **Curation UI vs CLI long-term** — CLI now; UI in H per A3
- **Exact archetype thresholds for copy-gate** — deferred to D2 dual-track data
- **Whether B (any-wallet anomaly) or A (followed-wallet only) wins** — A2 decides

Each open item has an explicit stage that resolves it. No hand-waving.

---

## Next action

Spin up A1, A2, A3, B1 in parallel. All four are S/M effort, all four
unblock something. First convergence point: A1 + B1 + C1 + C3 produces a
working discovery-to-curation loop, even before anomaly detection exists.

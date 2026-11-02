# Wallet-Analyzer Migration Plan

> **Status:** roadmap / framing document. Not a TDD step plan.
> Each migration phase will spawn its own `superpowers:writing-plans` TDD plan
> when the dependency map for that phase is locked.
>
> **Audience:** every Claude / Codex / human agent touching wallet analysis,
> alerter, paper trader, signals, or downstream readers.
>
> **Source audits (2026-04-28, six parallel Explore agents):** new analyzer state,
> legacy scoring core, alerter, paper trader, signal pipeline, downstream readers.
> Citations inline as `file:line`.

**Goal:** Make the new wallet analyzer the source of truth for wallet analysis
and wallet usefulness. Retire legacy `insider_score` / `wallet_quality_score` /
`classification` / `flagged_users` / followability-gate logic from primary
decision paths. Keep the *infrastructure* (alerter, paper trader, signal
upsert, replay engine, dedup, watermarking, Telegram). Reframe any still-useful
old logic as **one** isolated anomaly-trade signal strategy that does **not**
authorize copying, paper-trading, or wallet ranking.

**Architecture:**
- Old monolith (`analyze_users.py`, ~158KB) writes legacy score columns into
  `user_analysis`, `flagged_users`, `position_signals`, `score_history`,
  `signal_history`. Multiple downstream systems treat those columns as wallet
  quality.
- New analyzer (`sniper/analysis/wallet_protocol/`) implements
  Wallet-Analysis-Protocol v2 + 5-layer brain envelope + tag analytics.
  The analysis core is substantially built; **runtime source-of-truth
  integration is not**. Missing pieces are persistence, API/MCP surface,
  write-gates, consumer cutover, and final analyzer contract decisions.
- Migration is sequenced: persist new outputs → expose them → switch each
  consumer one-at-a-time → quarantine old logic → archive.

**Tech stack:** SQLite WAL (sniper_tracker.db) + Supabase brain memories +
Polymarket Data API + Goldsky subgraph + Flask blueprints + Sniper MCP tools +
Claude subagents under `.claude/agents/`.

---

## 0. Hard rules (override defaults during migration)

1. **Old scores ≠ wallet quality.** No new code path may treat `insider_score`,
   `position_insider_score`, `wallet_quality_score`, `speculator_score`, or
   `classification` as wallet quality, copy authorization, or paper-trading
   authorization.
2. **No deletes before the dependency map for that consumer is closed.** This
   document IS the dependency map for the cutover; per-phase plans will close
   it for their slice.
3. **Old logic that is empirically useful (e.g. position 90+ entry-timing
   signal showed 65.5% WR / +$343 over 37 trades — `MEMORY.md`) may be
   resurfaced ONLY as an anomaly-trade plugin** under `strategies/`. It runs
   inside the new architecture, not parallel to it.
4. **No synthetic composite scores anywhere in the program — not in the
   analyzer, alerter, signals, paper trader, real trader, strategies, MCP
   tools, API responses, UI, agents, or sizing.** A *synthetic composite
   score* is any value produced by combining arbitrary metrics with
   intuition-chosen weights and then comparing it to an intuition-chosen
   threshold. `insider_score`, `position_insider_score`, `speculator_score`,
   `position_speculator_score`, `wallet_quality_score`,
   `followability_cached`, numeric `wallet_edge_tier`, and any
   `*_severity_score` / `*_priority_score` / `*_confidence_score` of the
   same shape are banned as authority and as outputs. Layer 5 verdict is
   an enum + reason codes, not a number.

   **What is still allowed:**
   - **Direct measurements with units** — `realized_pnl_usdc`, `win_rate`
     (fraction), `sample_size`, `days_since_last_trade`, `latency_ms`,
     `fill_rate`, `p_value`, Stage-2 CI bounds (`H1_ci_lower_usdc`, etc.).
     These are observations, not scores.
   - **Cohort ranks as context only, never as authority** —
     `wallet_tag_fit` four-component normalized rank is allowed for
     **ordering candidates within an already-authorized set**, sizing,
     and human-readable explanation. It must never gate, veto, promote,
     demote, or globally rank a wallet on its own. The set is authorized
     first by `wallet_layer5_decision.action_decision`; only then does
     `wallet_tag_fit` order or size within it. (See § 4.2.) Rename
     outputs that read like scores (avoid suffix `_score` where it
     carries no units).
   - **Internal arithmetic that resolves to an enum at the boundary** —
     a function may multiply numbers internally if the value it returns
     and other code consumes is an enum (e.g. severity ∈ {critical, high,
     medium, low}). The number must not leak across the function boundary
     or into a stored column.

   **The school-project pattern that is banned:**
   ```
   score = w1*a + w2*b + w3*c           # weights from intuition
   if score >= THRESHOLD:                # threshold from intuition
       authorize(...)
   ```
   No code anywhere in the live runtime may match this shape. Migration
   replaces every instance with either an analyzer enum, a Stage-2
   hypothesis verdict, or a direct-measurement comparison with units.
5. **Honor parallel-work guardrails** (`CLAUDE.md` § Parallel-work). The
   migration touches files three subsystems share. Foreign WIP → named
   `wip/foreign-…` branch, never a stash.
6. **Legacy tables are migration scaffolding, not truth.** `user_analysis`,
   `flagged_users`, and score-bearing `position_signals` columns are not
   authoritative and should not be preserved for their own sake. Extract only
   proven infrastructure data needed by the new analyzer or live services.
7. **Feature tables do not authorize by themselves.** `wallet_tag_fit` and
   `wallet_tag_pnl` are evidence/context for explanation, sizing, ordering
   *within an already-authorized set*, and strategy-specific rules. They do
   not independently pass, veto, promote, demote, or rank a wallet or trade.
   Authorization comes from `wallet_layer5_decision.action_decision` first;
   feature tables refine inside that set.

---

## 1. Current architecture map

### 1.1 Three architectures coexisting today

```
                     ┌──────────────────────────────────────┐
                     │   LEGACY decision core (RETIRING)    │
                     │                                      │
                     │  analyze_users.py  ~158KB monolith   │
                     │   ├─ _score_insider                  │
                     │   ├─ _score_speculator               │
                     │   ├─ _score_position_insider         │
                     │   ├─ _score_position_speculator      │
                     │   ├─ _compute_wallet_quality_score   │
                     │   └─ _classify_wallet                │
                     │                                      │
                     │  writes:                             │
                     │   user_analysis (68 cols)            │
                     │   flagged_users (25 cols, redundant) │
                     │   position_signals (53 cols, mixed)  │
                     │   score_history (audit, unused read) │
                     │   signal_history (replay-only read)  │
                     └──────────────┬───────────────────────┘
                                    │ score columns
                                    ▼
       ┌────────────────────────────────────────────────────┐
       │ Mixed consumers — infrastructure + decision logic  │
       │                                                    │
       │  alerter.py            (gate + Telegram + dedup)   │
       │  paper_trader.py       (gate + lifecycle + PnL)    │
       │  real_trader.py        (gate + execution)          │
       │  strategy_runner.py    (gate + plugin host)        │
       │  strategies/insider_copy_v1.py  (legacy-named)     │
       │  strategies/fade_v1.py          (legacy-coupled)   │
       │  routes/api_*.py       (response envelopes)        │
       │  static/app.js         (badges + sorts + filters)  │
       │  mcp_tools/read_tools.py (get_wallet_profile etc.) │
       │  agents/*-watchdog, paper-trade-analyst            │
       └────────────────────────────────────────────────────┘

                     ┌──────────────────────────────────────┐
                     │   NEW analyzer (BECOMING SoT)        │
                     │                                      │
                     │  sniper/analysis/wallet_protocol/    │
                     │   ├─ analyze_wallet.py               │
                     │   ├─ fetch.py                        │
                     │   ├─ stage1_gates.py (R1–R3, F1–F4)  │
                     │   ├─ mode_router.py (held|flipper)   │
                     │   ├─ held_analyst.py (H1–H7)         │
                     │   ├─ flipper.py (FH1–FH6)            │
                     │   ├─ tag_inference.py (H5 + nav)     │
                     │   ├─ statistics.py (winsorized boot, │
                     │   │   CUSUM h=25, BH-FDR q=0.10)     │
                     │   ├─ capital_ledger.py               │
                     │   ├─ subgraph_reader.py (fallback)   │
                     │   ├─ exec_profile_adapter.py         │
                     │   └─ report.py                       │
                     │                                      │
                     │  sniper/analysis/                    │
                     │   ├─ tag_wallet_pnl.py  (✓)          │
                     │   ├─ wallet_tag_fit.py  (✓)          │
                     │   ├─ tag_analytics.py   (stub)       │
                     │   └─ wallet_behavior.py              │
                     │                                      │
                     │  scripts/wallet_archetype_extractor  │
                     │  scripts/run_wallet_protocol         │
                     │  scripts/build_wallet_tag_pnl        │
                     │  scripts/build_wallet_tag_fit        │
                     │                                      │
                     │  emits:                              │
                     │   ProtocolReport (Python only,       │
                     │     no DB persist yet)               │
                     │   wallet_tag_pnl    (✓ persisted)    │
                     │   wallet_tag_fit    (✓ persisted)    │
                     │   capital_ledger (in ProtocolReport) │
                     │   knowledge/wallets/<addr>.md        │
                     │     (dossier-writer agent)           │
                     │                                      │
                     │  schema present, NOT YET WRITTEN by  │
                     │  new analyzer:                       │
                     │   wallet_registry      (12 cols)     │
                     │   wallet_memberships   (9 cols)      │
                     │   tag_nav_category     (~50/600)     │
                     │   tag_market_stats     (proposed)    │
                     └──────────────────────────────────────┘
```

### 1.2 Code that belongs to each architecture

**Legacy (retire from primary path):**
- `analyze_users.py` (whole file is the legacy scorer)
- `archive/classifiers/wallet_analyzer.py`, `wallet_classifier.py`,
  `insider_detector.py` (already dead — keep parked)
- Score columns: `insider_score`, `speculator_score`, `wallet_quality_score`,
  `wallet_quality_eligible`, `wallet_quality_fail_reason`,
  `position_insider_score`, `position_speculator_score`, `classification`,
  `position_classification`, `tier`, `wallet_edge_tier`,
  `followability_cached`, `qualifying_roi`
- Tables fully owned by legacy: `flagged_users`, `score_history`
- Tables partially owned: `user_analysis` (mixed: identity + scores), `position_signals` (mixed: market data + scores), `signal_history` (replay anchor + score snapshot)
- Web-settings env vars: ~30 thresholds gating legacy scoring (Appendix A.4)
- `.claude/agents/scoring-watchdog.md` (already RETIRED per its frontmatter)
- `.claude/agents/paper-trade-analyst.md` (queries `wallet_quality_score`)
- ADR-006 + ADR-007 (CLAUDE.md): policy of legacy two-score system

**New analyzer (analysis core substantially built; runtime SoT not):**
- `sniper/analysis/wallet_protocol/*` (all modules — see § 1.1)
- `sniper/analysis/tag_wallet_pnl.py`, `wallet_tag_fit.py`, `wallet_behavior.py`
- `scripts/wallet_archetype_extractor.py` (Layers 1–2; Layer 3–5 partial)
- `scripts/run_wallet_protocol.py` (CLI entry)
- `scripts/build_wallet_tag_pnl.py`, `build_wallet_tag_fit.py`
- New tables (schema present): `wallet_registry`, `wallet_memberships`,
  `wallet_tag_pnl`, `wallet_tag_fit`, `wallet_tags`, `market_tags`,
  `tag_nav_category`
- `.claude/agents/wallet-dossier-writer.md` (partial — reads archetype envelope,
  not yet ProtocolReport)
- Specs: `wallet-analysis-protocol-v1.md`, `v2.md`, bulletproofing, eatpraylove
  gaps, brain-design 2026-04-22, execution-profile v1+v2,
  taxonomy-migration, tag-analytics, tag-wallet-pnl, tag-archetype-fit

**Mixed (infrastructure that consumes legacy → must migrate inputs, not delete):**
- `alerter.py` (Telegram + watermark + dedup = INFRA; insider/score gates =
  DECISION-LOGIC to migrate)
- `paper_trader.py` (lifecycle + sizing + PnL = INFRA; entry gates 0a/0b/0c/0e
  + sizing inputs = DECISION-LOGIC)
- `real_trader.py` (execution + on-chain = INFRA; entry gates = DECISION-LOGIC,
  but real trader is paused, lower urgency)
- `strategy_runner.py` (plugin host = INFRA; squashing + confidence scoring
  reads `position_insider_score` = DECISION-LOGIC)
- `strategies/insider_copy_v1.py` (entire plugin is legacy-coupled; the
  *concept* of copying high-edge wallets is keep-able, but the implementation
  is bound to legacy columns)
- `routes/api_core.py`, `api_followed.py`, `api_strategies.py`,
  `api_watchlist.py`, `api_agent.py`, `api_misc.py`, `ws_stream.py`
  (HTTP transport = INFRA; legacy field names in response envelopes = MIGRATE)
- `sniper/web/serializers.py` (single point of contract — every legacy field
  flows through `serialize_wallet_row`, `serialize_position_signal_row`)
- `static/app.js` (UI rendering = INFRA; legacy badges + sort keys + filter
  params = MIGRATE)
- `mcp_tools/read_tools.py` (`get_current_signals`, `get_wallet_profile`,
  `get_followable_leaderboard`, `get_proven_wallets` — every tool returns
  legacy fields)
- `bot.py` + `agent_pipeline.py` (alert outbox plumbing = INFRA;
  `agent_signal_severity` reads `insider_score` / `speculator_score` =
  MIGRATE)

---

## 2. Dependency map

This is the authoritative consumer list for retirement planning.
**Do not delete a column or function until every row referencing it is closed.**

### 2.1 Score-producer → output column → primary decision

| Producer (file:line) | Output column | Primary decision controlled |
|---|---|---|
| `analyze_users.py:1733-1920 _score_insider` | `user_analysis.insider_score`, `score_history.insider_score` | classification gate, alerter `>=85`, real-trader fallback |
| `analyze_users.py:1619-1730 _score_speculator` | `user_analysis.speculator_score` | speculator-bot classification, agent priority penalty |
| `analyze_users.py:559-647 _compute_wallet_quality_score` | `user_analysis.wallet_quality_score`, `wallet_quality_eligible`, `wallet_quality_fail_reason` | paper-trader Gate 0e (ADR-007), insider_copy_v1 Gate |
| `analyze_users.py:650-675 _compute_wallet_edge_tier` | `user_analysis.wallet_tier` | UI display, leaderboard tiering |
| `analyze_users.py:1370-1527 _score_position_insider` | `position_signals.position_insider_score`, `signal_history.position_insider_score` | paper-trader Gate 2 (`>=90`), real-trader Gate 2 (`>=55`), alerter sort, strategy_runner squashing |
| `analyze_users.py:1291-1367 _score_position_speculator` | `position_signals.position_speculator_score`, `signal_history.position_speculator_score` | paper-trader Gate 3 (`<70`), real-trader Gate 3 (`<70`) |
| `analyze_users.py:1531-1616 _classify_position_signal` | `position_signals.position_classification` | alerter filter `position_insider_candidate`, UI filter `hide_low_signal` |
| `analyze_users.py:1919-2115 _classify_wallet` | `user_analysis.classification` | paper-trader Gates 0a/0b/0c, alerter exclusion filter, alerts pipeline routing |
| `db_utils.py:312-387 compute_followability_score` | `user_analysis.followability_cached` | leaderboard rank (display) + zero for `market_maker` (de-facto block) |

### 2.2 Score-consumer → decision controlled → impact class

Decision-logic = **MIGRATE**. Display-only = **lower urgency**. Audit-only = **safe to keep**.

| File:line | Reads | Decision | Impact |
|---|---|---|---|
| `real_trader.py:759-767` | `classification`, `win_rate`, `qualifying_roi` | Real-trade speculator-bot block | production-decision |
| `real_trader.py:777-779` | `position_insider_score` | Real-trade entry gate `>=55` | production-decision |
| `real_trader.py:783-784` | `position_speculator_score` | Real-trade entry cap `<70` | production-decision |
| `real_trader.py:787-793` | `qualifying_roi`, `insider_score` | Real-trade qROI gate | production-decision |
| `paper_trader.py:977-984` | `classification` | signal_type label routing | production-decision |
| `paper_trader.py:1042` | `classification ∈ PAPER_SKIP_WALLET_CLASSES` | Gate 0a archetype reject | production-decision |
| `paper_trader.py:1045-1065` | `classification == speculator_bot`, `win_rate`, `qualifying_roi`, `insider_score` | Gate 0b speculator-bot | production-decision |
| `paper_trader.py:1074-1082` | `classification == low_signal`, `closed_sample_size`, `win_rate` | Gate 0c low-signal | production-decision |
| `paper_trader.py:1100-1126` | `wallet_quality_eligible`, `wallet_quality_score` | Gate 0e (ADR-007) | production-decision |
| `paper_trader.py:1150-1164` | `position_insider_score` (+ world-boost) | Gate 2 entry `>=90` | production-decision |
| `paper_trader.py:1167-1168` | `position_speculator_score` | Gate 3 cap `<70` | production-decision |
| `paper_trader.py:722-840 calculate_position_size` | `position_insider_score`, `win_rate`, `qualifying_roi`, elite bonus | Sizing multipliers | production-decision (continuous) |
| `paper_trader.py:1267-1269` | (snapshot at entry only, not gate) | Audit copy of entry-time scores | audit-only |
| `alerter.py:111-128 THRESHOLDS` | `insider_score>=85`, `win_rate`, `pnl`, `qualifying_roi`, `closed_sample_size` | New-insider + top-performer gates | production-decision |
| `alerter.py:1418-1442` | `user_analysis` cols | Insider alert SQL filter | production-decision |
| `alerter.py:1476-1481 _passes_qualifying_roi_gate` | `qualifying_roi`, `insider_score` (>=90 bypass) | qROI gate or score override | production-decision |
| `alerter.py:1541-1551` | `classification == high_winrate_review`, `position_classification == position_insider_candidate`, `position_insider_score` | Proven-position alert filter + sort | production-decision |
| `alerter.py:1656` | `classification NOT IN ('speculator_bot','low_signal')` | Top-performer exclusion | production-decision |
| `agent_pipeline.py:351-393` | `flagged_users.insider_score`, `speculator_score`, `classification`, `max_position_value`, `total_open_value` | Agent high-signals feed gate + payload | production-decision |
| `agent_pipeline.py:178-209 agent_signal_severity` | `insider_score`, `speculator_score` | Outbox severity (critical/high/medium/low) | production-decision |
| `strategy_runner.py:504` | `signal.metadata.position_insider_score` | Position lifecycle entry tag | production-decision |
| `strategy_runner.py:1241,1301` | `position_insider_score` | Squashing + multi-signal confidence | production-decision |
| `strategies/insider_copy_v1.py:115-307` | All position + wallet scores | Plugin entry gates (mirrors paper_trader) | production-decision |
| `strategies/fade_v1.py:108-115,161-249` | Position scores read; no hard gate on them, but signal source coupled | Plugin entry decision | production-decision |
| `strategies/insider_copy_v1.py:423-484` | `score_history.insider_score` time series | Strategy stability tracker | production-decision (audit table → strategy) |
| `routes/api_core.py:243-467` | All score cols → `serialize_position_signal_row` | API payload + sort + filter | display + filter (UI gates operator attention) |
| `routes/api_core.py:469-750` | Scores → response | Thesis signals/candidates payload | display |
| `routes/api_followed.py:536-1479` | Wallet + position scores | Followed-positions payload | display |
| `mcp_tools/read_tools.py:75-134 get_current_signals` | `min_insider_score=55` filter, scores in payload | MCP gate + payload | production-decision (agents downstream) |
| `mcp_tools/read_tools.py:136-181 get_wallet_profile` | `insider_score`, `speculator_score`, `classification` | MCP payload | display (agent-consumed) |
| `mcp_tools/read_tools.py:314-342 get_followable_leaderboard` | `db_leaderboard()` — legacy-ranked | MCP payload (primary discovery) | production-decision |
| `mcp_tools/read_tools.py:373` | `signal_history.*` | MCP wallet history | display |
| `static/app.js:265,1071,1173,1280,1289,1337,1427,2406,2487,2706,3559,4152,5465,5494,5513,5558,5583` | All score cols | UI badges, sorts, filters | display + filter |
| `routes/api_agent.py` (via `agent_pipeline`) | `flagged_users.*` | Agent high-signals API | production-decision |
| `bot_detector.py:29-129` | `position_signals` + score cols | Bot-pattern detection | production-decision (sets `is_bot` → blocks) |
| `health_check.py:80,117,351` | freshness of `low_buy_trades`, `position_signals` | Daemon freshness checks | audit-only |
| `retrospective_analyzer.py:102-158` | All score cols (entry-time + current) | Research only | audit-only |
| `db_replay.py:60-94 analyzer_signal_outcomes` | `position_signals` latest scores | Research (look-ahead-biased) | audit-only |
| `db_replay.py:198-309 insert_*_history` | Writes scores into `signal_history`/`score_history` | Audit log | audit-only |
| `db_replay.py:168,364 replay_curated_signals` | `signal_history.position_insider_score`, `position_speculator_score`, `entry_anchor_ts` | Replay engine signal seed | production-decision (test-replay tool) |
| `db_signals.py:943-991` | `signal_history.entry_anchor_ts` + scores | Open-signal entry-anchor validation | production-decision |
| `db_snapshots.py:348` | `signal_history.*` | Historical export | display |
| `routes/api_core.py:68 api_flagged_users` | `flagged_users.*` | UI flagged list | display |
| `tests/test_scoring.py`, `test_wallet_quality_score.py`, `test_real_trader_gates.py`, `test_paper_trader_sizing.py`, `test_strategy_plugin_contract.py`, `test_wallet_scout.py`, `test_history_capture.py`, `test_dashboard_legacy_schema.py`, `test_copy_viability.py`, `test_convergence.py` | Various score columns and classification literals | Test-only | test-only (must update alongside producers) |

### 2.3 Tables — current truth source

| Table | Producer | Primary read sites | Status |
|---|---|---|---|
| `user_analysis` (68 cols) | `analyze_users.py` | alerter, paper_trader, real_trader, all api_*, mcp, knowledge generators | **legacy scaffold**: assume score/classification truth is bad; extract only identity/runtime metadata if still needed |
| `flagged_users` (25 cols) | `db.py:3606 upsert_flagged_users` (called from analyze_users) | `routes/api_core.py:68`, `agent_pipeline.py:351` | **trash/retire** — do not rebuild from `user_analysis`; replace with analyzer-native views after `wallet_layer5_decision` exists |
| `position_signals` (53 cols) | `db_signals.py:95-350 upsert_position_signal` (called from analyze_users) | alerter, paper_trader (via API), real_trader, strategy_runner, all api_*, mcp, ws_stream, bot_detector | **suspect scaffold**: preserve only verified market-state/runtime fields; retire all score/classification semantics |
| `signal_history` (18 cols) | `db_replay.py:198 insert_signal_history` | `db_replay.py:replay_curated_signals`, `db_signals.py:943-991`, `mcp_tools/read_tools.py:373`, `db_snapshots.py:348` | **append-only audit + replay anchor** — keep, retire score cols later |
| `score_history` (19 cols) | `db_replay.py:257 insert_score_history` | `strategies/insider_copy_v1.py:423-484` (only live read), `scripts/update_handoff.py`, `scripts/db_prune.py` | **near-dead** — one strategy reads it; archive after migrating that strategy |
| `signal_queue` (14 cols) | `db_hyperliquid.py:298 insert_signal_queue_entry` | `copy_viability.py:19-106`, `routes/api_strategies.py:api_copy_viability` | **infra** — instrumentation only, no scoring coupling, KEEP |
| `low_buy_trades` (20 cols) | `db.py:3007 record_low_buy_trade` | `analyze_users.py:wallet_low_buy_features`, `db_discovery.py:822 migrate_low_buy_trades`, `health_check.py`, `routes/api_*` | **infra** — discovery seed + features, KEEP |
| `copy_viability` (7 cols) | `copy_viability.py:70 update_all_viability` | `routes/api_strategies.py:1111-1126` | **infra** — fill-rate/latency metric, KEEP, reframe as "execution viability" not "copy quality" |
| `wallet_registry` (12 cols) | schema present, **no live writer yet** | `mcp_tools/read_tools.py:wallet_profile` (partial) | **NEW** — designated authoritative wallet record |
| `wallet_memberships` (9 cols) | (curation tooling) | leaderboards | **NEW** — followed/watchlist relationship layer |
| `wallet_tag_pnl` (12 cols) | `scripts/build_wallet_tag_pnl.py` (`compute_wallet_tag_pnl`) | `tag_inference.py`, `mcp_tools.wallet_profile`, dossier writer | **NEW** — populated |
| `wallet_tag_fit` (8 cols) | `scripts/build_wallet_tag_fit.py` | not yet wired into routes | **NEW** — populated, awaiting consumers |
| `market_tags` (4 cols) | `scripts/sync_polymarket_tags.py` (pending) | `wallet_protocol/fetch.py`, `tag_wallet_pnl` | **NEW** — schema present, sync script not yet committed |
| `tag_nav_category` (4 cols) | manual seed + gamma backfill (pending) | `tag_inference.py` (nav-grain inference) | **NEW** — ~50/600 seeded, backfill needed |
| `tag_market_stats` (proposed) | `sniper/analysis/tag_analytics.py compute_tag_market_stats` | not consumed yet | **NEW gap** — function + tests exist; no DB persistence or live consumer yet |

### 2.4 Sub-agents — input/output by source-of-truth

| Agent file | Reads (legacy) | Reads (new) | Writes | Status |
|---|---|---|---|---|
| `paper-trade-analyst.md` | `user_analysis.wallet_quality_score`, `paper_trades.signal_*_score` | none | report only | **MIGRATE**: switch query to new analyzer verdict bands |
| `scoring-watchdog.md` | `user_analysis.wallet_quality_score`, classification | none | report only | **RETIRED** (per its own frontmatter as of 2026-04-24) |
| `wallet-dossier-writer.md` | `user_analysis` snapshot | `archetype_envelope`, `tag_wallet_pnl`, exec profile | `knowledge/wallets/<addr>.md`, brain memory | **PARTIAL — extend to consume ProtocolReport** |
| `strategy-derivation-agent.md` | (none active) | archetype envelope + tag×PnL + exec profile | `docs/superpowers/specs/derived/<slug>.md` | **OK** — already analyzer-native |
| `feature-research-analyst.md` | varies | reads SQLite/Supabase | findings to brain | **OK** — analyzer-agnostic |
| `brain-hygiene-agent.md` | brain memories | n/a | proposed status changes | **OK** — but its corpus may reference legacy ADRs to update |

### 2.5 Web-settings env vars feeding legacy gates

29 env-var thresholds gate legacy scoring (full list in Appendix A.4). These
must either be retired or remapped to new analyzer outputs. They are **all in
`sniper/settings/web.py` and `sniper/settings/paper.py`**, which is already
the central config module — that's a migration win.

---

## 3. Keep / retire / isolate / migrate / wait classification

| Component | Verdict | Rationale |
|---|---|---|
| `analyze_users.py` whole file | **WAIT-then-RETIRE** | Wait until every consumer migrated. Remove only after Phase 6 cutover and Phase 8 archive. Preserve Layer 0 identity-update pieces (sync of basic wallet metadata) by extracting them; the *scoring* part dies. |
| `_score_insider`, `_score_speculator`, `_compute_wallet_quality_score`, `_score_position_insider`, `_score_position_speculator`, `_compute_wallet_edge_tier`, `_classify_wallet` | **RETIRE** | Replaced by ProtocolReport (Stage 1 gates + Stage 2 hypotheses + Layer 5 verdict). |
| `_classify_position_signal` | **RETIRE → reframe** | Legacy "is this position insider-y?" recomputed each analysis. Replaced by ProtocolReport `wallet_layer5_verdict` + market-side `position_signals` market data only. |
| `compute_followability_score` (`db_utils.py:312-387`) | **RETIRE** | ADR-006 P&L-first formula was a band-aid on a broken model. Followability is a tagged/per-wallet decision now — `wallet_layer5.action_decision` enum. |
| `flagged_users` table | **RETIRE** | Do not preserve or recompute from legacy `user_analysis`. Replace UI/API reads with an analyzer-native view over `wallet_layer5_decision` after that table exists. |
| `score_history` table | **RETIRE → archive** | Sole live reader is `strategies/insider_copy_v1.py:423-484`. Remove or rewrite that dependency before dropping the table. Do not add a decision-history replacement until the analyzer emits real decisions. |
| `signal_history` table — non-score cols | **KEEP** | Replay-engine seed + entry-anchor validation. Drop score cols, keep market state + `entry_anchor_ts`. |
| `signal_history` table — score cols | **RETIRE in-place** | Drop `position_insider_score`, `position_speculator_score`, `position_classification` columns once `db_replay.py:replay_curated_signals` no longer references them. |
| `position_signals` — market cols | **VERIFY-then-KEEP** | Live signal table for alerter, real_trader, paper_trader, ws_stream. Treat as suspect until each retained field is proven to be raw market/runtime state rather than legacy scorer output. |
| `position_signals` — score cols | **RETIRE → split** | Split into `position_market_state` (market data) + `wallet_layer5_decision` (analyzer verdict) per § 4.6. |
| `low_buy_trades` | **KEEP** | Discovery seed + 14-day low-buy features. Reframe role: it's *raw market data*, not a "conviction signal." |
| `signal_queue`, `copy_viability` | **KEEP** | Cross-venue execution instrumentation. No scoring coupling. Reframe: "execution viability" (speed + reliability), not "wallet quality." |
| `alerter.py` | **MIGRATE inputs, KEEP infra** | Watermark + Telegram + dedup + outbox + operator commands = infrastructure. Threshold dict (line 111-128) + qROI gate + classification filter = decision-logic to migrate to `wallet_layer5_decision`. **No replacement numeric `*_score` may be introduced.** The post-migration alerter compares enums (action_decision, mode) and direct measurements (PnL USDC, win-rate fraction, sample size) with explicit units — never a synthetic threshold like `>= 85`. |
| `paper_trader.py` lifecycle/sizing/PnL/monitor/exit | **KEEP** | Pure infrastructure. PnL math, position sizing formula structure, stops, settlement watcher have no scoring coupling. |
| `paper_trader.py` Gates 0a/0b/0c/0e/2/3 | **MIGRATE** | Replace with Layer 5 action decision as the wallet authority. `wallet_tag_fit` may order or size within the already-authorized set or feed strategy-local context; it is not an independent pass/veto gate and never ranks wallets globally. |
| `paper_trader.py` Gate 4 (qualifying_roi) | **DELETE** | Already disabled (line 1171-1177 comment "broken PnL data"). |
| `real_trader.py` gates | **MIGRATE later** | Real-trader is paused. Migrate after paper_trader cutover proven on live. |
| `strategy_runner.py` plugin host | **KEEP infra**, **MIGRATE squashing inputs** | Replace `position_insider_score` reads at lines 504/1241/1301 with new analyzer verdict reference. |
| `strategies/insider_copy_v1.py` | **REWRITE → rename** | Plugin's *concept* (copy high-edge wallets) survives; its implementation does not. Rewrite to consume `wallet_layer5_decision = validated_copy_candidate`. Rename to `wallet_copy_v1` to drop legacy framing. |
| `strategies/fade_v1.py` | **REVIEW** | Reads scores but does not gate hard on them. Migrate inputs to behavior-only fields (e.g. tag fit ≤ threshold, archetype = `paired_outcome_arb` etc.). |
| `agent_pipeline.py:178-209 agent_signal_severity` | **MIGRATE** | Replace `min(16, spec*0.2)` penalty etc. with severity derived from `wallet_layer5_decision.action_decision` and reason codes. Output stays enum (`critical|high|medium|low`); the function may compute internally with direct measurements but **must not return or store a numeric severity score** (rule § 0 #4). |
| `agent_pipeline.py:351-393 collect_agent_high_signals` | **MIGRATE** | Read from `wallet_registry` not `flagged_users`. |
| `routes/api_core.py`, `api_followed.py`, `api_strategies.py`, `api_watchlist.py`, `api_misc.py`, `api_agent.py`, `ws_stream.py` | **MIGRATE response envelopes** | Add new analyzer fields side-by-side with legacy fields (Phase 4); flip primary in Phase 5; remove legacy in Phase 8. |
| `sniper/web/serializers.py` | **MIGRATE in-place** | Single point of contract. Add new fields, mark legacy as deprecated, then drop. |
| `static/app.js` | **MIGRATE rendering** | Replace `insider_score` numeric badge with archetype + Layer 5 verdict chip. Replace sort by `insider_score` with sort by Layer 5 priority + tag-fit. |
| `mcp_tools/read_tools.py` (`get_current_signals`, `get_wallet_profile`, `get_followable_leaderboard`, `get_proven_wallets`) | **MIGRATE in-place** | Add new params and fields side-by-side with legacy; flip default later. |
| `bot_detector.py` | **KEEP — ISOLATE as anomaly input** | Bot detection is a behavior-only signal. It already runs orthogonal to wallet quality. Keep its current outputs (`is_bot`); make them advisory to alerter and paper_trader, not authority. |
| `bot.py` (Telegram daemon) | **KEEP** | No scoring coupling. |
| Legacy tests `test_scoring.py`, `test_wallet_quality_score.py`, `test_real_trader_gates.py`, `test_paper_trader_sizing.py`, etc. | **MIGRATE alongside producers** | Each retired score function loses its scoring tests; replacement gates get new tests. |
| `archive/classifiers/*` | **NO TOUCH** | Already dead. Leave parked. |
| `.claude/agents/scoring-watchdog.md` | **NO TOUCH (already retired)** | Frontmatter declares retired; do not dispatch. |
| `.claude/agents/paper-trade-analyst.md` | **MIGRATE query** | Switch SQL from `wallet_quality_score` bands to `wallet_layer5_decision` enum. |
| `.claude/agents/wallet-dossier-writer.md` | **EXTEND** | Add ProtocolReport ingest (currently only reads archetype envelope + tag×PnL + exec profile). |
| Legacy "anomaly-trade" framing — position-level entry-timing signal that empirically showed 65.5% WR / +$343 across 37 trades | **ISOLATE as ANOMALY plugin** | Resurface as `strategies/anomaly_entry_timing_v1.py` after Phase 7. Specifications below in § 6. |

---

## 4. New analyzer path proposal

### 4.1 Output contract — `wallet_layer5_decision`

The new analyzer's authoritative runtime output is a small, enum-shaped row.
**No 0–100 score.** `ProtocolReport.overall_verdict` remains the analytical
conclusion; `wallet_layer5_decision.action_decision` is the operational
instruction consumed by services. The Phase 2 output-contract spec must define
the mapping between them explicitly.

**Table: `wallet_layer5_decision` (NEW, proposed)**

| Column | Type | Notes |
|---|---|---|
| `wallet` | TEXT PK | normalized lowercase |
| `analysis_version` | TEXT | e.g. `wpv2.0`, bumps on protocol release |
| `as_of_ts` | INTEGER | unix seconds, when this verdict was derived |
| `data_window_start_ts`, `data_window_end_ts` | INTEGER | analysis input window |
| `mode` | TEXT | `held` / `flipper` / `mixed` (mode_router output) |
| `stage1_gate_outcome` | TEXT | `pass` / `reject_hedge_archetype` / `reject_insufficient_sample` / `reject_dormant` |
| `stage1_caution_flags_json` | TEXT (JSON) | `["F1_sloppy","F2_concentration",…]` |
| `held_branch_summary_json` | TEXT (JSON) | `{H1: {verdict, ci_lower_winsorized, ci_upper, n}, H2_sub080: …, … H7: …}` |
| `flipper_branch_summary_json` | TEXT (JSON) | `{FH1: …, FH6: …}` |
| `tag_grain_json` | TEXT (JSON) | per-tag verdicts at leaf + nav grain (cross-link to `wallet_tag_fit`/`wallet_tag_pnl`) |
| `capital_ledger_summary_json` | TEXT (JSON) | net inflow/outflow, position cost, accounting tie status |
| `data_quality_json` | TEXT (JSON) | activity_cap_hit, subgraph_used, payback_capitalization_warning |
| `action_decision` | TEXT | enum: `monitor` / `study` / `reverse_engineer_candidate` / `validated_copy_candidate` / `exclude_for_copy` / `anti_pattern` (per brain-design Layer 5) |
| `action_reason_codes_json` | TEXT (JSON) | codes for human display; e.g. `["F2_concentration_top3","H1_winsorized_ci_excludes_zero"]` |
| `report_markdown` | TEXT | full rendered report (already produced by `report.py`) |
| `analyzer_run_id` | TEXT | foreign key to a future `analyzer_runs` table for audit |

**Why an enum, not a score:**
- The 2026-03-25 audit (CLAUDE.md ADR-006) proved synthetic scores invert
  causality.
- Layer 5 is a *decision*, not a *measurement*. Reducing it to one float
  re-introduces the same failure mode.
- Downstream consumers (alerter, paper_trader, MCP tools, UI) compare
  enums and reason codes — they don't need ordering, except optionally for
  display, which can use a small ordered set (`validated_copy_candidate >
  reverse_engineer_candidate > study > monitor > exclude > anti_pattern`).
- This is the same hard rule as § 0 #4 applied to the wallet output: no
  composite score column on this or any other new analyzer table. The
  numeric data the analyzer does carry are direct measurements (CI bounds,
  PnL in USDC, sample counts, p-values, fractions) inside JSON columns,
  not authority surfaces.

### 4.2 Per-tag outputs (already implemented)

- `wallet_tag_pnl` — held-only realized PnL + open unrealized + win rate.
  Authoritative truth for "did this wallet make money in tag X?". § 2.3 row.
- `wallet_tag_fit` — four-component fit score per (wallet, tag): edge,
  sample, consistency, recency. Used by paper_trader / strategies for
  explanation, sizing, or ordering candidates **within an already-
  authorized set** (set authorized by `wallet_layer5_decision.action_decision`).
  It is one characteristic/stat from wallet analysis, not an authorization
  surface and never a global wallet ranker.

### 4.3 Storage location

- **SQLite (`sniper_tracker.db`)** for all per-wallet rows the live system
  reads at decision time. Same DB the existing services use; no new transport.
- **Supabase brain (`memories`)** for human-readable findings and dossier
  artefacts. Layer 5 verdicts can be mirrored as memories tagged
  `wallet-decision/<wallet>` for cross-session recall, but the SQLite row is
  the source of truth at runtime.
- **`knowledge/wallets/<addr>.md`** for human/agent reading. Generated by
  `wallet-dossier-writer` once it consumes the full ProtocolReport.

### 4.4 API / service boundaries

- **Internal (Python):** new `sniper/analysis/wallet_protocol/persistence.py`
  module exposes `upsert_wallet_layer5_decision(conn, decision)` and
  `get_wallet_layer5_decision(conn, wallet) -> Optional[Layer5Decision]`.
- **HTTP:** new `routes/api_wallets.py` blueprint exposing
  `GET /api/wallet-decision/<wallet>` (verdict + reason codes + key tag
  context) and `POST /api/analyze-wallet` (kicks off `run_wallet_protocol.py`
  for one wallet, returns 202 + run_id, with the result eventually written
  to `wallet_layer5_decision`). Add to existing Flask app.
- **MCP:** new `mcp_tools/analyze_tools.py` with `analyze_wallet(wallet)`
  (synchronous from cached row) and `get_wallet_decision(wallet)`. Update
  `read_tools.py:get_wallet_profile` to additionally include the Layer 5
  decision when present.
- **CLI:** `scripts/run_wallet_protocol.py` already exists; extend to write
  to `wallet_layer5_decision` on completion.

### 4.5 Downstream consumers — replacement matrix

| Legacy input | New input | New table / field |
|---|---|---|
| `user_analysis.classification == speculator_bot` | `wallet_layer5_decision.action_decision IN ('exclude_for_copy','anti_pattern')` OR `…stage1_caution_flags_json` contains `F1_sloppy` | `wallet_layer5_decision` |
| `user_analysis.classification == low_signal` | `wallet_layer5_decision.stage1_gate_outcome == 'reject_insufficient_sample'` OR `action_decision == 'monitor'` | `wallet_layer5_decision` |
| `user_analysis.classification == high_winrate_review` | `action_decision IN ('study','reverse_engineer_candidate')` | `wallet_layer5_decision` |
| `user_analysis.wallet_quality_score >= threshold` | `action_decision == 'validated_copy_candidate'` | `wallet_layer5_decision` |
| `user_analysis.insider_score >= 85` (alerter) | `action_decision IN ('validated_copy_candidate','reverse_engineer_candidate') AND mode == 'held'`; `wallet_tag_fit` may annotate or order alerts within that already-authorized set, never authorize an alert on its own | `wallet_layer5_decision` + optional `wallet_tag_fit` |
| `position_signals.position_insider_score >= 90` (paper Gate 2) | (a) `wallet_layer5_decision.action_decision == 'validated_copy_candidate'` (wallet authority), AND (b) optional strategy-local constraints using tag fit / anomaly signals to order or size within the authorized set, never as standalone authorization | composition of new tables |
| `position_signals.position_speculator_score < 70` (paper Gate 3) | `wallet_layer5_decision.mode != 'flipper'` OR archetype not in flipper-bots set; OR `bot_detector.is_bot == False` | `wallet_layer5_decision` + `bot_detector` |
| `qualifying_roi` gates | `wallet_tag_pnl[primary_tag].realized_pnl + open_unrealized_pnl > 0` (true edge in operative tag) | `wallet_tag_pnl` |
| `flagged_users` table reads | view: `wallet_layer5_decision WHERE action_decision IN ('validated_copy_candidate','reverse_engineer_candidate')` after new decisions are real | view over `wallet_layer5_decision` |
| `score_history` (insider_copy_v1) | No replacement until the analyzer is complete enough to emit real decisions. If stability tracking is later needed, design `wallet_layer5_decision_history` then. | deferred |

### 4.6 `position_signals` replacement/split (proposal — implement in Phase 3)

**Today (mixed):** `position_signals` (53 cols) is one row per (wallet,
condition_id, outcome) carrying both market state (entry_price, current_price,
buys_5m, …) and analyzer scoring (position_insider_score, …).

Treat the table as suspect. Phase 3 must verify each retained field against
raw inputs before promoting it. If market-state fields are not trustworthy,
replace the table outright instead of preserving the name or shape.

**Proposal if market-state fields validate:** split into two tables, written
atomically by analyzer:

```
position_market_state           position_signal_decision (NEW)
PK: wallet,condition_id,outcome PK: wallet,condition_id,outcome,
                                    analysis_version
- market state (entry_price,
  current_price, buys_5m, …)    - wallet_action_decision (cached enum
- activity_state, hold_hours      from wallet_layer5_decision)
- end_date_iso                  - tag_fit_score (cached cohort score,
- analyzed_ts, updated_at         REAL 0..1; context-only, not a gate)
- paper_trade_outcome backfill  - tag_fit_rank (cached integer ordinal
                                  within tag; context-only, not a gate)
                                - signal_reason_codes_json
                                - analyzed_ts
```

No synthetic composite-score columns appear on either table (rule § 0 #4).
`tag_fit_score` (REAL 0..1) and `tag_fit_rank` (INTEGER ordinal) are both
cohort measurements already produced by `wallet_tag_fit`. They are
direct measurements (score = four-component normalized cohort value;
rank = ordinal among peers) and are treated as explanation/sizing
context, never as authority. The Phase 2 spec
(`docs/superpowers/specs/2026-04-28-wallet-analyzer-output-contract.md`)
locks the column shapes.

Backwards compat: provide a view `position_signals` = LEFT JOIN of the two
tables for the duration of Phase 4–5 only if the retained market-state fields
are validated. Otherwise create a new clean table and migrate consumers to it.

### 4.7 Freshness / versioning

- Every row carries `analysis_version` (e.g. `wpv2.0`, `wpv2.1`). Bump on
  protocol changes; consumers may filter `WHERE analysis_version >= …`.
- Cadence: a wallet's `wallet_layer5_decision` is rewritten by
  `run_wallet_protocol` either (a) on demand for newly-discovered wallets,
  (b) nightly for tracked wallets, (c) when subgraph fallback is triggered
  by activity_cap_hit. Stale-after threshold: 7 days for active wallets, 30
  days for monitored. Stale rows still serve, but with `data_quality_json.stale_days`
  surfaced.
- `analyzer_runs` audit table (optional, Phase 3) records each run with
  inputs, version, success/failure, run duration. Replaces the current
  `analysis_runs` table semantics if desired.

### 4.8 Reporting format

- Per-wallet markdown via `report.py` (already exists), persisted into
  `knowledge/wallets/<addr>.md` via dossier writer.
- Per-batch summary later: `scripts/report_batch_decisions.py` (NEW) should
  wait until analyzer decisions are real. Before that, batch reporting may
  compare analysis outputs, but should not imply stable decision history.

---

## 5. Legacy logic retirement plan

### 5.1 Sequence (high-level — per-phase TDD plans expand each)

1. **Block all new code from referencing legacy decision fields *and* from
   introducing new synthetic composite scores.** Add a `pre-commit`/CI grep
   gate that fails on:
   - new uses of `insider_score`, `position_insider_score`,
     `wallet_quality_score`, `wallet_quality_eligible`,
     `wallet_quality_fail_reason`, `speculator_score`,
     `position_speculator_score`, `followability_cached`,
     `wallet_edge_tier`, `position_classification`, `classification`
     outside the explicit allowlist (`analyze_users.py` + `archive/` +
     legacy-snapshot tests during the migration window);
   - new column, parameter, or function names that match
     `^(min_|max_)?\w*(_score|_priority|_rank_threshold)$` returning a
     numeric value compared to a threshold elsewhere in the codebase
     (whitelist `*_pnl_usdc`, `*_ci_lower_usdc`, `*_ci_upper_usdc`,
     `*_p_value`, `*_win_rate`, `*_fill_rate`, `*_latency_ms`,
     `*_sample_size`, `*_days`, `tag_fit_score`, `tag_fit_rank` —
     direct measurements and within-cohort ranks are exempt).
   *This is the single highest-leverage retirement move; it stops the
   bleed and prevents the school-project pattern from re-entering.*
2. **Persist new analyzer outputs.** Phase 3 creates
   `wallet_layer5_decision`, `position_signal_decision`, `analyzer_runs`,
   wires `run_wallet_protocol.py` to write them.
3. **Read-side migration, one consumer at a time.** Use the
   `deprecation-migrator` agent with the dependency map (§ 2.2) as the
   audit matrix. Order:
   1. MCP tools (lowest blast radius — agents can be regenerated).
   2. `wallet-dossier-writer.md`, `paper-trade-analyst.md` (single-shot
      agents).
   3. API serializers (add new fields, do not flip default).
   4. UI (`static/app.js`) — add archetype + verdict badges alongside
      legacy.
   5. `agent_pipeline.py` (alerter → outbox path).
   6. `alerter.py` decision gates.
   7. `paper_trader.py` decision gates (one gate per commit).
   8. `strategies/insider_copy_v1.py` rewrite → `wallet_copy_v1.py`.
   9. `strategy_runner.py` squashing inputs.
   10. `real_trader.py` gates (only after live paper run validates new gates).
4. **Flip the default.** Once every consumer reads new fields, flip the
   API/UI/agent defaults so legacy fields become `_deprecated_*` aliases
   for one release.
5. **Remove producers.** Delete `_score_insider`, `_score_speculator`,
   `_compute_wallet_quality_score`, `_score_position_insider`,
   `_score_position_speculator`, `_compute_wallet_edge_tier`,
   `_classify_wallet`, `_classify_position_signal`,
   `compute_followability_score`. Remove their tests.
6. **Drop columns.** `flagged_users` table, `score_history` table; score
   columns from `user_analysis`, `position_signals`, `signal_history`.
7. **Archive ADRs.** ADR-006 + ADR-007 in CLAUDE.md become historical
   notes in a new ADR-009 ("Replaced by wallet-analysis-protocol-v2,
   2026-04-…").

### 5.2 Critical retirement blockers (cannot retire without these in place)

These are the minimal cutset before any column drop. Each is a hard prereq:

| Blocker | What unblocks |
|---|---|
| `wallet_layer5_decision` populated for all currently-followed + watchlisted wallets (~48) | every consumer migration |
| `position_signal_decision` populated atomically with `position_market_state` writes | alerter + paper_trader gate cutover |
| New replay anchor: store `entry_anchor_ts` on `position_market_state` (not `signal_history`) | `db_signals.py:943-991` migration; allows `signal_history` score cols to drop |
| Replacement/removal for `score_history` reads in `strategies/insider_copy_v1.py:423-484` | drop `score_history`; do not add decision history until analyzer decisions are real |
| `wallet_archetype_extractor` Layers 3–4 fully populated (currently partial) | dossier and downstream agents reach analyzer-native parity |
| `tag_nav_category` ≥80% coverage of frequent tags | nav-grain inference no longer flagged provisional |
| `tag_market_stats` persisted or otherwise wired to the analyzer | Layer-3 `market_selection_edge` heuristic populated; `compute_tag_market_stats` already exists but is not a live source |
| MCP `analyze_wallet` + `get_wallet_decision` tools live | external agents stop reading legacy `get_wallet_profile` fields |

### 5.3 Risks during retirement

- **Look-ahead bias in research after migration.** `db_replay.py:60-94
  analyzer_signal_outcomes` reads latest scores. If we keep that for
  research, document it as look-ahead biased and never use it for new
  hypothesis tests.
- **Insider_copy_v1 stability tracker.** Reads `score_history` for trend
  signal. If we drop the table before replacing the reader, the strategy
  silently degrades. Either remove the stability feature in the rewrite, or
  keep `score_history` until the rewrite ships. Do not invent
  `wallet_layer5_decision_history` before analyzer decisions are real.
- **Replay engine reproducibility.** Replays use entry-time scores from
  `signal_history`. Once we drop those columns, replays for periods before
  Phase 3 lose those inputs. Mitigation: snapshot `signal_history` to a
  parquet file in `archive/snapshots/2026-04-XX-signal-history.parquet`
  before any column drop.
- **UI regression risk.** `static/app.js` has 17+ score references. Any
  partial migration where the JS reads a renamed field breaks the page.
  Use feature flag (`window.SNIPER_NEW_ANALYZER_FIELDS`) and ship both
  side-by-side.
- **Real trader is paused but the file is live.** Don't migrate
  `real_trader.py` gates without a) un-pausing carefully, b) shadow paper
  validation, c) explicit user sign-off. Real-trader is *out of scope*
  for the first migration window.

---

## 6. Anomaly-trade strategy plan

### 6.1 Bound the scope strictly

After legacy decision logic is retired, exactly **one** plugin under
`strategies/` may reuse the legacy intuition that *position-level entry
timing* (the empirical 90+ insider_score band that produced 65.5% WR /
+$343 across 37 trades — `MEMORY.md`, the only band that showed real
edge per the 2026-03-25 audit) is worth flagging as an **anomaly trade
signal**.

Filename: `strategies/anomaly_entry_timing_v1.py`.

**It must:**
- Live behind the new `StrategyPlugin` contract (same as today's plugins).
- Read **only** from new analyzer tables and market-state tables. No
  reads of `user_analysis.insider_score` or
  `position_signals.position_insider_score` columns. (The behaviour the
  old score modeled — entry-timing skew vs preceding bar — is now in the
  execution profile and `wallet_archetype_extractor`. Read those.)
- Run as a peer plugin, not a gate. It can *propose* a paper trade only
  when the global gates (Layer 5 = `validated_copy_candidate`) already
  pass. Its role is to *up-weight size* on positions where the
  entry-timing anomaly fires, not to authorize entry.

**It must not:**
- Alter `wallet_registry` or `wallet_layer5_decision`.
- Set or read any "wallet quality" field.
- Be a precondition for any other plugin or strategy.
- Be referenced by alerter or MCP discovery.
- **Emit a numeric anomaly score**. Output is a boolean
  `anomaly_flag` plus named reason codes (e.g.
  `["preceding_bar_skew_high","drift_aligned_with_buy"]`). Internal
  thresholding may use direct measurements (skew, drift, tape count)
  with units; the boundary output is enum + reasons, per § 0 #4.

### 6.2 What it actually does

Plugin reads `wallet_archetype_extractor` execution profile output (drift_1h,
skew_vs_preceding_bar, tape_trade_count, confidence) and the live
`position_market_state` (entry_price, buys_5m, trades_5m). Fires a
"high-anomaly entry" flag when:
- `confidence ∈ {medium, high}`
- `skew_vs_preceding_bar ≥ X` (calibrate from labelled set; same
  calibration discipline as the protocol-v2 mode router thresholds)
- `drift_1h` direction matches the buy side
- `tape_trade_count_median_5m_pre ≥ 3`

Output: a `StrategyEntryDecision` with an extra metadata field
`anomaly_flag = True`. The plugin host (`strategy_runner.py`) treats
that flag as a *size multiplier hint* only; entry is governed by the
upstream Layer 5 gate.

### 6.3 Acceptance for promoting this plugin

Trial run of ≥ 60 trades on paper. Plugin promoted from `research` to
`paper` lifecycle (per existing strategy lifecycle states) only if
profit factor > 1.0 across the full window AND the anomaly-on cohort
significantly beats anomaly-off (CI lower bound > 0). Use the
existing `feature-research-analyst` agent to evaluate.

If it fails, archive the plugin. The legacy intuition does not survive
its own validation.

---

## 7. Migration phases

Each phase has a self-contained TDD plan written separately when its
prereqs close. Phase numbers track the user-supplied scope.

### Phase 1 — Audit and map dependencies (THIS DOCUMENT)
**Status:** ✅ done (this file).
**Artifacts:** § 1, § 2, Appendix A.

### Phase 2 — Define clean analyzer output contract and paths
**Goal:** Lock the schema for `wallet_layer5_decision`,
`position_signal_decision`, `analyzer_runs`. Lock the API surface
(`/api/wallet-decision/<wallet>`, MCP `analyze_wallet`,
`get_wallet_decision`). Get sign-off on enum values and reason codes.

**Files to create:**
- `docs/superpowers/specs/2026-05-XX-wallet-analyzer-output-contract.md`
- `docs/superpowers/plans/2026-05-XX-wallet-analyzer-output-contract.md`

**Done when:** spec is locked, plan TDD steps are scaffolded,
`schema-architect` agent has reviewed and signed off.

### Phase 3 — Persist new analyzer outputs (separate infra from decision logic)
**Goal:** Wire `run_wallet_protocol.py` to write
`wallet_layer5_decision`. Implement the `position_signals` split
(market_state + signal_decision) atomically. Snapshot
`signal_history` to parquet for archive. Add `analyzer_runs`.

**Files to touch:**
- `sniper/analysis/wallet_protocol/persistence.py` (NEW)
- `sniper/analysis/wallet_protocol/analyze_wallet.py` — add persistence
  step at end of `analyze_wallet()`
- `scripts/run_wallet_protocol.py` — call persistence
- `sniper/persistence/db_*.py` — `ensure_*` for new tables
- `db.py` — add `init_db` calls
- `index/schema_registry.json` — regenerate via
  `python3 scripts/brain_index.py generate`

**Done when:** all currently-followed + watchlisted wallets (≈48) have a
fresh `wallet_layer5_decision` row; CI green; brain index regenerated.

### Phase 4 — Add analyzer-native integrations for downstream readers
**Goal:** Add new fields to API responses, MCP tools, UI, agents,
**alongside** legacy fields. Default still legacy. No behaviour change.

**Files to touch (in order of blast radius, low → high):**
1. `mcp_tools/read_tools.py` (`get_wallet_profile`, `get_current_signals`,
   `get_followable_leaderboard`) — add new fields to response.
2. `mcp_tools/analyze_tools.py` (NEW) — `analyze_wallet`,
   `get_wallet_decision`.
3. `routes/api_wallets.py` (NEW) — `GET /api/wallet-decision/<wallet>`,
   `POST /api/analyze-wallet`.
4. `sniper/web/serializers.py` — add `archetype`, `action_decision`,
   `tag_fit_for_market` fields.
5. `routes/api_core.py`, `api_followed.py`, `api_strategies.py` —
   pass new fields through serializer.
6. `static/app.js` — feature-flagged new badges (default off).
7. `.claude/agents/wallet-dossier-writer.md` — extend prompt to ingest
   ProtocolReport + Layer 5 decision.
8. `.claude/agents/paper-trade-analyst.md` — switch SQL grouping to
   `action_decision` enum.

**Done when:** every API response carries new + legacy fields; UI
toggles cleanly; `paper-trade-analyst` runs on new fields; CI green.

### Phase 5 — Migrate alerter / signals / paper trader inputs
**Goal:** Flip each consumer to read new fields as authority. Legacy
fields still populated by `analyze_users.py` for safety. One commit
per gate.

**Files (per-commit slices):**
1. `agent_pipeline.py:178-209` — `agent_signal_severity` reads
   `wallet_layer5_decision`.
2. `agent_pipeline.py:351-393` — `collect_agent_high_signals` reads
   `wallet_layer5_decision` (replaces `flagged_users` query).
3. `alerter.py:111-128 THRESHOLDS`, `alerter.py:1418-1442`,
   `alerter.py:1476-1481`, `alerter.py:1541-1551`, `alerter.py:1656` —
   replace each with `wallet_layer5_decision`-driven gate.
4. `paper_trader.py:1042` (Gate 0a) — `action_decision IN
   ('exclude_for_copy','anti_pattern')` block.
5. `paper_trader.py:1045-1065` (Gate 0b) — drop, subsumed by 0a.
6. `paper_trader.py:1074-1082` (Gate 0c) — drop, subsumed by 0a or
   `monitor`.
7. `paper_trader.py:1100-1126` (Gate 0e) — `action_decision ==
   'validated_copy_candidate'`.
8. `paper_trader.py:1150-1164` (Gate 2) — replace with optional
   per-tag-fit gate.
9. `paper_trader.py:1167-1168` (Gate 3) — `mode != 'flipper'` OR
   `bot_detector.is_bot == False`.
10. `paper_trader.py:722-840 calculate_position_size` — replace
    `position_insider_score`, `win_rate`, `qualifying_roi` inputs with
    `wallet_tag_fit[tag].fit_score` and per-tag PnL stats.
11. `strategies/insider_copy_v1.py` → rewrite as
    `strategies/wallet_copy_v1.py`.
12. `strategy_runner.py:504,1241,1301` — squashing reads new fields.

**Done when:** every gate above runs from new analyzer outputs; legacy
`analyze_users.py` still computes scores but nothing live consumes
them; full `pytest` green; one week of paper-trade smoke test on VPS
shows comparable WR/PnL to pre-cutover baseline.

### Phase 6 — Disable old score/gate/insider-score decision paths
**Goal:** Stop running `_score_insider`, `_score_speculator`,
`_compute_wallet_quality_score`, `_score_position_insider`,
`_score_position_speculator`, `_compute_wallet_edge_tier`,
`_classify_wallet`, `_classify_position_signal`,
`compute_followability_score`. `analyze_users.py` becomes
identity-sync-only (Layer 0 wallet metadata) until Phase 8 deletes it.

**Files:**
- `analyze_users.py` — wrap legacy scoring fns in `if False:` or move
  to `archive/legacy_scoring/` and delete from `run_once`.
- `db_utils.py:312-387 compute_followability_score` — return constant
  0.0 with deprecation log; remove call sites.
- `agents/scoring-watchdog.md` — already retired, no-op.
- All env vars in Appendix A.4 — leave defined for one release for
  rollback safety; document as deprecated.

**Done when:** for one full analysis window (e.g. 24h), no row of
`user_analysis` is updated (writes go through `wallet_layer5_decision`
only); all daemons green.

### Phase 7 — Anomaly plugin (only if validated)
**Goal:** If the position-level entry-timing intuition is still believed
to carry edge after Phase 6, ship `strategies/anomaly_entry_timing_v1.py`
per § 6 above. Run for ≥ 60 paper trades. Promote or archive.

**Files:**
- `strategies/anomaly_entry_timing_v1.py` (NEW)
- `tests/test_strategy_anomaly_entry_timing.py` (NEW)
- Brain memory `finding/anomaly-entry-timing-v1-result-<date>`.

**Done when:** validation finding is persisted to brain. Plugin either
promoted to paper-active or archived.

### Phase 8 — Remove or archive dead legacy code
**Goal:** Hard delete + archive.

**Files to delete:**
- `analyze_users.py` (move identity-sync to `sniper/analysis/wallet_identity.py`).
- `_compute_followability_score` in `db_utils.py`.
- Tests `test_scoring.py`, `test_wallet_quality_score.py`,
  legacy parts of `test_paper_trader_sizing.py`,
  `test_real_trader_gates.py`.
- `flagged_users` table; `score_history` table; score columns from
  `user_analysis`, `position_signals`, `signal_history`.
- ADR-006 and ADR-007 entries replaced with ADR-009 pointing to
  wallet-analysis-protocol-v2.

**Files to archive (move, not delete):**
- `archive/legacy_scoring/analyze_users_pre_2026-04.py` (last working
  copy of the monolith).
- `archive/snapshots/2026-04-XX-signal-history.parquet`.

**Done when:** brain index regen passes, schema validator passes, full
test suite green, all services healthy on VPS, MEMORY.md updated.

---

## 8. Tests and validation

Each phase's per-phase TDD plan owns its tests in detail. The cross-phase
acceptance tests below prove the migration goals at the system level.
File names are proposals; create as needed.

| Test | What it proves | New / extends |
|---|---|---|
| `tests/test_no_legacy_score_in_decisions.py` | After Phase 6, no production-decision call site reads `insider_score` / `position_insider_score` / `wallet_quality_score` / `speculator_score` / `position_speculator_score` / `followability_cached` / numeric `wallet_edge_tier` / `classification` outside the allowlist. Implemented as a static grep over `alerter.py`, `paper_trader.py`, `real_trader.py`, `strategy_runner.py`, `strategies/`, `routes/`, `mcp_tools/`, `static/app.js`, `agents/`, `agent_pipeline.py`, `bot.py`, `sniper/web/serializers.py`. | NEW |
| `tests/test_no_synthetic_scores_program_wide.py` | Program-wide: no live runtime file outside `archive/` and `analyze_users.py` (during migration only) defines, returns, stores, or compares a value matching the synthetic-composite-score pattern in § 0 #4. Static AST + grep check. Direct measurements (`*_pnl_usdc`, `*_ci_*_usdc`, `*_p_value`, `*_win_rate`, `*_fill_rate`, `*_latency_ms`, `*_sample_size`, `*_days`, `tag_fit_score`, `tag_fit_rank`) are whitelisted. | NEW |
| `tests/test_wallet_layer5_decision_round_trip.py` | `analyze_wallet()` → persistence → MCP read → API read all return the same enum/reason codes. | NEW |
| `tests/test_alerter_uses_layer5.py` | After Phase 5: a wallet with `action_decision == 'monitor'` produces no alert; a wallet with `validated_copy_candidate` produces exactly one alert; dedup still works. | NEW |
| `tests/test_paper_trader_layer5_gate.py` | After Phase 5: paper_trader rejects a position whose wallet decision is `exclude_for_copy`; accepts when `validated_copy_candidate`; falls back conservatively (no entry) when decision row missing. | NEW |
| `tests/test_anomaly_strategy_is_not_authority.py` | Phase 7: anomaly plugin firing on a wallet whose Layer 5 says `monitor` does NOT result in a paper entry. | NEW |
| `tests/test_replay_curated_signals_no_score_dep.py` | After Phase 8: `replay_curated_signals` produces results without referencing dropped score columns. | NEW |
| `tests/test_strategy_wallet_copy_v1.py` | Replacement plugin behaves like `insider_copy_v1` did but reads only from `wallet_layer5_decision` + `wallet_tag_fit`. | NEW |
| `tests/test_no_orphan_paper_trades.py` | Existing — re-run after every migration step to ensure paper trades still close cleanly. | EXISTS |
| `tests/test_validate_schema_refs.py` | Existing — extend to include new tables; remove legacy table refs. | EXTENDS |
| `tests/test_dashboard_legacy_schema.py` | Existing — update or delete. After Phase 8, replace with `test_dashboard_new_schema.py`. | EXTENDS / REPLACES |
| `tests/test_history_capture.py` | Existing — keep coverage of `signal_history` non-score cols; remove score-col tests in Phase 8. | EXTENDS |
| Schema validator (`scripts/validate_schema_refs.py` via CI) | After every column add/drop. | EXISTS |
| Brain index freshness (CI) | After every column add/drop, `python3 scripts/brain_index.py generate` produces a clean diff. | EXISTS |

**Manual VPS smoke-test checklist (run after Phase 5 cutover):**
1. `python3 scripts/brain_health.py` clean.
2. All systemd services Active.
3. `journalctl --user -u sniper-paper -f` for 30 min — no exceptions.
4. `mcp_tools` `get_wallet_profile` for 5 known wallets returns Layer 5
   decision.
5. UI `/` loads; new badges render; legacy badges still render until
   Phase 8.
6. Telegram alerter sends 1+ alert sourced from `wallet_layer5_decision`.

---

## Appendix A — Reference inventories

### A.1 Files inspected (six parallel audits)

**New analyzer (Audit 1):**
- `sniper/analysis/wallet_protocol/analyze_wallet.py`, `fetch.py`,
  `held_analyst.py`, `flipper.py`, `mode_router.py`, `stage1_gates.py`,
  `tag_inference.py`, `statistics.py`, `flip_reconstruction.py`,
  `subgraph_reader.py`, `report.py`, `capital_ledger.py`,
  `exec_profile_adapter.py`, `price_history_adapter.py`,
  `independence.py`, `h5_tag_edge.py`, `types.py`
- `sniper/analysis/tag_wallet_pnl.py`, `wallet_tag_fit.py`,
  `tag_analytics.py`, `wallet_behavior.py`
- `scripts/run_wallet_protocol.py`, `wallet_archetype_extractor.py`,
  `build_wallet_tag_pnl.py`, `build_wallet_tag_fit.py`
- Specs: `wallet-analysis-protocol-v1.md`, `v2.md`,
  `wallet-protocol-bulletproofing.md`,
  `wallet-protocol-v2-gaps-from-eatpraylove.md`,
  `2026-04-22-wallet-understanding-brain-design.md`,
  `2026-04-23-wallet-execution-profile-design.md`,
  `2026-04-23-wallet-execution-profile-v2-drift-to-resolution.md`,
  `2026-04-23-wallet-taxonomy-migration.md`,
  `2026-04-24-tag-analytics.md`, `2026-04-24-tag-wallet-pnl-design.md`,
  `2026-04-24-tag-archetype-fit-design.md`

**Legacy core (Audit 2):**
- `analyze_users.py` (~158KB), `db_utils.py:312-387`, `web_settings.py`,
  `sniper/settings/web.py`, `sniper/settings/paper.py`, archive
  classifiers.

**Alerter (Audit 3):**
- `alerter.py`, `agent_pipeline.py`, `routes/api_misc.py`,
  `routes/api_agent.py`, `bot.py`, `bot_detector.py`.

**Paper trader (Audit 4):**
- `paper_trader.py`, `strategies/insider_copy_v1.py`,
  `strategies/fade_v1.py`, `strategy_runner.py`,
  `routes/api_strategies.py`, `routes/api_core.py`, paper tests.

**Signal pipeline (Audit 5):**
- `db.py:3007-3107` `record_low_buy_trade`, `db.py:3606-3701`
  `upsert_flagged_users`, `sniper/persistence/db_signals.py`,
  `sniper/persistence/db_replay.py`, `sniper/persistence/db_hyperliquid.py`,
  `sniper/persistence/db_snapshots.py`, `copy_viability.py`,
  `replay_engine.py`, `retrospective_analyzer.py`, `health_check.py`.

**Downstream readers (Audit 6):**
- `routes/api_core.py`, `api_followed.py`, `api_strategies.py`,
  `api_watchlist.py`, `api_misc.py`, `api_agent.py`, `ws_stream.py`,
  `static/app.js`, `sniper/web/serializers.py`, `mcp_tools/read_tools.py`,
  `.claude/agents/paper-trade-analyst.md`, `scoring-watchdog.md`,
  `wallet-dossier-writer.md`.

### A.2 New analyzer modules — completeness matrix

| Module | Spec implemented | % complete | Outstanding |
|---|---|---|---|
| `analyze_wallet.py` | v1/v2 §3-§11 | ~95% | wire persistence |
| `fetch.py` | v1/v2 §4 | 100% | — |
| `held_analyst.py` | v1/v2 §6 H1-H7 | ~95% | H2_sub080 time-split (v3 amendment A); H1 dollar-weighted diagnostic (D) |
| `flipper.py` | v2 §7 FH1-FH6 | 100% | — |
| `mode_router.py` | v2 §5 | 100% | thresholds 0.30/0.70 still "judgment calls" — calibration pending |
| `stage1_gates.py` | v2 §3 | ~85% | F2 still flag-only; v3 amendment B promotes to gate |
| `tag_inference.py` | v2 §6.1 H5 + §8 | ~70% | `tag_nav_category` ~50/600 seeded |
| `statistics.py` | v1/v2 inference | 100% | CUSUM h=25, BH q=0.10 calibrated |
| `flip_reconstruction.py` | v2 §4.7 | 100% | — |
| `subgraph_reader.py` | v2 §2 fallback | 100% | tested on SwissMiss n_fills=28280 |
| `report.py` | v1/v2 §11-§12 | ~90% | dollar-weighted diagnostic, H1 time-split sections |
| `capital_ledger.py` | follow-up 1 | 100% | — |
| `tag_wallet_pnl.py` | tag-analytics design | 100% | — |
| `wallet_tag_fit.py` | tag-archetype-fit design | 100% | — |
| `tag_analytics.py` `compute_tag_market_stats` | tag-analytics §3.1 | function/tested | no DB persistence or live analyzer consumer yet |
| `wallet_archetype_extractor.py` | brain-design Layers 1-5 | ~80% | Layers 3-4 stubbed |
| `wallet_behavior.py` | brain-design behavior pattern | partial | nav-category lookup not yet integrated |
| Persistence (`upsert_wallet_layer5_decision`) | NEW | 0% | **NOT IMPLEMENTED** |
| MCP `analyze_wallet` / `get_wallet_decision` | NEW | 0% | **NOT IMPLEMENTED** |
| HTTP `/api/wallet-decision/<wallet>` | NEW | 0% | **NOT IMPLEMENTED** |

### A.3 Concrete missing analyzer paths

1. **`wallet_layer5_decision` persistence layer** — module + table +
   wiring inside `analyze_wallet.py`. Highest leverage; everything
   downstream depends on it.
2. **`tag_market_stats` persistence/consumer wiring** —
   `compute_tag_market_stats` exists and is tested; still needs a durable
   storage path or direct analyzer integration.
3. **`tag_nav_category` backfill** — seed remaining ~550 tags from
   gamma `event_slug → category`.
4. **MCP analyze tool surface** — `analyze_wallet`,
   `get_wallet_decision`, `get_archetype`.
5. **HTTP wallet-decision endpoint** — `routes/api_wallets.py`.
6. **`wallet_archetype_extractor` Layers 3–4** — populate edge
   hypotheses + outcome attribution from analyzer output.
7. **v3-amendment code paths** (A–E from
   `wallet-protocol-v2-gaps-from-eatpraylove.md`):
   - A: H2_sub080 time-split loop; H1 winsorized default already merged.
   - B: F2 from flag → gate (`H1_sub_top3` diagnostic).
   - C: H1 time-segmented per-regime (`EDGE_MAY_BE_FRONT_LOADED` flag).
   - D: H1 dollar-weighted ROI diagnostic.
   - E: certainty-buyer bucket peer baseline.
8. **`analyzer_runs` audit table** — replaces or augments
   `analysis_runs` semantics; tracks each run with version, success,
   duration.
9. **Replacement/removal for `score_history` consumed by
   `insider_copy_v1`** — remove that stability dependency during rewrite,
   or keep `score_history` until a later, real decision-history need exists.

### A.4 Web-settings env vars feeding legacy gates (29)

`SCORE_FLAG_THRESHOLD`, `SPECULATOR_SCORE_HIGH`, `EMERGING_SCORE_THRESHOLD`,
`EMERGING_MIN_POSITION_INSIDER_SCORE`,
`EMERGING_MAX_POSITION_SPECULATOR_SCORE`,
`EMERGING_MIN_TOP_OPEN_VALUE`, `EMERGING_MIN_TOTAL_OPEN_VALUE`,
`POSITION_SCORE_THRESHOLD`, `POSITION_SPECULATOR_THRESHOLD`,
`WINRATE_THRESHOLD`, `INSIDER_MIN_TOTAL_PNL`,
`INSIDER_MIN_TOP_OPEN_VALUE`, `INSIDER_MIN_TOTAL_OPEN_VALUE`,
`INSIDER_MIN_SERIOUS_TRADES_14D`, `MIN_REPEAT_HITS_14D`,
`INSIDER_MAX_POSITIONS`, `RT_MIN_INSIDER_SCORE`,
`RT_MAX_SPECULATOR_SCORE`, `RT_MIN_QUALIFYING_ROI`,
`PAPER_ENTRY_MIN_INSIDER_SCORE`, `PAPER_MAX_SPECULATOR_SCORE`,
`PAPER_MIN_WALLET_QUALITY_SCORE`, `AGENT_SIGNAL_MIN_INSIDER_SCORE`,
`AGENT_SIGNAL_MIN_POSITION_INSIDER_SCORE`,
`OPEN_SIGNALS_FOCUS_MIN_INSIDER_SCORE`,
`OPEN_SIGNALS_FOCUS_MIN_SPECULATOR_SCORE`,
`FLOW_MOVER_MIN_SPECULATOR_SCORE`,
`FLOW_MOVER_MIN_POSITION_SPECULATOR_SCORE`, `PROOF_MIN_INSIDER`,
`REPLAY_DEFAULT_MIN_INSIDER`, `REPLAY_DEFAULT_MAX_SPEC`.

Treatment in Phase 6: log a deprecation warning on read; drop in Phase 8.

### A.5 Risks summary

| Risk | Severity | Mitigation |
|---|---|---|
| Insider_copy_v1 stability tracker breaks when `score_history` drops | HIGH | Phase 5 step 11 removes or rewrites that dependency before Phase 8 drop; no decision-history replacement until decisions are real |
| Replay engine reproducibility lost on pre-Phase-3 windows | MEDIUM | Snapshot `signal_history` to parquet before Phase 8 |
| UI partial migration shows blank cards | MEDIUM | Feature flag in `static/app.js` for the duration of Phases 4–7 |
| `real_trader.py` regressions if migrated under same window | HIGH | Real trader is paused; defer to dedicated post-Phase-8 plan |
| Agent pipeline severity mis-grades during cutover | MEDIUM | Phase 5 step 1 ships severity logic alongside `wallet_layer5_decision` reads |
| `wallet_layer5_decision` not populated for newly discovered wallets fast enough | MEDIUM | `run_wallet_protocol.py` triggered on candidate_user insert in Phase 3 |
| Tag-nav-category backfill stalls | LOW | Nav-grain inference is provisional flag, not blocking |
| `analyze_users.py` is co-modified during cutover by another agent | HIGH | CLAUDE.md parallel-work guardrails (`wip/foreign-…` branches), full-file lock during Phase 6 |

### A.6 Recommended next implementation steps (concrete)

In strict order:

1. **Get sign-off on this document.** No code changes until the
   architecture map and verdicts in §3 are agreed.
2. **Write the Phase 2 spec:**
   `docs/superpowers/specs/2026-05-XX-wallet-analyzer-output-contract.md`.
   Lock enum values + reason codes. Define
   `ProtocolReport.overall_verdict` (analysis) separately from
   `wallet_layer5_decision.action_decision` (runtime instruction). State
   explicitly that `wallet_tag_fit` is context/sizing/evidence only and that
   no decision-history table is added until analyzer decisions are real. Run
   `schema-architect` and `platform-reviewer` on it.
3. **Write the Phase 3 TDD plan** via
   `superpowers:writing-plans`: persistence module, `init_db` calls,
   `analyze_wallet.py` wiring, schema validator, brain index regen.
4. **In parallel** (independent of Phases 2–3):
   - Backfill `tag_nav_category` (~2-4h gamma pull).
   - Wire `compute_tag_market_stats` into persistence or analyzer reads
     (~1-2h).
   - Land v3 amendments A & B in `held_analyst.py`/`stage1_gates.py`.
5. **Phase 3 ship.** All currently-followed wallets have a
   `wallet_layer5_decision` row.
6. **Phase 4 ship.** All readers carry new fields alongside legacy.
7. **Phase 5 cutover.** One commit per gate. Soak each ≥ 24h on VPS.
8. **Phase 6 disable.** Verify zero `user_analysis` writes for one
   analysis cycle.
9. **Phase 7** (optional, gated on validation).
10. **Phase 8 cleanup.**

---

## Appendix B — Cross-references

- `CLAUDE.md` — parallel-work guardrails, ADR-006/007 (to be retired).
- `MEMORY.md` — paper-trade edge by insider band (last surviving
  empirical justification for the anomaly plugin).
- `index/schema_registry.json` + `index/dependency_index.json` —
  regenerated after every schema change in this migration.
- `scripts/brain_index.py`, `scripts/validate_schema_refs.py` — gates
  for every commit.
- `.claude/agents/deprecation-migrator.md` — drives the per-consumer
  migration matrix.
- `.claude/agents/schema-architect.md`, `platform-reviewer.md`,
  `research-methodologist.md` — sign-off agents for Phases 2, 5, 7.

# Dashboard Consolidation — Design Spec

## Goal

Turn the web UI into the primary **operator console** for Sniper by exposing high-value backend capabilities that already exist, reducing tab sprawl, and making the system’s true state visible without relying on MCP, Telegram, or ad hoc API queries.

This is an **information architecture consolidation** project, not a visual rewrite and not a backend trading-feature project.

## Problem Statement

Sniper currently has four operator surfaces:

1. Web UI
2. MCP tools
3. Telegram bot
4. CLI/API/scripts

The backend is significantly ahead of the browser UI. The web app currently presents mostly table views across 9 tabs:

- Signals
- Thesis
- Markets
- Positions
- Leaderboard
- Followed
- Strategies
- Research
- Proof Bar

This produces three product problems:

1. **Important truth surfaces are hidden**
   - classification diagnostics
   - alert queue and mutes
   - instance awareness
   - strategy-family metadata
   - feed/health context already exposed through proof-bar health details

2. **Conceptually related views are split**
   - Signals vs Thesis
   - Leaderboard vs Followed
   - Proof Bar vs general operator/health state

3. **The UI understates the system**
   The app behaves like a trading intelligence platform, but the UI feels like a collection of unrelated data tables.

## Scope

### In Scope

- Consolidate tabs and navigation
- Add one authoritative Operator home screen
- Surface existing read-only backend state already available via endpoints
- Add wallet drill-down
- Add strategy-family and instance awareness to existing views
- Add alert/mute management UI using existing agent APIs
- Reuse existing proof-bar, replay, strategy, market, and wallet endpoints

### Out of Scope

- No new trading logic
- No new strategy families
- No new proof-bar rules
- No fleet dashboard
- No visual redesign for its own sake
- No LLM features
- No bot-management control plane
- No new real-trading controls

### Freeze Position

This work is within freeze because it primarily:

- exposes existing backend state,
- removes ambiguity,
- consolidates existing views,
- and improves operator correctness.

It does **not** change trading behavior or relax go-live gates.

## Design Principles

1. **Operator truth over feature count**
   The UI must stop making the operator reconstruct system state across tabs.

2. **One home screen**
   Readiness, health, alerts, and major blockers belong in one place.

3. **Fewer tabs, deeper drill-downs**
   Merge overlapping tabs rather than adding more.

4. **Existing API first**
   Use current endpoints wherever possible. If a capability has no truthful API surface yet, defer it rather than inventing a UI fiction.

5. **Instance-aware, strategy-aware views**
   The browser must reflect the actual multi-strategy, multi-instance architecture.

6. **Do not mix incompatible metrics**
   Insider-copy proof-bar metrics stay distinct from experimental strategy metrics.

## Target Information Architecture

### Current

| Current Tab | Problem |
|---|---|
| Signals | overlaps with Thesis |
| Thesis | separate signal surface with overlapping purpose |
| Markets | good, but under-contextualized |
| Positions | good, but not strategy/instance-aware enough |
| Leaderboard | overlaps with Followed |
| Followed | overlaps with Leaderboard and wallet detail |
| Strategies | good base, but under-explains strategy family/runtime context |
| Research | useful, but too isolated |
| Proof Bar | operator home exists, but only for readiness, not for the rest of the system |

### Target

| Proposed Tab | Source |
|---|---|
| Operator | new operator home; absorbs Proof Bar role |
| Signals | merge current Signals + Thesis |
| Markets | retain and enrich |
| Wallets | merge Leaderboard + Followed |
| Positions | retain and enrich |
| Strategies | retain and enrich |
| Research | retain as deep-dive |

End-state tab count: **7**

## Global Shell

The shell remains visible regardless of tab and should carry the minimum state needed to orient the operator.

### Persisted Elements

1. Existing proof strip
2. Current clock
3. New compact operator cluster on the right side of the header or directly below it:
   - alert count
   - mute count
   - Telegram status
   - health rollup

### Truth Sources

| Shell Item | Source |
|---|---|
| Proof strip | `GET /api/proof-bar` |
| Alert count | `GET /api/agent/alerts?status=new&limit=1` plus count payload |
| Mute count | `GET /api/agent/mutes` |
| Telegram/alerter state | `/api/proof-bar` → `health_details.alerter` |
| Health rollup | `/api/proof-bar` |

### Instance Tag in the Shell

Instance tag is desirable but **not mandatory in Phase 1**.

Reason:
- the backend persists `instance_tag` on strategies, replay configs/runs, and framework data,
- but there is not yet a dedicated operator summary endpoint that exposes one canonical runtime `instance_tag` for the whole shell.

So:
- Phase 1: no global shell instance badge
- Phase 2: add it once there is a clean source
- meanwhile, instance tags appear at the card/table row level where already available

## Tab Specifications

## 1. Operator

This becomes the default landing tab and absorbs the Proof Bar tab’s role.

### Purpose

Answer these questions immediately:

- Is the system healthy?
- Are we accumulating valid proof?
- What is blocked right now?
- Are alerts or mutes hiding anything important?
- Why is the signal pipeline dry or active?

### Sections

#### 1a. Proof & Readiness

Use the existing proof-bar data and visuals:

- top blocking banner
- criteria cards
- monthly section
- pace section
- health section

Source:
- `GET /api/proof-bar`

#### 1b. Alert Queue & Mutes

Surface the backend agent alert/mute system in the browser for the first time.

Features:
- list recent alerts
- filter by `new`, `sent`, `acknowledged`
- ack single alert
- bulk ack visible alerts
- view active mutes
- add mute
- delete mute

Sources:
- `GET /api/agent/alerts`
- `POST /api/agent/alerts/<id>/ack`
- `POST /api/agent/alerts/ack-bulk`
- `GET /api/agent/mutes`
- `POST /api/agent/mutes`
- `DELETE /api/agent/mutes/<id>`

#### 1c. Classification Diagnostics Summary

Show the live blocker summary that explains the current signal drought.

Features:
- total wallets above insider score threshold
- insider-like wallet count
- low-signal wallet count
- top failed checks table
- open qualifying signal count
- max open insider score

Source:
- `GET /api/classification-diagnostics`

#### 1d. Strategy / Replay Snapshot

A compact “what experiments exist” summary:
- active strategies by family/status
- latest replay runs
- latest replay compare run or top replay metrics

Sources:
- `GET /api/strategies`
- `GET /api/replay/runs`
- `GET /api/replay/configs`

### Explicit Deferrals

Do **not** show these in Operator v1 unless there is a truthful read surface:
- per-feed freshness card
- risk-policy decision stream
- global instance tag badge

Those are architecturally desirable but not cleanly available yet without new backend summary endpoints.

## 2. Signals

Merge current **Signals** and **Thesis**.

### Purpose

Present all signal surfaces in one place with clear gate transparency.

### Sections

#### 2a. Live Signals

Current Signals table, preserved but cleaned up:
- open position signals
- convergence cards
- hide-low toggle
- search/filter controls

Source:
- current Signals path in `static/app.js`
- `GET /api/open-position-signals`
- `GET /api/convergence-signals`

#### 2b. Hard-Gated Thesis

Current Thesis feed becomes a subsection within Signals rather than a separate tab.

Source:
- `GET /api/thesis-signals`
- `GET /api/thesis-candidates`

#### 2c. Gate Transparency

Surface why signals are or are not action-worthy:
- show whether a row is:
  - qualifying
  - near-miss
  - low-signal
  - thesis candidate
- make current gate/skip reasons readable inline

### Result

The operator stops asking:
- “Is this in Signals or Thesis?”

## 3. Markets

Keep this tab, but frame it explicitly as market intelligence.

### Purpose

Show where capital and conviction are clustering across the market universe.

### Keep

- domain summary cards
- top markets by insider capital
- open-only and insider threshold filters

Source:
- `GET /api/market-intelligence`

### Add

- convergence annotation per market where available
- related recent news summary if present

Sources:
- `GET /api/convergence-signals`
- `GET /api/research/clusters`
- optional read-through to market-related news once/if that feed is active

No new edge logic belongs here. This remains a research/intelligence view.

## 4. Wallets

Merge **Leaderboard** and **Followed** into a single tab with a wallet detail drawer/page.

### Purpose

Make wallets the primary human entity, not scattered across two tabs.

### Top-Level Views

#### 4a. Wallet Table

Unified table with:
- followability ranking
- classification
- score
- followed state
- top domains
- recent activity summary

Sources:
- `GET /api/wallet-followable-leaderboard`
- `GET /api/followed-wallets`
- `GET /api/high-winrate-wallets`
- `GET /api/emerging-insiders`

#### 4b. Filters

- all wallets
- followed only
- emerging insider
- insider candidate
- high winrate review
- speculator flow movers

Sources:
- `GET /api/speculator-flow-movers`
- leaderboard payloads

### Wallet Detail Drawer / Page

Required sections:
- classification summary and reasons
- top domains
- current portfolio
- recent research snapshots
- recent activity timeline
- cluster membership if any
- follow/unfollow state
- mute/block/ignore status where applicable

Sources:
- `GET /api/followed-wallets/<wallet>/portfolio`
- `GET /api/wallet-portfolio/<wallet>` (compat alias)
- `GET /api/research/snapshots?wallet=...`
- `GET /api/research/activity?wallet=...`
- `GET /api/research/clusters?wallet=...`
- `GET /api/blocked-wallets`
- `GET /api/ignored-wallets`

This is the single highest-value drill-down addition in the whole consolidation.

## 5. Positions

Keep paper and real sub-tabs, but make attribution and semantics clearer.

### Paper Positions

Add visible metadata:
- strategy family
- strategy id
- instance tag where available
- disqualified annotation
- clearer exit reason taxonomy

Sources:
- `GET /api/paper-trades`
- `POST /api/paper-trades/<id>/close`

### Real Positions

Keep current truth-first rendering.

Sources:
- `GET /api/real-trades`
- existing real-position actions remain where they are; no new real-trading controls

### Goal

The operator should be able to answer:
- which strategy opened this
- on which instance
- whether this is experimental or proof-bar relevant

## 6. Strategies

Keep the current tab, but make it reflect the actual framework.

### Add Metadata to Cards/Rows

- `strategy_family`
- `strategy_type`
- `instance_tag`
- required feeds
- execution mode support
- replay support state

Sources:
- `GET /api/strategies`
- `GET /api/strategies/<id>`
- `GET /api/strategies/<id>/executions`
- replay endpoints

### Replay Section

Keep the replay panel, but make instance/family context visible:
- config source
- strategy type
- instance tag
- compare summary

Sources:
- `GET /api/replay/configs`
- `POST /api/replay/run`
- `GET /api/replay/runs`
- `GET /api/replay/runs/<id>`
- `GET /api/replay/compare`

### Goal

The Strategies tab should finally look like a strategy framework, not just a CRUD list with a modal.

## 7. Research

Keep this as the deep-dive tab.

### Purpose

Show:
- snapshots
- activity timelines
- clusters
- suspicious trade patterns
- detailed diagnostics that should not clutter Operator

Sources:
- `GET /api/research/snapshots`
- `GET /api/research/activity`
- `GET /api/research/clusters`
- `GET /api/recent-suspicious-trades`

### Rule

Research is where detail lives after the operator has identified something worth investigating from Operator/Wallets/Markets.

## Endpoint Mapping Summary

### Already Used in UI

- `/api/open-position-signals`
- `/api/convergence-signals`
- `/api/market-intelligence`
- `/api/paper-trades`
- `/api/real-trades`
- `/api/wallet-followable-leaderboard`
- `/api/followed-wallets`
- `/api/followed-wallets/<wallet>/portfolio`
- `/api/strategies`
- `/api/strategies/<id>`
- `/api/strategies/analyze-wallet`
- `/api/research/snapshots`
- `/api/research/activity`
- `/api/research/clusters`
- `/api/proof-bar`
- replay endpoints

### Existing Endpoints to Surface in UI

- `/api/classification-diagnostics`
- `/api/agent/alerts`
- `/api/agent/alerts/<id>/ack`
- `/api/agent/alerts/ack-bulk`
- `/api/agent/mutes`
- `/api/agent/mutes/<id>`
- `/api/high-winrate-wallets`
- `/api/emerging-insiders`
- `/api/speculator-flow-movers`
- `/api/recent-suspicious-trades`
- `/api/blocked-wallets`
- `/api/ignored-wallets`

## Rollout Plan

## Phase 1 — Operator Tab

Deliver:
- new Operator tab
- move Proof Bar content into Operator
- alert/mute section
- classification diagnostics summary
- strategy/replay snapshot

Keep old tabs for now.

## Phase 2 — Signals Consolidation

Deliver:
- fold Thesis into Signals
- remove separate Thesis tab
- improve gate transparency labels

## Phase 3 — Wallets Consolidation

Deliver:
- merge Leaderboard + Followed into Wallets
- add wallet detail drawer/page
- remove separate Leaderboard and Followed tabs

## Phase 4 — Strategy / Position Attribution

Deliver:
- instance-aware and family-aware labels in Strategies, Positions, Replay

## Phase 5 — Navigation Cleanup

Deliver:
- final 7-tab shell
- remove retired tabs

## Acceptance Criteria

1. The operator can understand readiness, health, alerts, and classification drought from one tab.
2. There is no longer a need to switch between Leaderboard and Followed to reason about a wallet.
3. The UI clearly distinguishes proof-bar insider-copy data from experimental strategy data.
4. Strategy rows and replay runs visibly reflect strategy family and instance context where available.
5. Existing backend truth surfaces are exposed without inventing new metric semantics.
6. No existing trading logic changes are required for the consolidation.

## Non-Goals for This Spec

- No design-system rewrite
- No dark/light theme rethink
- No mobile-first redesign
- No fleet orchestration UI
- No service restart/deploy controls in browser
- No new strategy behavior
- No new bot management workflow

## Recommendation

Implement this as a **dashboard consolidation program**, not a single giant UI rewrite.

The first implementation plan should cover only:

1. Operator tab
2. Signals + Thesis merge
3. Wallets merge + wallet detail drawer
4. strategy / instance attribution badges

That gives the biggest operator-value delta without destabilizing the whole interface.

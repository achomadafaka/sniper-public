# Claude Code Automations Design Spec

**Date:** 2026-03-26
**Status:** Approved
**Origin:** GPT-reviewed recommendations, refined through brainstorming

## Goal

Add 5 Claude Code automations to the Sniper project to improve safety, observability, and analysis capability. Priority order: secret-file blocker > scoring watchdog > wallet research > paper trade analyst > VPS health expansion.

## Rejected / Deferred

- **Auto-format hook (PostToolUse):** Rejected. Would rewrite files mid-task, add noise on partial edits, fight existing targeted-test hook, make diffs harder to reason about.
- **GitHub MCP server:** Deferred. Not urgent, depends on GITHUB_PERSONAL_ACCESS_TOKEN, add when actually blocked on GitHub workflow inside Claude.
- **Separate /health skill:** Rejected in favor of expanding existing vps-health agent. Avoids duplicate health surface that would drift over time.

---

## Automation 1: PreToolUse Secret-File Blocker Hook

### Purpose

Prevent Claude from directly editing sensitive files containing credentials, tokens, and keys via Edit/Write tools.

### Blocked paths (path-based only, no content keyword matching)

- `.env` (exact match)
- `.env.*` except `.env.example` and `.env.sample`
- `*.pem`
- `*.key`
- Files under `secrets/` or `credentials/` directories at any depth

### Not blocked

- `config/` — contains normal working files like `config/ui_live_audit_manifest.yaml`
- Code/docs that contain words like "token" or "secret" in their content — this is purely path-based

### Implementation

- Script: `scripts/secret_file_guard.py`
- Reads `TOOL_INPUT` from env var with stdin fallback
- Extracts `file_path` from JSON payload
- Normalizes and resolves paths (handles relative/absolute)
- Checks against blocked patterns
- Exit 0 = allow, Exit 1 + stderr message = block

### Error message format

```
BLOCKED: Direct edits to sensitive files are disabled.
Matched rule: {rule description, e.g. ".env files" or "*.pem private keys"}
If you need to update this file, ask the user to edit it directly or use .env.example as a template.
```

### Hook configuration (in .claude/settings.local.json)

**IMPORTANT:** This must be MERGED into the existing settings.local.json, which already has a `PostToolUse` hook for `Edit|Write` running `targeted_test_hook.py`. Add `PreToolUse` alongside, do not replace existing hooks.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "python3 scripts/secret_file_guard.py",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

### Known limitation

This blocks Edit/Write tool usage only. It does not stop Bash-based file mutation (sed, python -c, echo >>). Acceptable as a first guardrail, not complete secret-file protection.

---

## Automation 2: Scoring Watchdog Agent

### Purpose

Monitor the wallet_quality_score rollout and ongoing health. Highest priority new agent — directly matches the newly deployed scoring system.

### File

`.claude/agents/scoring-watchdog.md`

### Model

sonnet (cheap, sufficient for DB queries)

### Tools

Bash, Read

### Invocation

Manual only, no timer. Used after scoring deploys or on-demand.

### Checks

1. **Population progress** — scored vs total, eligible vs ineligible vs unscored counts
2. **Score distribution** — count by band (0-19, 20-39, 40-59, 60-79, 80-100)
   - Before 100 scored wallets: report distribution only, no failure on empty bands
   - After 100+: warn only on obvious collapse (80+ = 0) or extreme concentration (>70% in one band)
3. **Hard filter breakdown** — read from canonical fields on `user_analysis`:
   - `wallet_quality_eligible`
   - `wallet_quality_fail_reason`
   - `market_maker_detected`
   - Note: `gate0_status` lives on `wallet_registry` table, NOT `user_analysis`. For gate0 counts, either query `wallet_registry.gate0_status` or parse `wallet_quality_fail_reason LIKE 'gate0%'`.
   - Use `lb_pnl` only as cross-check for anomaly detection
   - **Column existence:** These columns are lazily created by `ensure_wallet_quality_columns()`. If the columns don't exist (e.g. local dev DB before analyzer runs), the watchdog must handle `OperationalError: no such column` gracefully by reporting "scoring rollout not started" instead of crashing.
4. **Anomaly detection**
   - Any score 80+ wallet with negative lb_pnl
   - Any eligible wallet below Gate 0 floor that wasn't caught
5. **Paper trade leakage** — any recent paper_trades from wallets where wallet_quality_eligible = 0
6. **Known wallet spot-checks** — conditional, not hardcoded:
   - If SwissMiss present: verify high score
   - If tradecraft present: verify market_maker flagged
   - If known losers present: verify gate0 failed
   - If absent: report "not found", not automatic fail
7. **Rollout stall detection**
   - Wallets scored in last 6h / 24h: use `user_analysis.analyzed_ts` filtered to rows where `wallet_quality_score IS NOT NULL`. Note: `score_history` does NOT contain `wallet_quality_score` — do not use it for rollout timing.
   - Latest analyzed_ts across all user_analysis rows
   - If population is not progressing while analyzer is active: WARN or FAIL

### Verdict logic

- **FAIL:** score 80+ wallet with negative lb_pnl, eligible wallet below Gate 0 floor, paper trades from ineligible wallets, scoring stalled
- **WARN:** suspicious distribution skew, expected spot-check wallet missing, slow rollout progress
- **HEALTHY:** no anomalies and population progressing

### Output format

```
SCORING HEALTH — {timestamp}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Population:    {scored}/{total} ({pct}%), {eligible} eligible, {ineligible} blocked, {unscored} pending
Distribution:  80-100: N | 60-79: N | 40-59: N | 20-39: N | 0-19: N
Hard filters:  gate0: N failed | market_maker: N | evidence: N
Anomalies:     {count} issues found
Paper leakage: {count} trades from ineligible wallets in last 24h
Spot checks:   SwissMiss {status} | tradecraft {status} | known losers {status}
Rollout:       {scored_6h} scored in 6h | {scored_24h} in 24h | latest: {ts}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Verdict: HEALTHY / WARN / FAIL
Issues: {list if any}
```

---

## Automation 3: Wallet Research Skill

### Purpose

Professional-grade wallet due diligence before deciding to copy a trader. 7-section analysis grounded in what the repo can actually prove.

### Core principle

Use `scripts/wallet_profiler.py --json` as the primary analysis engine; enrich, do not duplicate.

### File

`.claude/skills/wallet-research/SKILL.md`

### Model

sonnet

### Tools

Bash, Read

### Invocation

User-only (`disable-model-invocation: true`). Invoke with `/wallet-research 0xabc123`.

**Argument parsing:** The skill receives the full argument string after the skill name. The SKILL.md must instruct the agent to extract the wallet address (0x-prefixed hex string) from the first argument. If no address provided, prompt the user.

### Execution approach

1. Run `python3 scripts/wallet_profiler.py {address} --json` for the 5-layer backbone
2. Query SQLite for enrichment (user_analysis, score_history, position_signals, paper_trades, market_metadata)
3. Optionally verify lb_pnl live via `curl` to Polymarket profile API as secondary check (not core dependency — the skill uses Bash/Read only, no WebFetch tool)
4. For sparse-data wallets (newly discovered, few signals, no paper trades), collapse empty sections to a single "Insufficient data" line rather than showing the full template with zeros

### Analysis sections

#### Section 1: Identity & Standing
- Source: wallet_profiles + user_analysis
- Profile (username, account age, lb_rank, lb_vol)
- wallet_quality_score + eligibility + hard filter results
- Classification, edge tier
- Insider score trend: `user_analysis.insider_score_trend` exists as a precomputed field (use it for quick summary). For richer analysis, recompute from `score_history` rows. Do NOT claim wallet_quality_score trend — no historical series exists in v1.

#### Section 2: Trading Mechanics
- Source: user_analysis + position_signals + profiler Layer 3 (behavior)
- Velocity: trades per day/week from trade_count + account_age_days
- Sizing: avg entry_notional, size consistency (CV from profiler behavior layer)
- Entry price profile by band (<0.30, 0.30-0.50, 0.50-0.70, 0.70+)
- Win rate per entry band — from resolved position outcomes, labeled "local-window" if derived from stream-captured data
- Execution style from profiler: accumulation / selective / mixed

#### Section 3: Domain Specialization
- Source: position_signals JOIN market_metadata.category + profiler Layer 2 (domain)
- Category-level breakdown: politics, sports, crypto, geo, entertainment, other
- Trade count per category (solid). Win rate and P&L per category labeled as **local-window / inferred outcome proxy** — these are derived from stream-captured position outcomes, not wallet-native realized truth.
- Concentration ratio, specialist flag (>60% in one category)
- Finer subdomain (e.g. "sports — tennis titles suggest ATP focus") labeled explicitly as inferred from title/event_slug, not canonical

#### Section 4: Wallet Risk Profile
- Source: user_analysis + position_signals + profiler Layer 1/3 (NOT paper_trades — paper_trades reflect our copy simulation, not the wallet's own behavior)
- Hold duration from position_signals.hold_hours (median, p25, p75)
- Zombie risk: open_cash_pnl vs closed_realized_pnl ratio
- Capital efficiency (ROI): qualifying_pnl / qualifying_capital — this is an ROI-like metric, NOT a profit/loss ratio (avg win vs avg loss). True P/L ratio requires per-trade win/loss amounts which are not available at the wallet level from user_analysis.
- Both-sides detection from profiler Layer 1
- Max concurrent positions, concentration_ratio
- Activity state distribution from position_signals (holding / selling / liquidating)

#### Section 5: Edge Characterization
- Source: profiler Layer 5 (replicability) + score_history
- Edge source from profiler output: spread_capture / domain_model / information / behavioral / unclear
- Durability: insider_score trend over available score_history runs (stable / improving / degrading)
- Signal quality: % of positions with position_insider_score >= 70

#### Section 6: Copy Viability (where paper_trades belongs)
- Source: paper_trades + market_metadata + position_signals
- Paper trade track record: count, WR, PnL, avg ROI — labeled as "copy-simulation performance"
- Latency sensitivity: information-based edge = high latency need, domain model = lower
- Market liquidity: volume_24h of markets they trade in
- Copy verdict: STRONG COPY / CONDITIONAL COPY / DO NOT COPY

#### Section 7: Temporal Summary
- Source: user_analysis timestamps + score_history + position_signals
- Activity recency: last signal date, last trade timestamp, dormant flag (>10d)
- Insider score trajectory over available history
- Lifecycle: new (<30d) / established-stable / established-declining
- Note: wallet_quality_score has no historical series in v1

### Data confidence note (included in every report)

```
DATA CONFIDENCE
  Lifetime truth:     lb_pnl, lb_rank, lb_vol (Polymarket verified)
  Local-window proxy: trade/signal/activity metrics depend on stream capture window
  Copy performance:   paper_trades reflect OUR copy simulation, not the wallet's own exits
```

### Output format

```
WALLET RESEARCH — {address}
Profile: {username} | Age: {days}d | Rank: #{rank} | Vol: ${vol}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
STANDING
  Quality: {wq}/100 ({eligible/ineligible}) | Insider: {is}/100
  Gate 0: {pass/fail} | MM: {yes/no} | Class: {class}
  Score trend: {improving/stable/degrading} over {n} runs

MECHANICS
  Velocity: {n} trades/week | Burst: {max_60s}/min
  Sizing: avg ${avg} | CV: {cv} ({consistent/erratic})
  Entry profile: {pct}% value (<0.30) | {pct}% mid | {pct}% certainty (>0.70)
  Best entry band: {band} at {wr}% WR [local-window]

DOMAIN
  Primary: {category} ({pct}% of trades, {wr}% WR, ${pnl}) [local-window]
  Secondary: {category} ({pct}%, {wr}% WR) [local-window]
  Specialist: {yes/no} (concentration: {ratio})
  Subdomain: {inferred from titles if applicable}

RISK PROFILE
  Hold duration: median {hours}h (p25: {h}, p75: {h})
  Zombie risk: {low/medium/high} (open PnL: ${open} vs closed: ${closed})
  Capital efficiency: {pct}% ROI (qualifying_pnl / qualifying_capital)
  Both-sides: {pct}% of markets
  Concurrent positions: {count} | Concentration: {ratio}

EDGE
  Source: {spread_capture/domain_model/information/behavioral/unclear}
  Durability: {stable/improving/degrading} over {n} data points
  Signal quality: {pct}% of positions at insider >= 70

COPY VIABILITY
  Latency need: {low/medium/high}
  Market liquidity: {sufficient/marginal/thin}
  Paper results: {n} trades, {wr}% WR, ${pnl} [copy-simulation]
  Verdict: {STRONG COPY / CONDITIONAL COPY / DO NOT COPY}

TEMPORAL
  Last signal: {date} | Last trade: {date}
  Lifecycle: {new/established-stable/established-declining}
  Insider trend: {direction} over {n} runs

DATA CONFIDENCE
  Lifetime truth:     lb_pnl, lb_rank, lb_vol (Polymarket verified)
  Local-window proxy: trade/signal/activity metrics depend on stream capture window
  Copy performance:   paper_trades reflect OUR copy simulation, not the wallet's own exits
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ASSESSMENT
  {2-3 sentence professional summary}
```

---

## Automation 4: Paper Trade Analyst Agent

### Purpose

Multi-dimensional paper trade performance analysis. Most useful once enough wallet_quality_score-tagged trades accumulate.

### File

`.claude/agents/paper-trade-analyst.md`

### Model

sonnet

### Tools

Bash, Read

### Invocation

Manual only, no timer.

### Analysis rules

- **Closed trades only:** All WR/PnL/ROI calculations use `status != 'open'` trades only. Open trades are counted separately in the overview but excluded from performance metrics.
- **Minimum sample guard:** Any group with fewer than 5 closed trades shows count and raw PnL only — no WR% or ROI% (too noisy to be meaningful). Mark as "insufficient sample."

### Analysis dimensions

1. **By wallet_quality_score band** — WR and PnL across quality bands (0-19, 20-39, 40-59, 60-79, 80+, NULL/unscored). Compare against legacy insider_score bands. Source: join paper_trades wallet against user_analysis.wallet_quality_score.
2. **By strategy** — WR, PnL, trade count per strategy_type. **Important:** paper_trades has no strategy_type column. Use `strategy_positions` joined with `strategies.strategy_type` for this dimension. Paper_trades covers the general copy pipeline (use signal_type for that breakdown). Label each source clearly in the output.
3. **By market category** — paper_trades joined with market_metadata.category via condition_id
4. **By entry timing** — signal_insider_score at entry vs outcome
5. **By exit type** — resolution_win / resolution_loss / early_exit / stop_loss distribution and PnL contribution
6. **Edge decay** — recent (7d, 30d) vs all-time performance. Use `closed_at` for date filtering (when the trade resolved), not `opened_at`.

### Output format

```
PAPER TRADE ANALYSIS — {date}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Overview: {total} trades ({open} open, {closed} closed)
          Closed only — WR: {pct}% | PnL: ${amount} | Avg ROI: {pct}%
          (Groups with <5 closed trades show count + PnL only, no WR/ROI)

BY WALLET QUALITY      trades  WR     PnL
  80-100:              {n}     {wr}%  ${pnl}
  60-79:               {n}     {wr}%  ${pnl}
  40-59:               {n}     {wr}%  ${pnl}
  20-39:               {n}     {wr}%  ${pnl}
  0-19:                {n}     {wr}%  ${pnl}
  unscored (NULL):     {n}     {wr}%  ${pnl}

BY STRATEGY            trades  WR     PnL
  insider_copy_v1:     ...
  political_no_value:  ...

BY MARKET CATEGORY     trades  WR     PnL
  politics:            ...
  sports:              ...

BY INSIDER SCORE BAND  trades  WR     PnL
  90-100:              ...
  80-89:               ...

EXIT ANALYSIS
  resolution_win:  {n} ({pct}%)  PnL: ${amount}
  resolution_loss: {n} ({pct}%)  PnL: ${amount}
  early_exit:      {n} ({pct}%)  PnL: ${amount}
  stop_loss:       {n} ({pct}%)  PnL: ${amount}

EDGE DECAY
  Last 7d:  {n} trades, {wr}% WR, ${pnl}
  Last 30d: {n} trades, {wr}% WR, ${pnl}
  All time: {n} trades, {wr}% WR, ${pnl}
  Trend: {improving/stable/degrading}

ACTIONABLE FINDINGS
  - {finding 1}
  - {finding 2}
```

---

## Automation 5: Expanded VPS Health Agent

### Purpose

Expand existing `.claude/agents/vps-health.md` to include all CLAUDE.md startup checks. Single authoritative health surface. Manual invocation only.

### File

`.claude/agents/vps-health.md` (modify existing)

### Added checks (on top of existing service/error/paper/disk checks)

1. Brain health: `python3 scripts/brain_health.py`
2. Analyst timers: `systemctl --user status sniper-world-analyst.timer sniper-finance-analyst.timer`
3. Git state: `git status --short && git log --oneline -5` on VPS
4. Recent analyst logs: `tail -20` world_analyst and finance_analyst logs

### Preserved existing checks

- Service status — check this exact named set:
  - **6 daemons** (from CLAUDE.md startup checks): sniper-web, sniper-stream, sniper-analyzer, sniper-paper, sniper-alerter, sniper-sports-runner
  - **2 timers** (checked separately in the Timers section): sniper-world-analyst.timer, sniper-finance-analyst.timer
  - **Optional** (WARN if down, not FAIL): sniper-bot (Telegram relay)
  - Core 5 (web, stream, analyzer, paper, alerter) are FAIL-worthy if down; sports-runner and bot are WARN-worthy
- Recent errors (15 min window)
- Paper trade stats
- DB size and disk

### Stream freshness thresholds

- PASS: last trade ingested < 5 minutes ago
- WARN: 5-30 minutes ago
- FAIL: > 30 minutes ago

### Updated output format

```
VPS HEALTH — {timestamp}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Brain:         PASS / WARN / FAIL  {detail}
Core:          web {s}  stream {s}  analyzer {s}  paper {s}  alerter {s}
Support:       bot {s}  sports-runner {s}
Timers:        world_analyst {s} (next: {time})  finance_analyst {s} (next: {time})
Git:           {branch} @ {commit} {clean/dirty}
Errors (15m):  None / {count} errors
Stream:        Last trade {time ago}  PASS / WARN / FAIL
Paper trades:  {closed} closed, WR {pct}%, PnL ${amount}
Open trades:   {count}
Disk:          DB {size}, {free} free
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Recent analyst activity:
  World:    {last run time} — {status}
  Finance:  {last run time} — {status}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Verdict: HEALTHY / WARN / FAIL
Issues: {list if any}
Recommended: {next actions if WARN/FAIL}
```

### Verdict logic

- PASS/WARN/FAIL per check with exact command run and short interpretation
- Overall: FAIL if any section FAIL, WARN if any WARN, HEALTHY otherwise
- Recommended next actions only if WARN/FAIL

### Constraint

Manual invocation only. Not auto-invoked at session start.

#!/usr/bin/env python3
"""
Strategy Performance Reviewer — monitors plugin strategies for drift and bleeding.

Runs twice weekly (Wed + Sat 15:00 UTC) via run_strategy_agent.py.
Uses claude-sonnet-4-6 via the Claude Agent SDK.

Capabilities:
  - SQLite queries (read-only) to analyse strategy_positions + strategies
  - obs_tool.py (Supabase persistent memory)
  - predict_tool.py (self-improvement loop)
  - action_tool.py (pause safe action + recommend)
  - upsert_memory via Bash (brain section writes)

Owns brain section: strategies/
Domain: plugin strategies ONLY (NOT insider_copy — that is the Insider Scoring Analyst's domain)
"""

import anyio
from pathlib import Path

import sys as _sys, pathlib as _pathlib   # a bare `python3 agents/strategy_reviewer_agent.py` puts this file's
_R = str(_pathlib.Path(__file__).resolve().parents[1])  # own dir on sys.path,
if _R not in _sys.path:                    # never the repo root
    _sys.path.insert(0, _R)
from sniper.settings import models as _M
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    query,
)

# ──────────────────────────────────────────────────────────────────────────────
# System Prompt
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the Strategy Performance Reviewer — an autonomous monitoring agent that
evaluates plugin strategy health, catches parameter drift, detects bleeding, and recommends
pauses or calibration changes.

═══ YOUR ROLE ═══
You are the strategist's watchdog. Your job is to:
1. MONITOR all plugin strategies (NOT insider_copy — that is the Insider Scoring Analyst's domain).
2. COMPARE actual win rates against estimated_win_prob defaults (hardcoded in plugins).
3. DETECT consecutive loss streaks that warrant pausing a strategy.
4. ATTRIBUTE early exit PnL to identify strategies with poor exit timing.
5. RANK strategies by performance and identify best/worst performers.
6. PAUSE strategies that are actively bleeding (safe action via action_tool).
7. RECOMMEND parameter changes with evidence (you cannot modify DB params directly).

You run on a twice-weekly schedule. Your Supabase observations are your memory across runs.
Be specific, cite numbers, strategy IDs, and build on what previous-you wrote.

═══ TOOLS YOU USE ═══

1. DB QUERIES (read-only SQLite via Bash):
   sqlite3 -json sniper_tracker.db "SELECT ..."
   Always use -json flag for machine-readable output.
   NEVER run DELETE, UPDATE, INSERT SQL — read-only DB access only.

2. SAFE ACTIONS (via action_tool.py):
   # Pause a bleeding strategy (SAFE — you CAN do this)
   python3 agents/tools/action_tool.py pause \\
     --strategy-id 18 \\
     --reason "5 consecutive losses, total PnL -$87, WR=25% (4/16 closed)"

   # Recommend parameter change (human must apply)
   python3 agents/tools/action_tool.py recommend \\
     --strategy-id 18 --strategy sports_no_value_v1 \\
     --recommendation "Lower estimated_win_prob from 0.85 to 0.70" \\
     --reason "Actual WR=62% vs estimated 85%. 8 closed trades, PnL=-$22"

   # Flag for human review
   python3 agents/tools/action_tool.py flag \\
     --strategy-id 20 \\
     --reason "Cross-arb strategy has 0 wins in 6 closed positions"

   # Urgent alert (only for critical issues)
   python3 agents/tools/action_tool.py alert \\
     --severity warning \\
     --message "3 strategies simultaneously bleeding — total unrealized PnL: -$150"

   NEVER use: stop, create, activate — those are not your domain.
   You CAN pause (safe action). You CANNOT modify strategy DB params, create, or delete strategies.

3. SUPABASE MEMORY (via obs_tool.py):
   # Read past observations (YOUR memory across runs)
   python3 agents/tools/obs_tool.py read --strategy strategy_reviewer --limit 10
   python3 agents/tools/obs_tool.py read --strategy strategy_reviewer --type diagnosis --limit 5

   # Write new observation
   python3 agents/tools/obs_tool.py write \\
     --strategy strategy_reviewer \\
     --type diagnosis \\
     --confidence high \\
     --content "Your finding here..."

   Observation types: diagnosis | pattern | recommendation | action_taken | run_summary
   Confidence: low | medium | high

4. BRAIN SECTION WRITES (via upsert_memory):
   python3 -c "
   import sys, json; sys.path.insert(0, '.')
   from bot.supabase_helper import upsert_memory
   data = {<performance snapshot>}
   upsert_memory(type='knowledge', name='strategies/plugin_performance_snapshot',
     description='Per-plugin WR, PnL, and health status',
     content=json.dumps(data), source='strategy_reviewer')
   "

5. SELF-IMPROVEMENT (via predict_tool.py — USE EVERY RUN):
   # Auto-grade past predictions FIRST
   python3 agents/tools/predict_tool.py auto-grade --agent strategy_reviewer

   # Read your active lessons
   python3 agents/tools/predict_tool.py lessons --agent strategy_reviewer

   # Check accuracy stats
   python3 agents/tools/predict_tool.py stats --agent strategy_reviewer

   # Make verifiable predictions
   python3 agents/tools/predict_tool.py predict \\
     --agent strategy_reviewer \\
     --prediction "Sports NO Value will recover to >40% WR in next 10 trades" \\
     --condition "Check strategy_positions closed WR for strategy 18" \\
     --confidence low \\
     --verify-hours 336 \\
     --query "SELECT ROUND(100.0 * SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) FROM strategy_positions WHERE strategy_id='18' AND state='closed'"

   # Log this run
   python3 agents/tools/predict_tool.py log-run \\
     --agent strategy_reviewer --model claude-sonnet-4-6 --cost 0.50 --turns 40 --duration 180 \\
     --summary "Reviewed N plugins, paused N, flagged N, recommended N parameter changes"

═══ DATABASE SCHEMA (verified against live VPS) ═══

strategies: id, wallet, label, strategy_type, parameters (JSON text),
  status (active/paused/stopped), pause_reason, paused_at, created_at, updated_at

strategy_positions: position_id, wallet, condition_id, outcome (YES/NO),
  strategy_id, state (open/closed), entry_price, avg_entry_price,
  realized_pnl, pnl_usdc, current_price, size_usdc, entry_insider_score,
  opened_ts (unix epoch), closed_ts (unix epoch)

═══ PLUGIN WIN PROBABILITY DEFAULTS (hardcoded, NOT in DB params) ═══
  sports_no_value_v1:      estimated_win_prob = 0.85 (strategies/sports_no_value_v1.py:48)
  political_no_value_v1:   estimated_win_prob = 0.88 (strategies/political_no_value_v1.py:51)

These are the hardcoded defaults in the plugin source code. They are used for Kelly sizing.
If actual WR diverges by more than 15 percentage points, recommend recalibration.

═══ DECISION FRAMEWORK ═══

PAUSE CRITERIA (use action_tool pause — you have permission):
  - 5+ consecutive closed losses (most recent backward) for any strategy
  - Total closed PnL < -$50 with 5+ closed positions (confirmed bleeding)
  - Actual WR more than 20 percentage points below estimated_win_prob

FLAG CRITERIA (use action_tool flag — for human review):
  - Any strategy losing > $75 total (closed + open)
  - Estimated vs actual WR divergence > 15 percentage points
  - Strategy with 0 wins in 5+ closed positions

RECOMMEND CRITERIA (use action_tool recommend — parameter suggestions):
  - estimated_win_prob diverges from actual by > 15 points with 5+ closed trades
  - Position sizing appears too large based on drawdown pattern
  - Strategy parameters don't match current market conditions

STRATEGY OWNERSHIP (domain experts have primary responsibility):
  #21 Political NO Value → Geopolitics Analyst owns (you audit as second opinion)
  #28 Geo Intel → Geopolitics Analyst owns
  #29 Finance Intel → Finance Analyst owns
  All others (quant strategies) → you are primary monitor

When reviewing owned strategies (#21, #28, #29), note that the domain expert may have
already flagged or paused them. Before acting on these, check the expert's latest observation:
  python3 agents/tools/obs_tool.py read --strategy world_analyst --type run_summary --limit 1
  python3 agents/tools/obs_tool.py read --strategy finance_analyst --type run_summary --limit 1

For owned strategies, your role is AUDIT (second opinion), not primary monitor:
  - Still include them in your performance ranking and brain snapshots
  - Only pause/flag if the domain expert has NOT already acted AND the strategy meets pause criteria
  - Prefer "recommend" actions over "pause" for owned strategies — let the domain expert decide

IMPORTANT EXCLUSIONS:
  - NEVER analyze insider_copy_v1 strategies — those are the Insider Scoring Analyst's domain
  - NEVER modify strategy DB parameters directly
  - NEVER create or delete strategies
  - Require minimum 5 closed positions before recommending changes or pausing

═══ SAFETY RULES ═══
  - CAN pause strategies via action_tool (safe action for bleeding strategies)
  - CANNOT modify strategy DB params, create, or delete strategies
  - CANNOT modify .env or plugin source code
  - Require minimum 5 closed positions before taking any action
  - NEVER run DELETE, UPDATE, INSERT SQL — only SELECT

═══ BRAIN MAP ═══
You are part of a multi-agent brain. Each agent owns a section.

  SECTION          OWNER                    YOUR ACCESS
  world_state      World Analyst            READ
  portfolio        System (auto)            READ
  wallets          Wallet Scout             READ
  signals          Insider Scoring Analyst   READ
  strategies       Strategy Reviewer         WRITE — you own this section
  adversarial      Adv. Reviewer            READ — adversarial/run_stats, adversarial/rejection_patterns
  predictions      All agents               WRITE own agent_type only
  health           Health Checker            READ
  knowledge        Static                   READ-ONLY
  meta             Human                    READ-ONLY

RULES:
  - Only WRITE to strategies/* memories and your own observations/predictions
  - READ other sections for awareness, but don't act on stale data (check timestamps)
  - If you notice something wrong in another agent's section, FLAG it — don't fix it

CROSS-READS (do these in Step 1):
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('brain_health_latest',type_filter='knowledge'); print(m['content'][:500] if m else 'no health data')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('strategies/plugin_performance_snapshot',type_filter='knowledge'); print(m['content'][:1000] if m else 'first run')"
  python3 agents/tools/obs_tool.py read --strategy insider_analyst --type diagnosis --limit 1

═══ CRITICAL RULES ═══
  - Each observation must contain SPECIFIC numbers, strategy IDs, PnL, and win rates
  - Never claim to have verified something you didn't actually check
  - Build on previous observations — reference past findings and track trends
  - When pausing, always explain WHY and what criteria were met
"""

# ──────────────────────────────────────────────────────────────────────────────
# Run Prompt Builder
# ──────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone


def build_run_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""Today is {today}. Run your scheduled strategy performance review.

═══ WORKFLOW (follow in order) ═══

STEP 0 — Self-improvement check (ALWAYS DO THIS FIRST):
  a) Auto-grade past predictions:
     python3 agents/tools/predict_tool.py auto-grade --agent strategy_reviewer
  b) Read your lessons:
     python3 agents/tools/predict_tool.py lessons --agent strategy_reviewer
  c) Check accuracy stats:
     python3 agents/tools/predict_tool.py stats --agent strategy_reviewer

STEP 1 — Load context + brain awareness:
  # Your observations
  python3 agents/tools/obs_tool.py read --strategy strategy_reviewer --limit 10
  Read all recent observations. Note past pauses, recommendations, performance trends.

  # Brain awareness (cross-reads)
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('brain_health_latest',type_filter='knowledge'); print(m['content'][:500] if m else 'no health data')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('strategies/plugin_performance_snapshot',type_filter='knowledge'); print(m['content'][:1000] if m else 'first run')"
  python3 agents/tools/obs_tool.py read --strategy insider_analyst --type diagnosis --limit 1

  # Domain expert observations (check before acting on owned strategies #21, #28, #29)
  python3 agents/tools/obs_tool.py read --strategy world_analyst --type run_summary --limit 1
  python3 agents/tools/obs_tool.py read --strategy finance_analyst --type run_summary --limit 1

STEP 2 — Per-plugin performance:
  sqlite3 -json sniper_tracker.db "SELECT s.id, s.label, s.strategy_type, s.status, s.parameters,
    COUNT(CASE WHEN sp.state='closed' THEN 1 END) as closed,
    SUM(CASE WHEN sp.state='closed' AND sp.pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
    SUM(CASE WHEN sp.state='closed' AND sp.pnl_usdc <= 0 THEN 1 ELSE 0 END) as losses,
    ROUND(SUM(CASE WHEN sp.state='closed' THEN sp.pnl_usdc ELSE 0 END), 2) as closed_pnl,
    COUNT(CASE WHEN sp.state='open' THEN 1 END) as open_ct,
    ROUND(SUM(CASE WHEN sp.state='open' THEN sp.pnl_usdc ELSE 0 END), 2) as open_pnl,
    ROUND(SUM(sp.pnl_usdc), 2) as total_pnl
  FROM strategies s
  LEFT JOIN strategy_positions sp ON CAST(s.id AS TEXT) = sp.strategy_id
  WHERE s.strategy_type NOT IN ('insider_copy_v1')
  GROUP BY s.id ORDER BY s.status, closed_pnl"

  For each strategy with >= 5 closed positions, calculate actual WR:
    actual_wr = wins / closed

STEP 3 — Win probability calibration:
  For each plugin with a hardcoded estimated_win_prob, compare to actual:
    - sports_no_value_v1: default=0.85, check actual WR
    - political_no_value_v1: default=0.88, check actual WR

  If divergence > 15 percentage points, flag for recommendation in Step 7.

  sqlite3 -json sniper_tracker.db "SELECT s.strategy_type,
    COUNT(CASE WHEN sp.state='closed' THEN 1 END) as closed,
    SUM(CASE WHEN sp.state='closed' AND sp.pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
    ROUND(100.0 * SUM(CASE WHEN sp.state='closed' AND sp.pnl_usdc > 0 THEN 1 ELSE 0 END) / NULLIF(COUNT(CASE WHEN sp.state='closed' THEN 1 END), 0), 1) as actual_wr
  FROM strategies s
  LEFT JOIN strategy_positions sp ON CAST(s.id AS TEXT) = sp.strategy_id
  WHERE s.strategy_type IN ('sports_no_value_v1','political_no_value_v1')
  GROUP BY s.strategy_type"

STEP 4 — Consecutive loss detection:
  For each active strategy, get closed positions ordered by closed_ts DESC:
  sqlite3 -json sniper_tracker.db "SELECT sp.strategy_id, s.label, s.status,
    sp.pnl_usdc, sp.closed_ts
  FROM strategy_positions sp
  JOIN strategies s ON CAST(s.id AS TEXT) = sp.strategy_id
  WHERE sp.state='closed' AND s.strategy_type NOT IN ('insider_copy_v1')
  ORDER BY sp.strategy_id, sp.closed_ts DESC"

  For each strategy, count consecutive losses from the most recent trade backward.
  Any strategy with 5+ consecutive losses is a PAUSE candidate (Step 7).

STEP 5 — Early exit attribution:
  sqlite3 -json sniper_tracker.db "SELECT s.id, s.label,
    COUNT(*) as closed_ct,
    SUM(CASE WHEN sp.realized_pnl < 0 THEN 1 ELSE 0 END) as negative_realized,
    ROUND(SUM(sp.realized_pnl), 2) as total_realized_pnl,
    ROUND(AVG(sp.realized_pnl), 2) as avg_realized_pnl
  FROM strategy_positions sp
  JOIN strategies s ON CAST(s.id AS TEXT) = sp.strategy_id
  WHERE sp.state='closed' AND s.strategy_type NOT IN ('insider_copy_v1')
  GROUP BY s.id
  HAVING closed_ct >= 3"

  Strategies with large negative realized_pnl may have exit timing issues.

STEP 6 — Cross-strategy ranking:
  From Step 2 data, rank all plugins by:
  1. Total PnL (closed + open)
  2. Win rate (closed only, minimum 5 trades)
  3. PnL per trade (total PnL / total positions)
  Identify the best and worst performers clearly.

STEP 7 — Recommend actions:
  Based on Steps 2-6, take actions:

  # Pause if 5+ consecutive losses AND 5+ closed total
  python3 agents/tools/action_tool.py pause \\
    --strategy-id <ID> \\
    --reason "<consecutive losses count>, total PnL=$<X>, WR=<Y>%"

  # Recommend win_prob update if divergence > 15pts
  python3 agents/tools/action_tool.py recommend \\
    --strategy-id <ID> --strategy <strategy_type> \\
    --recommendation "Update estimated_win_prob from X to Y" \\
    --reason "Actual WR=Z% vs estimated X%. N closed trades."

  # Flag strategies losing > $75 total
  python3 agents/tools/action_tool.py flag \\
    --strategy-id <ID> \\
    --reason "Total PnL: $<X> with <N> positions. Needs human review."

STEP 8 — Update brain section:
  Write strategies/plugin_performance_snapshot with per-plugin data:
  python3 -c "
  import sys, json; sys.path.insert(0, '.')
  from bot.supabase_helper import upsert_memory
  data = {{<per-plugin performance from Step 2>}}
  upsert_memory(type='knowledge', name='strategies/plugin_performance_snapshot',
    description='Per-plugin WR, PnL, and health status',
    content=json.dumps(data), source='strategy_reviewer')
  "

  Write strategies/winprob_calibration with calibration data:
  python3 -c "
  import sys, json; sys.path.insert(0, '.')
  from bot.supabase_helper import upsert_memory
  data = {{<win prob calibration from Step 3>}}
  upsert_memory(type='knowledge', name='strategies/winprob_calibration',
    description='Plugin estimated_win_prob vs actual WR calibration',
    content=json.dumps(data), source='strategy_reviewer')
  "

  If any strategies are actively losing, write strategies/bleeding_watchlist:
  python3 -c "
  import sys, json; sys.path.insert(0, '.')
  from bot.supabase_helper import upsert_memory
  data = {{<bleeding strategy list>}}
  upsert_memory(type='knowledge', name='strategies/bleeding_watchlist',
    description='Strategies currently losing money that need monitoring',
    content=json.dumps(data), source='strategy_reviewer')
  "

STEP 8b — Write discovered patterns to brain:
  If you discover a repeatable pattern across strategies (e.g. a plugin type that consistently
  underperforms, a parameter setting that correlates with losses, or a market category
  where a strategy bleeds), write it to the brain's knowledge section:
    python3 -c "from bot.supabase_helper import upsert_memory; upsert_memory('knowledge', 'patterns/strategy-TOPIC', 'DESCRIPTION', 'CONTENT with stats, dates, sample size', source='vps')"
  Example:
    python3 -c "from bot.supabase_helper import upsert_memory; upsert_memory('knowledge', 'patterns/strategy-sports-no-value-overestimated-wp', 'Sports NO Value estimated_win_prob is too high', 'Pattern: estimated_win_prob=0.85 but actual WR=62%% across 16 closed trades. Kelly sizing is too aggressive. Recommend lowering to 0.65. Discovered 2026-03-22.', source='vps')"
  Only write genuinely new patterns — not things you already wrote in previous runs.

STEP 9 — Write observations + predictions:
  python3 agents/tools/obs_tool.py write \\
    --strategy strategy_reviewer --type diagnosis --confidence high \\
    --content "Run {{date}}: {{N}} plugin strategies reviewed. \\
    Best: {{label}} ({{WR}}% WR, ${{PnL}}). Worst: {{label}} ({{WR}}% WR, ${{PnL}}). \\
    {{N}} strategies paused, {{N}} flagged, {{N}} recommendations made."

  If actions taken:
  python3 agents/tools/obs_tool.py write \\
    --strategy strategy_reviewer --type action_taken --confidence high \\
    --content "Paused strategy #{{id}} ({{label}}): {{reason}}"

  Always write a run_summary:
  python3 agents/tools/obs_tool.py write \\
    --strategy strategy_reviewer --type run_summary --confidence high \\
    --content "Strategy review run {{date}}: Reviewed {{N}} plugin strategies. \\
    Performance: {{best}} to {{worst}}. Win prob calibration: {{summary}}. \\
    Actions: {{paused N, flagged N, recommended N}}. Key concern: {{or 'none'}}."

  Make exactly 1 high-quality verifiable prediction.
  CRITICAL: Always include --query so the prediction can be auto-graded.
  Without --query, the prediction can NEVER be auto-graded. Every ungraded prediction is wasted learning.
  Set --verify-hours to 48-72h, not 168h. Shorter cycles = faster learning.

  python3 agents/tools/predict_tool.py predict \\
    --agent strategy_reviewer \\
    --prediction "<prediction about strategy performance>" \\
    --condition "<what to check>" \\
    --confidence medium \\
    --verify-hours 72 \\
    --query "<SQL to auto-verify>"

STEP 10 — Log run + conclude:
  python3 agents/tools/predict_tool.py log-run \\
    --agent strategy_reviewer --model claude-sonnet-4-6 --cost <estimate> \\
    --turns <count> --duration <seconds> \\
    --summary "Reviewed {{N}} plugins, paused {{N}}, flagged {{N}}, recommended {{N}} changes"

  Write a 2-3 sentence natural language summary of this run's key findings.
  If actions were taken, confirm what changed."""


# ──────────────────────────────────────────────────────────────────────────────
# Agent Runner
# ──────────────────────────────────────────────────────────────────────────────

async def run(custom_prompt: str | None = None, verbose: bool = False) -> str:
    """Run the Strategy Performance Reviewer agent and return the final result."""
    cwd = str(Path(__file__).resolve().parent.parent)
    prompt = custom_prompt or build_run_prompt()

    result_text = ""
    session_id = None

    async for message in query(
        prompt=prompt,
        options=ClaudeAgentOptions(
            model=_M.OPUS,  # twice-weekly cadence (Wed+Sat) — reasoning-heavy calibration worth the Opus quota
            cwd=cwd,
            allowed_tools=["Bash", "Read"],
            permission_mode="bypassPermissions",
            system_prompt=SYSTEM_PROMPT,
            max_turns=60,
            setting_sources=[],
        ),
    ):
        if isinstance(message, SystemMessage) and message.subtype == "init":
            session_id = message.data.get("session_id") if isinstance(message.data, dict) else None
            if verbose:
                print(f"[strategy_reviewer] Session: {session_id}")

        elif isinstance(message, ResultMessage):
            result_text = message.result
            session_id = message.session_id
            if verbose:
                print(f"[strategy_reviewer] Stop reason: {message.stop_reason}, cost: ${message.total_cost_usd or 0:.4f}")

    return result_text


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run Strategy Performance Reviewer agent")
    parser.add_argument("--prompt", help="Custom prompt (overrides default run prompt)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = anyio.run(run, args.prompt, args.verbose)
    print(result)

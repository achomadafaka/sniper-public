#!/usr/bin/env python3
"""
InsiderCopy Strategy Agent — autonomous analyst for insider_copy_v1 instances.

Runs on a cron schedule via run_strategy_agent.py.
Uses claude-sonnet-4-6 via the Claude Agent SDK (no separate API key required).

Capabilities:
  - WebSearch + WebFetch for live internet research
  - Bash access to query the live SQLite DB
  - Bash to call obs_tool.py (Supabase persistent memory)
  - Bash to call action_tool.py (safe strategy actions)
  - Full analysis of positions, wallets, scores, trade patterns
"""

import anyio
import json
import subprocess
import sys
from pathlib import Path

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ResultMessage,
    SystemMessage,
    query,
)

# ──────────────────────────────────────────────────────────────────────────────
# System Prompt
# ──────────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the InsiderCopy Position Quality Analyst — a specialized autonomous analyst
for the insider_copy_v1 trading strategy on a Polymarket prediction market sniper system.

═══ YOUR ROLE ═══
You are the position quality analyst. You focus on what the Wallet Scout does NOT do:
  - Position-level PnL pattern analysis (wins vs losses by entry score)
  - Exit reason analysis (which exit reasons correlate with losses)
  - Open position health monitoring (drift, aging, underwater positions)
  - Thesis maintenance (what is the core edge, is it holding)
  - Bleeding detection (48h PnL drawdown)
  - Parameter tuning recommendations

The Wallet Scout handles wallet discovery, vetting, and strategy lifecycle (create/stop/activate).
You focus on position-level quality and parameter tuning.

You run on a scheduled basis. Your Supabase observations are your memory
across runs. Be specific, cite numbers, and build on what previous-you wrote.

═══ TOOLS YOU USE ═══

1. DB QUERIES (read-only SQLite via Bash):
   sqlite3 -json sniper_tracker.db "SELECT ..."
   Always use -json flag for machine-readable output.

2. SELF-IMPROVEMENT (via predict_tool.py — USE THIS EVERY RUN):
   # Auto-grade past predictions FIRST (before any new analysis)
   python3 agents/tools/predict_tool.py auto-grade --agent insider_copy
   # This runs verification queries and grades automatically. Review any skipped ones:
   python3 agents/tools/predict_tool.py mark --id <UUID> --correct true --notes "Score held at 74"

   # Read your active lessons (hard-won insights from past mistakes)
   python3 agents/tools/predict_tool.py lessons --agent insider_copy

   # Check your accuracy stats (are you well-calibrated?)
   python3 agents/tools/predict_tool.py stats --agent insider_copy

   # Make verifiable predictions during analysis
   # Use the literal run UUID shown in this run's prompt; do not use '$RUN_ID' verbatim.
   python3 agents/tools/predict_tool.py predict \
     --agent insider_copy \
     --run-id "<literal run UUID from this run's prompt>" \
     --calibration-note '{"schema_version":1,"calibration":{"saw_red_flags":[],"verification_query_provided":true,"confidence_rationale":"baseline medium-confidence call with a direct SQL verification query"}}' \
     --prediction "Wallet 0xde9f will maintain insider_score >= 70 for 7 days" \
     --condition "Check user_analysis.insider_score for wallet 0xde9f" \
     --confidence medium \
     --verify-hours 168 \
     --query "SELECT insider_score FROM user_analysis WHERE wallet LIKE '0xde9f%'"

   # Record a lesson when you discover a pattern in your accuracy
   python3 agents/tools/predict_tool.py learn \
     --agent insider_copy \
     --lesson "Sports market wallets are more predictable than politics — my accuracy is 72% vs 41%" \
     --evidence "Graded 30 predictions: 18/25 sports correct, 7/17 politics correct" \
     --accuracy 59.5 --graded 42

   # Run lifecycle is wrapper-owned. Do NOT call predict_tool.py log-run here;
   # the wrapper records finish-run automatically after the SDK session ends.

3. SUPABASE MEMORY (via obs_tool.py):
   # Read past observations
   python3 agents/tools/obs_tool.py read --strategy insider_copy_v1 --limit 15
   python3 agents/tools/obs_tool.py read --strategy insider_copy_v1 --type thesis --limit 3

   # Write new observation
   python3 agents/tools/obs_tool.py write \
     --strategy insider_copy_v1 \
     --type diagnosis \
     --confidence high \
     --content "Your finding here..."

   Observation types: diagnosis | thesis | pattern | recommendation | action_taken | run_summary
   Confidence: low | medium | high

3. SAFE ACTIONS (via action_tool.py):
   # Pause a bleeding strategy (ONLY if >20% drawdown AND wallet scores declining)
   python3 agents/tools/action_tool.py pause \
     --strategy-id 5 \
     --reason "Lost 22% in 48h, wallet score 90→63→41 over 3 runs"

   # Flag for human review
   python3 agents/tools/action_tool.py flag \
     --strategy-id 7 \
     --reason "Wallet 0xABC has been inactive 14 days"

   # Record param recommendation (human must apply — you never change params directly)
   python3 agents/tools/action_tool.py recommend \
     --strategy-id 5 \
     --strategy insider_copy_v1 \
     --recommendation "Raise min_insider_score to 70 — entries <70 have 31% win rate vs 67% for ≥70" \
     --reason "Pattern found in 30-day closed position analysis"

   # Urgent alert (use sparingly)
   python3 agents/tools/action_tool.py alert \
     --severity critical \
     --message "Wallet 0xABC mass-exited all positions — check cascade risk"

4. INTERNET RESEARCH (built-in):
   WebFetch — fetch any URL directly
   WebSearch — search the web

   Use for market research (understanding what a market is about), NOT for wallet profiling.
   The Wallet Scout handles wallet research via Polymarket API.
   • Market info:      https://gamma-api.polymarket.com/markets?conditionIds={conditionId}

═══ DATABASE SCHEMA ═══

strategies: id, name, strategy_type, status (active/paused/stopped), params (JSON),
            pause_reason, paused_at, updated_at
  → params fields: min_insider_score, max_entry_price, position_size_usdc, wallet

strategy_positions: id, strategy_id, condition_id, outcome (YES/NO), state (open/closed),
                    entry_price, current_price, size_usdc, pnl_usdc, entry_insider_score,
                    opened_ts (unix epoch), closed_ts (unix epoch),
                    exit_reason (resolution_win|resolution_loss|stop_loss|score_decay|
                                 signal_is_open_0|staleness|cascade)

user_analysis: wallet, insider_score (0–100), speculator_score, win_rate,
               trade_count, closed_sample_size, is_bot, lb_pnl, lb_rank, lb_username,
               classification, avg_roi, analyzed_at

score_history: wallet, insider_score, analyzed_ts, classification

position_signals: wallet, condition_id, outcome, current_price, is_open,
                  insider_score, captured_at, days_to_resolution, position_classification

═══ DECISION FRAMEWORK ═══

IMMEDIATE ACTION (pause_strategy):
  ✓ Strategy lost >20% in last 48h AND wallet insider_score is declining (3 consecutive drops)
  ✓ Cascade pattern: >5 consecutive stop-losses on same strategy in 24h

FLAG FOR REVIEW:
  ✓ Wallet has been inactive >10 days (no new signals)
  ✓ Wallet's score dropped >20pts since we started copying
  ✓ >40% of exits are stop_loss (suggests bad entry quality)
  ✓ Open position drifting >30% against entry price for >7 days

RECORD RECOMMENDATION (human applies):
  ✓ min_insider_score should be adjusted (evidence from win/loss analysis)
  ✓ A market category seems consistently unprofitable
  ✓ Exit parameters need tuning (stop_loss too tight/loose based on exit analysis)

DO NOTHING (monitor only):
  ✓ Normal variance in a small sample
  ✓ Strategy paused with pause_reason='wallet_degradation' (auto-resume handles it)

═══ OBSERVATION WRITING GUIDE ═══

thesis (update when your core view changes):
  "As of {date}: Core edge = [what we believe makes insider copy profitable].
   Evidence supporting: [specific stats]. Active risks: [concerns].
   Current confidence: [low/medium/high]. Last update trigger: [what changed]."

diagnosis (per-run health check):
  "Run {date}: {N} active instances, {N} paused. Open positions: {N} ({$X} exposure).
   Last 7d: {N} closed, {wins}W/{losses}L ({win_rate}%), PnL ${X}.
   Notable: [anything specific]."

pattern (when you discover something repeatable):
  "Pattern found ({date}): [description]. Sample: {N} trades. Evidence: [stats].
   Implication: [what to do about it]."

═══ CALIBRATION ENVELOPE (required with every prediction this run) ═══

At the top of this run's prompt you saw a CALIBRATION STATE block. Every prediction
you make must be accompanied by a --calibration-note flag carrying this JSON envelope:

  {
    "schema_version": 1,
    "calibration": {
      "saw_red_flags": [<list of red-flag names from the block, or empty []>],
      "confidence_rationale": "<required if saw_red_flags non-empty OR confidence=high>",
      "differs_from_prior": "<optional: what makes this call different from recent misses>",
      "verification_query_provided": <true|false>
    }
  }

Pass it as a single-quoted argument to predict_tool predict:
  python3 agents/tools/predict_tool.py predict \
    --agent insider_copy \
    --run-id $RUN_ID \
    --calibration-note '{"schema_version":1,"calibration":{...}}' \
    --prediction "..." \
    --condition "..." \
    --confidence medium \
    --verify-hours 48 \
    --query "SELECT ..."

The wrapper does NOT export an environment variable. Instead, it inlines the
literal run UUID into this run's user prompt examples. Copy that UUID when you
invoke predict_tool; never pass the literal string "$RUN_ID".

If red flags are present in the block you saw, you MUST either:
  (a) downgrade confidence from high to medium/low, AND explain why in confidence_rationale; or
  (b) keep confidence=high ONLY if differs_from_prior convincingly argues this query targets
      different data than the ones that historically failed.

═══ CRITICAL RULES ═══
  - NEVER modify params directly — only record recommendations
  - NEVER resume manually-paused strategies (pause_reason='manual') — human only
  - NEVER run DELETE, UPDATE, INSERT SQL — read-only DB access
  - NEVER recommend new wallets — the Wallet Scout handles wallet discovery
  - Each observation must contain SPECIFIC numbers and wallet addresses
  - Focus on position patterns, exit analysis, and parameter tuning — not wallet scouting

═══ BRAIN MAP ═══
You are part of a multi-agent system. Each agent owns a section of the shared brain (Supabase).

  SECTION          OWNER             YOU
  world_state/*    World Analyst      READ
  portfolio/*      System (auto)      READ — convergence:latest-scan, conflict:latest-scan, Strategy Performance
  wallets/*        Wallet Scout       READ
  signals/*        Insider Copy       WRITE — you own this section
  adversarial/*    Adv. Reviewer      READ — adversarial/run_stats, adversarial/rejection_patterns
  predictions/*    All agents         WRITE (own agent_type only)
  health/*         Health Checker     READ — brain_health_latest
  knowledge/*      Static (seeded)    READ-ONLY
  meta/*           Human              READ-ONLY

RULES:
  - Only WRITE to sections you own
  - READ other sections for awareness, but don't act on stale data (check timestamps)
  - If you notice something wrong in another agent's section, FLAG it — don't fix it

CROSS-READS (do these in Step 1):
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('brain_health_latest',type_filter='knowledge'); print(m['content'][:500] if m else 'no health data')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('convergence:latest-scan',type_filter='project'); print(m['content'][:500] if m else 'no convergence data')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('conflict:latest-scan',type_filter='project'); print(m['content'][:500] if m else 'no conflict data')"
  python3 agents/tools/obs_tool.py read --strategy wallet_scout --type run_summary --limit 1
"""

# ──────────────────────────────────────────────────────────────────────────────
# Run Prompt Builder
# ──────────────────────────────────────────────────────────────────────────────

from datetime import datetime, timezone

def build_run_prompt() -> str:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""Today is {today}. Run your scheduled insider_copy_v1 strategy analysis.

═══ WORKFLOW (follow in order) ═══

STEP 0 — Self-improvement check (ALWAYS DO THIS FIRST):
  a) Auto-grade past predictions:
     python3 agents/tools/predict_tool.py auto-grade --agent insider_copy
     This auto-grades predictions with verification queries. Review any skipped ones manually.
  b) Read your lessons:
     python3 agents/tools/predict_tool.py lessons --agent insider_copy
     These are hard-won insights from your past mistakes. Apply them in this run.
  c) Check accuracy stats:
     python3 agents/tools/predict_tool.py stats --agent insider_copy
     If accuracy < 50%, you need to recalibrate. If high-confidence predictions
     are less accurate than medium, your confidence calibration is off.

STEP 1 — Load memory + brain awareness:
  # Your observations
  Run: python3 agents/tools/obs_tool.py read --strategy insider_copy_v1 --limit 15
  Read all recent observations. Note the last thesis and any open recommendations.

  # Brain awareness (cross-reads)
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('brain_health_latest',type_filter='knowledge'); print(m['content'][:500] if m else 'no health data')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('convergence:latest-scan',type_filter='project'); print(m['content'][:500] if m else 'no convergence data')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('conflict:latest-scan',type_filter='project'); print(m['content'][:500] if m else 'no conflict data')"
  python3 agents/tools/obs_tool.py read --strategy wallet_scout --type run_summary --limit 1
  Note any health warnings, convergence signals, conflicts, or wallet roster changes.

STEP 2 — Strategy health snapshot:
  a) List all insider_copy_v1 strategies:
     sqlite3 -json sniper_tracker.db "SELECT id, name, status, params, pause_reason, paused_at, updated_at FROM strategies WHERE strategy_type='insider_copy_v1' ORDER BY id"

  b) For each strategy, get the tracked wallet's CURRENT score (no trend deep-dive — Scout does that):
     sqlite3 -json sniper_tracker.db "SELECT wallet, insider_score, speculator_score, win_rate, trade_count, closed_sample_size, lb_pnl, lb_username, analyzed_at FROM user_analysis WHERE wallet IN (...)"

  c) Recent performance (last 7 days):
     sqlite3 -json sniper_tracker.db "SELECT sp.id, sp.strategy_id, sp.condition_id, sp.outcome, sp.state, sp.entry_price, sp.current_price, sp.size_usdc, sp.pnl_usdc, sp.entry_insider_score, sp.exit_reason, sp.opened_ts, sp.closed_ts, s.name as strategy_name FROM strategy_positions sp JOIN strategies s ON s.id=sp.strategy_id WHERE s.strategy_type='insider_copy_v1' AND sp.opened_ts > strftime('%s','now','-7 days') ORDER BY sp.opened_ts DESC LIMIT 60"

  d) Exit reason breakdown (last 30 days):
     sqlite3 -json sniper_tracker.db "SELECT exit_reason, COUNT(*) as count, ROUND(AVG(pnl_usdc),2) as avg_pnl, ROUND(SUM(pnl_usdc),2) as total_pnl FROM strategy_positions sp JOIN strategies s ON s.id=sp.strategy_id WHERE s.strategy_type='insider_copy_v1' AND sp.state='closed' AND sp.closed_ts > strftime('%s','now','-30 days') GROUP BY exit_reason ORDER BY count DESC"

STEP 3 — Position quality analysis:
  a) Compare entry_insider_score for wins vs losses (core edge analysis):
     sqlite3 -json sniper_tracker.db "SELECT CASE WHEN pnl_usdc > 0 THEN 'win' ELSE 'loss' END as result, ROUND(AVG(entry_insider_score),1) as avg_score, COUNT(*) as count, ROUND(SUM(pnl_usdc),2) as total_pnl FROM strategy_positions sp JOIN strategies s ON s.id=sp.strategy_id WHERE s.strategy_type='insider_copy_v1' AND sp.state='closed' GROUP BY result"
  b) Exit reason vs PnL correlation (which exits lose money?):
     sqlite3 -json sniper_tracker.db "SELECT exit_reason, COUNT(*) as count, ROUND(AVG(pnl_usdc),2) as avg_pnl, SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins, SUM(CASE WHEN pnl_usdc <= 0 THEN 1 ELSE 0 END) as losses FROM strategy_positions sp JOIN strategies s ON s.id=sp.strategy_id WHERE s.strategy_type='insider_copy_v1' AND sp.state='closed' GROUP BY exit_reason ORDER BY count DESC"
  c) Entry score band analysis (where is the edge?):
     sqlite3 -json sniper_tracker.db "SELECT CASE WHEN entry_insider_score >= 90 THEN '90-100' WHEN entry_insider_score >= 80 THEN '80-89' WHEN entry_insider_score >= 70 THEN '70-79' ELSE 'below-70' END as score_band, COUNT(*) as count, SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins, ROUND(SUM(pnl_usdc),2) as total_pnl FROM strategy_positions sp JOIN strategies s ON s.id=sp.strategy_id WHERE s.strategy_type='insider_copy_v1' AND sp.state='closed' GROUP BY score_band ORDER BY score_band DESC"

STEP 4 — Open position health:
  a) Check all open positions — drift, aging, underwater:
     sqlite3 -json sniper_tracker.db "SELECT sp.id, sp.condition_id, sp.outcome, sp.entry_price, sp.current_price, sp.size_usdc, sp.pnl_usdc, sp.entry_insider_score, sp.opened_ts, s.name, ROUND((julianday('now') - julianday(datetime(sp.opened_ts,'unixepoch'))),1) as age_days FROM strategy_positions sp JOIN strategies s ON s.id=sp.strategy_id WHERE s.strategy_type='insider_copy_v1' AND sp.state='open' ORDER BY sp.pnl_usdc ASC"
  b) Flag positions drifting >30% against entry for >7 days
  c) Flag positions open >14 days with negative PnL (aging losers)

STEP 5 — Bleeding detection + action triggers:
  For each active strategy — is it bleeding? (calculate: sum pnl_usdc from last 48h, check if <-20% of position_size_usdc × open_count)
  For each tracked wallet — quick score check (is current insider_score still above threshold?)

STEP 6 — Apply actions (if warranted):
  - Pause a strategy only if BOTH conditions met: >20% drawdown AND score declining
  - Record recommendations for anything requiring param changes
  - Flag any wallets that look suspicious or have gone dark

STEP 7 — Write observations:
  Minimum this run:
  • One 'diagnosis' observation (current state summary with numbers)
  • One 'thesis' observation IF your view has changed (otherwise skip)
  • One 'pattern' if you found something repeatable in position/exit data
  • One 'run_summary' at the end

STEP 7b — Write discovered patterns to brain:
  If you discover a repeatable pattern (e.g. a market category that consistently loses,
  a score band that outperforms, or an exit reason that correlates with losses), write it
  to the brain's knowledge section so other agents and future runs can benefit:
    python3 -c "from bot.supabase_helper import upsert_memory; upsert_memory('knowledge', 'patterns/insider-copy-TOPIC', 'DESCRIPTION', 'CONTENT with stats, dates, sample size', source='vps')"
  Example:
    python3 -c "from bot.supabase_helper import upsert_memory; upsert_memory('knowledge', 'patterns/sports-tennis-insider-losses', 'Sports tennis markets: high insider score does not predict wins', 'Pattern: insider_score=100 wallets on tennis markets have 0%% win rate across 8 trades. Tennis market pricing may already be efficient. Discovered 2026-03-20.', source='vps')"
  Only write genuinely new patterns — not things you already wrote in previous runs.

STEP 8 — Make predictions (exactly 1 high-quality prediction):
  Make exactly 1 high-quality verifiable prediction about what will happen in the next 48-72 hours.

  REQUIRED FLAGS for this run (calibration loop v1 is active for insider_copy):
    --run-id "$RUN_ID"                         (from wrapper env; empty means wrapper is broken → skip predict)
    --calibration-note '{{"schema_version":1,"calibration":{{...}}}}'   (spec §6.3 envelope)
    --query "SELECT ..."                       (auto-gradable; without this the prediction is wasted)

  Red-flag handling:
    If CALIBRATION STATE block showed any red flags, your calibration.saw_red_flags must list them
    AND you must EITHER downgrade confidence OR provide differs_from_prior explaining why this
    prediction is structurally different from the failing ones.

  Good predictions are SPECIFIC, VERIFIABLE, and have --query:
    ✓ "Wallet 0xde9f insider_score will stay above 70 for 3 days" (verify via SQL)
    ✓ "Strategy #7 will have positive PnL in next 48h" (verify via strategy_positions)
    ✗ "Markets will be volatile" (too vague, not verifiable)
    ✗ Any prediction without --query (permanently ungradeable)

  Example:
    python3 agents/tools/predict_tool.py predict \
      --agent insider_copy \
      --run-id "$RUN_ID" \
      --calibration-note '{{"schema_version":1,"calibration":{{"saw_red_flags":[],"verification_query_provided":true}}}}' \
      --prediction "Wallet 0xde9f insider_score stays >= 70 for 3 days" \
      --condition "Check user_analysis.insider_score for wallet 0xde9f" \
      --confidence medium \
      --verify-hours 72 \
      --query "SELECT insider_score FROM user_analysis WHERE wallet LIKE '0xde9f%'"

STEP 9 — Extract lessons (if accuracy data available):
  If you graded 5+ predictions in Step 0 and found patterns in what you got wrong:
  python3 agents/tools/predict_tool.py learn \
    --agent insider_copy \
    --lesson "What I learned" \
    --evidence "X/Y correct in category A vs Z/W in category B" \
    --accuracy <your_overall_accuracy> --graded <total_graded>

STEP 10 — Conclude:
  Do NOT call python3 agents/tools/predict_tool.py log-run.
  The wrapper finalizes the run automatically via finish-run after your session ends.
  Write a 2-3 sentence natural language summary of this run's key findings.
  If critical issues found, confirm action was taken.
  If no action needed, confirm system is healthy."""

# ──────────────────────────────────────────────────────────────────────────────
# Agent Runner
# ──────────────────────────────────────────────────────────────────────────────

def _run_predict_tool(args: list[str]) -> dict:
    """Invoke predict_tool.py as a subprocess and parse its JSON stdout.

    Any non-zero exit propagates as RuntimeError. The caller handles
    user-visible messaging (we're writing to agent audit trail, not Telegram).
    """
    root = Path(__file__).resolve().parent.parent
    cmd = [sys.executable, str(root / "agents" / "tools" / "predict_tool.py"), *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=str(root))
    if proc.returncode != 0:
        raise RuntimeError(f"predict_tool {args[0] if args else ''} failed: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"predict_tool stdout not JSON: {proc.stdout[:500]}") from exc


async def run(custom_prompt: str | None = None, verbose: bool = False) -> str:
    """Run the InsiderCopy agent with calibration loop wiring.

    Lifecycle:
      1. start-run (gets run_id + calibration block)
      2. prepend calibration block to the user prompt
      3. query() the SDK
      4. finish-run (status=success or status=error on exception)
    """
    cwd = str(Path(__file__).resolve().parent.parent)

    # Step 1: open run
    start = _run_predict_tool(["start-run", "--agent", "insider_copy",
                               "--model", "claude-sonnet-4-6"])
    run_id = start["run_id"]
    calibration_block = start["calibration_block"]

    base_prompt = custom_prompt or build_run_prompt()
    # Inline RUN_ID directly into the prompt so Bash steps can reference it literally.
    prompt_with_run_id = base_prompt.replace('"$RUN_ID"', f'"{run_id}"').replace("$RUN_ID", run_id)
    prompt = f"{calibration_block}\n\n---\n\n{prompt_with_run_id}"

    result_text = ""
    session_id = None
    turns = 0
    cost_usd = 0.0
    status = "success"
    findings = None

    try:
        async for message in query(
            prompt=prompt,
            options=ClaudeAgentOptions(
                model="claude-sonnet-4-6",
                cwd=cwd,
                allowed_tools=["WebSearch", "WebFetch", "Bash", "Read"],
                permission_mode="bypassPermissions",
                system_prompt=SYSTEM_PROMPT,
                max_turns=60,
                setting_sources=[],
            ),
        ):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                session_id = message.data.get("session_id") if isinstance(message.data, dict) else None
                if verbose:
                    print(f"[agent] Session: {session_id}")
            elif isinstance(message, ResultMessage):
                result_text = message.result or ""
                session_id = message.session_id
                cost_usd = float(message.total_cost_usd or 0.0)
                turns = int(getattr(message, "num_turns", 0) or 0)
                findings = (result_text[:500] if result_text else None)
                if verbose:
                    print(f"[agent] Stop reason: {message.stop_reason}, cost: ${cost_usd:.4f}")
    except BaseException:
        status = "error"
        raise
    finally:
        finish_args = ["finish-run", "--run-id", run_id, "--status", status]
        if turns:
            finish_args.extend(["--turns", str(turns)])
        if cost_usd:
            finish_args.extend(["--cost-usd", f"{cost_usd:.4f}"])
        if findings:
            finish_args.extend(["--findings-summary", findings])
        try:
            _run_predict_tool(finish_args)
        except Exception as exc:
            # Never swallow the original exception. Log and move on.
            print(f"[agent] warning: finish-run failed: {exc}", file=sys.stderr)

    return result_text


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Run InsiderCopy strategy agent")
    parser.add_argument("--prompt", help="Custom prompt (overrides default run prompt)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    result = anyio.run(run, args.prompt, args.verbose)
    print(result)

#!/usr/bin/env python3
"""
world_analyst.py — Geopolitics & Political Intelligence Analyst for Sniper.

A specialist Claude Code agent that reasons about geopolitical and political
events, maps them to open Polymarket markets, and writes structured intelligence
to the Supabase brain. Finance, crypto, and macro analysis is handled by the
Finance Analyst (finance_analyst.py).

How it works:
  - Runs the full Claude Code toolset (WebSearch, WebFetch, Bash, etc.)
  - Queries the local DB for open positions and pending signals
  - Searches geopolitics + politics domains for events in the last 24-48h
  - Uses domain_query tool to cross-reference finance context when needed
  - Reasons about market connections → writes three Supabase entries:
      world_state:          Compact machine-readable JSON (<450 chars, survives 500-char truncation)
      world_narrative:      Full prose intelligence report (for finance_analyst + Telegram)
      market_boosts_current: {condition_id → boost_dict} for active trading
  - Sends a concise Telegram summary of key findings

Usage:
    python3 scripts/world_analyst.py              # full analysis (geo + politics)
    python3 scripts/world_analyst.py --quick      # 2-3 events per domain
    python3 scripts/world_analyst.py --domains geo,politics
    python3 scripts/world_analyst.py --quiet      # no Telegram output

Triggered by:
    - systemd timer every 4h (00, 04, 08, 12, 16, 20 UTC)
    - post-session hook in bot/relay.py (_hook_post_session)
    - manual: /brain refresh command in Telegram
"""

import argparse
import anyio
import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Ensure project root is on path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("world-analyst")

# ── Constants ─────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("DB_PATH", str(ROOT / "sniper_tracker.db"))
WORKING_DIR = str(ROOT)
# Model defaults route through the central registry so a fleet-wide bump is one line
# in sniper/settings/models.py. Domain env overrides (ANALYST_MODEL) still win where set.
from sniper.settings import models as _M
PRIMARY_MODEL = os.getenv("ANALYST_MODEL", _M.OPUS)
FALLBACK_MODEL = os.getenv("ANALYST_FALLBACK_MODEL", _M.SONNET)
WORLD_STATE_TMP = Path("/tmp/world_state_report.json")
WORLD_NARRATIVE_TMP = Path("/tmp/world_narrative_report.md")


def _compose_system_prompt() -> tuple[str, bool]:
    """Build the analyst system prompt, optionally injecting calibration context."""
    try:
        from sniper.analysis.agent_calibration import get_calibration_blurb

        calibration = get_calibration_blurb("world_analyst")
        if calibration:
            logger.info("world_analyst: injected calibration blurb into system prompt")
            return ANALYST_SYSTEM_PROMPT + calibration, True
    except Exception as exc:
        logger.warning("world_analyst: calibration blurb unavailable: %s", exc)
    return ANALYST_SYSTEM_PROMPT, False


def _should_retry_empty_stop(stop_reason: str | None, tool_count: int, elapsed_seconds: float) -> bool:
    """Treat instant 0-tool stop_sequence exits as failed attempts worth retrying."""
    return (
        str(stop_reason or "").strip().lower() == "stop_sequence"
        and int(tool_count or 0) == 0
        and float(elapsed_seconds or 0.0) <= 10.0
    )


def _fresh_memory(name: str, max_age_hours: float = 2.0) -> bool:
    from bot.supabase_helper import get_memory

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    mem = get_memory(name, type_filter="knowledge")
    if not mem:
        return False
    ts_raw = mem.get("updated_at") or ""
    if not ts_raw:
        return False
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
    except Exception:
        return False
    return ts > cutoff


def _humanize_world_slug(slug: str) -> str:
    words = []
    for raw in str(slug or "").split("-"):
        part = raw.strip()
        if not part:
            continue
        lowered = part.lower()
        if lowered.startswith("day") and lowered[3:].isdigit():
            continue
        if lowered.startswith("mar") and lowered[3:].isdigit():
            continue
        if lowered.isdigit():
            continue
        words.append(part.upper() if part.upper() in {"US", "UK", "EU", "NATO"} else part.capitalize())
    if not words:
        return "World event"
    return " ".join(words[:5])


def _build_world_recovery_payload(rows: list[dict], *, now: datetime | None = None) -> tuple[str, str] | None:
    if not rows:
        return None

    now = now or datetime.now(timezone.utc)

    hot_regions = []
    tail_risks = []
    escalating_regions = set()
    deescalating = False
    event_lines = []

    for row in rows:
        region = str(row.get("region") or "").strip()
        slug = str(row.get("slug") or "").strip()
        status = str(row.get("status") or "").strip().lower()
        trajectory = str(row.get("trajectory") or "").strip().lower()

        if region and region not in hot_regions and len(hot_regions) < 3:
            hot_regions.append(region)
        if trajectory == "escalating" and region:
            escalating_regions.add(region)
        if trajectory == "de-escalating":
            deescalating = True

        label = _humanize_world_slug(slug)
        if len(tail_risks) < 3:
            risk = f"{label} escalation" if trajectory == "escalating" else label
            if risk not in tail_risks:
                tail_risks.append(risk[:32])
        if len(event_lines) < 4:
            region_note = f" ({region})" if region else ""
            traj_note = f" — {trajectory}" if trajectory else ""
            event_lines.append(f"- {label}{region_note}: {status or 'active'}{traj_note}")

    regime = "stable"
    if "Middle East" in escalating_regions:
        regime = "elevated-uncertainty"
    elif escalating_regions:
        regime = "risk-off"
    elif deescalating:
        regime = "stable"

    macro_bias = "risk-off" if escalating_regions else "neutral"
    cross_domain = "War risk and credit stress keep oil and volatility bid" if escalating_regions else "No fresh cross-domain shock dominating markets"
    agent_note = "Avoid new YES on geo markets; fade ceasefire optimism." if escalating_regions else "Hold geo exposure neutral until fresh catalysts appear."

    state = {
        "updated": now.replace(microsecond=0).isoformat(),
        "regime": regime,
        "hot_regions": hot_regions[:3],
        "tail_risks": tail_risks[:3],
        "macro_bias": macro_bias,
        "cross_domain": cross_domain[:60],
        "agent_note": agent_note[:60],
        "confidence": 0.68 if escalating_regions else 0.55,
    }
    state_json = json.dumps(state, separators=(",", ":"))
    if len(state_json) >= 450:
        state["tail_risks"] = state["tail_risks"][:2]
        state["hot_regions"] = state["hot_regions"][:2]
        state["cross_domain"] = state["cross_domain"][:45]
        state["agent_note"] = state["agent_note"][:45]
        state_json = json.dumps(state, separators=(",", ":"))

    narrative = "\n".join(
        [
            "## EXECUTIVE SUMMARY: What changed",
            "Recovery write from fresh structured world events after the agent completed without final brain upserts.",
            "",
            "## GEOPOLITICS: Core shifts",
            *event_lines,
            "",
            "## MARKET IMPLICATIONS: Position impact",
            "- Cross-domain bias remains risk-off while Middle East escalation stays active.",
            "- Avoid treating ceasefire chatter as confirmation until bilateral public confirmation appears.",
        ]
    ).strip()
    return state_json, narrative


def _recover_world_brain_from_tmp(max_age_seconds: int = 1800) -> tuple[bool, bool]:
    try:
        from bot.supabase_helper import upsert_memory
    except Exception as exc:
        logger.warning("world_analyst temp recovery unavailable: %s", exc)
        return False, False

    recovered_state = False
    recovered_narrative = False
    now_ts = datetime.now(timezone.utc).timestamp()

    if WORLD_STATE_TMP.exists():
        age_s = now_ts - WORLD_STATE_TMP.stat().st_mtime
        if age_s < max_age_seconds and WORLD_STATE_TMP.stat().st_size > 20:
            try:
                state_json = json.dumps(json.loads(WORLD_STATE_TMP.read_text(encoding="utf-8")), separators=(",", ":"))
                if len(state_json) < 450:
                    upsert_memory("knowledge", "world_state", "World State (recovered from temp file)", state_json, source="vps")
                    recovered_state = True
            except Exception as exc:
                logger.warning("world_analyst state temp recovery failed: %s", exc)

    if WORLD_NARRATIVE_TMP.exists():
        age_s = now_ts - WORLD_NARRATIVE_TMP.stat().st_mtime
        if age_s < max_age_seconds and WORLD_NARRATIVE_TMP.stat().st_size > 120:
            try:
                narrative = WORLD_NARRATIVE_TMP.read_text(encoding="utf-8").strip()
                if narrative:
                    upsert_memory("knowledge", "world_narrative", "World Intelligence Narrative (recovered from temp file)", narrative, source="vps")
                    recovered_narrative = True
            except Exception as exc:
                logger.warning("world_analyst narrative temp recovery failed: %s", exc)

    return recovered_state, recovered_narrative


def _recover_world_brain_from_events() -> tuple[bool, bool]:
    try:
        from bot.supabase_helper import _request, upsert_memory

        rows = _request(
            "GET",
            "world_events?select=slug,region,country,status,trajectory,last_updated"
            "&status=in.(active,escalating,developing)"
            "&order=last_updated.desc&limit=12",
        ) or []
        payload = _build_world_recovery_payload(rows)
        if not payload:
            return False, False
        state_json, narrative = payload
        upsert_memory(
            "knowledge",
            "world_state",
            "World State (event-derived recovery)",
            state_json,
            source="vps",
        )
        upsert_memory(
            "knowledge",
            "world_narrative",
            "World Intelligence Narrative (event-derived recovery)",
            narrative,
            source="vps",
        )
        return True, True
    except Exception as exc:
        logger.warning("world_analyst event-derived recovery failed: %s", exc)
        return False, False

# ── System prompt: scopes analyst role precisely ──────────────────────────────
ANALYST_SYSTEM_PROMPT = f"""You are the Geopolitics & Political Intelligence Analyst for Sniper, an automated prediction market \
copy-trading system running on Polymarket. Your working directory is {WORKING_DIR}.

YOUR ROLE:
Synthesize current geopolitical and political events into actionable prediction market intelligence.
You are a senior analyst with deep domain knowledge in geopolitics and politics.
Finance, crypto, and macro analysis is handled by the Finance Analyst — focus on conflicts, sanctions, elections, alliances, regime changes.
You think like a hedge fund analyst, not a news aggregator.

WHAT YOU DO:
1. Query the local SQLite database for open paper trades and high-score signals
2. Research world events from the last 24-48 hours using WebSearch and WebFetch
3. Reason about which events affect which open markets, and in which direction
4. Write structured intelligence reports to the Supabase brain
5. Send a concise Telegram summary (3-5 bullet points, not a wall of text)

YOUR ANALYTICAL FRAMEWORK:
When you find an event, ask:
  (a) Does this change the probability of any open market resolving YES or NO?
  (b) Does this corroborate or contradict the thesis behind any open position?
  (c) Are there CROSS-DOMAIN connections? (e.g., oil price spike + Iran talks = Middle East
      market implications cascade across multiple markets simultaneously)
  (d) Is the market already pricing this in, or is there still information asymmetry?

SOURCE CREDIBILITY FRAMEWORK:
When you analyze articles, classify each source into a credibility tier:
  FACTUAL   — High-reliability (BBC, Reuters, AP, FP, DW, NHK, Meduza): treat as ground truth until contradicted
  SPECULATIVE — Medium-reliability, opinionated, or unverified: treat as informed inference
  OPINION   — Editorial/analysis content: label as analyst perspective, not fact
  PROPAGANDA — State media (Global Times, TASS) or articles with 2+ propaganda technique signals

SemEval-2020 Propaganda Technique Detection (flag if 2+ in one article):
  1. Loaded Language: emotionally charged labels ("terrorist", "regime", "freedom fighter", "genocide")
  2. Name-Calling: reducing actors to slurs/labels ("puppet", "stooge", "fascist")
  3. Appeal to Fear: catastrophizing ("existential threat", "catastrophic", "point of no return")
  4. Black-and-White Fallacy: false binary ("either with us or against us", "only option")
  5. Whataboutism: deflection ("but what about when THEY did...", "double standard")
  6. Appeal to Authority: vague sourcing ("unnamed officials", "experts say", "sources close to")
  7. Simplification: over-simplifying ("obviously", "everyone knows", "self-evident")
  8. False Equivalence: equating unequals ("both sides equally to blame", "same as")
  9. Straw Man: misrepresenting opposing argument ("so you're saying we should...")
 10. Repetition: same charged phrase repeated without new evidence (structural signal)

PRACTICE:
  - Note source tiers inline: "BBC (factual) + Al Jazeera (factual) + TASS (propaganda: loaded language)"
  - If a story comes ONLY from state media / propaganda tier → set confidence = LOW regardless of content
  - If the same event is reported by 3+ factual sources across different regions → treat as established fact
  - Counter-narrative from a biased source (e.g., Chinese state media reporting something against China's interest) = ELEVATED SIGNAL weight

ACH (ANALYSIS OF COMPETING HYPOTHESES):
For any event with 2+ plausible explanations, structure your reasoning:
  H1: [Most obvious interpretation — mainstream narrative]
  H2: [Alternative — who benefits from H1 being believed?]
  H3: [Counter-narrative from opposing sources, if available]

  For each piece of evidence: CONSISTENT (C), INCONSISTENT (I), or NEUTRAL (N) with each hypothesis?
  The hypothesis with the most INCONSISTENCIES is LEAST likely.

  Key insight: ONE solid inconsistency outweighs ten confirmations.
  Strong consensus from one-sided sources = evidence of narrative control, NOT truth.
  Ask: What evidence would change my mind? If nothing could, that's a flag.

SITUATION MODEL (persistent temporal knowledge):
The situation_model table stores claims about entities/events with validity timestamps.
Use it to build cumulative knowledge across runs rather than re-analyzing from scratch each time.

  Check what we already know before searching:
    python3 situation_model.py read --slug <event-slug> --summary

  Write new findings:
    python3 situation_model.py upsert --slug iran-us-2026 \\
      --subject "Iran" --predicate "has_status" --object "escalating_tensions" \\
      --confidence 0.85 --source-tier factual --sources "bbc_world,france24" \\
      --diversity-score 0.72

  Claim types: fact, hypothesis, propaganda_flag, uncertainty
  Source tiers: factual, speculative, opinion, propaganda

OUTPUT FORMAT — write these Supabase entries using Python via Bash (plus structured intel via intel_tool.py):

ENTRY 1: world_state (machine-readable JSON — compact, ~400 chars, survives 500-char truncation)
  Format:
  {{"updated":"<ISO timestamp>","regime":"<e.g. elevated-uncertainty|risk-off|risk-on|stable>",
    "hot_regions":["<region>",...],"tail_risks":["<short phrase>",...],"macro_bias":"<risk-off|risk-on|neutral>",
    "cross_domain":"<key cross-domain inference in ≤15 words>",
    "agent_note":"<trading instruction ≤20 words, e.g. Avoid new YES on geo markets>",
    "confidence":<0.0-1.0>}}
  Strict rules: no newlines, no trailing commas, valid JSON, under 450 chars total.

ENTRY 2: world_narrative (full prose intelligence report — for humans and finance_analyst)
  - Timestamp + key events across all domains
  - Specific inferences: "Event X + Event Y → Market Z should be reassessed because..."
  - Include condition_ids of affected markets where known
  - 400-800 words, structured by domain

ENTRY 3: market_boosts_current (machine-readable JSON)
  Format: {{"CONDITION_ID": {{"boost": INT, "reason": "str", "direction": "YES|NO|NEUTRAL",
                             "confidence": "high|medium|low", "expires_hours": INT}}}}

  Boost scale (-30 to +30):
  +20 to +30 = Strong world context confirming market thesis (high conviction)
  +10 to +20 = Meaningful supporting evidence
  +01 to +10 = Weak positive signal
       0     = No relevant context
  -01 to -10 = Weak contrary signal
  -10 to -20 = Meaningful counter-evidence
  -20 to -30 = Strong world context against market thesis (consider alert to exit)

  CRITICAL: Boosts MUST target condition_ids from our OPEN positions (queried in Step 2).
  Do NOT boost markets where we have zero exposure — those are useless.
  If you find a compelling opportunity in a market we DON'T hold, create a trade idea via
  intel_tool.py opportunity instead — that feeds the news_intel pipeline to potentially enter.
  Only include markets where you have genuine conviction (+/-8 or greater).
  Expires: 24h for fast-moving events (sports, breaking news), 72h for slow-moving (geopolitical).

HOW TO WRITE TO SUPABASE:
Use Bash to run Python inline. Write world_state (JSON) and world_narrative (prose) as separate entries:

  # Write machine-readable JSON to world_state (keep under 450 chars — no newlines)
  python3 -c "
  import sys, json; sys.path.insert(0, '{WORKING_DIR}')
  from bot.supabase_helper import upsert_memory
  state = {{'updated':'<ISO>','regime':'elevated-uncertainty','hot_regions':['Middle East'],
            'tail_risks':['Iran escalation'],'macro_bias':'risk-off',
            'cross_domain':'Energy shock repricing inflation',
            'agent_note':'Avoid new YES on geo markets. Favor NO resolution-near.',
            'confidence':0.72}}
  content = json.dumps(state, separators=(',',':'))
  assert len(content) < 450, f'world_state too long: {{len(content)}} chars'
  upsert_memory('knowledge', 'world_state', 'World State (machine JSON)', content, source='vps')
  print(f'world_state written ({{len(content)}} chars)')
  "

  # Write full prose to world_narrative (no length limit)
  python3 -c "
  import sys; sys.path.insert(0, '{WORKING_DIR}')
  from bot.supabase_helper import upsert_memory
  content = '''YOUR FULL PROSE REPORT HERE'''
  upsert_memory('knowledge', 'world_narrative', 'World Intelligence Narrative', content, source='vps')
  print('world_narrative written')
  "

HOW TO SEND TELEGRAM:
  Use this EXACT curl format — do NOT use -d "text=..." (breaks on & characters like S&P):
  MSG="your message here"
  curl -s -X POST "https://api.telegram.org/bot${{TELEGRAM_BOT_TOKEN}}/sendMessage" \
    --data-urlencode "chat_id=${{TELEGRAM_CHAT_ID}}" \
    --data-urlencode "text=${{MSG}}"
  The --data-urlencode flag handles all special characters (& $ % + etc.) safely.

GEOGRAPHIC INTELLIGENCE (via agents/tools/intel_tool.py):
  # Load geographic context for a region
  python3 agents/tools/intel_tool.py geo --region middle_east
  python3 agents/tools/intel_tool.py geo --all

  # Find cascade impacts (which neighbors + industries are affected)
  python3 agents/tools/intel_tool.py cascade --region middle_east

  # Upsert a world event (structured, replaces unstructured memories)
  python3 agents/tools/intel_tool.py event \
    --slug iran-us-tensions-2026 --title "Iran-US Military Tensions" \
    --region middle_east --country Iran --status escalating --trajectory escalating \
    --winners '[["defense","oil_producers"]]' --losers '[["airlines","tourism"]]' \
    --industries '[["energy","defense","shipping"]]'

  # Add industry impact analysis
  python3 agents/tools/intel_tool.py impact \
    --event-slug iran-us-tensions-2026 --industry energy --direction bullish \
    --magnitude major --reasoning "Strait of Hormuz risk premium" \
    --tickers '[["OIL","XLE"]]' --hyperliquid '[["OIL-USD"]]' --horizon days

  # Create a POLYMARKET trade idea (feeds strategy #28 Geo Intel — USE THIS for Polymarket)
  # IMPORTANT: This is different from 'opportunity'! This writes to the SQLite trade_ideas
  # table that the news_intel trading pipeline reads from. Source agent is automatically set
  # to "world_analyst" so strategy #28 can track your ideas separately.
  python3 agents/tools/intel_tool.py trade-idea \
    --condition-id 0xabc123... \
    --market-title "Will Iran attack a US base by June 2026?" \
    --direction buy_yes \
    --thesis "Causal chain: event X → probability of YES increases because..." \
    --confidence high \
    --current-price 0.35 \
    --news-trigger "Headline that triggered this idea" \
    --event-slug iran-us-tensions-2026

  ⚠️  HARD RULES FOR trade-idea — VIOLATIONS CAUSE REAL FINANCIAL LOSSES:

  RULE 1 — PRICE BAND: Only propose trade ideas where the current YES price is between
           0.15 and 0.60 (15¢–60¢). This is the uncertainty zone where information
           asymmetry can still exist. Outside this band the market has already priced in
           the outcome — your analysis adds no edge.
             - YES price > 0.60 → market already believes it will happen. Do NOT propose
               a NO bet. An active crisis is NOT evidence it won't happen. Write a boost
               on existing positions instead.
             - YES price < 0.15 → market already believes it won't happen. Do NOT propose
               a YES bet unless you have specific, credible, high-diversity-score evidence
               that overturns the consensus.

  RULE 2 — NO NARRATIVE LAUNDERING: Do not re-analyze a market and flip direction based
           on a price move you observe. A falling price does NOT mean "the event is less
           likely" unless you have new fundamental evidence. Price moves lag your analysis
           by 6–12 hours — you are not seeing new information, you are chasing.
           If you proposed buy_yes yesterday and price has fallen, that's a loss, not
           new intelligence about direction.

  RULE 3 — HYPOTHETICAL DISASTERS ONLY: Your edge is in markets for events that MIGHT
           happen — geopolitical risks the crowd underestimates, elections with uncertain
           outcomes, policy decisions still in flux. If a crisis is ALREADY HAPPENING and
           dominating headlines, the market already reflects it. Your job is to find
           what the crowd is UNDERPRICING, not to confirm what is already priced in.

  RULE 4 — BEFORE WRITING ANY trade-idea: Query the live YES price from the CLOB API
           and confirm it is in [0.15, 0.60] before calling intel_tool.py trade-idea.
           python3 -c "
           import requests, json
           r = requests.get('https://clob.polymarket.com/markets/CONDITION_ID', timeout=8)
           tokens = r.json().get('tokens', [])
           yes_price = next((t['price'] for t in tokens if t.get('outcome','').upper()=='YES'), None)
           print(f'YES price: {{yes_price}} — ', 'OK to trade' if yes_price and 0.15 <= float(yes_price) <= 0.60 else '❌ OUTSIDE BAND — skip trade-idea')
           "

  # Create a HYPERLIQUID / non-Polymarket opportunity (stores in Supabase knowledge base only)
  python3 agents/tools/intel_tool.py opportunity \
    --event-slug iran-us-tensions-2026 --platform hyperliquid \
    --instrument "OIL-USD" --direction long \
    --thesis "Hormuz disruption risk" --entry-reasoning "Oil at $72, premium is $8-15" \
    --confidence high --expires-hours 168

  # List current events and opportunities
  python3 agents/tools/intel_tool.py events --status active,escalating
  python3 agents/tools/intel_tool.py opportunities --status idea,active

SELF-IMPROVEMENT (via agents/tools/predict_tool.py — USE THIS EVERY RUN):
  # Auto-grade past predictions FIRST (before any new analysis)
  python3 agents/tools/predict_tool.py auto-grade --agent world_analyst
  # This runs verification queries and grades automatically. Review any skipped ones:

  # Read your active lessons (hard-won insights from past mistakes)
  python3 agents/tools/predict_tool.py lessons --agent world_analyst

  # Check your accuracy stats (are you well-calibrated?)
  python3 agents/tools/predict_tool.py stats --agent world_analyst

  # Make verifiable predictions during analysis
  python3 agents/tools/predict_tool.py predict \
    --agent world_analyst \
    --prediction "Oil will rise above $80 within 7 days due to Iran tensions" \
    --condition "Check oil price vs $80" \
    --confidence high --verify-hours 168

  # Record a lesson when you discover a pattern in your accuracy
  python3 agents/tools/predict_tool.py learn \
    --agent world_analyst --lesson "..." --evidence "..." --accuracy 65.0

  # Log this run at the end
  python3 agents/tools/predict_tool.py log-run \
    --agent world_analyst --model opus --cost 1.20 --turns 45 \
    --summary "..."

CROSS-DOMAIN QUERIES (when you need financial context):
  python3 agents/tools/domain_query.py --domain finance --topic "oil prices"
  python3 agents/tools/domain_query.py --domain finance --topic "sanctions impact"

CROSS-DOMAIN FLAGS (when you discover something urgent for finance):
  python3 agents/tools/domain_query.py --flag --from geopolitics --to finance \
    --urgency high --message "Hormuz closure — oil supply disruption"

READ FLAGS DIRECTED AT YOU:
  python3 agents/tools/domain_query.py --read-flags --domain geopolitics

WHAT YOU MUST NOT DO:
- Do NOT modify any .py, .js, .css, .html files
- Do NOT run raw INSERT/UPDATE/DELETE SQL on sniper_tracker.db directly
- Do NOT enter, close, or modify paper trades or strategy positions
- Do NOT change strategy parameters or scoring thresholds
- Do NOT analyze finance, crypto, or macro topics in depth — flag them for the Finance Analyst

WHAT YOU MUST DO (writing outputs):
- Use `intel_tool.py trade-idea` to write Polymarket trade ideas to the trading pipeline
  (this uses Brain.add_trade_idea() under the hood — it's the approved write path for trade ideas)
- Use `intel_tool.py event/impact` to write structured world event intelligence to Supabase
- Use `upsert_memory` to write world_state (JSON), world_narrative (prose), and market_boosts_current to Supabase brain

If you find no significant events affecting open markets, write a minimal world_state JSON noting
this (regime: "stable", confidence: 0.5), a brief world_narrative, and set market_boosts_current to {{}}.

═══ BRAIN MAP ═══
You are part of a multi-agent system. Each agent owns a section of the shared brain (Supabase).

  SECTION          OWNER                       YOU
  world_state      Geopolitics Analyst         WRITE — compact JSON (<450 chars), machine-readable
  world_narrative  Geopolitics Analyst         WRITE — full prose for finance_analyst + Telegram
  markets/*        Finance Analyst             READ — query via domain_query tool
  flags/*          Any specialist              WRITE (own flags) / READ (flags to you)
  portfolio/*      System (auto)               READ — convergence:latest-scan, conflict:latest-scan, Strategy Performance
  wallets/*        Wallet Scout                READ
  signals/*        Insider Copy                READ
  predictions/*    All agents                  WRITE (own agent_type only)
  health/*         Health Checker              READ — brain_health_latest
  knowledge/*      Static (seeded)             READ-ONLY
  meta/*           Human                       READ-ONLY

COUNTRY KNOWLEDGE (stored in brain as knowledge/country:{{ISO}}):
  Before analyzing events in a country, load its profile:
    python3 -c "from bot.supabase_helper import get_memory; m=get_memory('country:IR',type_filter='knowledge'); print(m['content'][:1000] if m else 'no profile')"

  If you discover significant changes to a country's situation (new sanctions, leadership change,
  economic crisis, new alliance), update the profile's current_situation_notes:
    python3 -c "
    import sys; sys.path.insert(0, '.')\n\
    from bot.supabase_helper import get_memory, upsert_memory\n\
    import json\n\
    m = get_memory('country:IR', type_filter='knowledge')\n\
    if m:\n\
        p = json.loads(m['content'])\n\
        p['current_situation_notes'] = 'Updated situation note here'\n\
        p['updated_at'] = '2026-03-21'\n\
        upsert_memory('knowledge', 'country:IR', 'Iran country profile', json.dumps(p), source='vps')\n\
        print('Updated')\n\
    "

RULES:
  - Only WRITE to sections you own (exception: you may update country profile current_situation_notes)
  - READ other sections for awareness, but don't act on stale data (check timestamps)
  - If you notice something wrong in another agent's section, FLAG it — don't fix it

CROSS-READS (do these in Step 1):
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('brain_health_latest',type_filter='knowledge'); print(m['content'][:500] if m else 'no health data')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('convergence:latest-scan',type_filter='project'); print(m['content'][:500] if m else 'no convergence data')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('conflict:latest-scan',type_filter='project'); print(m['content'][:500] if m else 'no conflict data')"
"""

# ── Context pre-fetcher (runs Steps 0–2 before agent starts, saves ~15 turns) ──
def _prefetch_context(domains: list[str], quick: bool = False) -> str:
    """Pre-run all mechanical data-gathering steps (0, 1, 2) before the agent starts.

    Runs subprocesses and direct imports in parallel, assembles results into a
    formatted string that gets injected into the task prompt. The agent is told
    to skip those steps and jump to Step 3, saving ~15 turns.

    Returns a formatted multi-section string, or empty string on total failure.
    """
    import concurrent.futures as _cf
    import sqlite3 as _sq3
    import subprocess as _sp
    import sys as _sys

    _py = _sys.executable  # same venv Python we're running with

    def _run(args: list[str], timeout: int = 30) -> str:
        """Run a subprocess, return stdout (capped at 1500 chars)."""
        try:
            r = _sp.run(args, capture_output=True, text=True, timeout=timeout, cwd=WORKING_DIR)
            out = (r.stdout or "").strip()
            if not out and r.stderr:
                out = f"[stderr: {r.stderr.strip()[:300]}]"
            return out[:1500] or "[no output]"
        except _sp.TimeoutExpired:
            return f"[timeout after {timeout}s]"
        except Exception as exc:
            return f"[error: {exc}]"

    def _supabase_reads() -> dict[str, str]:
        try:
            from bot.supabase_helper import get_memory  # type: ignore
            specs = [
                ("brain_health_latest",            "knowledge", 300),
                ("intel/report_delivery_contract", "reference", 1200),
                ("convergence:latest-scan",        "project",   400),
                ("conflict:latest-scan",           "project",   400),
                ("adversarial/rejection_patterns", "knowledge", 800),
            ]
            out: dict[str, str] = {}
            for name, tf, maxlen in specs:
                m = get_memory(name, type_filter=tf)
                out[name] = (m.get("content") or "")[:maxlen] if m else f"[no {name}]"
            return out
        except Exception as exc:
            return {"_error": str(exc)}

    def _country_profiles() -> str:
        try:
            from bot.supabase_helper import search_memories  # type: ignore
            profiles = search_memories("country:")
            profiles = [m for m in profiles if m.get("name", "").startswith("country:")]
            if not profiles:
                return "  (no country profiles found)"
            lines = []
            for p in profiles[:20]:
                content = (p.get("content") or "")[:200]
                lines.append(f"  {p['name']}: {content}")
            return "\n".join(lines)
        except Exception as exc:
            return f"[country profiles error: {exc}]"

    def _g(fut: _cf.Future) -> str:
        try:
            return fut.result(timeout=50)
        except Exception as exc:
            return f"[future error: {exc}]"

    # Launch all I/O-bound tasks in parallel
    with _cf.ThreadPoolExecutor(max_workers=12) as pool:
        f_grade    = pool.submit(_run, [_py, "agents/tools/predict_tool.py", "auto-grade",    "--agent", "world_analyst"])
        f_less     = pool.submit(_run, [_py, "agents/tools/predict_tool.py", "lessons",       "--agent", "world_analyst"])
        f_stats    = pool.submit(_run, [_py, "agents/tools/predict_tool.py", "stats",         "--agent", "world_analyst"])
        f_geo      = pool.submit(_run, [_py, "agents/tools/intel_tool.py",   "geo", "--all"])
        f_events   = pool.submit(_run, [_py, "agents/tools/intel_tool.py",   "events",        "--status", "active,escalating,developing"])
        f_opps     = pool.submit(_run, [_py, "agents/tools/intel_tool.py",   "opportunities", "--status", "idea,active"])
        f_sit      = pool.submit(_run, [_py, "situation_model.py",           "read", "--limit", "30", "--summary"])
        f_flags    = pool.submit(_run, [_py, "agents/tools/domain_query.py", "--read-flags",   "--domain", "geopolitics"])
        f_supa     = pool.submit(_supabase_reads)
        f_country  = pool.submit(_country_profiles)

        grade    = _g(f_grade)
        lessons  = _g(f_less)
        stats    = _g(f_stats)
        geo      = _g(f_geo)
        events   = _g(f_events)
        opps     = _g(f_opps)
        sitmod   = _g(f_sit)
        flags    = _g(f_flags)
        country  = _g(f_country)
        try:
            supa = f_supa.result(timeout=30)
        except Exception as exc:
            supa = {"_error": str(exc)}

    # Supabase section
    if "_error" in supa:
        supa_text = f"[supabase error: {supa['_error']}]"
    else:
        supa_text = "\n".join(f"  [{k}]:\n  {v}" for k, v in supa.items())

    # Step 2: SQLite (fast, sequential)
    positions_text = ideas_text = "[sqlite error]"
    try:
        with _sq3.connect(DB_PATH) as conn:
            conn.row_factory = _sq3.Row
            rows = conn.execute("""
                SELECT sp.condition_id, sp.outcome, s.label AS strategy,
                       ROUND(sp.avg_entry_price, 3) AS entry,
                       ROUND(COALESCE(sp.current_price, sp.avg_entry_price), 3) AS cur,
                       ROUND((COALESCE(sp.current_price, sp.avg_entry_price) - sp.avg_entry_price)
                             / sp.avg_entry_price * 100, 1) AS drift_pct,
                       ROUND(sp.pnl_usdc, 2) AS pnl_usdc, sp.end_date_iso
                FROM strategy_positions sp
                LEFT JOIN strategies s ON CAST(s.id AS TEXT) = sp.strategy_id
                WHERE sp.state = 'open'
                ORDER BY ABS(COALESCE(sp.pnl_usdc, 0)) DESC LIMIT 30
            """).fetchall()
            positions_text = "\n".join(
                f"  {r['condition_id'][:10]}… {r['outcome']} | {r['strategy']} | "
                f"entry={r['entry']} cur={r['cur']} drift={r['drift_pct']}% pnl={r['pnl_usdc']} | ends={r['end_date_iso']}"
                for r in rows
            ) if rows else "  (no open positions)"

            ideas = conn.execute("""
                SELECT condition_id, market_title, direction, confidence
                FROM trade_ideas WHERE status='open' AND confidence IN ('HIGH','MEDIUM')
                ORDER BY created_at DESC LIMIT 15
            """).fetchall()
            ideas_text = "\n".join(
                f"  {r['condition_id'][:10]}… | {r['direction']} {r['confidence']} | {r['market_title'][:60]}"
                for r in ideas
            ) if ideas else "  (no HIGH/MEDIUM trade ideas)"
    except Exception as exc:
        positions_text = ideas_text = f"[sqlite error: {exc}]"

    return (
        "╔══ PRE-FETCHED CONTEXT (Steps 0 / 1 / 2 already done) ════════════════════╗\n"
        "║  Data below was gathered before this session started.                   ║\n"
        "║  ✓ Review it, then JUMP DIRECTLY TO STEP 3.                             ║\n"
        "║  ✗ If a section shows [error] or [timeout], redo that step only.        ║\n"
        "╚════════════════════════════════════════════════════════════════════════════╝\n"
        f"\n── STEP 0a: auto-grade ──\n{grade}"
        f"\n\n── STEP 0b: lessons ──\n{lessons}"
        f"\n\n── STEP 0c: stats ──\n{stats}"
        f"\n\n── STEP 1a: geo context ──\n{geo}"
        f"\n\n── STEP 1b: intel events ──\n{events}"
        f"\n\n── STEP 1c: intel opportunities ──\n{opps}"
        f"\n\n── STEP 1d: country profiles ──\n{country}"
        f"\n\n── STEP 1e: situation model ──\n{sitmod}"
        f"\n\n── STEP 1f: domain flags (geopolitics) ──\n{flags}"
        f"\n\n── STEP 1g: supabase brain reads ──\n{supa_text}"
        f"\n\n── STEP 2a: open positions ──\n{positions_text}"
        f"\n\n── STEP 2b: trade ideas ──\n{ideas_text}"
        "\n\n╔══ END PRE-FETCHED CONTEXT ═════════════════════════════════════════════════╗\n"
        "║  Jump to STEP 3 now. Re-run sections marked [error]/[timeout] only.     ║\n"
        "╚════════════════════════════════════════════════════════════════════════════╝\n"
    )


# ── Task prompt: step-by-step analytical cycle ───────────────────────────────
def build_task_prompt(domains: list[str], quick: bool, prefetched: str | None = None) -> str:
    domain_list = ", ".join(domains).upper()
    depth = "Focus on the 2-3 highest-impact events per domain." if quick else \
            "Be thorough — cover all material events in the last 24-48 hours."

    if prefetched:
        early_steps = f"""{prefetched}
━━━ STEPS 0 / 1 / 2: PRE-FETCHED — see context above ━━━
  All data for these steps has been gathered above before this session.
  ✓ Validate the data looks reasonable, then proceed to STEP 3.
  ✗ If any section shows [error] or [timeout], redo only that step.
  Do NOT re-run Steps 0-2 commands when data is present above.

"""
    else:
        early_steps = f"""
━━━ STEP 0: SELF-IMPROVEMENT (ALWAYS DO THIS FIRST) ━━━
  a) Auto-grade past predictions:
     python3 agents/tools/predict_tool.py auto-grade --agent world_analyst
     This auto-grades predictions with verification queries. Review any skipped ones manually.
  b) Read your lessons:
     python3 agents/tools/predict_tool.py lessons --agent world_analyst
     These are hard-won insights from your past mistakes. Apply them in this run.
  c) Check accuracy stats:
     python3 agents/tools/predict_tool.py stats --agent world_analyst
     If accuracy < 50%, you need to recalibrate. If high-confidence predictions
     are less accurate than medium, your confidence calibration is off.

━━━ STEP 1: LOAD GEOGRAPHIC CONTEXT + BRAIN AWARENESS ━━━
  # Geographic context
  python3 agents/tools/intel_tool.py geo --all
  python3 agents/tools/intel_tool.py events --status active,escalating,developing
  python3 agents/tools/intel_tool.py opportunities --status idea,active
  Understand the current state of world events before searching for new ones.

  # Load country profiles for active event regions
  python3 -c "
import sys; sys.path.insert(0, '.')
from bot.supabase_helper import search_memories
profiles = search_memories('country:')
profiles = [m for m in profiles if m.get('name','').startswith('country:')]
for p in profiles:
    print(f\\"  {{p['name']}}: {{p.get('content','')[:200]}}\\")
print(f'Loaded {{len(profiles)}} country profiles')
"

  # Situation model — load existing knowledge before fresh analysis
  # Run for each active event slug you'll be analyzing (if known from geo/event list above)
  python3 situation_model.py read --limit 30 --summary || true
  # (Note: --slug <slug> --summary for specific events after you know the slug)

  # Cross-domain flags (check if Finance Analyst flagged something for you)
  python3 agents/tools/domain_query.py --read-flags --domain geopolitics

  # Brain awareness (cross-reads)
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('brain_health_latest',type_filter='knowledge'); print(m['content'][:500] if m else 'no health data')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('convergence:latest-scan',type_filter='project'); print(m['content'][:500] if m else 'no convergence data')"
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('conflict:latest-scan',type_filter='project'); print(m['content'][:500] if m else 'no conflict data')"

  # ADVERSARIAL FEEDBACK — read what GPT-5.4 has been killing and why
  # MANDATORY: Do NOT propose ideas that match these rejection patterns.
  python3 -c "from bot.supabase_helper import get_memory; m=get_memory('adversarial/rejection_patterns',type_filter='knowledge'); print(m['content'][:800] if m else 'no rejection patterns yet')"
  # Key patterns to watch for: crowded breaking-news bets, priced-in geopolitical headlines,
  # commodity directional bets during active conflict, weak causal chains.
  # If your new idea resembles a recently rejected pattern, drop it or differentiate clearly.
  Note any health warnings, convergence signals, or conflicts before proceeding.
  If there are flags from Finance Analyst, incorporate them into your analysis and clear them when done.

━━━ STEP 2: LOAD TRADING CONTEXT ━━━
Query open strategy positions (all strategies):
  sqlite3 {DB_PATH} "SELECT sp.condition_id, sp.outcome, s.label as strategy, sp.avg_entry_price, COALESCE(sp.current_price, sp.avg_entry_price) as cur_price, ROUND((COALESCE(sp.current_price, sp.avg_entry_price) - sp.avg_entry_price) / sp.avg_entry_price * 100, 1) as drift_pct, sp.pnl_usdc, sp.end_date_iso FROM strategy_positions sp LEFT JOIN strategies s ON CAST(s.id AS TEXT) = sp.strategy_id WHERE sp.state = 'open' ORDER BY ABS(COALESCE(sp.pnl_usdc, 0)) DESC LIMIT 30"

Query recent HIGH-confidence trade ideas:
  sqlite3 {DB_PATH} "SELECT condition_id, market_title, direction, confidence, reasoning FROM trade_ideas WHERE status='open' AND confidence IN ('HIGH','MEDIUM') ORDER BY created_at DESC LIMIT 15"

"""

    return f"""Run the full world intelligence analysis cycle. {depth}

{early_steps}
━━━ STEP 3: SEARCH WORLD EVENTS — DOMAINS: {domain_list} ━━━
For each domain below, search for material events in the LAST 24-48 HOURS.
Do not report background context — only what actually changed recently.

{"GEOPOLITICS (search: Middle East, Russia-Ukraine, China-Taiwan, Iran nuclear, Israeli government):" if "geo" in domains else ""}
{"US POLITICS (search: Trump administration, Congress, Supreme Court, federal policy):" if "politics" in domains else ""}
{"FINANCE/MACRO (search: Federal Reserve, interest rates, S&P500, CPI data, recession):" if "finance" in domains else ""}
{"SPORTS (search: NFL playoffs, NBA, Champions League, boxing, major sporting events this week):" if "sports" in domains else ""}
{"CRYPTO (search: Bitcoin price, Ethereum, major crypto news):" if "crypto" in domains else ""}

━━━ STEP 3.5: REFRESH STALE ACTIVE EVENTS ━━━
Some ongoing events may not have come up in your search but are still active in our database.
Review and refresh non-finance events that haven't been touched in >48h:
  python3 agents/tools/intel_tool.py events \
    --status active,escalating,developing \
    --older-than-hours 48 \
    --exclude-prefix fin: \
    --limit 20 \
    --touch-reviewed

This command refreshes last_updated for the events it returns. Do not re-run
intel_tool.py event just to bump freshness.

For any stale event that has clearly resolved, escalated, or is no longer relevant, update it explicitly:
  python3 agents/tools/intel_tool.py event --slug <existing-slug> --title "..." --region ... --status resolved

Do not skip this step. Un-touched events accumulate as false health warnings.

━━━ STEP 4: CONNECT EVENTS TO MARKETS ━━━
For each event you found:
  - Which open positions or pending signals does this affect?
  - Does it corroborate or contradict the market thesis?
  - What direction does this push the probability, and by how much?
  - Are there cross-domain cascades? (geo + finance + political all moving together?)
  - Run: python3 agents/tools/intel_tool.py cascade --region X for each significant event
  - Use geo_context to identify cross-regional impacts

For each SIGNIFICANT event, write structured intelligence:
  - python3 agents/tools/intel_tool.py event --slug ... --title ... --region ... --status ...
  - python3 agents/tools/intel_tool.py impact --event-slug ... --industry ... --direction ...

EVENT TITLE CONTRACT:
  Event titles render directly in the Intel map and cards. Keep them compact and scannable.
  - 2-5 words only
  - Actor + development, not a full sentence
  - No dates, no prices, no scoreboard-style suffixes unless essential
  - Prefer "Iran-US Talks", "Russia-Ukraine", "US Tariffs", "China Deploys"
  - Avoid "Iran-US War Day 28: +1" or "Oil Hits 12.57 Brent"
  - Avoid stacked noun phrases that force the UI to truncate badly

IMPORTANT: Create events across ALL domains you searched, not just the dominant crisis.
Each domain should have at least 1 structured event. If nothing major happened in sports,
create a monitoring-level event for the biggest upcoming match/tournament.

After creating/updating events for a country, update that country's profile if you found NEW information:
  python3 -c "
  import sys; sys.path.insert(0, '.')
  from bot.supabase_helper import get_memory, upsert_memory
  import json
  m = get_memory('country:{{ISO}}', type_filter='knowledge')
  if m:
      p = json.loads(m['content'])
      p['current_situation_notes'] = '{{Brief current situation based on your analysis}}'
      p['updated_at'] = '{{today YYYY-MM-DD}}'
      upsert_memory('knowledge', 'country:{{ISO}}', '{{Country}} country profile', json.dumps(p), source='vps')
      print('Updated country:{{ISO}}')
  "
Only update if you found NEW information — don't rewrite with the same content.

When creating events via intel_tool.py, ALWAYS include approximate lat/lon coordinates:
  python3 agents/tools/intel_tool.py event --slug ... --lat 38.9 --lon -77.0 ...
Without coordinates, events cluster at a default point and the map becomes unreadable.
Common coordinates: Washington DC (38.9,-77.0), London (51.5,-0.1), Beijing (39.9,116.4),
Moscow (55.8,37.6), Tehran (35.7,51.4), Jerusalem (31.8,35.2), Kyiv (50.5,30.5),
Taipei (25.0,121.5), New Delhi (28.6,77.2), Brasilia (-15.8,-47.9), Tokyo (35.7,139.7)

MANDATORY: Create at least 1 POLYMARKET TRADE IDEA per run for markets we DON'T currently hold:
  python3 agents/tools/intel_tool.py trade-idea \
    --condition-id <polymarket_condition_id_hex> \
    --market-title "<full market question>" \
    --direction buy_yes|buy_no \
    --thesis "<causal chain: event → outcome probability change>" \
    --confidence high|medium|low \
    --current-price <YES price 0.0-1.0> \
    --news-trigger "<headline that triggered this>" \
    --event-slug <related-event-slug>

  WHY THIS MATTERS: This writes to the SQLite trade_ideas table with source_agent="world_analyst".
  Strategy #28 (Geo Intel) reads this table and can act on your ideas — that IS the pipeline.
  The 'opportunity' command writes to a Supabase knowledge table only, NOT the trading pipeline.
  If nothing is compelling, still create one with --confidence low so the pipeline has signal.

  ⚠️  CRITICAL — VALID CONDITION IDs ONLY:
  NEVER submit a trade-idea with a fake or placeholder condition_id such as:
    0x0000000000000000000000000000000000000000000000000000000000000000
    0xfordow_strike_placeholder  (or any non-hex / non-64-char string)
  Strategy #28 reads condition_id directly from trade_ideas to place orders. A fake ID means
  the trade can NEVER execute — it pollutes the pipeline and wastes analysis.
  If you cannot find the real Polymarket condition_id for a market:
    (a) Search harder: WebFetch https://polymarket.com and search for the market title,
        then extract the 0x... hex from the URL or page source.
    (b) If you still can't find it, use intel_tool.py opportunity instead — that stores
        the idea safely in Supabase without blocking the trading pipeline.
  A missing trade idea is better than a dead-ID trade idea.

  For Hyperliquid entries (not Polymarket), use 'opportunity' instead:
  python3 agents/tools/intel_tool.py opportunity --platform hyperliquid ...

━━━ STEP 4.5: SOURCE AUDIT, ACH SYNTHESIS & SITUATION MODEL UPDATE ━━━
For each significant event found in Step 3-4:

A) SOURCE TIER AUDIT — classify the sources you used:
   - List sources as: "BBC (factual), Al Jazeera (factual), TASS (propaganda: repetition+loaded_language)"
   - If story has <2 factual sources AND no contradictory coverage → lower confidence one tier

B) PROPAGANDA SCAN — for each article from low-reliability or state sources:
   - Check for 2+ SemEval-2020 technique signals (loaded language, appeal to fear, whataboutism, etc.)
   - If 2+ detected → flag the article as propaganda-tier; note in world_state report
   - IMPORTANT: state media propaganda ≠ useless. The direction they're spinning reveals intent.
     Russian state media downplaying losses = they ARE significant. Chinese state media alarmed about
     Taiwan = elevated real risk. Read the signal behind the propaganda.

C) ACH FOR TOP 2 EVENTS — apply Analysis of Competing Hypotheses:
   H1: [Mainstream narrative]
   H2: [Alternative interpretation]
   Evidence matrix: which hypothesis has the most inconsistencies?
   Conclusion: "Most supported hypothesis: H1/H2 because evidence X is INCONSISTENT with H2"

D) UPDATE SITUATION MODEL — write key findings as temporal claims:
   For each significant new fact, hypothesis, or propaganda flag found:
   python3 situation_model.py upsert \\
     --slug <event-slug> \\
     --subject "<entity>" --predicate "<relationship/status>" --object "<value>" \\
     --confidence <0.0-1.0> --source-tier <factual|speculative|opinion|propaganda> \\
     --sources "<comma-separated source keys>" --diversity-score <0.0-1.0>

   Write at least 2-3 claims per major event. Examples:
     subject="Iran" predicate="nuclear_program_status" object="enriching_at_60pct"
     subject="Gaza_ceasefire" predicate="current_status" object="negotiations_stalled"
     subject="TASS_coverage" predicate="propaganda_flag" object="loaded_language+repetition"

━━━ STEP 5: WRITE world_state + world_narrative TO SUPABASE ━━━

PART A — world_state (compact JSON, machine-readable):
  Write a single valid JSON object, no newlines, under 450 chars total. Fields:
    updated:      ISO timestamp (e.g. "2026-03-25T12:00Z")
    regime:       "elevated-uncertainty" | "risk-off" | "risk-on" | "stable" | "crisis"
    hot_regions:  array of up to 3 region strings actively moving markets
    tail_risks:   array of up to 3 short tail-risk phrases
    macro_bias:   "risk-off" | "risk-on" | "neutral"
    cross_domain: key cross-domain inference in ≤15 words
    agent_note:   actionable trading instruction ≤20 words
    confidence:   float 0.0–1.0 (your overall confidence in this read)

  Example (must stay under 450 chars):
    {{"updated":"2026-03-25T12:00Z","regime":"elevated-uncertainty","hot_regions":["Middle East","East Asia"],
      "tail_risks":["Iran-Israel escalation","Taiwan Strait tension"],"macro_bias":"risk-off",
      "cross_domain":"Energy shock repricing inflation expectations","agent_note":"Avoid new YES on geo markets.",
      "confidence":0.72}}

PART B — world_narrative (full prose intelligence report):
  Write a complete intelligence report. Include:
  - Timestamp + date
  - Short heading labels only — keep them stable for downstream readers
  - Prefer bullets and short paragraphs over dense prose
  - Use this exact heading pattern when relevant:
    ## EXECUTIVE SUMMARY: What changed
    ## GEOPOLITICS: Core shifts
    ## POLITICS: Policy moves
    ## FINANCE: Market spillover
    ## SPORTS: Event watch
    ## CRYPTO: Risk signals
    ## MARKET IMPLICATIONS: Position impact
    ## SOURCE CREDIBILITY: Source audit
    ## ACH: Competing hypotheses
    ## DIVERSITY: Coverage balance
  - The text after the colon should be 2-4 words
  - Open each section with 1 short lead sentence, then bullets if useful
  - Omit a section if nothing material happened in that domain
  - Write for briefing delivery, not essay completeness

  Before any Supabase upserts, write your final outputs to temp files so the wrapper can recover them if you hit the tool limit:
    python3 -c "
    import json
    from pathlib import Path
    Path('/tmp/world_state_report.json').write_text(json.dumps(YOUR_WORLD_STATE_JSON), encoding='utf-8')
    Path('/tmp/world_narrative_report.md').write_text(YOUR_WORLD_NARRATIVE_TEXT, encoding='utf-8')
    print('Temp files written')
    "

  Then use the normal Supabase upserts. See HOW TO WRITE TO SUPABASE above for the exact Python commands.

━━━ STEP 6: WRITE market_boosts_current TO SUPABASE ━━━
Write the JSON boost dict. CRITICAL RULES:
  - ONLY boost condition_ids that appear in our open positions (from Step 2)
  - If you found something compelling for a market we DON'T hold, use intel_tool.py opportunity instead
  - If you have no material view on any open position, write an empty JSON {{}}
  - Don't pad with zeros — only genuine conviction (+/-8 or greater)

━━━ STEP 7: MAKE PREDICTIONS (0–1 per run) ━━━
Make at most 1 high-quality verifiable prediction per run. Quality > quantity.
Abstaining is correct when evidence is genuinely mixed — a skipped run costs nothing;
a wrong high-confidence prediction degrades the calibration record.

CALIBRATION RULES — read before choosing confidence:

  Rule 1 — Political/military ultimatums:
    Stated rhetoric or ultimatums alone are NOT sufficient for confidence=high.
    High confidence requires at least one of:
      • Independent reporting of operational military movement (staging, deployment orders)
      • Congressional/parliamentary notification
      • Confirmed logistics activity (satellite imagery, port activity, airlift)
    If you only have the stated ultimatum → cap at confidence=medium.
    Historical base rate: political ultimatums are executed far less often than stated.

  Rule 2 — Bidirectional uncertainty (abstention rule):
    Before filing a prediction, ask: "Would I also plausibly predict the opposite
    at similar confidence right now?"
    If yes → do NOT file either direction. Predict status-quo persistence instead,
    or abstain entirely.
    Example: If you are unsure whether talks will collapse OR progress, the correct
    call is "talks remain active but inconclusive" — not two separate predictions
    on each outcome in the same run.

  Rule 3 — Polymarket resolution vs real-world event occurrence:
    A real-world event occurring does NOT guarantee a Polymarket market resolves.
    Before predicting "market will resolve YES", check:
      • The market's exact resolution criteria (not just the headline event)
      • Oracle timing — UMA liveness window can delay settlement 2–48h after event
      • Whether the criteria match what you observed (e.g., "major offensive" may
        require a specific threshold, not just any cross-border action)
    If resolution criteria are ambiguous → lower confidence by one tier or abstain.

Query targets (SQLite only — world_events is Supabase-only and CANNOT be used as --query):
  ✓ Market outcomes:  --query "SELECT status FROM trade_ideas WHERE condition_id='0x...' AND status IN ('resolved','expired')"
  ✓ Wallet scores:    --query "SELECT insider_score FROM user_analysis WHERE wallet LIKE '0xabc%'"
  ✓ Price snapshots:  --query "SELECT captured_at FROM poly_price_snapshots WHERE condition_id='0x...' ORDER BY captured_at DESC LIMIT 1"
  ✗ world_events:     NEVER use world_events as --query. It lives in Supabase, not SQLite.
                      auto-grade will skip it and it will never clear. Omit --query for
                      geopolitical/event predictions and grade manually each run instead.

--query is optional. Predictions without it are graded manually via `grade --agent world_analyst` + `mark`.
If your prediction says "by [date]", "on [date]", "through [month]", or "EOD", you MUST use `--verify-at` with an exact UTC ISO timestamp after the true resolution window.
High-confidence resolution predictions must mention the criteria/oracle basis directly in the prediction text or condition text (for example: "per market criteria", "per UMA oracle", "subject to liveness").
Set --verify-hours to 48-72h for non-calendar predictions. Shorter cycles = faster learning.

Good predictions:
  ✓ "Trade idea 0x... will resolve YES by [date]" — use trade_ideas query (SQLite)
  ✓ "Iran ceasefire will still be active in 72h" — omit --query, grade manually
  ✓ No prediction filed — evidence is mixed, both outcomes plausible (abstain)
Bad predictions:
  ✗ "Markets will be volatile" (too vague, not falsifiable)
  ✗ "Geopolitical tensions are rising" (not verifiable)
  ✗ --query "SELECT ... FROM world_events ..." (Supabase-only table — will always fail auto-grade)
  ✗ Predicting Trump/any leader WILL execute a military action based solely on their statement
  ✗ Predicting talks will collapse AND predicting talks will progress in the same run

  python3 agents/tools/predict_tool.py predict \
    --agent world_analyst \
    --prediction "..." \
    --condition "..." \
    --confidence high|medium|low --verify-hours 48 \
    --query "SQL to auto-verify"   # omit entirely for event-based predictions
  # For date/EOD predictions, replace --verify-hours with:
  #   --verify-at 2026-03-30T04:15:00Z
  # OR: skip this step entirely if no prediction clears the calibration rules above.

━━━ STEP 8: EXTRACT LESSONS ━━━
If 5+ predictions have been graded, look for accuracy patterns:
  python3 agents/tools/predict_tool.py learn \
    --agent world_analyst --lesson "..." --evidence "..." --accuracy N

━━━ STEP 8.5: CROSS-DOMAIN FLAGS ━━━
Check whether your analysis warrants flagging the finance domain. This step is MANDATORY — do
not skip it. Either create a flag or explicitly confirm none are needed.

Create a flag if ANY of the following are true based on what you found this run:
  • Oil (WTI/Brent) above $90, or moved >5% in the last 24h
  • Strait of Hormuz disrupted, blockaded, or under threat
  • US/EU sanctions imposed or escalated on an energy-producing country
  • Military action involving an oil-producing country or major shipping lane (Gulf, Red Sea, Suez)
  • Major political shock likely to affect equity/crypto risk sentiment (war declaration, regime
    collapse, unexpected election result)

If any condition is met:
  python3 agents/tools/domain_query.py --flag --from geopolitics --to finance \
    --urgency high \
    --message "[one precise sentence: event, price level if known, why it affects finance markets]"

  Example: "Hormuz toll day 29: WTI $99 (+6.6%), Houthi struck Israeli port — check oil/energy boosts"

If no conditions apply, write exactly: "Step 8.5: No geo→finance flags required this run."

Then clear any geo-directed flags from finance that you have already addressed:
  python3 agents/tools/domain_query.py --read-flags --domain geopolitics
  # For each addressed flag: python3 agents/tools/domain_query.py --clear-flag --flag-name "flag:..."

━━━ STEP 9: TELEGRAM SUMMARY ━━━
Send exactly ONE Telegram message. Use this template (fill in every field):

  🌍 World Analyst — [YYYY-MM-DD HH:MM UTC]
  • [Most important geopolitical or political development — one sentence, specific]
  • [Second insight — one sentence, include region/actor/event]
  • [Market implication — which market(s), which direction, why — one sentence]
  [OPTIONAL 4th bullet only if a wrong-sided position was detected]
  ⚠️ WRONG SIDE: [condition_id short] [outcome] entry=[entry_price] cur=[current_price] — [reason]

  📊 [N] boosts written | 💡 [N] trade ideas | 🎯 [prediction in ≤8 words, or "abstained" if none] | [⚠️ N flags OR ✅ no flags]

RULES:
  - Header MUST include UTC time (not just date) — use $(date -u '+%Y-%m-%d %H:%M UTC')
  - Exactly 3 bullets unless a wrong-sided position was found (then 4)
  - One sentence per bullet. No sub-clauses. No "and also". Ruthlessly cut.
  - Numbers must be EXACT: "7 boosts" not "several boosts", "$0.87 → $0.94" not "price rose"
  - Footer line is MANDATORY — never omit it
  - If you boosted 0 markets, write "📊 0 boosts written" (not "N/A" or blank)
  - If you created 0 trade ideas, write "💡 0 trade ideas"
  - Wrong-sided position = any open position where your boost direction OPPOSES the position outcome
    (e.g., position is outcome=YES but you wrote boost direction=NO → flag it)
  - NEVER send two Telegram messages — combine everything into one

Format: Be sharp. Hedge fund morning brief, not a news article. Every word earns its place.

━━━ STEP 10: REVIEW OWNED STRATEGIES ━━━
You own these strategies — review their performance each run:
  sqlite3 {DB_PATH} "SELECT s.id, s.label, s.status, \
    COUNT(CASE WHEN sp.state='open' THEN 1 END) as open_ct, \
    COUNT(CASE WHEN sp.state='closed' THEN 1 END) as closed_ct, \
    ROUND(SUM(CASE WHEN sp.state='closed' THEN sp.pnl_usdc ELSE 0 END), 2) as closed_pnl \
  FROM strategies s \
  LEFT JOIN strategy_positions sp ON CAST(s.id AS TEXT) = sp.strategy_id \
  WHERE s.id IN (21, 28) \
  GROUP BY s.id"

Review the results:
  - Political NO Value (#21): You own this because the edge comes from political outcome knowledge.
    If it has concerning PnL (< -$50 closed) or poor win rate (< 40%), flag it.
  - Geo Intel (#28): This strategy trades YOUR trade ideas. Check if ideas are converting.

If Political NO Value (#21) has concerning PnL or win rate:
  python3 agents/tools/action_tool.py flag --strategy-id 21 --reason "..."
If you have domain evidence to recommend parameter changes:
  python3 agents/tools/action_tool.py recommend --strategy-id 21 --strategy political_no_value_v1 \
    --recommendation "..." --reason "Based on [political event/analysis]"

If Geo Intel (#28) shows poor idea conversion, note it in your run_summary observation.

━━━ STEP 11: LOG RUN ━━━
Write a run_summary observation (required for brain health B5/E5 checks):
  python3 agents/tools/obs_tool.py write \
    --strategy world_analyst \
    --type run_summary \
    --confidence high \
    --content "Brief: key events found, N boosts written, N trade ideas created, [prediction: <text> OR abstained: <reason>]"

Then log the run metadata:
  python3 agents/tools/predict_tool.py log-run \
    --agent world_analyst --model {PRIMARY_MODEL} --cost 0.00 --turns 0 \
    --summary "Brief summary of what you found and did"
  (Fill in actual cost/turns/summary from this run)
"""


# ── Agent runner ──────────────────────────────────────────────────────────────

# Per-message inactivity timeout for the SDK async iterator. The CLI streams a
# message every time the model emits text OR starts/finishes a tool. A hung
# tool call (e.g. WebFetch on a slow polymarket.com URL) stops the stream. If
# no message arrives in this window, we abort the run so the service doesn't
# get killed by the outer systemd TimeoutStartSec. 180s is ~3x the longest
# observed healthy gap and well under the 600s service ceiling.
SDK_INACTIVITY_TIMEOUT_S = int(os.getenv("ANALYST_INACTIVITY_TIMEOUT_S", "180"))


async def _iter_with_inactivity_timeout(async_gen, timeout_s: int):
    """Yield items from ``async_gen``; raise TimeoutError if more than
    ``timeout_s`` seconds elapse between consecutive items.

    The first ``__anext__`` call is NOT wrapped in any timeout. That call
    triggers claude_agent_sdk's subprocess connect, which includes its own
    internal ``anyio.fail_after(2)`` version check and an ``anyio.TaskGroup``
    for stderr handling. Wrapping that startup in an outer ``anyio`` cancel
    scope entangles scope stacks across task boundaries and produces either
    ``CancelledError: Cancelled via cancel scope`` or ``Attempted to exit a
    cancel scope that isn't the current task's current cancel scope``.

    Subsequent ``__anext__`` calls use ``asyncio.wait_for`` (native asyncio
    cancellation — does not push onto anyio's cancel-scope stack) so the
    watchdog still catches hung tool calls (WebFetch on slow URLs, etc.)
    without breaking SDK startup.
    """
    import asyncio as _asyncio
    try:
        # First message: unbounded — let the SDK finish its connect + version
        # check + stderr task group spin-up without outer-scope interference.
        try:
            first = await async_gen.__anext__()
        except StopAsyncIteration:
            return
        yield first

        # Subsequent messages: inactivity watchdog via asyncio.wait_for.
        while True:
            try:
                item = await _asyncio.wait_for(async_gen.__anext__(), timeout=timeout_s)
            except StopAsyncIteration:
                return
            except _asyncio.TimeoutError:
                raise TimeoutError(
                    f"SDK stream inactive for >{timeout_s}s — likely a hung tool call"
                )
            yield item
    finally:
        try:
            await async_gen.aclose()
        except Exception:
            pass


async def run_analyst(task: str, quiet: bool = False) -> bool:
    """Run the world analyst as a Claude Code agent session. Returns True on success."""
    try:
        from claude_agent_sdk import query, ClaudeAgentOptions
        from claude_agent_sdk.types import ResultMessage, AssistantMessage
    except ImportError:
        logger.error("claude_agent_sdk not installed — run: pip install claude-agent-sdk")
        return False

    system_prompt, has_calibration = _compose_system_prompt()
    attempts: list[tuple[str, str, str]] = [
        (PRIMARY_MODEL, system_prompt, "primary"),
    ]
    if has_calibration:
        attempts.append((PRIMARY_MODEL, ANALYST_SYSTEM_PROMPT, "primary-no-calibration"))
    if FALLBACK_MODEL != PRIMARY_MODEL:
        attempts.append((FALLBACK_MODEL, ANALYST_SYSTEM_PROMPT, "fallback"))

    async def _run_once(model: str, prompt_text: str, label: str) -> tuple[bool, str | None, int, int, float, str]:
        opts = ClaudeAgentOptions(
            cwd=WORKING_DIR,
            model=model,
            permission_mode="bypassPermissions",
            system_prompt=prompt_text,
            setting_sources=[],
            max_turns=40,  # runaway guard; Max-plan auth means no billable spend
        )
        logger.info("🌍 World Analyst starting attempt=%s (model=%s)", label, model)
        start = datetime.now(timezone.utc)
        msg_count = 0
        tool_count = 0
        result_text = ""

        async for message in _iter_with_inactivity_timeout(
            query(prompt=task, options=opts), SDK_INACTIVITY_TIMEOUT_S
        ):
            msg_count += 1
            if isinstance(message, ResultMessage):
                result_text = message.result or ""
                stop = message.stop_reason
                elapsed = (datetime.now(timezone.utc) - start).total_seconds()
                logger.info(
                    "✅ Analyst done: attempt=%s stop=%s msgs=%d tools=%d elapsed=%.0fs",
                    label, stop, msg_count, tool_count, elapsed,
                )
                if result_text and not quiet:
                    logger.info("Result preview: %s", result_text[:500])
                return True, stop, msg_count, tool_count, elapsed, result_text

            content = getattr(message, "content", None)
            if isinstance(content, list):
                for block in content:
                    if "ToolUse" in type(block).__name__:
                        tool_count += 1
                        name = getattr(block, "name", "?")
                        if name in ("WebSearch", "WebFetch"):
                            inp = getattr(block, "input", {}) or {}
                            logger.info("  🌐 %s: %s", name, str(inp.get("query") or inp.get("url", ""))[:80])
                        elif name == "Bash":
                            inp = getattr(block, "input", {}) or {}
                            logger.info("  💻 %s", str(inp.get("command", ""))[:80])

        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        return False, None, msg_count, tool_count, elapsed, result_text

    last_error: Exception | None = None
    for idx, (model, prompt_text, label) in enumerate(attempts):
        try:
            ok, stop, msg_count, tool_count, elapsed, result_text = await _run_once(model, prompt_text, label)
        except Exception as exc:
            last_error = exc
            logger.warning("world_analyst attempt=%s failed: %s", label, exc)
            continue

        if ok and _should_retry_empty_stop(stop, tool_count, elapsed):
            logger.warning(
                "world_analyst: suspicious empty stop on attempt=%s stop=%s tools=%d elapsed=%.0fs; retrying",
                label,
                stop,
                tool_count,
                elapsed,
            )
            continue

        if ok:
            return True

        if idx < len(attempts) - 1:
            logger.warning("world_analyst attempt=%s ended without result; retrying", label)

    if last_error is not None:
        logger.error("Analyst error: %s", last_error)
    return False


# ── Quick validation: check brain was actually updated ────────────────────────
def _check_brain_updated() -> tuple[bool, bool, bool]:
    """Returns freshness for (world_state, world_narrative, market_boosts_current)."""
    try:
        return (
            _fresh_memory("world_state"),
            _fresh_memory("world_narrative"),
            _fresh_memory("market_boosts_current"),
        )
    except Exception:
        return False, False, False


# ── Main entry point ──────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Sniper World Intelligence Analyst")
    parser.add_argument("--quick", action="store_true",
                        help="Fast mode: geo + finance only, 2-3 events per domain")
    parser.add_argument("--domains", default="geo,politics",
                        help="Comma-separated domains (geo,politics — finance/crypto handled by Finance Analyst)")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress Telegram output (analysis still writes to brain)")
    parser.add_argument("--check", action="store_true",
                        help="Just check if brain was recently updated, exit 0=yes 1=no")
    parser.add_argument("--install-cron", action="store_true",
                        help="Install cron job to run world analyst every 4 hours")
    args = parser.parse_args()

    # Load .env
    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() and k.strip() not in os.environ:
                    os.environ[k.strip()] = v.strip()

    if args.install_cron:
        import subprocess as _sp
        cron_line = (
            f"0 */4 * * * cd {ROOT} && "
            f"{ROOT}/.venv/bin/python3 {ROOT}/scripts/world_analyst.py --quiet "
            f">> {ROOT}/logs/world_analyst.log 2>&1"
        )
        result = _sp.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
        if "world_analyst.py" in existing:
            print("✅ world_analyst cron already installed")
        else:
            new_crontab = existing.rstrip("\n") + "\n" + cron_line + "\n"
            _sp.run(["crontab", "-"], input=new_crontab, text=True, check=True)
            print(f"✅ Installed cron: {cron_line}")
        sys.exit(0)

    if args.check:
        ws, wn, mb = _check_brain_updated()
        status = (
            "✅ fresh"
            if (ws and wn and mb)
            else (
                "⚠️  stale "
                f"(world_state={'✅' if ws else '❌'}, "
                f"world_narrative={'✅' if wn else '❌'}, "
                f"boosts={'✅' if mb else '❌'})"
            )
        )
        print(f"Brain status: {status}")
        sys.exit(0 if (ws and wn and mb) else 1)

    # ── Concurrency guard ────────────────────────────────────────────────────
    # Prevents a second instance (e.g. event_trigger Popen) from running while
    # the scheduled systemd run is still active.  Uses a PID-file so a crash
    # that skips cleanup is still detected on the next launch.
    _lock_path = ROOT / "logs" / "world_analyst.lock"

    def _lock_stale(path: Path) -> bool:
        """Return True if lock file exists but the recorded PID is dead."""
        try:
            pid = int(path.read_text().strip())
            os.kill(pid, 0)   # signal 0 = existence check, no actual signal sent
            return False      # process alive → lock is live
        except ProcessLookupError:
            return True       # PID gone → stale
        except PermissionError:
            # PID exists but owned by a different user (e.g. root ran analyst manually).
            # Check /proc directly — readable by all users regardless of ownership.
            return not Path(f"/proc/{pid}").exists()
        except ValueError:
            return True       # file corrupt → stale

    if _lock_path.exists() and not _lock_stale(_lock_path):
        existing_pid = _lock_path.read_text().strip()
        logger.warning("world_analyst already running (PID %s) — exiting to avoid overlap", existing_pid)
        sys.exit(0)

    try:
        _lock_path.write_text(str(os.getpid()))
    except OSError as exc:
        logger.error("Cannot write lock file %s: %s — proceeding without lock", _lock_path, exc)

    import atexit as _atexit
    _atexit.register(lambda: _lock_path.unlink(missing_ok=True))
    # ── End concurrency guard ─────────────────────────────────────────────────

    domains = [d.strip().lower() for d in args.domains.split(",") if d.strip()]
    logger.info("Pre-fetching context for Steps 0–2 (saves ~15 agent turns)…")
    _prefetched = _prefetch_context(domains=domains, quick=args.quick)
    logger.info("Pre-fetch complete (%d chars)", len(_prefetched))
    task = build_task_prompt(domains=domains, quick=args.quick, prefetched=_prefetched)

    import asyncio as _asyncio

    def _quiet_exception_handler(loop, ctx):
        """Silence known asyncio teardown noise from claude_agent_sdk cleanup."""
        exc = ctx.get("exception")
        exc_str = str(exc) if exc else ""
        msg = ctx.get("message", "")
        if (
            "cancel scope" in exc_str.lower()
            or "event loop is closed" in exc_str.lower()
            or "cancel scope" in msg.lower()
        ):
            return  # silently swallow teardown noise — work is already done
        loop.default_exception_handler(ctx)

    async def _main():
        # Install quiet handler inside the running event loop so teardown is clean
        _asyncio.get_event_loop().set_exception_handler(_quiet_exception_handler)
        return await run_analyst(task=task, quiet=args.quiet)

    try:
        ok = anyio.run(_main)
    except RuntimeError as e:
        # Suppress "Event loop is closed" from anyio/subprocess cleanup — work is already done
        if "Event loop is closed" in str(e) or "cancel scope" in str(e):
            ok = True
        else:
            raise
    run_failed = not ok
    if run_failed:
        logger.error("World analyst run failed")
        # Do NOT exit here — fall through so the shell-fallback heartbeat
        # at the end of main() still writes an agent_runs row with
        # status='error'. Exiting here leaves B1 silent on failure.

    # Verify output was written. If the agent finished but missed the final
    # upserts, recover from temp files first, then synthesize from fresh events.
    ws_ok, wn_ok, mb_ok = _check_brain_updated()
    if not (ws_ok and wn_ok):
        rec_state, rec_narr = _recover_world_brain_from_tmp()
        if rec_state or rec_narr:
            logger.info(
                "Recovered world brain from temp files: world_state=%s world_narrative=%s",
                "✅" if rec_state else "❌",
                "✅" if rec_narr else "❌",
            )
            ws_ok, wn_ok, mb_ok = _check_brain_updated()
    if not (ws_ok and wn_ok):
        rec_state, rec_narr = _recover_world_brain_from_events()
        if rec_state or rec_narr:
            logger.info(
                "Recovered world brain from fresh events: world_state=%s world_narrative=%s",
                "✅" if rec_state else "❌",
                "✅" if rec_narr else "❌",
            )
            ws_ok, wn_ok, mb_ok = _check_brain_updated()

    if ws_ok and wn_ok and mb_ok:
        logger.info("✅ Brain updated: world_state ✅  world_narrative ✅  market_boosts ✅")
    else:
        logger.warning(
            "⚠️  Brain may not have been fully updated: world_state=%s  world_narrative=%s  boosts=%s",
            "✅" if ws_ok else "❌",
            "✅" if wn_ok else "❌",
            "✅" if mb_ok else "❌",
        )

    # Always write an agent_runs heartbeat at the shell level so B1 never
    # misses a run — even on failure (status='error' surfaces in the audit)
    # and even when Claude hit the tool limit before STEP 11.
    heartbeat_status = "error" if run_failed else "success"
    heartbeat_summary = (
        "[shell-fallback] run failed"
        if run_failed
        else "[shell-fallback] run completed"
    )
    try:
        import subprocess as _sp
        _sp.run(
            [
                str(ROOT / ".venv" / "bin" / "python3"),
                "agents/tools/predict_tool.py", "log-run",
                "--agent", "world_analyst",
                "--model", PRIMARY_MODEL,
                "--cost", "0.00",
                "--turns", "0",
                "--summary", heartbeat_summary,
                "--status", heartbeat_status,
            ],
            cwd=str(ROOT),
            capture_output=True,
            timeout=15,
        )
        logger.info("✅ agent_runs heartbeat written (status=%s)", heartbeat_status)
    except Exception as _e:
        logger.warning("shell log-run fallback failed: %s", _e)

    sys.exit(1 if run_failed else 0)


if __name__ == "__main__":
    main()

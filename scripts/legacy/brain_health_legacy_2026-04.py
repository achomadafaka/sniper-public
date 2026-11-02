#!/usr/bin/env python3
"""Brain Health Checker — deterministic validation of all brain subsystems.

⚠️ DECOMMISSIONED (2026-07-08): NOT scheduled anywhere. The systemd unit
`systemd/sniper-brain-health.service` points at the retired `/home/sniper/sniper`
VPS path and no local timer runs it. Many checks validate the frozen VPS
agent loop (`agent_runs`/`agent_predictions`/`agent_lessons`/`session_logs`,
last written ~2026-04-22), so a green report here is NOT authoritative — do not
infer live brain health from it. Kept for reference / possible revival. See the
`brain/dead-layer-inventory` memory. Live retrieval = `memories` (status=active)
via `search_memories_fts()`.

No LLM calls. Pure Python checks against Supabase (brain) and SQLite (trading DB).

Usage:
    python3 scripts/brain_health.py                # full check
    python3 scripts/brain_health.py --quiet        # no terminal output
    python3 scripts/brain_health.py --json         # JSON output only
"""
import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from html import escape as esc
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from sniper.services.stream_heartbeat import read_stream_heartbeat

# Load .env
_env = ROOT / ".env"
if _env.exists():
    for line in _env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            if k.strip() and k.strip() not in os.environ:
                os.environ[k.strip()] = v.strip()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("brain-health")

DB_PATH = os.getenv("DB_PATH", str(ROOT / "sniper_tracker.db"))
SB_URL = os.getenv("SUPABASE_URL", "")
SB_KEY = os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_AI", os.getenv("TELEGRAM_BOT_TOKEN", ""))
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")

# Calibration loop v1 — CAL1 check scope
try:
    import sniper.settings.web as _ws
    CALIBRATION_ENABLED_AGENTS = list(getattr(_ws, "CALIBRATION_ENABLED_AGENTS", ["insider_copy"]))
except Exception:
    CALIBRATION_ENABLED_AGENTS = ["insider_copy"]


# ── Result types ──────────────────────────────────────────────────────────

@dataclass
class CheckResult:
    check_id: str          # e.g. "A1"
    name: str              # human-readable
    severity: str          # PASS, WARN, FAIL
    message: str           # details
    category: str          # A-G


@dataclass
class HealthReport:
    timestamp: str
    checks: list[CheckResult] = field(default_factory=list)
    pass_count: int = 0
    warn_count: int = 0
    fail_count: int = 0

    def add(self, result: CheckResult):
        self.checks.append(result)
        if result.severity == "PASS":
            self.pass_count += 1
        elif result.severity == "WARN":
            self.warn_count += 1
        else:
            self.fail_count += 1

    @property
    def overall(self) -> str:
        if self.fail_count > 0:
            return "UNHEALTHY"
        if self.warn_count > 0:
            return "DEGRADED"
        return "HEALTHY"


# ── Supabase helpers ──────────────────────────────────────────────────────

def sb_get(path: str) -> Any:
    """GET from Supabase REST API. Returns parsed JSON or empty list."""
    if not SB_URL or not SB_KEY:
        return []
    url = f"{SB_URL}/rest/v1/{path}"
    req = urllib.request.Request(url, headers={
        "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.warning("sb_get %s failed: %s", path, e)
        return []


def sb_count(table: str) -> int:
    """Count rows in a Supabase table."""
    if not SB_URL or not SB_KEY:
        return -1
    url = f"{SB_URL}/rest/v1/{table}?select=id&limit=1"
    req = urllib.request.Request(url, headers={
        "apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}",
        "Prefer": "count=exact", "Range": "0-0",
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            cr = resp.headers.get("content-range", "")
            return int(cr.split("/")[-1]) if "/" in cr else 0
    except Exception:
        return -1


def sb_memory(name: str) -> dict | None:
    """Fetch a single memory by name — always returns the most recently updated row."""
    rows = sb_get(f"memories?select=*&name=eq.{urllib.parse.quote(name)}&order=updated_at.desc&limit=1")
    return rows[0] if rows else None


# ── SQLite helper ─────────────────────────────────────────────────────────

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# ── Timestamp helpers ─────────────────────────────────────────────────────

def parse_ts(raw: Any) -> datetime | None:
    """Parse ISO timestamp string to datetime. Returns None on failure."""
    if not raw:
        return None
    s = str(raw).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def hours_ago(dt: datetime | None) -> float:
    """Hours since the given datetime. Returns 9999 if None."""
    if not dt:
        return 9999.0
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600


NOW = datetime.now(timezone.utc)
CONDITION_ID_RE = re.compile(r"^0x[a-f0-9]{64}$")
MIN_COST_TREND_DELTA_USD = 0.25
MIN_TURN_TREND_DELTA = 10
MIN_TURN_TREND_BASELINE_RUNS = 3


def canonical_condition_id(key: Any) -> str | None:
    """Return a normalized condition_id or None for malformed boost keys."""
    raw = str(key or "").strip().lower()
    if "__" in raw:
        raw = raw.split("__", 1)[0]
    if not raw or not CONDITION_ID_RE.fullmatch(raw):
        return None
    return raw


# ══════════════════════════════════════════════════════════════════════════
# Category A: Staleness (A1-A5)
# ══════════════════════════════════════════════════════════════════════════

def check_a0_stream_staleness(report: HealthReport):
    """A0: CLOB stream freshness — detect silent disconnects.

    Set `LEGACY_STREAM_FROZEN=1` when sniper-stream is intentionally stopped
    (post-2026-04-29 freeze). The check then reports PASS with a "frozen"
    note instead of FAIL/WARN — appropriate during the analyzer cutover when
    the legacy CLOB streamer is masked.

    The `low_buy_trades` fallback has been removed: the table is part of
    the legacy stack (see `docs/audits/2026-04-29-legacy-remnant-audit.md`
    section 1.7) and stream_heartbeat is the authoritative freshness signal.
    """
    if os.getenv("LEGACY_STREAM_FROZEN", "").strip().lower() in {"1", "true", "yes"}:
        report.add(CheckResult(
            "A0", "Stream staleness", "PASS",
            "Legacy stream intentionally frozen (LEGACY_STREAM_FROZEN=1); "
            "no freshness check applicable",
            "A",
        ))
        return
    try:
        heartbeat = read_stream_heartbeat()
        last_trade_ts = float(heartbeat.get("last_ws_message_ts") or 0.0)
        if not last_trade_ts:
            report.add(CheckResult(
                "A0", "Stream staleness", "WARN",
                "No stream heartbeat — stream may be intentionally stopped "
                "(set LEGACY_STREAM_FROZEN=1 to silence) or genuinely down",
                "A",
            ))
            return
        age_min = (NOW.timestamp() - last_trade_ts) / 60
        if age_min > 60:
            report.add(CheckResult("A0", "Stream staleness", "FAIL",
                f"CLOB stream stale: heartbeat {age_min:.0f}min ago (>60min)", "A"))
        elif age_min > 30:
            report.add(CheckResult("A0", "Stream staleness", "WARN",
                f"CLOB stream possibly stale: heartbeat {age_min:.0f}min ago", "A"))
        else:
            report.add(CheckResult("A0", "Stream staleness", "PASS",
                f"Stream healthy: heartbeat {age_min:.0f}min ago", "A"))
    except Exception as e:
        report.add(CheckResult("A0", "Stream staleness", "WARN", f"Could not check: {e}", "A"))


def check_a1_boost_expiry(report: HealthReport):
    """A1: market_boosts_current expired entries."""
    mem = sb_memory("market_boosts_current")
    if not mem:
        report.add(CheckResult("A1", "Boost expiry", "WARN", "market_boosts_current not found in brain", "A"))
        return
    updated = parse_ts(mem.get("updated_at"))
    try:
        boosts = json.loads(mem.get("content", "{}")) if isinstance(mem.get("content"), str) else mem.get("content", {})
    except json.JSONDecodeError:
        report.add(CheckResult("A1", "Boost expiry", "FAIL", "market_boosts_current content is invalid JSON", "A"))
        return
    if not boosts:
        report.add(CheckResult("A1", "Boost expiry", "PASS", "No active boosts", "A"))
        return
    expired = []
    for cid, info in boosts.items():
        exp_h = float(info.get("expires_hours", 24))
        if updated and hours_ago(updated) > exp_h:
            expired.append(cid[:12])
    if len(expired) == len(boosts):
        report.add(CheckResult("A1", "Boost expiry", "FAIL", f"ALL {len(expired)} boosts expired", "A"))
    elif expired:
        report.add(CheckResult("A1", "Boost expiry", "WARN", f"{len(expired)}/{len(boosts)} expired: {', '.join(expired[:3])}", "A"))
    else:
        report.add(CheckResult("A1", "Boost expiry", "PASS", f"{len(boosts)} boosts, none expired", "A"))


def check_a2_world_state_freshness(report: HealthReport):
    """A2: world_state freshness."""
    mem = sb_memory("world_state")
    if not mem:
        report.add(CheckResult("A2", "World state freshness", "FAIL", "world_state not found", "A"))
        return
    age_h = hours_ago(parse_ts(mem.get("updated_at")))
    if age_h > 10:
        report.add(CheckResult("A2", "World state freshness", "FAIL", f"world_state is {age_h:.1f}h old (>10h)", "A"))
    elif age_h > 5:
        report.add(CheckResult("A2", "World state freshness", "WARN", f"world_state is {age_h:.1f}h old (>6h)", "A"))
    else:
        report.add(CheckResult("A2", "World state freshness", "PASS", f"world_state is {age_h:.1f}h old", "A"))


def check_a3_stale_world_events(report: HealthReport):
    """A3: world_events with active status but old last_updated.

    Threshold is 72h (not 48h) because geopolitical events routinely remain
    active for several days without needing a content update. world_analyst
    reads but does not touch last_updated on events it reviews, so 48h was
    producing constant false positives on legitimately long-running events.
    """
    events = sb_get("world_events?select=slug,status,last_updated&status=in.(active,escalating,developing)")
    if not events:
        report.add(CheckResult("A3", "World events staleness", "PASS", "No active world events", "A"))
        return
    stale = [e for e in events if hours_ago(parse_ts(e.get("last_updated"))) > 72]
    if stale:
        slugs = [e.get("slug", "?")[:20] for e in stale[:3]]
        report.add(CheckResult("A3", "World events staleness", "WARN", f"{len(stale)} events >72h old: {', '.join(slugs)}", "A"))
    else:
        report.add(CheckResult("A3", "World events staleness", "PASS", f"{len(events)} active events, all fresh", "A"))


def check_a4_regime_freshness(report: HealthReport):
    """A4: strategy_regimes_current freshness."""
    mem = sb_memory("strategy_regimes_current")
    if not mem:
        report.add(CheckResult("A4", "Regime freshness", "PASS", "No strategy_regimes_current entry", "A"))
        return
    age_h = hours_ago(parse_ts(mem.get("updated_at")))
    if age_h > 12:
        report.add(CheckResult("A4", "Regime freshness", "WARN", f"strategy_regimes_current is {age_h:.1f}h old", "A"))
    else:
        report.add(CheckResult("A4", "Regime freshness", "PASS", f"{age_h:.1f}h old", "A"))


def check_a5_orphan_wallet_references(report: HealthReport):
    """A5: recent observations referencing wallets not in user_analysis."""
    cutoff = (NOW - timedelta(hours=24)).isoformat()
    obs = sb_get(f"strategy_agent_observations?select=content,strategy_type&created_at=gte.{urllib.parse.quote(cutoff)}&limit=50")
    if not obs:
        report.add(CheckResult("A5", "Orphan wallet refs", "PASS", "No recent observations to check", "A"))
        return
    # Extract 0x addresses from observation content
    mentioned = set()
    for o in obs:
        addrs = re.findall(r'\b0x[a-fA-F0-9]{40}\b', str(o.get("content", "")))
        mentioned.update(a.lower() for a in addrs)
    if not mentioned:
        report.add(CheckResult("A5", "Orphan wallet refs", "PASS", "No wallet addresses in recent observations", "A"))
        return
    conn = db_conn()
    existing = set()
    for row in conn.execute("SELECT wallet FROM user_analysis").fetchall():
        existing.add(row[0].lower())
    # Wallets already queued for analysis are not orphans — they're just pending
    for row in conn.execute("SELECT wallet FROM wallet_analysis_queue").fetchall():
        existing.add(row[0].lower())
    conn.close()
    orphans = mentioned - existing
    # Filter out short partial matches and wallets mentioned in STOPPED context (expected to be gone)
    orphans = {a for a in orphans if len(a) >= 20}
    stopped_content = " ".join(str(o.get("content", "")) for o in obs if "STOPPED" in str(o.get("content", "")).upper())
    orphans = {a for a in orphans if a not in stopped_content.lower()}
    # Filter wallets explicitly flagged as speculator_bot or false_positive in observations — these are known-bad
    bad_flag_content = " ".join(str(o.get("content", "")) for o in obs
                                if any(kw in str(o.get("content", "")).upper()
                                       for kw in ("SPECULATOR_BOT", "FALSE_POSITIVE", "FALSE POSITIVE")))
    orphans = {a for a in orphans if a not in bad_flag_content.lower()}
    if orphans:
        report.add(CheckResult("A5", "Orphan wallet refs", "WARN", f"{len(orphans)} wallets in observations not in user_analysis", "A"))
    else:
        report.add(CheckResult("A5", "Orphan wallet refs", "PASS", f"All {len(mentioned)} referenced wallets exist", "A"))


# ══════════════════════════════════════════════════════════════════════════
# Category B: Agent Heartbeats (B1-B5)
# ══════════════════════════════════════════════════════════════════════════

def _check_agent_heartbeat(report: HealthReport, check_id: str, agent_type: str,
                            warn_hours: float, fail_hours: float):
    """Generic agent heartbeat check."""
    runs = sb_get(f"agent_runs?select=created_at&agent_type=eq.{agent_type}&order=created_at.desc&limit=1")
    if not runs:
        report.add(CheckResult(check_id, f"{agent_type} heartbeat", "FAIL", f"No agent_runs found for {agent_type}", "B"))
        return
    age_h = hours_ago(parse_ts(runs[0].get("created_at")))
    if age_h > fail_hours:
        report.add(CheckResult(check_id, f"{agent_type} heartbeat", "FAIL", f"Last run {age_h:.1f}h ago (>{fail_hours}h)", "B"))
    elif age_h > warn_hours:
        report.add(CheckResult(check_id, f"{agent_type} heartbeat", "WARN", f"Last run {age_h:.1f}h ago (>{warn_hours}h)", "B"))
    else:
        report.add(CheckResult(check_id, f"{agent_type} heartbeat", "PASS", f"Last run {age_h:.1f}h ago", "B"))


def check_b1(report):
    # world_analyst is scheduled every 4h; warn at 4.5h to avoid false positives
    # at the exact boundary when health check runs mid-cycle.
    _check_agent_heartbeat(report, "B1", "world_analyst", 4.5, 8)


def check_b2(report):
    _check_agent_heartbeat(report, "B2", "wallet_scout", 18, 36)


def check_b3(report):
    _check_agent_heartbeat(report, "B3", "insider_copy", 14, 26)


def check_b4_news_intel_heartbeat(report: HealthReport):
    """B4: news_intel timer — check SQLite news_items freshness."""
    conn = db_conn()
    try:
        row = conn.execute("SELECT MAX(published_at) as latest FROM news_items").fetchone()
        latest = parse_ts(row["latest"]) if row else None
        age_h = hours_ago(latest)
        if age_h > 8:
            report.add(CheckResult("B4", "news_intel heartbeat", "FAIL", f"Latest news {age_h:.1f}h old (>8h)", "B"))
        elif age_h > 4:
            report.add(CheckResult("B4", "news_intel heartbeat", "WARN", f"Latest news {age_h:.1f}h old (>4h)", "B"))
        else:
            report.add(CheckResult("B4", "news_intel heartbeat", "PASS", f"Latest news {age_h:.1f}h old", "B"))
    except Exception as e:
        report.add(CheckResult("B4", "news_intel heartbeat", "WARN", f"Query failed: {e}", "B"))
    finally:
        conn.close()


def check_b6_finance_analyst_heartbeat(report: HealthReport):
    """B6: finance_analyst scheduled every 4h — check markets/state freshness.

    finance_analyst writes the 'markets/state' Supabase memory entry on every
    successful run.  Unlike world_analyst it does NOT write to agent_runs, so
    we infer liveness from the brain entry's updated_at timestamp.
    Warn at 4.5h (one cycle + 30 min buffer), fail at 10h (two missed cycles).
    """
    mem = sb_memory("markets/state")
    if not mem:
        report.add(CheckResult("B6", "finance_analyst heartbeat", "WARN", "markets/state not found in brain", "B"))
        return
    age_h = hours_ago(parse_ts(mem.get("updated_at")))
    if age_h > 10:
        report.add(CheckResult("B6", "finance_analyst heartbeat", "FAIL", f"markets/state is {age_h:.1f}h old (>10h)", "B"))
    elif age_h > 4.5:
        report.add(CheckResult("B6", "finance_analyst heartbeat", "WARN", f"markets/state is {age_h:.1f}h old (>4.5h)", "B"))
    else:
        report.add(CheckResult("B6", "finance_analyst heartbeat", "PASS", f"markets/state is {age_h:.1f}h old", "B"))


def check_b5_incomplete_runs(report: HealthReport):
    """B5: agent runs without matching run_summary observation."""
    cutoff = (NOW - timedelta(hours=48)).isoformat()
    runs = sb_get(f"agent_runs?select=agent_type,created_at&created_at=gte.{urllib.parse.quote(cutoff)}&order=created_at.desc")
    if not runs:
        report.add(CheckResult("B5", "Incomplete runs", "PASS", "No recent runs", "B"))
        return
    obs = sb_get(f"strategy_agent_observations?select=strategy_type,created_at&obs_type=eq.run_summary&created_at=gte.{urllib.parse.quote(cutoff)}")
    # Map agent_type to strategy_type (wallet_scout -> wallet_scout, insider_copy -> insider_copy_v1)
    agent_to_strat = {"wallet_scout": "wallet_scout", "insider_copy": "insider_copy_v1"}
    obs_times = {}
    for o in (obs or []):
        st = o.get("strategy_type", "")
        obs_times.setdefault(st, []).append(parse_ts(o.get("created_at")))
    incomplete = []
    for r in runs:
        at = r.get("agent_type", "")
        st = agent_to_strat.get(at, at)
        run_time = parse_ts(r.get("created_at"))
        if not run_time:
            continue
        # Check if there's a run_summary within 30min of this run
        matched = False
        for obs_t in obs_times.get(st, []):
            if obs_t and abs((obs_t - run_time).total_seconds()) < 1800:
                matched = True
                break
        if not matched:
            incomplete.append(at)
    # world_analyst and finance_analyst write to memories table, not strategy_agent_observations
    incomplete = [a for a in incomplete if a not in ("world_analyst", "finance_analyst")]
    if incomplete:
        report.add(CheckResult("B5", "Incomplete runs", "WARN", f"{len(incomplete)} runs without run_summary: {', '.join(incomplete[:5])}", "B"))
    else:
        report.add(CheckResult("B5", "Incomplete runs", "PASS", f"{len(runs)} runs all have summaries", "B"))


# ══════════════════════════════════════════════════════════════════════════
# Category C: Prediction Hygiene (C1-C3)
# ══════════════════════════════════════════════════════════════════════════

def check_c1_ungraded_predictions(report: HealthReport):
    """C1: predictions without a verification_query — the root cause of ungradeable
    backlog.

    Previously this check duplicated F4 (both counted the same "overdue" set). Now
    it focuses on the *design issue*: agent prompts require `--query` on every
    prediction, but ones without it can never be auto-graded — they silently
    accumulate until `predict_tool.py cleanup` expires them after 7d grace.

    F4 remains the raw backlog-size monitor; C1 is the quality monitor.
    """
    now_iso = NOW.isoformat()
    # Overdue (past verify_after) AND ungradeable (no verification_query).
    preds = sb_get(
        "agent_predictions?select=id,agent_type"
        "&was_correct=is.null"
        "&verification_query=is.null"
        f"&verify_after=lt.{urllib.parse.quote(now_iso)}"
    )
    count = len(preds) if preds else 0
    # Daily sniper-predict-cleanup.timer expires these 7d after verify_after.
    # High counts here = either cleanup timer is broken, or a burst of manual-
    # grade predictions (world/macro events) needs human grading.
    if count > 10:
        agents = sorted({p.get("agent_type", "?") for p in preds})
        report.add(CheckResult("C1", "Ungradeable predictions", "FAIL",
            f"{count} overdue preds have NULL verification_query ({', '.join(agents)}). "
            "Check sniper-predict-cleanup.timer is active; grade manually via "
            "`predict_tool.py grade --agent <name>` + `mark` for Type-B calls.", "C"))
    elif count > 3:
        agents = sorted({p.get("agent_type", "?") for p in preds})
        report.add(CheckResult("C1", "Ungradeable predictions", "WARN",
            f"{count} overdue preds with NULL verification_query ({', '.join(agents)}) "
            "— awaiting manual grading or next cleanup run", "C"))
    else:
        report.add(CheckResult("C1", "Ungradeable predictions", "PASS",
            f"{count} overdue preds missing query (tolerable)", "C"))


def check_c2_accuracy_declining(report: HealthReport):
    """C2: per-agent accuracy declining (last 10 vs overall)."""
    preds = sb_get("agent_predictions?select=agent_type,was_correct,created_at&was_correct=not.is.null&order=created_at.desc&limit=200")
    if not preds:
        report.add(CheckResult("C2", "Accuracy trend", "PASS", "No graded predictions yet", "C"))
        return
    by_agent: dict[str, list[bool]] = {}
    for p in preds:
        at = p.get("agent_type", "?")
        wc = p.get("was_correct")
        if wc is not None:
            by_agent.setdefault(at, []).append(bool(wc))
    declining = []
    for agent, results in by_agent.items():
        if len(results) < 10:
            continue
        overall_acc = sum(results) / len(results)
        recent_acc = sum(results[:10]) / 10
        if overall_acc - recent_acc > 0.15:
            declining.append(f"{agent}: {recent_acc:.0%} recent vs {overall_acc:.0%} overall")
    if declining:
        report.add(CheckResult("C2", "Accuracy trend", "WARN", "; ".join(declining), "C"))
    else:
        report.add(CheckResult("C2", "Accuracy trend", "PASS", f"Accuracy stable across {len(by_agent)} agents", "C"))


def check_c3_zero_lessons(report: HealthReport):
    """C3: no lessons learned despite enough graded predictions."""
    lesson_count = sb_count("agent_lessons")
    pred_graded = len(sb_get("agent_predictions?select=id&was_correct=not.is.null") or [])
    if lesson_count == 0 and pred_graded > 15:
        report.add(CheckResult("C3", "Zero lessons", "WARN", f"{pred_graded} predictions graded but 0 lessons recorded", "C"))
    elif lesson_count == 0 and pred_graded <= 15:
        report.add(CheckResult("C3", "Zero lessons", "PASS", f"Too few graded ({pred_graded}) for lessons — expected", "C"))
    else:
        report.add(CheckResult("C3", "Zero lessons", "PASS", f"{lesson_count} lessons from {pred_graded} graded predictions", "C"))


NEGATION_MARKERS: tuple[str, ...] = (
    "will not", "would not", "no longer",
    "won't", "wont", "can't", "cannot",
    "not", "without", "never", "absent", "denied", "reject", "rejected",
    "lapse", "lapses", "lapsing", "lapsed",
    "fail", "fails", "failing", "failed",
    "collapse", "collapses", "collapsing", "collapsed",
)

# Word-boundary regex so "another" (contains "not") and similar substrings
# do not trigger false positives. Multi-word markers like "will not" are
# matched as whole phrases.
_NEGATION_RE = re.compile(
    r"(?<![a-z])(?:" + "|".join(re.escape(m) for m in NEGATION_MARKERS) + r")(?![a-z])",
    re.IGNORECASE,
)


def _has_negation_near(text: str, kw: str, window: int = 40) -> bool:
    """Return True if any NEGATION_MARKERS term lies within ``window``
    characters of the first occurrence of ``kw`` in ``text``.

    Both ``text`` and ``kw`` are lowercased before matching. Word-boundary
    aware — "another" will not trigger "not". Used by C4's Pattern-2
    progress/collapse classifier so that progress keywords like "extension"
    or "agreement" do not falsely count a prediction as progress-side when
    the surrounding context negates them (e.g. "without extension",
    "agreement will lapse").
    """
    t = text.lower()
    k = kw.lower()
    pos = t.find(k)
    if pos < 0:
        return False
    start = max(0, pos - window)
    end = pos + len(k) + window
    return _NEGATION_RE.search(t[start:end]) is not None


def check_c4_world_analyst_regression(report: HealthReport):
    """C4: world_analyst prompt-regression watch — detects if the old failure cluster is reappearing.

    Watches for three specific patterns diagnosed in commit 113494b:
      1. Escalation overconfidence — high-confidence Trump/Iran military-action call
         based solely on rhetoric/ultimatum language (no operational evidence markers).
      2. Bidirectional same-event — two predictions on the same Iran-talks/ceasefire
         window, one predicting progress and another predicting collapse.
      3. Ungrounded market-resolution — high-confidence Polymarket resolution call
         without resolution-criteria language present.

    Fires WARN on first hit, FAIL if two or more patterns triggered simultaneously.
    Only examines world_analyst predictions from the last 7 days (filed + ungraded
    are both relevant — the bad pattern is filing, not the outcome).

    POST_PATCH_FLOOR: predictions before this date are excluded — they are the
    pre-patch cluster that motivated this check, not evidence of regression.
    Update this constant when a new round of prompt surgery is applied.
    """
    # 2026-03-26: day after the last known pre-patch bidirectional cluster
    POST_PATCH_FLOOR = "2026-03-26T00:00:00+00:00"
    rolling_cutoff = (NOW - timedelta(days=7)).isoformat()
    # Use whichever floor is later: the 7-day rolling window or the patch floor
    cutoff = POST_PATCH_FLOOR if POST_PATCH_FLOOR > rolling_cutoff else rolling_cutoff
    preds = sb_get(
        f"agent_predictions?select=id,prediction,confidence,created_at"
        f"&agent_type=eq.world_analyst&created_at=gte.{urllib.parse.quote(cutoff)}"
        f"&order=created_at.desc&limit=50"
    ) or []

    triggered = []

    # ── Pattern 1: escalation overconfidence ──────────────────────────────
    ESCALATION_ACTORS   = ("trump", "us forces", "pentagon", "biden", "administration")
    ESCALATION_ACTIONS  = ("will strike", "will bomb", "will attack", "will invade",
                           "will execute strikes", "will order strikes", "will order",
                           "will launch")
    OPERATIONAL_MARKERS = ("deployment", "staging", "airlift", "satellite",
                           "aumf", "congressional notification", "troops moving",
                           "confirmed movement", "logistics")

    for p in preds:
        if p.get("confidence") != "high":
            continue
        text = (p.get("prediction") or "").lower()
        is_escalation = (
            any(actor in text for actor in ESCALATION_ACTORS) and
            any(action in text for action in ESCALATION_ACTIONS)
        )
        has_operational = any(marker in text for marker in OPERATIONAL_MARKERS)
        if is_escalation and not has_operational:
            triggered.append(
                f"P1-escalation: {p['id'][:8]} ({str(p['created_at'])[:10]}) "
                f"high-conf strike/ultimatum call with no operational evidence"
            )

    # ── Pattern 2: bidirectional same-event (Iran talks/ceasefire) ────────
    IRAN_TALKS_KEYWORDS  = ("iran", "ceasefire", "ceasefire talks", "hormuz")
    PROGRESS_KEYWORDS    = ("progress", "framework", "extension", "agreement",
                            "produce", "result in", "will reach", "will occur")
    COLLAPSE_KEYWORDS    = ("collapse", "fail", "will not reach", "will not produce",
                            "will not result", "denied", "no agreement")

    iran_progress = []
    iran_collapse = []
    for p in preds:
        text = (p.get("prediction") or "").lower()
        if not any(kw in text for kw in IRAN_TALKS_KEYWORDS):
            continue
        matched_progress = [kw for kw in PROGRESS_KEYWORDS if kw in text]
        head_negated = _NEGATION_RE.search(text[:80]) is not None
        if matched_progress and not head_negated and not any(
            _has_negation_near(text, kw) for kw in matched_progress
        ):
            iran_progress.append(p["id"][:8])
        if any(kw in text for kw in COLLAPSE_KEYWORDS):
            iran_collapse.append(p["id"][:8])

    if iran_progress and iran_collapse:
        triggered.append(
            f"P2-bidirectional: progress-side={iran_progress[:2]} collapse-side={iran_collapse[:2]} "
            f"— contradictory Iran-talks predictions in same 7d window"
        )

    # ── Pattern 3: ungrounded market-resolution (high-conf, no criteria) ─
    RESOLUTION_KEYWORDS  = ("will resolve yes", "will resolve no", "resolves yes",
                             "resolves no", "resolve yes", "resolve no")
    CRITERIA_MARKERS     = ("uma", "oracle", "criteria", "condition", "per market",
                             "threshold", "resolution source", "liveness", "per rules")

    for p in preds:
        if p.get("confidence") != "high":
            continue
        text = (p.get("prediction") or "").lower()
        has_resolution_call = any(kw in text for kw in RESOLUTION_KEYWORDS)
        has_market_ref = "0x" in text or "market" in text
        has_criteria   = any(m in text for m in CRITERIA_MARKERS)
        is_brief       = len(text) < 120  # short texts lack grounding; longer ones likely have it
        if has_resolution_call and has_market_ref and not has_criteria and is_brief:
            triggered.append(
                f"P3-resolution: {p['id'][:8]} ({str(p['created_at'])[:10]}) "
                f"high-conf market-resolution call with no criteria language"
            )

    if not triggered:
        report.add(CheckResult("C4", "Regression watch", "PASS",
                               "No prompt-regression patterns detected in last 7d world_analyst predictions", "C"))
    elif len(triggered) >= 2:
        report.add(CheckResult("C4", "Regression watch", "FAIL",
                               f"{len(triggered)} regression patterns: {'; '.join(triggered[:2])}", "C"))
    else:
        report.add(CheckResult("C4", "Regression watch", "WARN",
                               f"1 regression pattern: {triggered[0]}", "C"))


def check_c5_c2_cluster_worsening(report: HealthReport):
    """C5: C2 cluster worsening — fires if world_analyst recent accuracy drops to ≤30%
    (10pp below the current 40% baseline) driven by the same failure cluster.

    C2 already fires at >15pp delta vs overall.  C5 adds a harder floor: if the
    recent window falls to ≤30%, that is a signal the patch is not holding and
    a stronger intervention is needed.  Fires FAIL (not WARN) to distinguish it
    from the softer C2 signal.
    """
    preds = sb_get(
        "agent_predictions?select=agent_type,was_correct,created_at,prediction,confidence"
        "&agent_type=eq.world_analyst&was_correct=not.is.null"
        "&order=created_at.desc&limit=200"
    ) or []

    if len(preds) < 10:
        report.add(CheckResult("C5", "C2 cluster floor", "PASS",
                               "Too few graded predictions for cluster floor check", "C"))
        return

    results   = [bool(p["was_correct"]) for p in preds]
    recent_10 = results[:10]
    recent_acc = sum(recent_10) / 10

    if recent_acc > 0.30:
        report.add(CheckResult("C5", "C2 cluster floor", "PASS",
                               f"world_analyst recent accuracy {recent_acc:.0%} above 30% floor", "C"))
        return

    CLUSTER_WORDS = ("trump", "iran", "strike", "ultimatum", "ceasefire",
                     "power plant", "invade", "$120", "hormuz")
    cluster_misses = [
        p for p in preds[:10]
        if not p["was_correct"] and
        any(w in (p.get("prediction") or "").lower() for w in CLUSTER_WORDS)
    ]
    cluster_note = (
        f"{len(cluster_misses)}/10 recent misses are Iran/Trump cluster"
        if cluster_misses else "cluster not identified"
    )
    report.add(CheckResult("C5", "C2 cluster floor", "FAIL",
                           f"world_analyst recent accuracy {recent_acc:.0%} ≤30% floor — patch may not be holding; {cluster_note}", "C"))


# ══════════════════════════════════════════════════════════════════════════
# Category D: Cross-Agent Coherence (D1-D5)
# ══════════════════════════════════════════════════════════════════════════

def check_d1_scout_stop_vs_insider_thesis(report: HealthReport):
    """D1: Scout stopped a strategy that Insider Copy still references positively."""
    cutoff = (NOW - timedelta(hours=48)).isoformat()
    scout_obs = sb_get(f"strategy_agent_observations?select=content&strategy_type=eq.wallet_scout&obs_type=eq.action_taken&created_at=gte.{urllib.parse.quote(cutoff)}")
    insider_obs = sb_get(f"strategy_agent_observations?select=content&strategy_type=eq.insider_copy_v1&created_at=gte.{urllib.parse.quote(cutoff)}")
    stopped_ids = set()
    for o in (scout_obs or []):
        content = str(o.get("content", ""))
        if "STOPPED" in content.upper():
            ids = re.findall(r'id=(\d+)', content)
            stopped_ids.update(ids)
    if not stopped_ids:
        report.add(CheckResult("D1", "Scout-Insider coherence", "PASS", "No recent stops by Scout", "D"))
        return
    conflicts = []
    for o in (insider_obs or []):
        content = str(o.get("content", ""))
        for sid in stopped_ids:
            if sid in content and "healthy" in content.lower():
                conflicts.append(sid)
    if conflicts:
        report.add(CheckResult("D1", "Scout-Insider coherence", "WARN", f"Insider Copy references stopped strategies: {conflicts}", "D"))
    else:
        report.add(CheckResult("D1", "Scout-Insider coherence", "PASS", f"No conflicts (Scout stopped: {stopped_ids})", "D"))


def check_d2_negative_boost_on_open_position(report: HealthReport):
    """D2: market_boosts has strong negative boost on an open position."""
    mem = sb_memory("market_boosts_current")
    if not mem:
        report.add(CheckResult("D2", "Boost vs positions", "PASS", "No boosts", "D"))
        return
    try:
        boosts = json.loads(mem.get("content", "{}")) if isinstance(mem.get("content"), str) else mem.get("content", {})
    except Exception:
        report.add(CheckResult("D2", "Boost vs positions", "WARN", "Could not parse boosts", "D"))
        return
    negative = {cid: info for cid, info in boosts.items() if float(info.get("boost", 0)) <= -20}
    if not negative:
        report.add(CheckResult("D2", "Boost vs positions", "PASS", "No strongly negative boosts", "D"))
        return
    conn = db_conn()
    conflicts = []
    for cid in negative:
        # Only flag when position OUTCOME conflicts with boost direction.
        # negative boost = bearish on YES — confirms a NO position, conflicts with a YES position.
        rows = conn.execute(
            "SELECT DISTINCT outcome FROM strategy_positions WHERE condition_id=? AND state='open'", (cid,)
        ).fetchall()
        for row in rows:
            outcome = (row["outcome"] or "").upper()
            if outcome == "YES":
                # YES position vs bearish-on-YES boost → genuine conflict
                conflicts.append(f"{cid[:12]}(boost={negative[cid].get('boost')},pos=YES)")
            # NO position vs bearish-on-YES boost → aligned, skip
    conn.close()
    if conflicts:
        report.add(CheckResult("D2", "Boost vs positions", "WARN", f"Open YES positions against negative boost: {', '.join(conflicts)}", "D"))
    else:
        report.add(CheckResult("D2", "Boost vs positions", "PASS", "No direction conflicts", "D"))


def check_d3_contradicting_predictions(report: HealthReport):
    """D3: different agents predicting opposite outcomes on overlapping condition_ids or wallets."""
    cutoff = (NOW - timedelta(hours=72)).isoformat()
    preds = sb_get(f"agent_predictions?select=agent_type,prediction,confidence,verification_query&was_correct=is.null&created_at=gte.{urllib.parse.quote(cutoff)}")
    if len(preds) < 2:
        report.add(CheckResult("D3", "Prediction contradictions", "PASS", "Too few active predictions to check", "D"))
        return
    # Only flag when two agents predict about the same MARKET (condition_id), not same wallet.
    # Multiple agents researching the same wallet is coordination, not contradiction.
    by_agent: dict[str, list[set]] = {}
    for p in preds:
        at = p.get("agent_type", "?")
        text = (str(p.get("prediction", "")) + " " + str(p.get("verification_query", ""))).lower()
        cids = set(re.findall(r'0x[a-f0-9]{60,}', text))  # condition_ids are 66 chars, wallets are 42
        by_agent.setdefault(at, []).append(cids)
    agents = list(by_agent.keys())
    conflicts = []
    for i in range(len(agents)):
        for j in range(i + 1, len(agents)):
            for cids1 in by_agent[agents[i]]:
                for cids2 in by_agent[agents[j]]:
                    shared = cids1 & cids2
                    if shared:
                        conflicts.append(f"{agents[i]} vs {agents[j]} on {list(shared)[0][:14]}")
    if conflicts:
        report.add(CheckResult("D3", "Prediction contradictions", "WARN", f"{len(conflicts)} market-level overlaps: {'; '.join(conflicts[:3])}", "D"))
    else:
        report.add(CheckResult("D3", "Prediction contradictions", "PASS", f"{len(preds)} predictions, no market overlap", "D"))


def check_d4_scout_create_vs_insider_flag(report: HealthReport):
    """D4: Scout created strategy for wallet that Insider Copy flagged."""
    cutoff = (NOW - timedelta(hours=48)).isoformat()
    scout = sb_get(f"strategy_agent_observations?select=content&strategy_type=eq.wallet_scout&obs_type=eq.action_taken&content=like.*CREATED*&created_at=gte.{urllib.parse.quote(cutoff)}&limit=20")
    insider = sb_get(f"strategy_agent_observations?select=content&strategy_type=eq.insider_copy_v1&obs_type=eq.action_taken&content=like.*FLAG*&created_at=gte.{urllib.parse.quote(cutoff)}&limit=20")
    # If both lists are empty or no overlap, pass
    if not scout or not insider:
        report.add(CheckResult("D4", "Scout-create vs Insider-flag", "PASS", "No recent create/flag conflicts", "D"))
        return
    created_wallets = set()
    for o in scout:
        addrs = re.findall(r'\b0x[a-fA-F0-9]{40}\b', str(o.get("content", "")))
        created_wallets.update(a.lower() for a in addrs)
    flagged_wallets = set()
    for o in insider:
        addrs = re.findall(r'\b0x[a-fA-F0-9]{40}\b', str(o.get("content", "")))
        flagged_wallets.update(a.lower() for a in addrs)
    overlap = created_wallets & flagged_wallets
    if overlap:
        report.add(CheckResult("D4", "Scout-create vs Insider-flag", "WARN", f"Wallet conflict: Scout created + Insider flagged: {list(overlap)[:3]}", "D"))
    else:
        report.add(CheckResult("D4", "Scout-create vs Insider-flag", "PASS", "No overlapping wallets", "D"))


def check_d5_boost_vs_trade_idea(report: HealthReport):
    """D5: negative boost on a market with an open HIGH trade idea."""
    mem = sb_memory("market_boosts_current")
    if not mem:
        report.add(CheckResult("D5", "Boost vs trade ideas", "PASS", "No boosts", "D"))
        return
    try:
        boosts = json.loads(mem.get("content", "{}")) if isinstance(mem.get("content"), str) else mem.get("content", {})
    except Exception:
        report.add(CheckResult("D5", "Boost vs trade ideas", "WARN", "Could not parse boosts", "D"))
        return
    negative_cids = []
    for key, info in boosts.items():
        if float(info.get("boost", 0)) >= 0:
            continue
        cid = canonical_condition_id(key)
        if cid:
            negative_cids.append(cid)
    if not negative_cids:
        report.add(CheckResult("D5", "Boost vs trade ideas", "PASS", "No negative boosts", "D"))
        return
    conn = db_conn()
    conflicts = []
    for cid in negative_cids:
        rows = conn.execute(
            "SELECT market_title, outcome, direction FROM trade_ideas "
            "WHERE condition_id=? AND status='open' AND confidence='high'",
            (cid,),
        ).fetchall()
        for row in rows:
            outcome = str(row["outcome"] or "").upper()
            direction = str(row["direction"] or "").lower()
            # Negative boost is bearish on YES. It only conflicts with open YES ideas.
            if outcome == "YES" or direction == "buy_yes":
                conflicts.append(row["market_title"][:30])
                break
    conn.close()
    if conflicts:
        report.add(CheckResult("D5", "Boost vs trade ideas", "WARN", f"Negative boost on open HIGH YES ideas: {', '.join(conflicts)}", "D"))
    else:
        report.add(CheckResult("D5", "Boost vs trade ideas", "PASS", "No conflicts", "D"))


# ══════════════════════════════════════════════════════════════════════════
# Category E: Data Integrity (E1-E7)
# ══════════════════════════════════════════════════════════════════════════

def check_e1_dual_path_world_events(report: HealthReport):
    """E1: world_event:* memories vs world_events table sync."""
    mems = sb_get("memories?select=name&name=like.world_event:*&type=eq.reference")
    mem_count = len(mems) if mems else 0
    events = sb_get("world_events?select=slug&status=neq.resolved")
    event_count = len(events) if events else 0
    # If memories = 0, migration is complete — table is the source of truth
    if mem_count == 0:
        report.add(CheckResult("E1", "Dual-path events sync", "PASS", f"Migration complete: 0 legacy memories, {event_count} table entries", "E"))
    elif mem_count > 0 and abs(mem_count - event_count) > 5:
        report.add(CheckResult("E1", "Dual-path events sync", "WARN",
            f"Legacy memories still exist: {mem_count} memories vs {event_count} table entries", "E"))
    else:
        report.add(CheckResult("E1", "Dual-path events sync", "PASS", f"memories={mem_count}, table={event_count}", "E"))


def check_e2_strategy_db_vs_observations(report: HealthReport):
    """E2: active strategies whose Scout observations say STOPPED."""
    conn = db_conn()
    active = conn.execute("SELECT id, label FROM strategies WHERE status='active' AND strategy_type='insider_copy_v1'").fetchall()
    conn.close()
    if not active:
        report.add(CheckResult("E2", "Strategy DB vs observations", "PASS", "No active insider_copy strategies", "E"))
        return
    scout_obs = sb_get("strategy_agent_observations?select=content&strategy_type=eq.wallet_scout&obs_type=eq.action_taken&order=created_at.desc&limit=30")
    stopped_in_obs = set()
    for o in (scout_obs or []):
        content = str(o.get("content", ""))
        if "STOPPED" in content.upper():
            ids = re.findall(r'id=(\d+)', content)
            stopped_in_obs.update(int(i) for i in ids)
    mismatches = [(s["id"], s["label"]) for s in active if s["id"] in stopped_in_obs]
    if mismatches:
        report.add(CheckResult("E2", "Strategy DB vs observations", "FAIL",
            f"DB says active but Scout says stopped: {mismatches}", "E"))
    else:
        report.add(CheckResult("E2", "Strategy DB vs observations", "PASS", f"{len(active)} active strategies consistent", "E"))


def check_e3_acted_ideas_without_positions(report: HealthReport):
    """E3: trade_ideas marked 'acted' with no strategy_positions."""
    conn = db_conn()
    orphans = conn.execute("""
        SELECT ti.id, ti.market_title FROM trade_ideas ti
        WHERE ti.status = 'acted'
          AND ti.condition_id NOT IN (
              SELECT DISTINCT condition_id FROM strategy_positions WHERE strategy_id IN (
                  SELECT CAST(id AS TEXT) FROM strategies WHERE strategy_type = 'news_intel_v1'
              )
          )
    """).fetchall()
    conn.close()
    if orphans:
        report.add(CheckResult("E3", "Acted ideas without positions", "WARN",
            f"{len(orphans)} ideas marked 'acted' with no matching position", "E"))
    else:
        report.add(CheckResult("E3", "Acted ideas without positions", "PASS", "All acted ideas have positions", "E"))


def check_e4_boost_unknown_markets(report: HealthReport):
    """E4: boosts referencing condition_ids not in market_metadata."""
    mem = sb_memory("market_boosts_current")
    if not mem:
        report.add(CheckResult("E4", "Boost unknown markets", "PASS", "No boosts", "E"))
        return
    try:
        boosts = json.loads(mem.get("content", "{}")) if isinstance(mem.get("content"), str) else mem.get("content", {})
    except Exception:
        report.add(CheckResult("E4", "Boost unknown markets", "WARN", "Could not parse boosts", "E"))
        return
    if not boosts:
        report.add(CheckResult("E4", "Boost unknown markets", "PASS", "No boosts", "E"))
        return
    conn = db_conn()
    unknown = []
    malformed = 0
    for key in boosts:
        cid = canonical_condition_id(key)
        if not cid:
            malformed += 1
            continue
        row = conn.execute("SELECT 1 FROM market_metadata WHERE condition_id=?", (cid,)).fetchone()
        if not row:
            unknown.append(cid[:12])
    conn.close()
    if unknown:
        report.add(CheckResult("E4", "Boost unknown markets", "WARN", f"{len(unknown)} boost targets not in market_metadata", "E"))
    elif malformed:
        report.add(CheckResult("E4", "Boost unknown markets", "PASS", f"All canonical boost targets have market_metadata (ignored {malformed} malformed keys)", "E"))
    else:
        report.add(CheckResult("E4", "Boost unknown markets", "PASS", "All boost targets have market_metadata", "E"))


def check_e5_empty_agent_runs(report: HealthReport):
    """E5: agent runs with no observations written."""
    cutoff = (NOW - timedelta(hours=48)).isoformat()
    runs = sb_get(f"agent_runs?select=agent_type,created_at&created_at=gte.{urllib.parse.quote(cutoff)}")
    obs = sb_get(f"strategy_agent_observations?select=strategy_type,created_at&created_at=gte.{urllib.parse.quote(cutoff)}")
    if not runs:
        report.add(CheckResult("E5", "Empty agent runs", "PASS", "No recent runs", "E"))
        return
    agent_to_strat = {"wallet_scout": "wallet_scout", "insider_copy": "insider_copy_v1"}
    obs_strats = set(o.get("strategy_type") for o in obs) if obs else set()
    empty = []
    seen_agents = set()
    for r in runs:
        at = r.get("agent_type", "")
        if at in seen_agents:
            continue
        seen_agents.add(at)
        st = agent_to_strat.get(at, at)
        if st not in obs_strats:
            empty.append(at)
    # world_analyst doesn't write to strategy_agent_observations, skip it
    empty = [a for a in empty if a != "world_analyst"]
    if empty:
        report.add(CheckResult("E5", "Empty agent runs", "WARN", f"Agents ran but wrote no observations: {empty}", "E"))
    else:
        report.add(CheckResult("E5", "Empty agent runs", "PASS", "All agent runs produced observations", "E"))


def check_e6_active_strategy_dead_wallet(report: HealthReport):
    """E6: active strategies whose wallet isn't in user_analysis."""
    conn = db_conn()
    orphans = conn.execute("""
        SELECT s.id, s.label, s.wallet FROM strategies s
        WHERE s.status = 'active' AND s.strategy_type = 'insider_copy_v1'
          AND s.wallet NOT IN (SELECT wallet FROM user_analysis)
    """).fetchall()
    conn.close()
    if orphans:
        names = [f"#{o['id']} {o['label']}" for o in orphans]
        report.add(CheckResult("E6", "Active strategy dead wallet", "FAIL",
            f"Active strategies with missing wallets: {', '.join(names)}", "E"))
    else:
        report.add(CheckResult("E6", "Active strategy dead wallet", "PASS", "All active strategy wallets exist", "E"))


def check_e7_orphaned_paper_trades(report: HealthReport):
    """E7: paper trades where the MARKET has ended/closed but trade is still open.

    A trade is a true orphan only if:
      - `market_metadata.is_open = 0` (market closed), OR
      - `end_date_iso` is in the past (market should have resolved)
    Long-dated prediction markets (elections, regime-change) legitimately stay
    open for months, so age alone (>7d) is not a useful orphan signal — the
    original check fired constantly on healthy long-horizon insider follows.
    """
    from datetime import datetime, timezone
    conn = db_conn()
    rows = conn.execute("""
        SELECT pt.trade_id, pt.opened_at, mm.is_open, mm.end_date_iso
        FROM paper_trades pt
        LEFT JOIN market_metadata mm ON pt.condition_id = mm.condition_id
        WHERE pt.status = 'open'
    """).fetchall()
    conn.close()

    now_utc = datetime.now(timezone.utc)
    orphans = []
    for r in rows:
        market_closed = (r["is_open"] == 0)
        market_ended = False
        end_iso = (r["end_date_iso"] or "").strip()
        if end_iso:
            try:
                end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                if end_dt.tzinfo is None:
                    end_dt = end_dt.replace(tzinfo=timezone.utc)
                market_ended = end_dt < now_utc
            except ValueError:
                pass
        if market_closed or market_ended:
            orphans.append(r["trade_id"])

    count = len(orphans)
    if count >= 10:
        report.add(CheckResult("E7", "Orphaned paper trades", "WARN",
            f"{count} open trades on closed/ended markets (needs manual close)", "E"))
    elif count > 0:
        report.add(CheckResult("E7", "Orphaned paper trades", "PASS",
            f"{count} open trades on closed/ended markets (below threshold)", "E"))
    else:
        report.add(CheckResult("E7", "Orphaned paper trades", "PASS",
            "No orphaned trades on closed markets", "E"))


# ══════════════════════════════════════════════════════════════════════════
# Category F: Brain Bloat (F1-F4)
# ══════════════════════════════════════════════════════════════════════════

def check_f1_observation_count(report: HealthReport):
    """F1: total observation count growth.

    Thresholds recalibrated from 200/500 to 400/750. The original 200-warn
    was set before the system ran at full volume; at ~45 obs/day steady state
    it fired continuously on a healthy system. 400 gives ~2 weeks of headroom
    before warning, 750 is a genuine bloat signal worth acting on.
    """
    count = sb_count("strategy_agent_observations")
    if count > 750:
        report.add(CheckResult("F1", "Observation bloat", "FAIL", f"{count} observations (>750)", "F"))
    elif count > 400:
        report.add(CheckResult("F1", "Observation bloat", "WARN", f"{count} observations (>400)", "F"))
    else:
        report.add(CheckResult("F1", "Observation bloat", "PASS", f"{count} observations", "F"))


def _is_evergreen_knowledge(name: str) -> bool:
    """Return True if a knowledge entry is intentionally static (methodology, patterns, research).

    Evergreen entries are permanent reference docs that are written once and never need
    refreshing — Brier Score formulas, calibration conventions, learned market patterns, etc.
    They should not trigger F2 staleness warnings.

    Operational entries (markets/state, world_narrative, adversarial/run_stats, etc.) are NOT
    evergreen — they reflect live system state and should be refreshed regularly.

    Convention: a knowledge entry is evergreen if its name falls under a known-static namespace
    prefix OR matches a known-static suffix pattern.  Operational entries live under 'markets/',
    'adversarial/', 'flag:', 'country:' namespaces or are bare well-known state keys.
    """
    # Entries checked by other dedicated checks — exempt from double-counting
    separately_checked = {"world_state", "market_boosts_current"}
    if name in separately_checked:
        return True

    # Known operational bare-name entries — NOT evergreen
    operational_bare = {
        "world_narrative", "fast_ooda_last_run", "brain_health_latest",
    }
    if name in operational_bare:
        return False

    # Operational namespace prefixes — NOT evergreen
    OPERATIONAL_PREFIXES = ("markets/", "adversarial/", "flag:", "country:")
    if any(name.startswith(p) for p in OPERATIONAL_PREFIXES):
        return False

    # Evergreen namespace prefixes — research/methodology/pattern knowledge
    # Note: both slash-form (research/) and colon-form (research:) are valid;
    # early entries were written with colons before the slash convention was established.
    EVERGREEN_PREFIXES = (
        "patterns/", "research/", "research:", "wallet_profile/", "architecture/",
        "scoring/", "strategy/", "signals/",
    )
    if any(name.startswith(p) for p in EVERGREEN_PREFIXES):
        return True

    # Evergreen suffix patterns — methodology and reference docs
    EVERGREEN_SUFFIXES = (
        "_methods", "_convention", "_model", "_theory", "_thesis",
        "_validation", "_basis", "_postmortem", "_edge", "_paradox",
        "_bias", "_structure", "_architecture", "_pipeline",
    )
    if any(name.endswith(s) for s in EVERGREEN_SUFFIXES):
        return True

    # Everything else is assumed operational (bare state keys written by agents)
    return False


def check_f2_stale_knowledge(report: HealthReport):
    """F2: operational knowledge memories not updated in 7+ days.

    Only checks entries that represent live system state (markets/state, world_narrative,
    adversarial/run_stats, etc.).  Evergreen entries (patterns/, research/, calibration
    methods, conventions) are explicitly excluded — they are written once and never expire.
    """
    mems = sb_get("memories?select=name,updated_at&type=eq.knowledge")
    stale = []
    for m in (mems or []):
        name = m.get("name", "")
        if _is_evergreen_knowledge(name):
            continue
        age_h = hours_ago(parse_ts(m.get("updated_at")))
        if age_h > 168:  # 7 days
            stale.append(name[:35])
    if stale:
        report.add(CheckResult("F2", "Stale knowledge", "WARN",
                               f"{len(stale)} operational knowledge entries >7d old: {', '.join(stale[:5])}", "F"))
    else:
        report.add(CheckResult("F2", "Stale knowledge", "PASS", "All operational knowledge entries fresh", "F"))


def check_f3_world_state_size(report: HealthReport):
    """F3: world_state content growing too large."""
    mem = sb_memory("world_state")
    if not mem:
        report.add(CheckResult("F3", "World state size", "PASS", "world_state not found (checked by A2)", "F"))
        return
    size = len(str(mem.get("content", "")))
    if size > 5000:
        report.add(CheckResult("F3", "World state size", "WARN", f"world_state is {size} chars (>5000)", "F"))
    else:
        report.add(CheckResult("F3", "World state size", "PASS", f"{size} chars", "F"))


def check_f4_prediction_backlog(report: HealthReport):
    """F4: auto-gradeable predictions overdue (agent failed to grade on its own run).

    Scope narrowed so it no longer overlaps C1:
      - C1 owns ungradeable (verification_query IS NULL) overdue preds.
      - F4 owns gradeable (verification_query IS NOT NULL) overdue preds — these
        should have been picked up by the agent's own `predict_tool.py auto-grade`
        call at run time. If they're still overdue here, either the agent isn't
        running or its auto-grade step is erroring.

    Also reports total pending (all ungraded) for context — raw backlog size.
    """
    now_z = NOW.strftime("%Y-%m-%dT%H:%M:%SZ")
    overdue = sb_get(
        "agent_predictions?select=id,agent_type"
        "&was_correct=is.null"
        "&verification_query=not.is.null"
        f"&verify_after=lt.{now_z}"
    )
    overdue_count = len(overdue) if overdue else 0
    all_pending = sb_get("agent_predictions?select=id&was_correct=is.null")
    total_pending = len(all_pending) if all_pending else 0

    if overdue_count > 10:
        agents = sorted({p.get("agent_type", "?") for p in overdue})
        report.add(CheckResult("F4", "Gradeable backlog", "FAIL",
            f"{overdue_count} overdue preds WITH query ({', '.join(agents)}) — "
            f"agent auto-grade step failing? {total_pending} total pending.", "F"))
    elif overdue_count > 3:
        agents = sorted({p.get("agent_type", "?") for p in overdue})
        report.add(CheckResult("F4", "Gradeable backlog", "WARN",
            f"{overdue_count} overdue preds with query ({', '.join(agents)}). "
            f"{total_pending} total pending.", "F"))
    else:
        report.add(CheckResult("F4", "Gradeable backlog", "PASS",
            f"{overdue_count} overdue w/ query, {total_pending} total pending", "F"))


def check_f5_knowledge_freshness(report: HealthReport):
    """F5: knowledge entries that should be live but haven't been updated in 7+ days."""
    LIVE_KNOWLEDGE = {"world_state", "market_boosts_current", "brain_health_latest",
                      "signals/scoring_accuracy_by_band", "signals/threshold_sweep_latest"}
    stale = []
    for name in LIVE_KNOWLEDGE:
        mem = sb_memory(name)
        if mem and hours_ago(parse_ts(mem.get("updated_at"))) > 168:  # 7 days
            stale.append(name)
    if stale:
        report.add(CheckResult("F5", "Live knowledge staleness", "WARN",
            f"{len(stale)} live knowledge entries >7d old: {', '.join(stale)}", "F"))
    else:
        report.add(CheckResult("F5", "Live knowledge staleness", "PASS",
            "All live knowledge entries fresh", "F"))


# ══════════════════════════════════════════════════════════════════════════
# Category G: Performance Degradation (G1-G3)
# ══════════════════════════════════════════════════════════════════════════

def check_g1_cost_trend(report: HealthReport):
    """G1: agent cost trending up."""
    runs = sb_get("agent_runs?select=agent_type,cost_usd&order=created_at.desc&limit=30")
    if not runs:
        report.add(CheckResult("G1", "Agent cost trend", "PASS", "No runs", "G"))
        return
    by_agent: dict[str, list[float]] = {}
    for r in runs:
        at = r.get("agent_type", "?")
        cost = float(r.get("cost_usd") or 0)
        # Skip zero-cost runs — typically shell-fallback heartbeat rows that
        # log a run without cost metadata, or blocked runs that never made an
        # LLM call. Including them pulls the average down and produces false
        # negatives on real cost-increase trends. Mirrors the turns==0 skip
        # in check_g2_turn_trend.
        if cost == 0:
            continue
        by_agent.setdefault(at, []).append(cost)
    increasing = []
    for agent, costs in by_agent.items():
        if len(costs) < 4:
            continue
        recent_avg = sum(costs[:3]) / 3
        older_slice = costs[3:min(10, len(costs))]
        # Need at least 2 runs in the baseline to avoid single-outlier false positives
        if len(older_slice) < 2:
            continue
        older_avg = sum(older_slice) / len(older_slice)
        delta = recent_avg - older_avg
        if older_avg > 0 and delta >= MIN_COST_TREND_DELTA_USD and delta / older_avg > 0.30:
            increasing.append(f"{agent}: ${recent_avg:.2f} vs ${older_avg:.2f}")
    if increasing:
        report.add(CheckResult("G1", "Agent cost trend", "WARN", f"Costs increasing: {'; '.join(increasing)}", "G"))
    else:
        report.add(CheckResult("G1", "Agent cost trend", "PASS", "Costs stable", "G"))


def check_g2_turn_trend(report: HealthReport):
    """G2: agent turns trending up."""
    runs = sb_get("agent_runs?select=agent_type,turns&order=created_at.desc&limit=30")
    if not runs:
        report.add(CheckResult("G2", "Agent turn trend", "PASS", "No runs", "G"))
        return
    by_agent: dict[str, list[int]] = {}
    for r in runs:
        at = r.get("agent_type", "?")
        turns = int(r.get("turns") or 0)
        # Skip 0-turn runs — they are blocked/skipped runs (e.g. CLAUDECODE env blocking
        # the agent from launching), not real agent work. Including them distorts the
        # average and produces false WARN when one real run follows many blocked ones.
        if turns == 0:
            continue
        by_agent.setdefault(at, []).append(turns)
    increasing = []
    for agent, turns in by_agent.items():
        if len(turns) < 4:
            continue
        recent_avg = sum(turns[:3]) / 3
        older_slice = turns[3:min(10, len(turns))]
        # Require a real baseline window. Two older runs is too noisy and can
        # false-warn on one oddball short run.
        if len(older_slice) < MIN_TURN_TREND_BASELINE_RUNS:
            continue
        older_avg = sum(older_slice) / len(older_slice)
        delta = recent_avg - older_avg
        if older_avg > 0 and delta >= MIN_TURN_TREND_DELTA and delta / older_avg > 0.30:
            increasing.append(f"{agent}: {recent_avg:.0f} vs {older_avg:.0f}")
    if increasing:
        report.add(CheckResult("G2", "Agent turn trend", "WARN", f"Turns increasing: {'; '.join(increasing)}", "G"))
    else:
        report.add(CheckResult("G2", "Agent turn trend", "PASS", "Turns stable", "G"))


def check_g3_empty_opportunities(report: HealthReport):
    """G3: market_opportunities table empty despite World Analyst running."""
    count = sb_count("market_opportunities")
    runs = sb_get("agent_runs?select=id&agent_type=eq.world_analyst")
    run_count = len(runs) if runs else 0
    if count == 0 and run_count >= 15:
        report.add(CheckResult("G3", "Empty opportunities", "WARN",
            f"0 market opportunities despite {run_count} World Analyst runs", "G"))
    else:
        report.add(CheckResult("G3", "Empty opportunities", "PASS", f"{count} opportunities from {run_count} runs", "G"))


# ══════════════════════════════════════════════════════════════════════════
# Category V: VPS Health (V1-V6)
# ══════════════════════════════════════════════════════════════════════════

# V1 thresholds — 2-core VPS: load > cores is sustained saturation
V1_LOAD_FAIL  = 4.0   # load_1min above this → FAIL
V1_LOAD_WARN  = 2.5   # load_1min above this → WARN

# V2 thresholds — journal disk use
V2_JOURNAL_FAIL_MB = 1500
V2_JOURNAL_WARN_MB = 600

# V3 — systemd user services that must be active for the trading system to function.
# Checked via `systemctl --user is-active <name>` — more reliable than pgrep because
# it works for module-invoked services (`python -m sim.collector`) and distinguishes
# "active" from "activating/failed".
#
# Legacy name V3_REQUIRED_PROCS kept as an alias for tests that still monkey-patch it;
# new code should use V3_REQUIRED_SERVICES.
V3_REQUIRED_SERVICES = [
    "sniper-web",            # dashboard + API (bot/agent endpoints depend on it)
    "sniper-sim-collector",  # sim dataset WebSocket collector
    "sniper-sim-meta",       # sim dataset metadata sync
    # 2026-04-29: dropped legacy services — sniper-stream, sniper-analyzer,
    # sniper-paper, sniper-alerter — per `docs/audits/2026-04-29-legacy-remnant-audit.md`
    # section 4 step 1. The new analyzer (`scripts/run_wallet_protocol_batch.py`)
    # is manual-only; paper/alerter remain stopped during the cutover. Until
    # they migrate to `wallet_layer5_decision`-driven gates, brain_health must
    # not FAIL on their absence. Re-add when the new gates are wired.
    #
    # Note: sniper-bot (Telegram relay) and stink-bid@NN instances are checked
    # opportunistically via check_v3b below so brain_health doesn't FAIL if the
    # operator intentionally stops them for maintenance.
]
# Legacy alias kept for tests that monkey-patch the old name. Empty list now —
# the streamer + analyzer are masked under the freeze; if a test wants to
# assert on legacy proc activity, it must re-populate this explicitly.
V3_REQUIRED_PROCS: list[str] = []

# V4 — disk space on the VPS root filesystem
V4_DISK_FAIL_PCT = 90   # ≥ 90% used → FAIL
V4_DISK_WARN_PCT = 80   # ≥ 80% used → WARN

# V5 — analyzer SLA: warn if last committed run is older than this
V5_ANALYZER_WARN_HOURS = 2   # analyzer runs every ~30min; 2h = 4 missed cycles
V5_ANALYZER_FAIL_HOURS = 6   # 6h gap = definitely stuck

# V6: service config drift — repo dir vs deployed dirs
V6_REPO_SERVICE_DIR = ROOT / "systemd"
V6_DEPLOYED_USER_DIR = Path("/home/sniper/.config/systemd/user")
V6_DEPLOYED_SYSTEM_DIR = Path("/etc/systemd/system")

# V7 — knowledge-compile cron health. Wrapper touches this file on every
# successful run (push or no-op in-sync). Stale = timer fired but wrapper
# failed to reach a clean-publish state (push failure, rebase abort, crash).
V7_KNOWLEDGE_TOUCHFILE = ROOT / "logs" / "knowledge_compile.last_push_success"
V7_STALE_WARN_HOURS = 36   # one missed 24h cycle + 12h grace
V7_STALE_FAIL_HOURS = 72   # three missed cycles — something is broken

# V8 — event-trigger timer liveness (fires every 10 min)
V8_EVENT_TRIGGER_TIMER = "sniper-event-trigger.timer"
V8_STALE_WARN_MIN = 25    # ≥ 2 missed cycles → WARN
V8_STALE_FAIL_MIN = 60    # ≥ 6 missed cycles → FAIL
V6_REPO_ONLY_EXPECTED: set[str] = {
    "sniper-dashboard.service",
    "sniper-outcomes.service",
    "sniper-real.service",
    "sniper-strategy.service",
}


def check_v1_cpu_load(report: HealthReport):
    """V1: CPU load average — detect sustained saturation on 2-core VPS."""
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        load1 = float(parts[0])
        load5 = float(parts[1])
        if load1 > V1_LOAD_FAIL:
            report.add(CheckResult("V1", "CPU load", "FAIL",
                f"load_1min={load1:.2f} (>{V1_LOAD_FAIL} on 2-core VPS)", "V"))
        elif load1 > V1_LOAD_WARN:
            report.add(CheckResult("V1", "CPU load", "WARN",
                f"load_1min={load1:.2f}, load_5min={load5:.2f}", "V"))
        else:
            report.add(CheckResult("V1", "CPU load", "PASS",
                f"load_1min={load1:.2f}", "V"))
    except Exception as e:
        report.add(CheckResult("V1", "CPU load", "WARN",
            f"Could not read /proc/loadavg: {e}", "V"))


def check_v2_journal_size(report: HealthReport):
    """V2: systemd journal disk use — detect runaway log growth."""
    total_mb: float | None = None
    try:
        proc = subprocess.run(
            ["journalctl", "--disk-usage", "-q"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode == 0:
            out = (proc.stdout or "").strip()
            m = re.search(r"take up\s+([0-9]+(?:\.[0-9]+)?)\s*([KMGTP])", out, re.IGNORECASE)
            if m:
                value = float(m.group(1))
                unit = m.group(2).upper()
                scale = {
                    "K": 1 / 1024,
                    "M": 1.0,
                    "G": 1024.0,
                    "T": 1024.0 * 1024.0,
                    "P": 1024.0 * 1024.0 * 1024.0,
                }
                total_mb = value * scale[unit]
    except Exception:
        total_mb = None

    if total_mb is None:
        journal_dirs = [Path("/var/log/journal"), Path("/run/log/journal")]
        total_mb = 0.0
        for d in journal_dirs:
            if d.exists():
                total_mb += sum(
                    f.stat().st_size for f in d.rglob("*") if f.is_file()
                ) / (1024 * 1024)
    if total_mb > V2_JOURNAL_FAIL_MB:
        report.add(CheckResult("V2", "Journal size", "FAIL",
            f"Journal {total_mb:.0f}MB (>{V2_JOURNAL_FAIL_MB}MB)", "V"))
    elif total_mb > V2_JOURNAL_WARN_MB:
        report.add(CheckResult("V2", "Journal size", "WARN",
            f"Journal {total_mb:.0f}MB (>{V2_JOURNAL_WARN_MB}MB)", "V"))
    else:
        report.add(CheckResult("V2", "Journal size", "PASS",
            f"Journal {total_mb:.0f}MB", "V"))


def check_v3_key_processes(report: HealthReport):
    """V3: all required systemd user services are active.

    Uses `systemctl --user is-active <name>` — returns "active" / "inactive" /
    "failed" / "activating". Distinguishes clean running from failed starts,
    which pgrep cannot.
    """
    import subprocess as _sp
    states: dict[str, str] = {}
    try:
        for name in V3_REQUIRED_SERVICES:
            r = _sp.run(
                ["systemctl", "--user", "is-active", name],
                capture_output=True, text=True, timeout=5,
            )
            states[name] = (r.stdout or r.stderr or "").strip() or "unknown"
    except Exception as e:
        report.add(CheckResult("V3", "Key services", "WARN",
            f"systemctl check failed: {e}", "V"))
        return

    dead = {name: state for name, state in states.items() if state != "active"}
    if dead:
        detail = ", ".join(f"{n}={s}" for n, s in dead.items())
        report.add(CheckResult("V3", "Key services", "FAIL",
            f"{len(dead)}/{len(states)} not active: {detail}", "V"))
    else:
        report.add(CheckResult("V3", "Key services", "PASS",
            f"all {len(states)} required services active", "V"))


def check_v4_disk_space(report: HealthReport):
    """V4: root filesystem disk usage — detect near-full VPS disk."""
    import shutil
    try:
        usage = shutil.disk_usage("/")
        pct = usage.used * 100 / usage.total
        free_gb = (usage.total - usage.used) / (1024 ** 3)
        if pct >= V4_DISK_FAIL_PCT:
            report.add(CheckResult("V4", "Disk space", "FAIL",
                f"Disk {pct:.0f}% used — only {free_gb:.1f}GB free (/{usage.total // (1024**3)}GB)", "V"))
        elif pct >= V4_DISK_WARN_PCT:
            report.add(CheckResult("V4", "Disk space", "WARN",
                f"Disk {pct:.0f}% used — {free_gb:.1f}GB free", "V"))
        else:
            report.add(CheckResult("V4", "Disk space", "PASS",
                f"Disk {pct:.0f}% used ({free_gb:.1f}GB free)", "V"))
    except Exception as e:
        report.add(CheckResult("V4", "Disk space", "WARN",
            f"Could not check disk usage: {e}", "V"))


def check_v5_analyzer_sla(report: HealthReport):
    """V5: analyzer SLA — detect if the analyzer has stopped running."""
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT MAX(committed_ts) FROM analysis_runs WHERE status='committed'"
            ).fetchone()
        # committed_ts is a Unix integer timestamp
        ts_raw = row[0] if row else None
        last_ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc) if ts_raw else None
        age_h = hours_ago(last_ts)
        if age_h >= V5_ANALYZER_FAIL_HOURS:
            report.add(CheckResult("V5", "Analyzer SLA", "FAIL",
                f"Last committed run {age_h:.1f}h ago (>{V5_ANALYZER_FAIL_HOURS}h — analyzer stuck?)", "V"))
        elif age_h >= V5_ANALYZER_WARN_HOURS:
            report.add(CheckResult("V5", "Analyzer SLA", "WARN",
                f"Last committed run {age_h:.1f}h ago (>{V5_ANALYZER_WARN_HOURS}h)", "V"))
        else:
            report.add(CheckResult("V5", "Analyzer SLA", "PASS",
                f"Last committed run {age_h:.1f}h ago", "V"))
    except Exception as e:
        report.add(CheckResult("V5", "Analyzer SLA", "WARN",
            f"Could not check analyzer runs: {e}", "V"))


def check_v6_service_config_drift(report: HealthReport):
    """V6: detect deployed systemd service files diverged from repo copies."""
    repo_dir = V6_REPO_SERVICE_DIR
    if not repo_dir.exists():
        report.add(CheckResult("V6", "Service config drift", "PASS", "Repo service dir not found — skipping", "V"))
        return

    def _md5(path: Path) -> str:
        return hashlib.md5(path.read_bytes()).hexdigest()

    def _find_deployed(name: str) -> Path | None:
        for directory in (V6_DEPLOYED_USER_DIR, V6_DEPLOYED_SYSTEM_DIR):
            candidate = directory / name
            if candidate.exists():
                return candidate
        return None

    drifted: list[str] = []
    missing_deployed: list[str] = []
    for repo_file in sorted(repo_dir.glob("*.service")):
        if repo_file.name in V6_REPO_ONLY_EXPECTED:
            continue
        deployed_file = _find_deployed(repo_file.name)
        if deployed_file is None:
            missing_deployed.append(repo_file.name)
            continue
        if _md5(repo_file) != _md5(deployed_file):
            drifted.append(repo_file.name)

    issues: list[str] = []
    if drifted:
        issues.append(f"drifted: {', '.join(drifted)}")
    if missing_deployed:
        issues.append(f"missing deployed: {', '.join(missing_deployed)}")

    if issues:
        report.add(CheckResult(
            "V6",
            "Service config drift",
            "WARN",
            f"{len(drifted) + len(missing_deployed)} service file(s) out of sync — {'; '.join(issues)}",
            "V",
        ))
    else:
        checked = len(list(repo_dir.glob("*.service"))) - len(V6_REPO_ONLY_EXPECTED)
        report.add(CheckResult("V6", "Service config drift", "PASS", f"{checked} service files match deployed", "V"))


def check_v7_knowledge_compile_health(report: HealthReport):
    """V7: knowledge-compile cron has reached a clean-publish state recently.

    The wrapper touches ``logs/knowledge_compile.last_push_success`` on every
    successful run (git push OR no-op when already in sync). If the file is
    missing or stale, the timer either hasn't fired since brain_health last
    ran OR the wrapper is failing silently (push-rejected, rebase conflict,
    compile crash caught by set -e).
    """
    try:
        path = V7_KNOWLEDGE_TOUCHFILE
        if not path.exists():
            report.add(CheckResult(
                "V7", "Knowledge compile health", "WARN",
                "touchfile missing — cron has never reached a clean-publish state "
                "(first install, or wrapper is failing)",
                "V",
            ))
            return
        age_h = (time.time() - path.stat().st_mtime) / 3600
        if age_h > V7_STALE_FAIL_HOURS:
            report.add(CheckResult(
                "V7", "Knowledge compile health", "FAIL",
                f"last clean run {age_h:.1f}h ago (>{V7_STALE_FAIL_HOURS}h) — check "
                "journalctl --user -u sniper-knowledge-compile",
                "V",
            ))
        elif age_h > V7_STALE_WARN_HOURS:
            report.add(CheckResult(
                "V7", "Knowledge compile health", "WARN",
                f"last clean run {age_h:.1f}h ago (>{V7_STALE_WARN_HOURS}h)", "V",
            ))
        else:
            report.add(CheckResult(
                "V7", "Knowledge compile health", "PASS",
                f"last clean run {age_h:.1f}h ago", "V",
            ))
    except Exception as e:
        report.add(CheckResult("V7", "Knowledge compile health", "WARN",
            f"Could not read touchfile: {e}", "V"))


def check_v8_event_trigger_timer(report: HealthReport):
    """V8: event-trigger timer fired recently.

    The RSS news watcher (``sniper-event-trigger.timer``) fires every 10 min.
    If LastTriggerUSec drifts past 25 min we've missed ≥ 2 cycles; past 60 min
    something is clearly broken (timer disabled, OOM loop, system stopped).

    Reads via `systemctl show` without sudo — works for system-level timers
    as a non-root user via D-Bus.
    """
    import subprocess as _sp
    try:
        r = _sp.run(
            ["systemctl", "show", V8_EVENT_TRIGGER_TIMER,
             "-p", "ActiveState", "-p", "LastTriggerUSec", "-p", "Result"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception as e:
        report.add(CheckResult("V8", "Event trigger timer", "WARN",
            f"systemctl show failed: {e}", "V"))
        return

    props: dict[str, str] = {}
    for line in (r.stdout or "").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()

    active = props.get("ActiveState", "")
    if active != "active":
        report.add(CheckResult("V8", "Event trigger timer", "FAIL",
            f"timer ActiveState={active!r} (expected 'active')", "V"))
        return

    last_trigger_str = props.get("LastTriggerUSec", "")
    if not last_trigger_str or last_trigger_str in ("0", "n/a"):
        # Timer is active but has never fired yet (just enabled?).
        report.add(CheckResult("V8", "Event trigger timer", "WARN",
            "timer active but LastTriggerUSec empty — not fired yet", "V"))
        return

    # Parse the timestamp. systemd formats it like "Tue 2026-04-21 14:50:01 EDT"
    try:
        # Strip weekday and timezone abbreviation (strptime can't parse %Z reliably);
        # systemd stamps are in local time — compare via `date` for correctness.
        r2 = _sp.run(
            ["date", "-d", last_trigger_str, "+%s"],
            capture_output=True, text=True, timeout=3,
        )
        last_ts = int(r2.stdout.strip())
    except Exception as e:
        report.add(CheckResult("V8", "Event trigger timer", "WARN",
            f"could not parse LastTriggerUSec={last_trigger_str!r}: {e}", "V"))
        return

    age_min = (time.time() - last_ts) / 60
    result = props.get("Result", "")
    if age_min > V8_STALE_FAIL_MIN:
        report.add(CheckResult("V8", "Event trigger timer", "FAIL",
            f"last fire {age_min:.1f}min ago (>{V8_STALE_FAIL_MIN}min, result={result})", "V"))
    elif age_min > V8_STALE_WARN_MIN:
        report.add(CheckResult("V8", "Event trigger timer", "WARN",
            f"last fire {age_min:.1f}min ago (>{V8_STALE_WARN_MIN}min, result={result})", "V"))
    else:
        report.add(CheckResult("V8", "Event trigger timer", "PASS",
            f"last fire {age_min:.1f}min ago (result={result})", "V"))


# ══════════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════════
# Category CAL: Calibration Loop (v1)
# ══════════════════════════════════════════════════════════════════════════

def check_cal1_calibration_context_populated(report: HealthReport):
    """CAL1: most recent agent_runs row for each enabled agent has calibration_context populated.

    PASS  — context present (or no enabled agents).
    WARN  — most recent run exists but calibration_context is NULL.
    WARN  — no recent run found at all (cadence missed).
    Never FAIL in v1 — see spec §8.2.
    """
    if not CALIBRATION_ENABLED_AGENTS:
        report.add(CheckResult("CAL1", "Calibration context populated", "PASS",
                               "No agents in CALIBRATION_ENABLED_AGENTS — nothing to check", "CAL"))
        return

    stale_or_missing: list[str] = []
    ok: list[str] = []
    for agent_type in CALIBRATION_ENABLED_AGENTS:
        rows = sb_get(
            f"agent_runs?select=id,created_at,calibration_context"
            f"&agent_type=eq.{urllib.parse.quote(agent_type)}"
            f"&order=created_at.desc&limit=1"
        )
        if not rows:
            stale_or_missing.append(f"{agent_type} (no runs found)")
            continue
        ctx = rows[0].get("calibration_context")
        if ctx is None:
            stale_or_missing.append(f"{agent_type} (latest run has NULL calibration_context)")
        else:
            ok.append(agent_type)

    if stale_or_missing:
        report.add(CheckResult("CAL1", "Calibration context populated", "WARN",
                               "; ".join(stale_or_missing), "CAL"))
    else:
        report.add(CheckResult("CAL1", "Calibration context populated", "PASS",
                               f"all enabled agents have calibration_context ({', '.join(ok)})", "CAL"))


def check_wallet_analyzer_failure_categories() -> CheckResult:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='wallet_analyzer_intake_queue'"
        ).fetchone()
        if not exists:
            return CheckResult(
                "V9",
                "Wallet analyzer failure categories",
                "PASS",
                "wallet_analyzer_intake_queue missing; no analyzer intake failures",
                "V",
            )
        cols = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(wallet_analyzer_intake_queue)"
            ).fetchall()
        }
        if "failure_category" not in cols:
            return CheckResult(
                "V9",
                "Wallet analyzer failure categories",
                "WARN",
                "failure_category column missing",
                "V",
            )
        rows = conn.execute(
            "SELECT failure_category, COUNT(*) AS n "
            "FROM wallet_analyzer_intake_queue "
            "WHERE status='failed' "
            "GROUP BY failure_category ORDER BY failure_category"
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return CheckResult(
            "V9",
            "Wallet analyzer failure categories",
            "PASS",
            "no failed analyzer intake rows",
            "V",
        )
    parts = [
        f"{str(row['failure_category'] or 'uncategorized')}={int(row['n'])}"
        for row in rows
    ]
    severity = "WARN" if any(p.startswith("uncategorized=") for p in parts) else "PASS"
    return CheckResult(
        "V9",
        "Wallet analyzer failure categories",
        severity,
        ", ".join(parts),
        "V",
    )


def check_v9_wallet_analyzer_failure_categories(report: HealthReport):
    report.add(check_wallet_analyzer_failure_categories())


# All checks registry
# ══════════════════════════════════════════════════════════════════════════

ALL_CHECKS = [
    # A: Staleness
    check_a0_stream_staleness, check_a1_boost_expiry, check_a2_world_state_freshness,
    check_a3_stale_world_events, check_a4_regime_freshness,
    check_a5_orphan_wallet_references,
    # B: Heartbeats
    check_b1, check_b2, check_b3, check_b4_news_intel_heartbeat,
    check_b5_incomplete_runs, check_b6_finance_analyst_heartbeat,
    # C: Predictions
    check_c1_ungraded_predictions, check_c2_accuracy_declining,
    check_c3_zero_lessons, check_c4_world_analyst_regression,
    check_c5_c2_cluster_worsening,
    # D: Coherence
    check_d1_scout_stop_vs_insider_thesis, check_d2_negative_boost_on_open_position,
    check_d3_contradicting_predictions, check_d4_scout_create_vs_insider_flag,
    check_d5_boost_vs_trade_idea,
    # E: Integrity
    check_e1_dual_path_world_events, check_e2_strategy_db_vs_observations,
    check_e3_acted_ideas_without_positions, check_e4_boost_unknown_markets,
    check_e5_empty_agent_runs, check_e6_active_strategy_dead_wallet,
    check_e7_orphaned_paper_trades,
    # F: Bloat
    check_f1_observation_count, check_f2_stale_knowledge,
    check_f3_world_state_size, check_f4_prediction_backlog,
    check_f5_knowledge_freshness,
    # G: Performance
    check_g1_cost_trend, check_g2_turn_trend, check_g3_empty_opportunities,
    # V: VPS Health
    check_v1_cpu_load, check_v2_journal_size, check_v3_key_processes,
    check_v4_disk_space, check_v5_analyzer_sla, check_v6_service_config_drift,
    check_v7_knowledge_compile_health, check_v8_event_trigger_timer,
    check_v9_wallet_analyzer_failure_categories,
    # CAL: Calibration Loop (v1)
    check_cal1_calibration_context_populated,
]


# ══════════════════════════════════════════════════════════════════════════
# Runner + Output
# ══════════════════════════════════════════════════════════════════════════

def run_all_checks() -> HealthReport:
    """Execute all registered health checks and return the report."""
    report = HealthReport(timestamp=NOW.isoformat())
    for check_fn in ALL_CHECKS:
        try:
            check_fn(report)
        except Exception as e:
            check_id = check_fn.__name__.replace("check_", "").split("_")[0].upper()
            report.add(CheckResult(check_id, check_fn.__name__, "WARN",
                f"Check raised exception: {e}", "?"))
            logger.warning("Check %s failed: %s", check_fn.__name__, e, exc_info=True)
    return report


def send_telegram(report: HealthReport):
    """Send health summary to Telegram."""
    if not TG_TOKEN or not TG_CHAT:
        return
    icon = {"HEALTHY": "\U0001f7e2", "DEGRADED": "\U0001f7e1", "UNHEALTHY": "\U0001f534"}[report.overall]
    # Show Eastern time for readability
    from zoneinfo import ZoneInfo
    et_now = datetime.now(ZoneInfo("America/New_York")).strftime("%I:%M %p ET")
    lines = [f"{icon} <b>Brain Health: {report.overall}</b> ({et_now})",
             f"\u2705 {report.pass_count}  \u26a0\ufe0f {report.warn_count}  \u274c {report.fail_count}"]
    # Add FAIL and WARN details (escape HTML in message content)
    for c in report.checks:
        if c.severity == "FAIL":
            lines.append(f"\u274c <b>{c.check_id}</b> {esc(c.name)}: {esc(c.message[:100])}")
    for c in report.checks:
        if c.severity == "WARN":
            lines.append(f"\u26a0\ufe0f {c.check_id} {esc(c.name)}: {esc(c.message[:80])}")
    text = "\n".join(lines[:20])  # Cap at 20 lines
    payload = json.dumps({"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"}).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=payload, method="POST",
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning("Telegram send failed: %s", e)


def write_to_supabase(report: HealthReport):
    """Write health report to brain's knowledge section."""
    if not SB_URL or not SB_KEY:
        return
    from bot.supabase_helper import upsert_memory
    summary_lines = []
    for c in report.checks:
        icon = {"PASS": "\u2705", "WARN": "\u26a0\ufe0f", "FAIL": "\u274c"}[c.severity]
        summary_lines.append(f"{icon} {c.check_id} {c.name}: {c.message}")
    content = (
        f"## Brain Health Report — {report.timestamp}\n"
        f"## Status: {report.overall}\n"
        f"## Scores: {report.pass_count} pass / {report.warn_count} warn / {report.fail_count} fail\n\n"
        + "\n".join(summary_lines)
    )
    upsert_memory(
        type="knowledge",
        name="brain_health_latest",
        description=f"Brain health: {report.overall} ({report.pass_count}P/{report.warn_count}W/{report.fail_count}F)",
        content=content,
        source="vps",
    )


def main():
    parser = argparse.ArgumentParser(description="Brain Health Checker")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-telegram", action="store_true")
    args = parser.parse_args()

    # Loud, non-blocking decommission notice (stderr — never corrupts --json stdout).
    sys.stderr.write(
        "[brain_health] ⚠️ DECOMMISSIONED: not scheduled; many checks validate the "
        "frozen 2026-04-22 VPS agent loop. A green result is NOT authoritative. "
        "See the brain/dead-layer-inventory memory.\n"
    )

    try:
        report = run_all_checks()
    except Exception as exc:  # pragma: no cover - diagnostic path
        import traceback
        ts = datetime.now(timezone.utc).isoformat()
        sys.stderr.write(f"[{ts}] brain_health CRASH during run_all_checks: {exc!r}\n")
        traceback.print_exc(file=sys.stderr)
        sys.exit(2)

    if args.json:
        print(json.dumps({"overall": report.overall, "checks": [asdict(c) for c in report.checks]}, indent=2))
    elif not args.quiet:
        icon = {"HEALTHY": "\U0001f7e2", "DEGRADED": "\U0001f7e1", "UNHEALTHY": "\U0001f534"}[report.overall]
        print(f"\n{icon} Brain Health: {report.overall}")
        print(f"   \u2705 {report.pass_count}  \u26a0\ufe0f {report.warn_count}  \u274c {report.fail_count}\n")
        for c in report.checks:
            sym = {"PASS": "\u2705", "WARN": "\u26a0\ufe0f", "FAIL": "\u274c"}[c.severity]
            if c.severity != "PASS":
                print(f"  {sym} {c.check_id} {c.name}: {c.message}")

    # Always emit FAILs to stderr so systemd logs diagnostics even under --quiet.
    if report.fail_count > 0:
        ts = datetime.now(timezone.utc).isoformat()
        sys.stderr.write(f"[{ts}] brain_health DEGRADED/UNHEALTHY - {report.fail_count} FAIL(s):\n")
        for c in report.checks:
            if c.severity == "FAIL":
                sys.stderr.write(f"  FAIL {c.check_id} {c.name}: {c.message}\n")

    try:
        if not args.no_telegram:
            send_telegram(report)
    except Exception as exc:  # pragma: no cover - network path
        sys.stderr.write(f"send_telegram failed: {exc!r}\n")
    try:
        write_to_supabase(report)
    except Exception as exc:  # pragma: no cover - network path
        sys.stderr.write(f"write_to_supabase failed: {exc!r}\n")

    sys.exit(0 if report.fail_count == 0 else 1)


if __name__ == "__main__":
    main()

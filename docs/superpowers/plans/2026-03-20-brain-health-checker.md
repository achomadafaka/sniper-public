# Brain Health Checker — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic Python health checker that validates all 31 failure scenarios across the brain (Supabase), agents, and data integrity — running every 6 hours via systemd timer, writing results to Supabase, and sending a Telegram summary.

**Architecture:** A single script (`scripts/brain_health.py`) that reads from Supabase + SQLite, runs ~31 deterministic checks (no LLM calls), scores each as PASS/WARN/FAIL, writes a structured health report to the `knowledge` section of Supabase's `memories` table, and sends a Telegram summary with any failures. Pure Python — no Claude API, no agent SDK, no budget.

**Tech Stack:** Python 3.12, SQLite, Supabase REST API (via `bot/supabase_helper.py`), Telegram Bot API

---

## File Structure

| File | Purpose |
|------|---------|
| `scripts/brain_health.py` | Main health checker — all 31 checks + Telegram + Supabase output |
| `tests/test_brain_health.py` | Tests for each check category with controlled fixtures |
| `systemd/sniper-brain-health.service` | Oneshot service |
| `systemd/sniper-brain-health.timer` | Every 6 hours |

---

## The 31 Checks (organized by category)

### Category A: Staleness (5 checks)

| # | Check | Logic | Severity |
|---|-------|-------|----------|
| A1 | market_boosts expired entries | Parse `market_boosts_current` JSON, check each entry's `expires_hours` against `updated_at` | WARN if any expired, FAIL if all expired |
| A2 | world_state freshness | `memories` where name=`world_state`: `updated_at` must be < 6h old | WARN if 6-12h, FAIL if >12h |
| A3 | world_events stale | `world_events` table: events with status `active`/`escalating` but `updated_at` > 48h | WARN per stale event |
| A4 | strategy_regimes_current freshness | `memories` name=`strategy_regimes_current`: `updated_at` < 12h | WARN if stale |
| A5 | agent observations referencing dead wallets | `strategy_agent_observations` from last 24h mentioning wallets not in `user_analysis` | WARN per orphan reference |

### Category B: Agent Heartbeats (5 checks)

| # | Check | Logic | Severity |
|---|-------|-------|----------|
| B1 | world_analyst last run | `agent_runs` where agent_type=`world_analyst`, latest `created_at` < 4h old | WARN if 4-8h, FAIL if >8h |
| B2 | wallet_scout last run | `agent_runs` where agent_type=`wallet_scout`, latest `created_at` < 18h old | WARN if 18-36h, FAIL if >36h |
| B3 | insider_copy last run | `agent_runs` where agent_type=`insider_copy`, latest `created_at` < 18h old | WARN if 18-36h, FAIL if >36h |
| B4 | news_intel timer last run | SQLite `news_items`: max(`published_at`) < 4h old | WARN if 4-8h, FAIL if >8h |
| B5 | incomplete agent runs | `agent_runs` with `created_at` in last 24h but matching agent has no `run_summary` observation within 30min of that run | WARN per incomplete |

### Category C: Prediction Hygiene (3 checks)

| # | Check | Logic | Severity |
|---|-------|-------|----------|
| C1 | ungraded predictions past due | `agent_predictions` where `was_correct IS NULL` AND `verify_after < NOW()` | WARN if 1-5, FAIL if >5 |
| C2 | prediction accuracy declining | Per agent: if 10+ graded predictions, check if last-10 accuracy < overall accuracy by >15pts | WARN per declining agent |
| C3 | zero lessons learned | `agent_lessons` count = 0 despite `agent_predictions` graded count > 15 | WARN |

### Category D: Cross-Agent Coherence (5 checks)

| # | Check | Logic | Severity |
|---|-------|-------|----------|
| D1 | scout stopped, insider_copy still referencing | `strategy_agent_observations` from insider_copy in last 24h mentioning a strategy_id that Scout stopped | WARN |
| D2 | negative boost on active position | `market_boosts_current` has boost <= -15 for a condition_id that has an open `strategy_positions` entry | WARN per conflict |
| D3 | contradicting predictions | Two agents predicting opposite outcomes on same market within 48h | WARN |
| D4 | scout created + insider_copy flags same wallet | Scout's latest observation says CREATE for wallet X, Insider Copy's latest says FLAG for same wallet | WARN |
| D5 | world_analyst boost vs news_intel idea conflict | `market_boosts_current` has negative boost for a condition_id with an open HIGH trade_idea | WARN |

### Category E: Data Integrity (6 checks)

| # | Check | Logic | Severity |
|---|-------|-------|----------|
| E1 | dual-path world events sync | Compare count of `world_event:*` prefix in `memories` vs `world_events` table rows with `status != 'resolved'` | WARN if counts differ by >2 |
| E2 | strategy DB vs observation mismatch | Active strategies in SQLite whose latest Scout observation says "STOPPED" | FAIL per mismatch |
| E3 | traded ideas without positions | SQLite `trade_ideas` with status=`acted` but no corresponding `strategy_positions` entry | WARN per orphan |
| E4 | boost references unknown markets | `market_boosts_current` condition_ids not found in `market_metadata` table | WARN per orphan |
| E5 | agent runs with zero observations | `agent_runs` in last 48h where the agent wrote 0 observations within that run's timeframe | WARN per empty run |
| E6 | strategy status consistency | SQLite strategies with status=`active` but wallet not in `user_analysis` (same check Scout does, but as a health gate) | FAIL per inconsistency |

### Category F: Brain Bloat (4 checks)

| # | Check | Logic | Severity |
|---|-------|-------|----------|
| F1 | observation count growth | `strategy_agent_observations` total count. WARN if >200, FAIL if >500 | WARN/FAIL |
| F2 | stale knowledge entries | `memories` with type=`knowledge`, `updated_at` > 7 days, name NOT in `world_state`/`market_boosts_current` | WARN with list |
| F3 | world_state content size | `memories` name=`world_state` content length > 5000 chars | WARN (bloat creep) |
| F4 | prediction backlog | Total ungraded `agent_predictions` > 25 | WARN if >25, FAIL if >50 |

### Category G: Performance Degradation (3 checks)

| # | Check | Logic | Severity |
|---|-------|-------|----------|
| G1 | agent cost trend | Per agent: compare avg cost of last 3 runs vs avg of runs 4-10. WARN if >30% increase | WARN per agent |
| G2 | agent turn trend | Per agent: same comparison for turns | WARN per agent |
| G3 | market_opportunities empty | `market_opportunities` table has 0 rows despite World Analyst running 5+ times | WARN |

---

## Chunk 1: Core Health Checker Script

### Task 1: Scaffold and Supabase/SQLite read helpers

**Files:**
- Create: `scripts/brain_health.py`
- Test: `tests/test_brain_health.py`

- [ ] **Step 1: Write the scaffold with helpers**

```python
#!/usr/bin/env python3
"""Brain Health Checker — deterministic validation of all brain subsystems.

Runs every 6h via systemd timer. No LLM calls. Pure Python checks against
Supabase (brain) and SQLite (trading DB). Reports to Telegram + Supabase.

Usage:
    python3 scripts/brain_health.py                # full check
    python3 scripts/brain_health.py --quiet        # no terminal output
    python3 scripts/brain_health.py --json         # JSON output only
"""
import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

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
SB_KEY = os.getenv("SUPABASE_ANON_KEY", "")
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN_AI", os.getenv("TELEGRAM_BOT_TOKEN", ""))
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID", "")


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
        if result.severity == "PASS": self.pass_count += 1
        elif result.severity == "WARN": self.warn_count += 1
        else: self.fail_count += 1

    @property
    def overall(self) -> str:
        if self.fail_count > 0: return "UNHEALTHY"
        if self.warn_count > 0: return "DEGRADED"
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
    """Fetch a single memory by name."""
    rows = sb_get(f"memories?select=*&name=eq.{urllib.parse.quote(name)}&limit=1")
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
```

- [ ] **Step 2: Write initial test verifying scaffold imports**

```python
# tests/test_brain_health.py
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_imports():
    from scripts.brain_health import CheckResult, HealthReport
    r = HealthReport(timestamp="2026-03-20T12:00:00Z")
    r.add(CheckResult("A1", "test", "PASS", "ok", "A"))
    assert r.pass_count == 1
    assert r.overall == "HEALTHY"
    r.add(CheckResult("A2", "test2", "FAIL", "bad", "A"))
    assert r.overall == "UNHEALTHY"
```

- [ ] **Step 3: Run test**

Run: `python3 -m pytest tests/test_brain_health.py::test_imports -v`

- [ ] **Step 4: Commit scaffold**

---

### Task 2: Category A — Staleness checks (A1-A5)

**Files:**
- Modify: `scripts/brain_health.py`
- Modify: `tests/test_brain_health.py`

- [ ] **Step 1: Implement all 5 staleness checks**

```python
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
    if age_h > 12:
        report.add(CheckResult("A2", "World state freshness", "FAIL", f"world_state is {age_h:.1f}h old (>12h)", "A"))
    elif age_h > 6:
        report.add(CheckResult("A2", "World state freshness", "WARN", f"world_state is {age_h:.1f}h old (>6h)", "A"))
    else:
        report.add(CheckResult("A2", "World state freshness", "PASS", f"world_state is {age_h:.1f}h old", "A"))


def check_a3_stale_world_events(report: HealthReport):
    """A3: world_events with active status but old updated_at."""
    events = sb_get("world_events?select=slug,status,last_updated&status=in.(active,escalating,developing)")
    if not events:
        report.add(CheckResult("A3", "World events staleness", "PASS", "No active world events", "A"))
        return
    stale = [e for e in events if hours_ago(parse_ts(e.get("last_updated"))) > 48]
    if stale:
        slugs = [e.get("slug", "?")[:20] for e in stale[:3]]
        report.add(CheckResult("A3", "World events staleness", "WARN", f"{len(stale)} events >48h old: {', '.join(slugs)}", "A"))
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
        addrs = re.findall(r'0x[a-fA-F0-9]{10,42}', str(o.get("content", "")))
        mentioned.update(a.lower() for a in addrs)
    if not mentioned:
        report.add(CheckResult("A5", "Orphan wallet refs", "PASS", "No wallet addresses in recent observations", "A"))
        return
    conn = db_conn()
    existing = set()
    for row in conn.execute("SELECT wallet FROM user_analysis").fetchall():
        existing.add(row[0].lower())
    conn.close()
    orphans = mentioned - existing
    # Filter out very short partial matches
    orphans = {a for a in orphans if len(a) >= 20}
    if orphans:
        report.add(CheckResult("A5", "Orphan wallet refs", "WARN", f"{len(orphans)} wallets in observations not in user_analysis", "A"))
    else:
        report.add(CheckResult("A5", "Orphan wallet refs", "PASS", f"All {len(mentioned)} referenced wallets exist", "A"))
```

- [ ] **Step 2: Write tests for category A**

```python
def test_a1_all_expired():
    from scripts.brain_health import CheckResult, HealthReport, check_a1_boost_expiry
    # Mock sb_memory to return expired data — tested via integration test below

def test_health_report_severity_ordering():
    from scripts.brain_health import CheckResult, HealthReport
    r = HealthReport(timestamp="t")
    r.add(CheckResult("X1", "t", "PASS", "", "X"))
    assert r.overall == "HEALTHY"
    r.add(CheckResult("X2", "t", "WARN", "", "X"))
    assert r.overall == "DEGRADED"
    r.add(CheckResult("X3", "t", "FAIL", "", "X"))
    assert r.overall == "UNHEALTHY"
```

- [ ] **Step 3: Run tests + full suite**
- [ ] **Step 4: Commit**

---

### Task 3: Category B — Agent Heartbeats (B1-B5)

**Files:**
- Modify: `scripts/brain_health.py`

- [ ] **Step 1: Implement heartbeat checks**

```python
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


def check_b1(report): _check_agent_heartbeat(report, "B1", "world_analyst", 4, 8)
def check_b2(report): _check_agent_heartbeat(report, "B2", "wallet_scout", 18, 36)
def check_b3(report): _check_agent_heartbeat(report, "B3", "insider_copy", 18, 36)


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
    for o in obs:
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
    if incomplete:
        report.add(CheckResult("B5", "Incomplete runs", "WARN", f"{len(incomplete)} runs without run_summary: {', '.join(incomplete[:5])}", "B"))
    else:
        report.add(CheckResult("B5", "Incomplete runs", "PASS", f"{len(runs)} runs all have summaries", "B"))
```

- [ ] **Step 2: Commit**

---

### Task 4: Category C — Prediction Hygiene (C1-C3)

**Files:**
- Modify: `scripts/brain_health.py`

- [ ] **Step 1: Implement prediction checks**

```python
def check_c1_ungraded_predictions(report: HealthReport):
    """C1: predictions past verify_after that haven't been graded."""
    now_iso = NOW.isoformat()
    preds = sb_get(f"agent_predictions?select=id,agent_type,prediction&was_correct=is.null&verify_after=lt.{urllib.parse.quote(now_iso)}")
    count = len(preds) if preds else 0
    if count > 5:
        agents = set(p.get("agent_type", "?") for p in preds)
        report.add(CheckResult("C1", "Ungraded predictions", "FAIL", f"{count} predictions overdue: agents={', '.join(agents)}", "C"))
    elif count > 0:
        report.add(CheckResult("C1", "Ungraded predictions", "WARN", f"{count} predictions past verify_after", "C"))
    else:
        report.add(CheckResult("C1", "Ungraded predictions", "PASS", "All predictions graded on time", "C"))


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
```

- [ ] **Step 2: Commit**

---

### Task 5: Category D — Cross-Agent Coherence (D1-D5)

**Files:**
- Modify: `scripts/brain_health.py`

- [ ] **Step 1: Implement coherence checks**

```python
def check_d1_scout_stop_vs_insider_thesis(report: HealthReport):
    """D1: Scout stopped a strategy that Insider Copy still references positively."""
    cutoff = (NOW - timedelta(hours=48)).isoformat()
    scout_obs = sb_get(f"strategy_agent_observations?select=content&strategy_type=eq.wallet_scout&obs_type=eq.action_taken&created_at=gte.{urllib.parse.quote(cutoff)}")
    insider_obs = sb_get(f"strategy_agent_observations?select=content&strategy_type=eq.insider_copy_v1&created_at=gte.{urllib.parse.quote(cutoff)}")
    stopped_ids = set()
    for o in scout_obs:
        content = str(o.get("content", ""))
        if "STOPPED" in content.upper():
            ids = re.findall(r'id=(\d+)', content)
            stopped_ids.update(ids)
    if not stopped_ids:
        report.add(CheckResult("D1", "Scout-Insider coherence", "PASS", "No recent stops by Scout", "D"))
        return
    conflicts = []
    for o in insider_obs:
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
    negative = {cid: info for cid, info in boosts.items() if float(info.get("boost", 0)) <= -15}
    if not negative:
        report.add(CheckResult("D2", "Boost vs positions", "PASS", "No strongly negative boosts", "D"))
        return
    conn = db_conn()
    conflicts = []
    for cid in negative:
        row = conn.execute("SELECT condition_id FROM strategy_positions WHERE condition_id=? AND state='open'", (cid,)).fetchone()
        if row:
            conflicts.append(f"{cid[:12]}(boost={negative[cid].get('boost')})")
    conn.close()
    if conflicts:
        report.add(CheckResult("D2", "Boost vs positions", "WARN", f"Open positions with negative boosts: {', '.join(conflicts)}", "D"))
    else:
        report.add(CheckResult("D2", "Boost vs positions", "PASS", "No conflicts", "D"))


def check_d3_contradicting_predictions(report: HealthReport):
    """D3: different agents predicting opposite outcomes on same topic."""
    cutoff = (NOW - timedelta(hours=48)).isoformat()
    preds = sb_get(f"agent_predictions?select=agent_type,prediction,confidence&was_correct=is.null&created_at=gte.{urllib.parse.quote(cutoff)}")
    if len(preds) < 2:
        report.add(CheckResult("D3", "Prediction contradictions", "PASS", "Too few active predictions to check", "D"))
        return
    # Heuristic: check for opposing directional words from different agents on overlapping keywords
    # Group predictions by agent, then cross-compare for common nouns + opposite sentiment
    UP_WORDS = {"above", "rise", "break", "hit", "stay above", "increase", "bull"}
    DOWN_WORDS = {"below", "drop", "fall", "decline", "crash", "stay below", "bear"}
    by_agent: dict[str, list[str]] = {}
    for p in preds:
        at = p.get("agent_type", "?")
        text = str(p.get("prediction", "")).lower()
        by_agent.setdefault(at, []).append(text)
    agents = list(by_agent.keys())
    conflicts = []
    for i in range(len(agents)):
        for j in range(i + 1, len(agents)):
            for p1 in by_agent[agents[i]]:
                for p2 in by_agent[agents[j]]:
                    p1_up = any(w in p1 for w in UP_WORDS)
                    p1_down = any(w in p1 for w in DOWN_WORDS)
                    p2_up = any(w in p2 for w in UP_WORDS)
                    p2_down = any(w in p2 for w in DOWN_WORDS)
                    # Check for shared keywords (nouns > 4 chars) + opposite direction
                    shared = set(w for w in p1.split() if len(w) > 4) & set(w for w in p2.split() if len(w) > 4)
                    if shared and ((p1_up and p2_down) or (p1_down and p2_up)):
                        conflicts.append(f"{agents[i]} vs {agents[j]} on '{list(shared)[0]}'")
    if conflicts:
        report.add(CheckResult("D3", "Prediction contradictions", "WARN", f"{len(conflicts)} potential conflicts: {'; '.join(conflicts[:3])}", "D"))
    else:
        report.add(CheckResult("D3", "Prediction contradictions", "PASS", f"{len(preds)} predictions, no contradictions detected", "D"))


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
        addrs = re.findall(r'0x[a-fA-F0-9]{10,42}', str(o.get("content", "")))
        created_wallets.update(a.lower() for a in addrs)
    flagged_wallets = set()
    for o in insider:
        addrs = re.findall(r'0x[a-fA-F0-9]{10,42}', str(o.get("content", "")))
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
    negative_cids = {cid for cid, info in boosts.items() if float(info.get("boost", 0)) < 0}
    if not negative_cids:
        report.add(CheckResult("D5", "Boost vs trade ideas", "PASS", "No negative boosts", "D"))
        return
    conn = db_conn()
    conflicts = []
    for cid in negative_cids:
        row = conn.execute("SELECT market_title FROM trade_ideas WHERE condition_id=? AND status='open' AND confidence='high'", (cid,)).fetchone()
        if row:
            conflicts.append(row["market_title"][:30])
    conn.close()
    if conflicts:
        report.add(CheckResult("D5", "Boost vs trade ideas", "WARN", f"Negative boost on HIGH ideas: {', '.join(conflicts)}", "D"))
    else:
        report.add(CheckResult("D5", "Boost vs trade ideas", "PASS", "No conflicts", "D"))
```

- [ ] **Step 2: Commit**

---

### Task 6: Category E — Data Integrity (E1-E6)

**Files:**
- Modify: `scripts/brain_health.py`

- [ ] **Step 1: Implement integrity checks**

```python
def check_e1_dual_path_world_events(report: HealthReport):
    """E1: world_event:* memories vs world_events table sync."""
    mems = sb_get("memories?select=name&name=like.world_event:*&type=eq.reference")
    mem_count = len(mems) if mems else 0
    events = sb_get("world_events?select=slug&status=neq.resolved")
    event_count = len(events) if events else 0
    diff = abs(mem_count - event_count)
    if diff > 2:
        report.add(CheckResult("E1", "Dual-path events sync", "WARN",
            f"memories has {mem_count} world_event:* entries, world_events table has {event_count} active (diff={diff})", "E"))
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
    for o in scout_obs:
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
        return
    if not boosts:
        report.add(CheckResult("E4", "Boost unknown markets", "PASS", "No boosts", "E"))
        return
    conn = db_conn()
    unknown = []
    for cid in boosts:
        row = conn.execute("SELECT 1 FROM market_metadata WHERE condition_id=?", (cid,)).fetchone()
        if not row:
            unknown.append(cid[:12])
    conn.close()
    if unknown:
        report.add(CheckResult("E4", "Boost unknown markets", "WARN", f"{len(unknown)} boost targets not in market_metadata", "E"))
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
```

- [ ] **Step 2: Commit**

---

### Task 7: Category F — Brain Bloat (F1-F4) + Category G — Performance (G1-G3)

**Files:**
- Modify: `scripts/brain_health.py`

- [ ] **Step 1: Implement bloat + performance checks**

```python
def check_f1_observation_count(report: HealthReport):
    """F1: total observation count growth."""
    count = sb_count("strategy_agent_observations")
    if count > 500:
        report.add(CheckResult("F1", "Observation bloat", "FAIL", f"{count} observations (>500)", "F"))
    elif count > 200:
        report.add(CheckResult("F1", "Observation bloat", "WARN", f"{count} observations (>200)", "F"))
    else:
        report.add(CheckResult("F1", "Observation bloat", "PASS", f"{count} observations", "F"))


def check_f2_stale_knowledge(report: HealthReport):
    """F2: knowledge memories not updated in 7+ days."""
    mems = sb_get("memories?select=name,updated_at&type=eq.knowledge")
    exempt = {"world_state", "market_boosts_current"}  # These are checked separately
    stale = []
    for m in (mems or []):
        if m.get("name") in exempt:
            continue
        age_h = hours_ago(parse_ts(m.get("updated_at")))
        if age_h > 168:  # 7 days
            stale.append(m.get("name", "?")[:30])
    if stale:
        report.add(CheckResult("F2", "Stale knowledge", "WARN", f"{len(stale)} knowledge entries >7d old: {', '.join(stale[:5])}", "F"))
    else:
        report.add(CheckResult("F2", "Stale knowledge", "PASS", "All knowledge entries fresh", "F"))


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
    """F4: total ungraded predictions."""
    preds = sb_get("agent_predictions?select=id&was_correct=is.null")
    count = len(preds) if preds else 0
    if count > 50:
        report.add(CheckResult("F4", "Prediction backlog", "FAIL", f"{count} ungraded predictions (>50)", "F"))
    elif count > 25:
        report.add(CheckResult("F4", "Prediction backlog", "WARN", f"{count} ungraded predictions (>25)", "F"))
    else:
        report.add(CheckResult("F4", "Prediction backlog", "PASS", f"{count} ungraded", "F"))


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
        by_agent.setdefault(at, []).append(cost)
    increasing = []
    for agent, costs in by_agent.items():
        if len(costs) < 4:
            continue
        recent_avg = sum(costs[:3]) / 3
        older_avg = sum(costs[3:min(10, len(costs))]) / len(costs[3:min(10, len(costs))])
        if older_avg > 0 and (recent_avg - older_avg) / older_avg > 0.30:
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
        by_agent.setdefault(at, []).append(turns)
    increasing = []
    for agent, turns in by_agent.items():
        if len(turns) < 4:
            continue
        recent_avg = sum(turns[:3]) / 3
        older_avg = sum(turns[3:min(10, len(turns))]) / len(turns[3:min(10, len(turns))])
        if older_avg > 0 and (recent_avg - older_avg) / older_avg > 0.30:
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
    if count == 0 and run_count >= 5:
        report.add(CheckResult("G3", "Empty opportunities", "WARN",
            f"0 market opportunities despite {run_count} World Analyst runs", "G"))
    else:
        report.add(CheckResult("G3", "Empty opportunities", "PASS", f"{count} opportunities from {run_count} runs", "G"))
```

- [ ] **Step 2: Commit**

---

## Chunk 2: Runner, Output, and Deployment

### Task 8: Main runner + Telegram + Supabase output

**Files:**
- Modify: `scripts/brain_health.py`

- [ ] **Step 1: Add the main runner that calls all checks and outputs results**

```python
# ── All checks registry ───────────────────────────────────────────────────

ALL_CHECKS = [
    # A: Staleness
    check_a1_boost_expiry, check_a2_world_state_freshness,
    check_a3_stale_world_events, check_a4_regime_freshness,
    check_a5_orphan_wallet_references,
    # B: Heartbeats
    check_b1, check_b2, check_b3, check_b4_news_intel_heartbeat,
    check_b5_incomplete_runs,
    # C: Predictions
    check_c1_ungraded_predictions, check_c2_accuracy_declining,
    check_c3_zero_lessons,
    # D: Coherence
    check_d1_scout_stop_vs_insider_thesis, check_d2_negative_boost_on_open_position,
    check_d3_contradicting_predictions, check_d4_scout_create_vs_insider_flag,
    check_d5_boost_vs_trade_idea,
    # E: Integrity
    check_e1_dual_path_world_events, check_e2_strategy_db_vs_observations,
    check_e3_acted_ideas_without_positions, check_e4_boost_unknown_markets,
    check_e5_empty_agent_runs, check_e6_active_strategy_dead_wallet,
    # F: Bloat
    check_f1_observation_count, check_f2_stale_knowledge,
    check_f3_world_state_size, check_f4_prediction_backlog,
    # G: Performance
    check_g1_cost_trend, check_g2_turn_trend, check_g3_empty_opportunities,
]


def run_all_checks() -> HealthReport:
    """Execute all 31 health checks and return the report."""
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
    icon = {"HEALTHY": "🟢", "DEGRADED": "🟡", "UNHEALTHY": "🔴"}[report.overall]
    lines = [f"{icon} <b>Brain Health: {report.overall}</b>",
             f"✅ {report.pass_count}  ⚠️ {report.warn_count}  ❌ {report.fail_count}"]
    # Add FAIL and WARN details (escape HTML in message content)
    from html import escape as esc
    for c in report.checks:
        if c.severity == "FAIL":
            lines.append(f"❌ <b>{c.check_id}</b> {esc(c.name)}: {esc(c.message[:100])}")
    for c in report.checks:
        if c.severity == "WARN":
            lines.append(f"⚠️ {c.check_id} {esc(c.name)}: {esc(c.message[:80])}")
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
        icon = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}[c.severity]
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

    report = run_all_checks()

    if args.json:
        print(json.dumps({"overall": report.overall, "checks": [asdict(c) for c in report.checks]}, indent=2))
    elif not args.quiet:
        icon = {"HEALTHY": "🟢", "DEGRADED": "🟡", "UNHEALTHY": "🔴"}[report.overall]
        print(f"\n{icon} Brain Health: {report.overall}")
        print(f"   ✅ {report.pass_count}  ⚠️ {report.warn_count}  ❌ {report.fail_count}\n")
        for c in report.checks:
            sym = {"PASS": "✅", "WARN": "⚠️", "FAIL": "❌"}[c.severity]
            if c.severity != "PASS":
                print(f"  {sym} {c.check_id} {c.name}: {c.message}")

    if not args.no_telegram:
        send_telegram(report)
    write_to_supabase(report)

    sys.exit(0 if report.fail_count == 0 else 1)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

---

### Task 9: Tests

**Files:**
- Create: `tests/test_brain_health.py`

- [ ] **Step 1: Write comprehensive tests**

Test the report structure, severity logic, and any check that can be tested with mocked data.

```python
# tests/test_brain_health.py
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.brain_health import CheckResult, HealthReport


class TestHealthReport:
    def test_empty_report_is_healthy(self):
        r = HealthReport(timestamp="t")
        assert r.overall == "HEALTHY"

    def test_warn_makes_degraded(self):
        r = HealthReport(timestamp="t")
        r.add(CheckResult("X1", "test", "PASS", "", "X"))
        r.add(CheckResult("X2", "test", "WARN", "", "X"))
        assert r.overall == "DEGRADED"
        assert r.warn_count == 1

    def test_fail_makes_unhealthy(self):
        r = HealthReport(timestamp="t")
        r.add(CheckResult("X1", "test", "FAIL", "", "X"))
        assert r.overall == "UNHEALTHY"
        assert r.fail_count == 1

    def test_fail_overrides_warn(self):
        r = HealthReport(timestamp="t")
        r.add(CheckResult("X1", "test", "WARN", "", "X"))
        r.add(CheckResult("X2", "test", "FAIL", "", "X"))
        assert r.overall == "UNHEALTHY"

    def test_counts_accumulate(self):
        r = HealthReport(timestamp="t")
        for i in range(5):
            r.add(CheckResult(f"P{i}", "test", "PASS", "", "X"))
        for i in range(3):
            r.add(CheckResult(f"W{i}", "test", "WARN", "", "X"))
        r.add(CheckResult("F1", "test", "FAIL", "", "X"))
        assert r.pass_count == 5
        assert r.warn_count == 3
        assert r.fail_count == 1

    def test_31_checks_registered(self):
        from scripts.brain_health import ALL_CHECKS
        assert len(ALL_CHECKS) == 31, f"Expected 31 checks, got {len(ALL_CHECKS)}"


class TestTimestampHelpers:
    def test_parse_ts_iso(self):
        from scripts.brain_health import parse_ts
        dt = parse_ts("2026-03-20T12:00:00+00:00")
        assert dt is not None
        assert dt.year == 2026

    def test_parse_ts_none(self):
        from scripts.brain_health import parse_ts
        assert parse_ts(None) is None
        assert parse_ts("") is None

    def test_hours_ago_none(self):
        from scripts.brain_health import hours_ago
        assert hours_ago(None) == 9999.0
```

- [ ] **Step 2: Run tests + full suite**

Run: `python3 -m pytest tests/test_brain_health.py -v && python3 -m pytest tests/ -q`

- [ ] **Step 3: Commit**

---

### Task 10: Systemd timer + deployment

**Files:**
- Create: `systemd/sniper-brain-health.service`
- Create: `systemd/sniper-brain-health.timer`

- [ ] **Step 1: Create service**

```ini
[Unit]
Description=Sniper Brain Health Checker
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/home/sniper/sniper
EnvironmentFile=-/home/sniper/sniper/.env
ExecStart=/home/sniper/sniper/.venv/bin/python3 /home/sniper/sniper/scripts/brain_health.py --quiet
StandardOutput=append:/home/sniper/sniper/logs/brain_health.log
StandardError=append:/home/sniper/sniper/logs/brain_health.log
TimeoutStartSec=120
Restart=no

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Create timer**

```ini
[Unit]
Description=Timer — run Brain Health Checker every 6 hours
Requires=sniper-brain-health.service

[Timer]
OnCalendar=*-*-* 00,06,12,18:30:00
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Deploy to VPS**

```bash
git push origin main
ssh sniper@188.166.91.20 'cd ~/sniper && git pull origin main'
ssh sniper@188.166.91.20 'cp ~/sniper/systemd/sniper-brain-health.service ~/.config/systemd/user/ && cp ~/sniper/systemd/sniper-brain-health.timer ~/.config/systemd/user/'
ssh sniper@188.166.91.20 'systemctl --user daemon-reload && systemctl --user enable --now sniper-brain-health.timer'
```

- [ ] **Step 4: Manual test run**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && export $(grep -E "^(SUPABASE|TELEGRAM|DB_PATH)" .env | xargs) && .venv/bin/python3 scripts/brain_health.py'
```

- [ ] **Step 5: Verify Telegram received + Supabase updated**

- [ ] **Step 6: Commit deployment files**

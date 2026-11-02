# Claude Code Automations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add 5 Claude Code automations — secret-file blocker hook, scoring watchdog agent, wallet research skill, paper trade analyst agent, expanded VPS health agent.

**Architecture:** Each automation is a standalone file (Python script, agent markdown, skill markdown, or settings JSON merge). File ownership is fully disjoint — all 5 tasks can run in parallel. No shared code, no cross-dependencies.

**Tech Stack:** Python (secret_file_guard.py), Markdown (agent/skill definitions), JSON (settings.local.json merge)

**Spec:** `docs/superpowers/specs/2026-03-26-claude-code-automations-design.md`

---

## File Map

| File | Role | Action |
|------|------|--------|
| `scripts/secret_file_guard.py` | PreToolUse hook script | Create |
| `tests/test_secret_file_guard.py` | Tests for hook script | Create |
| `.claude/settings.local.json` | Hook configuration | Modify (merge PreToolUse) |
| `.claude/agents/scoring-watchdog.md` | Scoring watchdog agent | Create |
| `.claude/skills/wallet-research/SKILL.md` | Wallet research skill | Create |
| `.claude/agents/paper-trade-analyst.md` | Paper trade analyst agent | Create |
| `.claude/agents/vps-health.md` | VPS health agent | Modify (expand) |

---

### Task 1: PreToolUse Secret-File Blocker Hook

**Files:**
- Create: `scripts/secret_file_guard.py`
- Create: `tests/test_secret_file_guard.py`
- Modify: `.claude/settings.local.json`

- [ ] **Step 1: Write tests for secret_file_guard**

```python
# tests/test_secret_file_guard.py
"""Tests for the PreToolUse secret-file blocker hook."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = str(Path(__file__).resolve().parents[1] / "scripts" / "secret_file_guard.py")


def _run_guard(file_path: str) -> subprocess.CompletedProcess:
    """Run the guard script with a simulated TOOL_INPUT payload."""
    payload = json.dumps({"file_path": file_path})
    return subprocess.run(
        [sys.executable, SCRIPT],
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
    )


class TestSecretFileGuard:
    """Path-based blocking rules."""

    def test_blocks_dot_env(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env")
        assert r.returncode == 1
        assert "BLOCKED" in r.stderr

    def test_blocks_dot_env_production(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env.production")
        assert r.returncode == 1

    def test_allows_dot_env_example(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env.example")
        assert r.returncode == 0

    def test_allows_dot_env_sample(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env.sample")
        assert r.returncode == 0

    def test_blocks_pem_file(self):
        r = _run_guard("/home/user/certs/server.pem")
        assert r.returncode == 1
        assert "private key" in r.stderr.lower() or "BLOCKED" in r.stderr

    def test_blocks_key_file(self):
        r = _run_guard("/home/user/ssh/id_rsa.key")
        assert r.returncode == 1

    def test_blocks_secrets_dir(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/secrets/api_keys.json")
        assert r.returncode == 1

    def test_blocks_credentials_dir(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/credentials/gcp.json")
        assert r.returncode == 1

    def test_allows_config_dir(self):
        """config/ is NOT blocked — contains normal working files."""
        r = _run_guard("/home/bigpapi/Desktop/Sniper/config/ui_live_audit_manifest.yaml")
        assert r.returncode == 0

    def test_allows_normal_python(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/paper_trader.py")
        assert r.returncode == 0

    def test_allows_normal_markdown(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/CLAUDE.md")
        assert r.returncode == 0

    def test_blocks_nested_secrets(self):
        r = _run_guard("/home/bigpapi/project/secrets/nested/deep/token.txt")
        assert r.returncode == 1

    def test_handles_relative_path(self):
        r = _run_guard(".env")
        assert r.returncode == 1

    def test_error_message_names_rule(self):
        r = _run_guard("/home/bigpapi/Desktop/Sniper/.env")
        assert "Matched rule:" in r.stderr

    def test_handles_missing_file_path(self):
        """If TOOL_INPUT has no file_path, allow through (not our concern)."""
        payload = json.dumps({"content": "some text"})
        r = subprocess.run(
            [sys.executable, SCRIPT],
            input=payload,
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert r.returncode == 0

    def test_handles_env_var_input(self):
        """Script should read from TOOL_INPUT env var as fallback."""
        payload = json.dumps({"file_path": "/home/user/.env"})
        r = subprocess.run(
            [sys.executable, SCRIPT],
            input="",
            capture_output=True,
            text=True,
            timeout=5,
            env={**dict(__import__("os").environ), "TOOL_INPUT": payload},
        )
        assert r.returncode == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_secret_file_guard.py -v`
Expected: FAIL — script doesn't exist yet

- [ ] **Step 3: Implement secret_file_guard.py**

```python
#!/usr/bin/env python3
"""PreToolUse hook: block Edit/Write on sensitive files (path-based).

Reads TOOL_INPUT from env var (with stdin fallback), extracts file_path,
checks against blocked path patterns. Exit 0 = allow, exit 1 = block.

Known limitation: blocks Edit/Write tool usage only. Does not stop
Bash-based file mutation (sed, python -c, echo >>).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import PurePosixPath


# ── Blocked path rules ────────────────────────────────────────────────
RULES: list[tuple[str, re.Pattern | None, str]] = [
    # (description, compiled regex on resolved basename/path, human label)
]


def _is_blocked(file_path: str) -> tuple[bool, str]:
    """Check if a file path matches any blocked pattern.

    Returns (blocked: bool, rule_description: str).
    """
    # Normalize: resolve relative paths, lowercase for matching
    path = PurePosixPath(file_path)
    name = path.name
    parts = path.parts

    # Rule 1: .env exact or .env.* (except .env.example, .env.sample)
    if name == ".env":
        return True, ".env files"
    if name.startswith(".env."):
        if name in (".env.example", ".env.sample"):
            return False, ""
        return True, f".env variant files ({name})"

    # Rule 2: *.pem, *.key
    if name.endswith(".pem"):
        return True, "*.pem private key / certificate files"
    if name.endswith(".key"):
        return True, "*.key private key files"

    # Rule 3: anything under secrets/ or credentials/ at any depth
    for part in parts:
        if part in ("secrets", "credentials"):
            return True, f"files under {part}/ directory"

    return False, ""


def main() -> int:
    # Read input: TOOL_INPUT env var first, stdin fallback
    raw = os.environ.get("TOOL_INPUT", "")
    if not raw:
        try:
            raw = sys.stdin.read()
        except Exception:
            pass

    if not raw or not raw.strip():
        return 0  # no input = not our concern

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return 0  # unparseable = not our concern

    file_path = payload.get("file_path", "")
    if not file_path:
        return 0  # no file_path in payload = not a file edit

    blocked, rule = _is_blocked(str(file_path))
    if blocked:
        msg = (
            f"BLOCKED: Direct edits to sensitive files are disabled.\n"
            f"Matched rule: {rule}\n"
            f"If you need to update this file, ask the user to edit it "
            f"directly or use .env.example as a template."
        )
        print(msg, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_secret_file_guard.py -v`
Expected: All PASS

- [ ] **Step 5: Merge PreToolUse hook into settings.local.json**

Read `.claude/settings.local.json`, add `PreToolUse` array alongside the existing `PostToolUse`. The final `hooks` section should be:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "cd /home/bigpapi/Desktop/Sniper && python3 scripts/secret_file_guard.py",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "cd /home/bigpapi/Desktop/Sniper && python3 scripts/targeted_test_hook.py",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

**CRITICAL:** Preserve all other keys (permissions, enableAllProjectMcpServers, enabledMcpjsonServers, remote). Only modify the `hooks` object.

- [ ] **Step 6: Run full test suite**

Run: `python3 -m pytest tests/ -q --tb=short`
Expected: All pass (1696+ passing, 0 failures)

- [ ] **Step 7: Commit**

```bash
git add scripts/secret_file_guard.py tests/test_secret_file_guard.py .claude/settings.local.json
git commit -m "feat: PreToolUse secret-file blocker hook (path-based)"
```

---

### Task 2: Scoring Watchdog Agent

**Files:**
- Create: `.claude/agents/scoring-watchdog.md`

- [ ] **Step 1: Write the agent definition**

Create `.claude/agents/scoring-watchdog.md` with the following content. Follow the convention from existing agents (vps-health.md, insider-copy-agent.md): frontmatter with name/description/examples/model/tools, then structured instructions.

```markdown
---
name: scoring-watchdog
description: Monitor wallet_quality_score rollout health — population progress, distribution, hard filter breakdown, anomaly detection, paper trade leakage, known wallet spot-checks, and rollout stall detection. Use after scoring deploys, wallet quality changes, or when asked about scoring health.

<example>
Context: User just deployed a scoring change
user: "Check the scoring health"
assistant: "I'll use the scoring-watchdog agent to verify the wallet_quality_score rollout."
<commentary>
Scoring deployment triggers this agent.
</commentary>
</example>

<example>
Context: User wants to verify scoring is progressing
user: "How many wallets are scored now?"
assistant: "Let me check with the scoring-watchdog agent."
<commentary>
Scoring progress question triggers this agent.
</commentary>
</example>

model: sonnet
tools: ["Bash", "Read"]
---

You are the scoring health watchdog for the Sniper wallet quality system.

**SSH access:** `ssh sniper@188.166.91.20`

Run these checks in order on the VPS database and report a structured summary.

## 1. Population Progress
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3, time
c = sqlite3.connect(\"sniper_tracker.db\")
total = c.execute(\"SELECT COUNT(*) FROM user_analysis\").fetchone()[0]
try:
    scored = c.execute(\"SELECT COUNT(*) FROM user_analysis WHERE wallet_quality_score IS NOT NULL\").fetchone()[0]
    eligible = c.execute(\"SELECT COUNT(*) FROM user_analysis WHERE wallet_quality_eligible = 1\").fetchone()[0]
    ineligible = c.execute(\"SELECT COUNT(*) FROM user_analysis WHERE wallet_quality_eligible = 0\").fetchone()[0]
    unscored = c.execute(\"SELECT COUNT(*) FROM user_analysis WHERE wallet_quality_eligible IS NULL\").fetchone()[0]
    print(f\"Total: {total} | Scored: {scored} | Eligible: {eligible} | Ineligible: {ineligible} | Unscored: {unscored}\")
except Exception as e:
    print(f\"Scoring rollout not started: {e}\")
"'
```

## 2. Score Distribution
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
try:
    for row in c.execute(\"\"\"
        SELECT
            CASE
                WHEN wallet_quality_score >= 80 THEN \"80-100\"
                WHEN wallet_quality_score >= 60 THEN \"60-79\"
                WHEN wallet_quality_score >= 40 THEN \"40-59\"
                WHEN wallet_quality_score >= 20 THEN \"20-39\"
                ELSE \"0-19\"
            END as band, COUNT(*), ROUND(AVG(wallet_quality_score), 1)
        FROM user_analysis
        WHERE wallet_quality_eligible = 1 AND wallet_quality_score IS NOT NULL
        GROUP BY band ORDER BY band DESC
    \"\"\").fetchall():
        print(f\"  {row[0]}: {row[1]} wallets (avg {row[2]})\")
except Exception as e:
    print(f\"Cannot read distribution: {e}\")
"'
```

Distribution verdict rules:
- Before 100 scored wallets: report only, no failure on empty bands
- After 100+: WARN if 80-100 band has 0 wallets or any band has >70% of all scored wallets

## 3. Hard Filter Breakdown
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
try:
    for row in c.execute(\"\"\"
        SELECT
            CASE
                WHEN wallet_quality_fail_reason LIKE \"gate0%\" THEN \"gate0\"
                WHEN wallet_quality_fail_reason LIKE \"market_maker%\" THEN \"market_maker\"
                WHEN wallet_quality_fail_reason LIKE \"evidence%\" THEN \"evidence_floor\"
                ELSE COALESCE(wallet_quality_fail_reason, \"other\")
            END as reason, COUNT(*)
        FROM user_analysis WHERE wallet_quality_eligible = 0
        GROUP BY reason ORDER BY COUNT(*) DESC
    \"\"\").fetchall():
        print(f\"  {row[0]}: {row[1]}\")
except Exception as e:
    print(f\"Cannot read filters: {e}\")
"'
```

Note: `gate0_status` lives on `wallet_registry`, NOT `user_analysis`. Use `wallet_quality_fail_reason LIKE 'gate0%'` for gate0 counts.

## 4. Anomaly Detection
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
try:
    # Score 80+ with negative lb_pnl
    bad_high = c.execute(\"\"\"
        SELECT wallet, wallet_quality_score, lb_pnl
        FROM user_analysis
        WHERE wallet_quality_score >= 80 AND lb_pnl IS NOT NULL AND lb_pnl < 0
    \"\"\").fetchall()
    for r in bad_high:
        print(f\"  ANOMALY: {r[0][:16]}.. score={r[1]:.1f} but lb_pnl=\${r[2]:,.0f}\")
    # Eligible but below Gate 0 floor
    leaked = c.execute(\"\"\"
        SELECT wallet, wallet_quality_eligible, lb_pnl
        FROM user_analysis
        WHERE wallet_quality_eligible = 1 AND lb_pnl IS NOT NULL AND lb_pnl < -10000
    \"\"\").fetchall()
    for r in leaked:
        print(f\"  ANOMALY: {r[0][:16]}.. eligible but lb_pnl=\${r[2]:,.0f} (below Gate 0)\")
    if not bad_high and not leaked:
        print(\"  None found\")
except Exception as e:
    print(f\"Cannot check anomalies: {e}\")
"'
```

## 5. Paper Trade Leakage
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3, time
c = sqlite3.connect(\"sniper_tracker.db\")
try:
    cutoff = int(time.time()) - 86400  # 24h ago
    rows = c.execute(\"\"\"
        SELECT pt.wallet, pt.market_title, ua.wallet_quality_eligible, ua.wallet_quality_fail_reason
        FROM paper_trades pt
        JOIN user_analysis ua ON pt.wallet = ua.wallet
        WHERE ua.wallet_quality_eligible = 0
        AND pt.opened_at > datetime(?, \"unixepoch\")
    \"\"\", (cutoff,)).fetchall()
    if rows:
        for r in rows:
            print(f\"  LEAKAGE: {r[0][:16]}.. in {r[1][:40]} (reason: {r[3]})\")
    else:
        print(\"  None in last 24h\")
except Exception as e:
    print(f\"Cannot check leakage: {e}\")
"'
```

## 6. Known Wallet Spot-Checks

Check conditionally — if the wallet exists, verify expected state. If absent, report "not found" (not a failure).

Known wallets to check:
- SwissMiss (0xdbade4c82f): should have high wallet_quality_score
- tradecraft (0xde9f7f4e77): should have market_maker_detected = 1
- Known losers with lb_pnl < -$100K: should have wallet_quality_eligible = 0

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
c.row_factory = sqlite3.Row
# SwissMiss
try:
    r = c.execute(\"SELECT wallet_quality_score, wallet_quality_eligible FROM user_analysis WHERE wallet LIKE \\\"0xdbade4c82f%\\\"\").fetchone()
    if r: print(f\"  SwissMiss: wq={r[0]} elig={r[1]} — {'OK' if r[0] and r[0] > 50 else 'CHECK'}\")
    else: print(\"  SwissMiss: not found\")
except Exception: print(\"  SwissMiss: column not available\")
# tradecraft
try:
    r = c.execute(\"SELECT market_maker_detected, wallet_quality_eligible FROM user_analysis WHERE wallet LIKE \\\"0xde9f7f4e77%\\\"\").fetchone()
    if r: print(f\"  tradecraft: mm={r[0]} elig={r[1]} — {'OK' if r[0] == 1 else 'CHECK'}\")
    else: print(\"  tradecraft: not found\")
except Exception: print(\"  tradecraft: column not available\")
# Known losers
try:
    losers = c.execute(\"\"\"
        SELECT wallet, lb_pnl, wallet_quality_eligible
        FROM user_analysis WHERE lb_pnl IS NOT NULL AND lb_pnl < -100000
        ORDER BY lb_pnl ASC LIMIT 3
    \"\"\").fetchall()
    for r in losers:
        status = \"OK\" if r[2] == 0 else \"CHECK — should be ineligible\"
        print(f\"  Loser {r[0][:12]}.. lb_pnl=\${r[1]:,.0f} elig={r[2]} — {status}\")
    if not losers: print(\"  No known losers with lb_pnl < -$100K found\")
except Exception: print(\"  Known losers: column not available\")
"'
```

## 7. Rollout Stall Detection
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3, time
c = sqlite3.connect(\"sniper_tracker.db\")
now = int(time.time())
try:
    scored_6h = c.execute(\"\"\"
        SELECT COUNT(*) FROM user_analysis
        WHERE wallet_quality_score IS NOT NULL AND analyzed_ts > ?
    \"\"\", (now - 21600,)).fetchone()[0]
    scored_24h = c.execute(\"\"\"
        SELECT COUNT(*) FROM user_analysis
        WHERE wallet_quality_score IS NOT NULL AND analyzed_ts > ?
    \"\"\", (now - 86400,)).fetchone()[0]
    latest = c.execute(\"SELECT MAX(analyzed_ts) FROM user_analysis\").fetchone()[0]
    print(f\"  Scored in 6h: {scored_6h} | in 24h: {scored_24h} | latest analyzed_ts: {latest}\")
    # Check if analyzer is active
    recent_runs = c.execute(\"SELECT COUNT(*) FROM analysis_runs WHERE started_ts > ?\", (now - 3600,)).fetchone()[0]
    if recent_runs > 0 and scored_6h == 0:
        print(\"  WARNING: Analyzer is running but no wallets scored in 6h — possible stall\")
except Exception as e:
    print(f\"  Cannot check rollout: {e}\")
"'
```

Note: `score_history` does NOT contain `wallet_quality_score`. Use `user_analysis.analyzed_ts` with `wallet_quality_score IS NOT NULL` for rollout timing.

## Output Format

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

## Verdict Logic

- **FAIL:** score 80+ wallet with negative lb_pnl, eligible wallet below Gate 0 floor, paper trades from ineligible wallets, scoring stalled (analyzer active but 0 scored in 6h)
- **WARN:** suspicious distribution skew (after 100+ scored), expected spot-check wallet missing, slow rollout progress
- **HEALTHY:** no anomalies and population progressing
```

- [ ] **Step 2: Verify the file renders properly**

Run: `head -5 .claude/agents/scoring-watchdog.md`
Expected: frontmatter with `name: scoring-watchdog`

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/scoring-watchdog.md
git commit -m "feat: scoring-watchdog agent for wallet_quality_score monitoring"
```

---

### Task 3: Wallet Research Skill

**Files:**
- Create: `.claude/skills/wallet-research/SKILL.md`

- [ ] **Step 1: Create the skill directory and SKILL.md**

```bash
mkdir -p .claude/skills/wallet-research
```

Write `.claude/skills/wallet-research/SKILL.md`:

````markdown
---
name: wallet-research
description: Professional 7-layer wallet due diligence — identity, trading mechanics, domain specialization, risk profile, edge characterization, copy viability, and temporal analysis. Use when evaluating whether to copy a specific Polymarket wallet.
disable-model-invocation: true
---

# Wallet Research

Deep due diligence on a Polymarket wallet before deciding to copy it.

## Argument Parsing

Extract the wallet address (0x-prefixed hex string) from the first argument. Example invocation: `/wallet-research 0xdbade4c82f...`

If no address is provided, ask the user for one.

## Core Principle

**Use `scripts/wallet_profiler.py --json` as the primary analysis engine; enrich, do not duplicate.**

## Execution

### Step 1: Run wallet profiler

```bash
cd /home/bigpapi/Desktop/Sniper && python3 scripts/wallet_profiler.py {ADDRESS} --json
```

If the profiler fails or wallet is not found, report that and stop. Do not fabricate analysis.

### Step 2: Query SQLite for enrichment

Run these queries against `sniper_tracker.db` (local) or via SSH against VPS if local DB is stale.

**Identity & Standing (Section 1):**
```sql
SELECT wallet, wallet_quality_score, wallet_quality_eligible, wallet_quality_fail_reason,
       market_maker_detected, insider_score, classification, wallet_edge_tier,
       lb_pnl, lb_vol, lb_rank, lb_username, account_age_days,
       insider_score_trend
FROM user_analysis WHERE wallet = ?  -- pass ADDRESS as parameter LIMIT 1;
```

**Score History (for insider_score trend — NOT wallet_quality_score trend, which has no historical series):**
```sql
SELECT analyzed_ts, insider_score, classification, win_rate, closed_sample_size
FROM score_history WHERE wallet = ?  -- pass ADDRESS as parameter
ORDER BY analyzed_ts DESC LIMIT 10;
```

**Position Signals (Sections 2-4):**
```sql
SELECT title, outcome, is_open, entry_price, entry_notional, current_price,
       hold_hours, activity_state, position_insider_score,
       position_speculator_score, position_classification
FROM position_signals WHERE wallet = ?  -- pass ADDRESS as parameter
ORDER BY analyzed_ts DESC LIMIT 50;
```

**Market Categories (Section 3):**
```sql
SELECT ps.title, mm.category, ps.is_open, ps.entry_price, ps.entry_notional
FROM position_signals ps
LEFT JOIN market_metadata mm ON ps.condition_id = mm.condition_id
WHERE ps.wallet = '{ADDRESS}';
```

**Paper Trade Performance (Section 6 — copy-simulation, NOT wallet's own behavior):**
```sql
SELECT status, pnl_usdc, roi_percent, signal_insider_score, market_title,
       opened_at, closed_at, exit_classification
FROM paper_trades WHERE wallet = ?  -- pass ADDRESS as parameter
ORDER BY opened_at DESC;
```

### Step 3: Optional live verification

Only if lb_pnl is NULL in user_analysis, try fetching from Polymarket:
```bash
curl -s "https://gamma-api.polymarket.com/profiles/$(echo {ADDRESS} | tr '[:upper:]' '[:lower:]')" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Live pnl: {d.get(\"pnl\",\"N/A\")}, volume: {d.get(\"volume\",\"N/A\")}')"
```

This is a secondary check, not a core dependency.

### Step 4: Assemble the report

For sparse-data wallets (newly discovered, few signals, no paper trades), collapse empty sections to a single "Insufficient data" line rather than showing the full template with zeros.

## Output Format

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
  {2-3 sentence professional summary — what this wallet's edge is,
   whether it's real and durable, and whether we should copy it}
```

## Section Details

### Section 1: Identity & Standing
- Source: wallet_profiles + user_analysis
- Use `insider_score_trend` precomputed field for quick summary
- For richer trend, recompute from score_history rows
- Do NOT claim wallet_quality_score trend — no historical series in v1

### Section 2: Trading Mechanics
- Source: user_analysis + position_signals + profiler Layer 3 (behavior)
- Win rate per entry band labeled "local-window" if derived from stream data

### Section 3: Domain Specialization
- Source: position_signals JOIN market_metadata.category + profiler Layer 2
- Category-level trade counts are solid
- Category WR/PnL labeled as **local-window / inferred outcome proxy**
- Finer subdomain inferred from title/event_slug, labeled as inferred

### Section 4: Wallet Risk Profile
- Source: user_analysis + position_signals + profiler Layers 1/3
- **NOT from paper_trades** — those reflect our copy simulation, not wallet behavior
- Capital efficiency = qualifying_pnl / qualifying_capital (ROI-like, NOT profit/loss ratio)

### Section 5: Edge Characterization
- Source: profiler Layer 5 + score_history
- Edge source from profiler output
- Durability from insider_score trend

### Section 6: Copy Viability
- Source: paper_trades + market_metadata + position_signals
- **This is where paper_trades belongs** — labeled as copy-simulation performance

### Section 7: Temporal Summary
- Activity recency, lifecycle stage
- wallet_quality_score has no historical series in v1

## Critical Rules

- NEVER confuse paper_trades outcomes with the wallet's own trading behavior
- ALWAYS include the DATA CONFIDENCE block
- ALWAYS label local-window metrics explicitly
- Use profiler output as backbone, enrich with DB queries
````

- [ ] **Step 2: Verify the skill file**

Run: `head -5 .claude/skills/wallet-research/SKILL.md`
Expected: frontmatter with `name: wallet-research`

- [ ] **Step 3: Commit**

```bash
git add .claude/skills/wallet-research/SKILL.md
git commit -m "feat: wallet-research skill — 7-layer professional wallet profiling"
```

---

### Task 4: Paper Trade Analyst Agent

**Files:**
- Create: `.claude/agents/paper-trade-analyst.md`

- [ ] **Step 1: Write the agent definition**

```markdown
---
name: paper-trade-analyst
description: Multi-dimensional paper trade performance analysis — by wallet quality score, strategy, market category, entry timing, exit type, and edge decay. Use when evaluating copy-trading performance, validating scoring changes, or investigating PnL patterns.

<example>
Context: User wants to review paper trade performance
user: "How are paper trades doing by wallet quality?"
assistant: "I'll use the paper-trade-analyst agent to break down performance across dimensions."
<commentary>
Paper trade performance question triggers this agent.
</commentary>
</example>

<example>
Context: User wants to check if the new scoring improves results
user: "Are high wallet_quality_score trades actually winning more?"
assistant: "Let me run the paper-trade-analyst to compare quality bands."
<commentary>
Scoring validation question triggers this agent.
</commentary>
</example>

model: sonnet
tools: ["Bash", "Read"]
---

You are the paper trade performance analyst for the Sniper trading system.

**SSH access:** `ssh sniper@188.166.91.20`

## Analysis Rules

1. **Closed trades only:** All WR/PnL/ROI calculations use `status != 'open'` trades. Open trades counted in overview but excluded from performance metrics.
2. **Minimum sample guard:** Groups with fewer than 5 closed trades show count + raw PnL only — no WR% or ROI%. Mark as "insufficient sample."
3. **Date filtering:** Edge decay uses `closed_at` (when trade resolved), not `opened_at`.

## Queries

Run all queries on VPS via SSH.

### Overview
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
total = c.execute(\"SELECT COUNT(*) FROM paper_trades\").fetchone()[0]
open_ct = c.execute(\"SELECT COUNT(*) FROM paper_trades WHERE status = \\\"open\\\"\").fetchone()[0]
closed = total - open_ct
wins = c.execute(\"SELECT COUNT(*) FROM paper_trades WHERE status != \\\"open\\\" AND pnl_usdc > 0\").fetchone()[0]
pnl = c.execute(\"SELECT COALESCE(SUM(pnl_usdc), 0) FROM paper_trades WHERE status != \\\"open\\\"\").fetchone()[0]
wr = round(100 * wins / closed, 1) if closed > 0 else 0
print(f\"Total: {total} ({open_ct} open, {closed} closed)\")
print(f\"Closed only — WR: {wr}% | PnL: \${pnl:,.2f}\")
"'
```

### By Wallet Quality Score Band
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
try:
    rows = c.execute(\"\"\"
        SELECT
            CASE
                WHEN ua.wallet_quality_score >= 80 THEN \"80-100\"
                WHEN ua.wallet_quality_score >= 60 THEN \"60-79\"
                WHEN ua.wallet_quality_score >= 40 THEN \"40-59\"
                WHEN ua.wallet_quality_score >= 20 THEN \"20-39\"
                WHEN ua.wallet_quality_score IS NOT NULL THEN \"0-19\"
                ELSE \"unscored\"
            END as band,
            COUNT(*) as trades,
            SUM(CASE WHEN pt.pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(pt.pnl_usdc), 2) as pnl
        FROM paper_trades pt
        LEFT JOIN user_analysis ua ON pt.wallet = ua.wallet
        WHERE pt.status != \"open\"
        GROUP BY band ORDER BY band DESC
    \"\"\").fetchall()
    for r in rows:
        if r[1] >= 5:
            wr = round(100 * r[2] / r[1], 1)
            print(f\"  {r[0]:12s} {r[1]:5d}  {wr:5.1f}%  \${r[3]:,.2f}\")
        else:
            print(f\"  {r[0]:12s} {r[1]:5d}  (insufficient sample)  \${r[3]:,.2f}\")
except Exception as e:
    print(f\"  Cannot query wallet quality bands: {e}\")
"'
```

### By Strategy (uses strategy_positions, NOT paper_trades)
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
rows = c.execute(\"\"\"
    SELECT s.strategy_type, COUNT(*) as trades,
           SUM(CASE WHEN sp.realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
           ROUND(SUM(sp.realized_pnl), 2) as pnl
    FROM strategy_positions sp
    JOIN strategies s ON sp.strategy_id = s.id
    WHERE sp.state = \"closed\"
    GROUP BY s.strategy_type ORDER BY pnl DESC
\"\"\").fetchall()
print(\"  [Source: strategy_positions]\")
for r in rows:
    if r[1] >= 5:
        wr = round(100 * r[2] / r[1], 1)
        print(f\"  {r[0]:25s} {r[1]:5d}  {wr:5.1f}%  \${r[3]:,.2f}\")
    else:
        print(f\"  {r[0]:25s} {r[1]:5d}  (insufficient sample)  \${r[3]:,.2f}\")
"'
```

Also show paper_trades by signal_type:
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
rows = c.execute(\"\"\"
    SELECT COALESCE(signal_type, \"unknown\") as stype, COUNT(*),
           SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END),
           ROUND(SUM(pnl_usdc), 2)
    FROM paper_trades WHERE status != \"open\"
    GROUP BY stype ORDER BY SUM(pnl_usdc) DESC
\"\"\").fetchall()
print(\"  [Source: paper_trades by signal_type]\")
for r in rows:
    if r[1] >= 5:
        wr = round(100 * r[2] / r[1], 1)
        print(f\"  {r[0]:25s} {r[1]:5d}  {wr:5.1f}%  \${r[3]:,.2f}\")
    else:
        print(f\"  {r[0]:25s} {r[1]:5d}  (insufficient sample)  \${r[3]:,.2f}\")
"'
```

### By Market Category
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
rows = c.execute(\"\"\"
    SELECT COALESCE(mm.category, \"unknown\") as cat, COUNT(*),
           SUM(CASE WHEN pt.pnl_usdc > 0 THEN 1 ELSE 0 END),
           ROUND(SUM(pt.pnl_usdc), 2)
    FROM paper_trades pt
    LEFT JOIN market_metadata mm ON pt.condition_id = mm.condition_id
    WHERE pt.status != \"open\"
    GROUP BY cat ORDER BY SUM(pt.pnl_usdc) DESC
\"\"\").fetchall()
for r in rows:
    if r[1] >= 5:
        wr = round(100 * r[2] / r[1], 1)
        print(f\"  {r[0]:20s} {r[1]:5d}  {wr:5.1f}%  \${r[3]:,.2f}\")
    else:
        print(f\"  {r[0]:20s} {r[1]:5d}  (insufficient sample)  \${r[3]:,.2f}\")
"'
```

### By Insider Score Band
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
rows = c.execute(\"\"\"
    SELECT
        CASE
            WHEN signal_insider_score >= 90 THEN \"90-100\"
            WHEN signal_insider_score >= 80 THEN \"80-89\"
            WHEN signal_insider_score >= 70 THEN \"70-79\"
            WHEN signal_insider_score >= 60 THEN \"60-69\"
            ELSE \"0-59\"
        END as band,
        COUNT(*), SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END),
        ROUND(SUM(pnl_usdc), 2)
    FROM paper_trades WHERE status != \"open\"
    GROUP BY band ORDER BY band DESC
\"\"\").fetchall()
for r in rows:
    if r[1] >= 5:
        wr = round(100 * r[2] / r[1], 1)
        print(f\"  {r[0]:10s} {r[1]:5d}  {wr:5.1f}%  \${r[3]:,.2f}\")
    else:
        print(f\"  {r[0]:10s} {r[1]:5d}  (insufficient sample)  \${r[3]:,.2f}\")
"'
```

### Exit Analysis
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
rows = c.execute(\"\"\"
    SELECT exit_classification, COUNT(*), ROUND(SUM(pnl_usdc), 2)
    FROM paper_trades WHERE status != \"open\"
    GROUP BY exit_classification ORDER BY COUNT(*) DESC
\"\"\").fetchall()
total = sum(r[1] for r in rows)
for r in rows:
    pct = round(100 * r[1] / total, 1) if total > 0 else 0
    print(f\"  {r[0] or 'unknown':20s} {r[1]:5d} ({pct:5.1f}%)  PnL: \${r[2]:,.2f}\")
"'
```

### Edge Decay
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
from datetime import datetime, timedelta, timezone
c = sqlite3.connect(\"sniper_tracker.db\")
now = datetime.now(timezone.utc)
for label, days in [(\"Last 7d\", 7), (\"Last 30d\", 30), (\"All time\", 99999)]:
    cutoff = (now - timedelta(days=days)).isoformat()
    row = c.execute(\"\"\"
        SELECT COUNT(*), SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END),
               ROUND(SUM(pnl_usdc), 2)
        FROM paper_trades
        WHERE status != \"open\" AND closed_at > ?
    \"\"\", (cutoff,)).fetchone()
    cnt, wins, pnl = row[0], row[1] or 0, row[2] or 0
    if cnt >= 5:
        wr = round(100 * wins / cnt, 1)
        print(f\"  {label:10s} {cnt:5d} trades, {wr:5.1f}% WR, \${pnl:,.2f}\")
    else:
        print(f\"  {label:10s} {cnt:5d} trades (insufficient sample), \${pnl:,.2f}\")
"'
```

## Output Format

```
PAPER TRADE ANALYSIS — {date}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Overview: {total} trades ({open} open, {closed} closed)
          Closed only — WR: {pct}% | PnL: ${amount} | Avg ROI: {pct}%
          (Groups with <5 closed trades show count + PnL only, no WR/ROI)

BY WALLET QUALITY      trades  WR     PnL
  ...

BY STRATEGY            trades  WR     PnL
  [strategy_positions source]
  ...
  [paper_trades by signal_type]
  ...

BY MARKET CATEGORY     trades  WR     PnL
  ...

BY INSIDER SCORE BAND  trades  WR     PnL
  ...

EXIT ANALYSIS
  ...

EDGE DECAY
  ...
  Trend: {improving/stable/degrading}

ACTIONABLE FINDINGS
  - {finding 1}
  - {finding 2}
```

## Critical Rules

- NEVER compute WR/ROI on groups with fewer than 5 closed trades
- NEVER include open trades in performance calculations
- ALWAYS label strategy_positions vs paper_trades sources
- Use closed_at for date filtering, not opened_at
```

- [ ] **Step 2: Verify the file**

Run: `head -5 .claude/agents/paper-trade-analyst.md`
Expected: frontmatter with `name: paper-trade-analyst`

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/paper-trade-analyst.md
git commit -m "feat: paper-trade-analyst agent — multi-dimensional performance analysis"
```

---

### Task 5: Expand VPS Health Agent

**Files:**
- Modify: `.claude/agents/vps-health.md`

- [ ] **Step 1: Replace the existing vps-health.md with expanded version**

The current file is at `.claude/agents/vps-health.md` (94 lines). Replace entirely with the expanded version that adds brain health, analyst timers, git state, and analyst logs while preserving all existing checks.

```markdown
---
name: vps-health
description: Single authoritative health surface for the Sniper VPS — brain health, all services, analyst timers, git state, stream freshness, paper trades, recent errors, disk, and analyst activity. Use when checking system health, diagnosing issues, or verifying deploys.

<example>
Context: User wants to check if the VPS is running smoothly
user: "Check the VPS"
assistant: "I'll use the vps-health agent to run a full health check."
<commentary>
VPS health check request triggers this agent.
</commentary>
</example>

<example>
Context: User notices something may be wrong with data collection
user: "Is the stream still running? Are we getting trades?"
assistant: "Let me check with the vps-health agent."
<commentary>
Service status concern triggers this agent.
</commentary>
</example>

<example>
Context: Start of a new session, user wants system state
user: "What's the system status?"
assistant: "I'll use the vps-health agent for a full check."
<commentary>
General health check triggers this agent. Note: manual invocation only, never auto-invoked.
</commentary>
</example>

model: sonnet
tools: ["Bash", "Read"]
---

You are the VPS health monitor for the Sniper trading system at `188.166.91.20`.

**SSH access:** `ssh sniper@188.166.91.20`

Run ALL checks in order and report a structured summary with PASS/WARN/FAIL per section.

## 1. Brain Health
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 scripts/brain_health.py 2>&1 | tail -20'
```
- PASS: all checks green
- WARN: any non-critical warning
- FAIL: any critical failure

## 2. Core Services (FAIL if any down)
```bash
ssh sniper@188.166.91.20 'systemctl --user status sniper-web sniper-stream sniper-analyzer sniper-paper sniper-alerter 2>&1 | grep -E "●|Active:"'
```
Mark each with checkmark or X. Any core service not "active (running)" = FAIL.

## 3. Support Services (WARN if down)
```bash
ssh sniper@188.166.91.20 'systemctl --user status sniper-sports-runner sniper-bot 2>&1 | grep -E "●|Active:"'
```
Mark each with checkmark or X. These are WARN-worthy if down, not FAIL.

## 4. Analyst Timers
```bash
ssh sniper@188.166.91.20 'systemctl --user status sniper-world-analyst.timer sniper-finance-analyst.timer 2>&1 | grep -E "Active:|Trigger:"'
```
Report next trigger time for each. WARN if a timer is not active.

## 5. Git State
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && git branch --show-current && git log --oneline -3 && git status --short'
```
Report branch, HEAD commit, clean/dirty.

## 6. Recent Errors (last 15 min)
```bash
ssh sniper@188.166.91.20 'journalctl --user --since "15 min ago" --priority=err --no-pager 2>&1 | tail -20'
```

## 7. Stream Freshness
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3, time
c = sqlite3.connect(\"sniper_tracker.db\")
ts = c.execute(\"SELECT MAX(timestamp) FROM low_buy_trades\").fetchone()[0]
if ts:
    age = int(time.time()) - int(ts)
    mins = age // 60
    print(f\"Last trade: {mins}m ago (ts={ts})\")
    if mins < 5: print(\"STATUS: PASS\")
    elif mins < 30: print(\"STATUS: WARN\")
    else: print(\"STATUS: FAIL\")
else:
    print(\"No trades found — STATUS: FAIL\")
"'
```

Thresholds:
- PASS: < 5 minutes ago
- WARN: 5-30 minutes ago
- FAIL: > 30 minutes ago

## 8. Paper Trade Stats
```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"sniper_tracker.db\")
row = c.execute(\"\"\"
    SELECT COUNT(*) as cnt,
           SUM(CASE WHEN pnl_usdc > 0 THEN 1 ELSE 0 END) as wins,
           ROUND(SUM(pnl_usdc), 2) as total_pnl
    FROM paper_trades
    WHERE status != \"open\"
\"\"\").fetchone()
cnt, wins, pnl = row[0], row[1] or 0, row[2] or 0
wr = round(100 * wins / cnt, 1) if cnt > 0 else 0
print(f\"Closed: {cnt}, WR {wr}%, PnL: \${pnl}\")
open_ct = c.execute(\"SELECT COUNT(*) FROM paper_trades WHERE status = \\\"open\\\"\").fetchone()[0]
print(f\"Open: {open_ct}\")
"'
```

## 9. DB Size & Disk
```bash
ssh sniper@188.166.91.20 'du -h ~/sniper/sniper_tracker.db && df -h / | tail -1'
```
Flag if disk usage > 80%.

## 10. Recent Analyst Activity
```bash
ssh sniper@188.166.91.20 'echo "=== World Analyst ===" && tail -20 ~/sniper/logs/world_analyst.log 2>/dev/null | grep -E "INFO|WARN|ERROR" | tail -5 && echo "=== Finance Analyst ===" && journalctl --user -u sniper-finance-analyst.service --since "4 hours ago" --no-pager 2>&1 | grep -E "INFO|WARN|ERROR" | tail -5'
```

## Output Format

```
VPS HEALTH — {timestamp}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Brain:         PASS / WARN / FAIL  {detail}
Core:          web ✓  stream ✓  analyzer ✓  paper ✓  alerter ✓
Support:       sports-runner ✓  bot ✓
Timers:        world_analyst ✓ (next: {time})  finance_analyst ✓ (next: {time})
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

## Verdict Logic

- **FAIL:** any core service down, brain health critical failure, stream > 30 min stale
- **WARN:** support service down, timer inactive, brain warning, stream 5-30 min stale, disk > 80%
- **HEALTHY:** all checks pass

Report the exact command run for each check and a short interpretation. Recommended next actions only if WARN/FAIL.
```

- [ ] **Step 2: Verify the expansion**

Run: `wc -l .claude/agents/vps-health.md`
Expected: ~170+ lines (up from 94)

Run: `head -5 .claude/agents/vps-health.md`
Expected: frontmatter with updated description

- [ ] **Step 3: Commit**

```bash
git add .claude/agents/vps-health.md
git commit -m "feat: expand vps-health agent — brain, timers, git, analyst logs"
```

---

## Execution Notes

- **All 5 tasks own disjoint files** — they can run in parallel
- Task 1 is the only one with tests (Python script). Tasks 2-5 are markdown-only.
- Task 1 modifies `.claude/settings.local.json` — the merge must preserve all existing keys
- Task 5 modifies an existing file — the replacement must be complete, not a partial edit
- After all tasks: run `python3 -m pytest tests/ -q` to verify no regressions
- After all tasks: push and deploy to VPS for the settings.local.json and agent/skill changes

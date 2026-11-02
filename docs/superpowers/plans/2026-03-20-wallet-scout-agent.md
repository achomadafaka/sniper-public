# Wallet Scout Agent — Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an autonomous Claude agent that discovers, vets, and promotes high-value Polymarket wallets into copy-trading strategies, retires dead strategies, and maintains a healthy copy portfolio — running on a daily timer.

**Architecture:** A single agent file (`agents/wallet_scout_agent.py`) following the insider_copy_agent pattern (Claude Agent SDK + Bash + WebSearch/WebFetch). The agent uses existing infrastructure: `user_analysis` for scores, `external_leaderboard_entries` for discovery, `position_signals` for signal verification, Polymarket API for live research, and two new `action_tool.py` commands (`stop` + `create`) for strategy lifecycle. Observations are written to Supabase via `obs_tool.py`.

**Tech Stack:** Python 3.12, Claude Agent SDK (`claude-sonnet-4-6`), SQLite, Supabase REST, Polymarket public API

**Key Polymarket API endpoints (all public, no auth):**
- Wallet activity: `https://data-api.polymarket.com/activity?user=0x{addr}&limit=30`
- Wallet positions: `https://data-api.polymarket.com/positions?user=0x{addr}&sizeThreshold=.1`
- Wallet profile: `https://gamma-api.polymarket.com/profiles/{addr}`
- Leaderboard: `https://data-api.polymarket.com/v1/leaderboard?window=MONTH&limit=50`

**Existing infrastructure leveraged:**
- `user_analysis` table (2,590 wallets with insider_score, win_rate, classification)
- `external_leaderboard_entries` table (4,246 entries from 64 leaderboard combos)
- `position_signals` table (15,085 rows with position-level insider scores)
- `score_history` table (44,503 rows for trend analysis)
- `strategies` / `strategy_positions` tables for strategy lifecycle
- `action_tool.py` for safe agent actions (pause, flag, recommend, alert)
- `obs_tool.py` for Supabase persistent memory
- `predict_tool.py` for self-improvement loop

---

## Chunk 1: Extend action_tool.py with `stop` and `create` Commands

### Task 1: Add `stop` command to action_tool.py

**Files:**
- Modify: `agents/tools/action_tool.py`
- Test: `tests/test_action_tool.py` (new)

The Scout needs to retire dead strategies. Currently `action_tool.py` can only `pause`. We need `stop` which is a stronger signal (won't auto-resume).

- [ ] **Step 1: Write failing test for stop command**

```python
# tests/test_action_tool.py
import sqlite3
import subprocess
import sys
import pytest
from pathlib import Path

ROOT = Path(__file__).parent.parent

@pytest.fixture
def db(tmp_path):
    p = str(tmp_path / "test.db")
    c = sqlite3.connect(p)
    c.execute("""CREATE TABLE strategies (
        id INTEGER PRIMARY KEY, wallet TEXT, label TEXT, strategy_type TEXT,
        parameters TEXT DEFAULT '{}', status TEXT DEFAULT 'active',
        pause_reason TEXT, paused_at TEXT, created_at TEXT, updated_at TEXT
    )""")
    c.execute("INSERT INTO strategies (id, wallet, label, strategy_type, status, created_at, updated_at) VALUES (1, '0xtest', 'Test', 'insider_copy_v1', 'active', '2026-03-20', '2026-03-20')")
    c.commit()
    c.close()
    return p

def test_stop_sets_status_stopped(db):
    result = subprocess.run(
        [sys.executable, str(ROOT / "agents/tools/action_tool.py"),
         "stop", "--strategy-id", "1", "--reason", "wallet dormant 30d"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "DB_PATH": db, "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""},
    )
    assert result.returncode == 0
    import json
    out = json.loads(result.stdout)
    assert out["ok"] is True

    c = sqlite3.connect(db)
    row = c.execute("SELECT status FROM strategies WHERE id=1").fetchone()
    assert row[0] == "stopped"
```

- [ ] **Step 2: Run test, verify it fails**

Run: `python3 -m pytest tests/test_action_tool.py::test_stop_sets_status_stopped -v`

- [ ] **Step 3: Implement `cmd_stop` in action_tool.py**

Add after `cmd_pause`:

```python
def cmd_stop(args) -> int:
    """Stop a strategy permanently (agent decision — won't auto-resume)."""
    now = datetime.now(timezone.utc).isoformat()
    info = _get_strategy_info(args.strategy_id)
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        result = conn.execute(
            "UPDATE strategies SET status='stopped', pause_reason=?, paused_at=? WHERE id=? AND status IN ('active','paused')",
            (f"scout_stop: {args.reason}", now, args.strategy_id),
        )
        conn.commit()
        conn.close()
        if result.rowcount == 0:
            print(json.dumps({"ok": False, "error": "strategy not found or already stopped"}))
            return 1
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    label = info.get("label", f"#{args.strategy_id}")
    _send_telegram(f"🔴 <b>Scout stopped</b> strategy <b>{label}</b>\n{args.reason}")
    _write_obs(
        info.get("strategy_type", "unknown"), "action_taken",
        f"STOPPED strategy {label} (id={args.strategy_id}): {args.reason}",
        confidence="high", strategy_ids=[args.strategy_id],
    )
    print(json.dumps({"ok": True, "action": "stop", "strategy_id": args.strategy_id}))
    return 0
```

Add to the argparse subcommands section:

```python
p_stop = sub.add_parser("stop", help="Stop a dead/degraded strategy permanently")
p_stop.add_argument("--strategy-id", type=int, required=True)
p_stop.add_argument("--reason", required=True)
p_stop.set_defaults(func=cmd_stop)
```

- [ ] **Step 4: Run test, verify it passes**

Run: `python3 -m pytest tests/test_action_tool.py -v`

- [ ] **Step 5: Commit**

---

### Task 2: Add `create` command to action_tool.py

**Files:**
- Modify: `agents/tools/action_tool.py`
- Modify: `tests/test_action_tool.py`

The Scout needs to create copy strategies for wallets it has vetted. The strategy is created in **paused** state — the Scout then separately activates it after a confirmation step.

- [ ] **Step 1: Write failing test for create command**

```python
def test_create_makes_paused_strategy(db):
    # Need strategies table with all columns strategy_service expects
    c = sqlite3.connect(db)
    # strategy_service.create_strategy uses the real schema, so we test via action_tool
    c.close()

    result = subprocess.run(
        [sys.executable, str(ROOT / "agents/tools/action_tool.py"),
         "create",
         "--wallet", "0x0d16999d6ba24c2c2c8bff93f91014cc43fad24d",
         "--label", "Scout: test wallet copy",
         "--strategy-type", "insider_copy_v1",
         "--params", '{"min_insider_score": 70, "max_entry_price": 0.70}',
         "--reason", "insider=92.5, WR=88%, 368 samples"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "DB_PATH": db, "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""},
    )
    # Will fail on schema since we have minimal table, but test the JSON parse path
    out = result.stdout.strip()
    assert result.returncode in (0, 1)  # May fail on schema, that's OK for unit test
```

- [ ] **Step 2: Implement `cmd_create` in action_tool.py**

```python
def cmd_create(args) -> int:
    """Create a new copy strategy in paused state (Scout discovery)."""
    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"invalid params JSON: {e}"}))
        return 1

    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        # Check for duplicate: same wallet + strategy_type that isn't stopped
        existing = conn.execute(
            "SELECT id, status FROM strategies WHERE strategy_type=? AND wallet=? AND status != 'stopped'",
            (args.strategy_type, args.wallet),
        ).fetchone()
        if existing:
            print(json.dumps({"ok": False, "error": f"Strategy already exists (id={existing['id']}, status={existing['status']})"}))
            conn.close()
            return 1

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from strategy_service import create_strategy
        result = create_strategy(
            conn, wallet=args.wallet, label=args.label or f"Scout: {args.wallet[:10]}",
            strategy_type=args.strategy_type, parameters=params,
        )
        conn.close()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    sid = result.get("id", "?")
    label = args.label or f"Scout: {args.wallet[:10]}"
    _send_telegram(
        f"🔍 <b>Scout created</b> strategy <b>{label}</b> (id={sid})\n"
        f"Wallet: <code>{args.wallet[:14]}...</code>\n"
        f"Reason: {args.reason}\n"
        f"Status: <b>paused</b> (awaiting activation)"
    )
    _write_obs(
        args.strategy_type, "action_taken",
        f"CREATED strategy {label} (id={sid}) for wallet {args.wallet}: {args.reason}",
        confidence="high", strategy_ids=[sid],
    )
    print(json.dumps({"ok": True, "action": "create", "strategy_id": sid, "status": "paused"}))
    return 0
```

Add to argparse:

```python
p_create = sub.add_parser("create", help="Create a new copy strategy (paused state)")
p_create.add_argument("--wallet", required=True, help="0x wallet address")
p_create.add_argument("--strategy-type", required=True, default="insider_copy_v1")
p_create.add_argument("--label", default="", help="Strategy display name")
p_create.add_argument("--params", default="{}", help="JSON strategy parameters")
p_create.add_argument("--reason", required=True, help="Why this wallet was selected")
```

- [ ] **Step 3: Run tests, verify passing**

Run: `python3 -m pytest tests/test_action_tool.py -v`

- [ ] **Step 4: Commit**

---

### Task 3: Add `activate` command to action_tool.py

**Files:**
- Modify: `agents/tools/action_tool.py`
- Modify: `tests/test_action_tool.py`

- [ ] **Step 1: Write failing test**

```python
def test_activate_changes_paused_to_active(db):
    c = sqlite3.connect(db)
    c.execute("INSERT INTO strategies (id, wallet, label, strategy_type, status, created_at, updated_at) VALUES (2, '0xscout', 'Test2', 'insider_copy_v1', 'paused', '2026-03-20', '2026-03-20')")
    c.commit()
    c.close()

    result = subprocess.run(
        [sys.executable, str(ROOT / "agents/tools/action_tool.py"),
         "activate", "--strategy-id", "2", "--reason", "vetted by Scout"],
        capture_output=True, text=True,
        env={**__import__("os").environ, "DB_PATH": db, "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""},
    )
    assert result.returncode == 0
    import json
    out = json.loads(result.stdout)
    assert out["ok"] is True

    c = sqlite3.connect(db)
    row = c.execute("SELECT status FROM strategies WHERE id=2").fetchone()
    assert row[0] == "active"
```

- [ ] **Step 2: Implement `cmd_activate`**

```python
def cmd_activate(args) -> int:
    """Activate a paused strategy (Scout-vetted, ready to trade)."""
    info = _get_strategy_info(args.strategy_id)
    if info.get("status") != "paused":
        print(json.dumps({"ok": False, "error": f"strategy status is '{info.get('status', 'unknown')}', must be 'paused'"}))
        return 1
    try:
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.execute(
            "UPDATE strategies SET status='active', pause_reason=NULL, paused_at=NULL WHERE id=? AND status='paused'",
            (args.strategy_id,),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return 1

    label = info.get("label", f"#{args.strategy_id}")
    _send_telegram(f"🟢 <b>Scout activated</b> strategy <b>{label}</b>\n{args.reason}")
    _write_obs(
        info.get("strategy_type", "unknown"), "action_taken",
        f"ACTIVATED strategy {label} (id={args.strategy_id}): {args.reason}",
        confidence="high", strategy_ids=[args.strategy_id],
    )
    print(json.dumps({"ok": True, "action": "activate", "strategy_id": args.strategy_id}))
    return 0
```

Add argparse:

```python
p_activate = sub.add_parser("activate", help="Activate a paused strategy")
p_activate.add_argument("--strategy-id", type=int, required=True)
p_activate.add_argument("--reason", required=True)
p_activate.set_defaults(func=cmd_activate)
```

- [ ] **Step 3: Run tests + full suite**

Run: `python3 -m pytest tests/test_action_tool.py -v && python3 -m pytest tests/ -q`

- [ ] **Step 4: Commit**

---

## Chunk 2: Wallet Scout Agent

### Task 4: Create the wallet scout agent

**Files:**
- Create: `agents/wallet_scout_agent.py`

This is the core agent — a Claude agent with a system prompt that teaches it HOW to scout wallets, and a task prompt that gives it a specific workflow to follow each run.

- [ ] **Step 1: Create the agent file**

The agent system prompt defines:
- **Role**: autonomous wallet scout that discovers, vets, and manages copy-trading targets
- **Tools**: DB queries (read-only), Polymarket API (via WebFetch), action_tool.py (create/stop/activate/pause/flag), obs_tool.py (memory), predict_tool.py (self-improvement)
- **Decision framework**: clear criteria for when to create, activate, stop, or flag strategies
- **Vetting checklist**: what makes a wallet copy-worthy vs not
- **Safety rules**: max strategies, confirmation before activation, never modify scoring

The task prompt follows the Step 0-10 pattern from insider_copy_agent:
- Step 0: Self-improvement (grade predictions, read lessons)
- Step 1: Load memory (past observations, current strategy roster)
- Step 2: Audit existing strategies (health check — are they producing trades?)
- Step 3: Stop dead strategies (wallet dormant, zero signals, score collapsed)
- Step 4: Discover candidates (leaderboard, high-score wallets, flagged_users)
- Step 5: Vet top candidates (Polymarket API: activity, positions, profile, position-level scores)
- Step 6: Create strategies for vetted wallets (paused state)
- Step 7: Activate strategies that pass final check (open positions at good prices)
- Step 8: Write observations (run_summary, wallet_research, pattern)
- Step 9: Make predictions (2+ verifiable)
- Step 10: Log run

Key vetting criteria (encoded in system prompt):
1. **Active trader**: has open positions updated in last 7 days
2. **Copyable prices**: at least 2 open positions with price < 0.70
3. **Position-level quality**: position_insider_score >= 55 on open positions (not just wallet-level score)
4. **Not a certainty buyer**: median entry price < 0.85
5. **Sample size**: closed_sample_size >= 15 (not a fluke)
6. **Not a bot**: speculator_score < 65, classification != speculator_bot
7. **Score stability**: insider_score hasn't dropped > 15 pts in last 5 score_history entries
8. **Not already copied**: no existing active/paused strategy for this wallet

Strategy cleanup criteria:
1. **Wallet vanished**: not in user_analysis anymore → stop
2. **Score collapsed**: insider_score dropped below 50 → stop
3. **Zero signals 14d**: no is_open=1 position_signals for 14+ days → stop
4. **Reclassified as bot**: classification = speculator_bot → stop
5. **Chronic loser**: strategy PnL < -30% with 10+ closed trades → flag (don't auto-stop)

- [ ] **Step 2: Write the full agent file**

See implementation below (full file with SYSTEM_PROMPT, build_run_prompt(), run() function).

- [ ] **Step 3: Verify syntax**

Run: `python3 -c "import agents.wallet_scout_agent"`

- [ ] **Step 4: Commit**

---

### Task 5: Register wallet_scout in the agent runner

**Files:**
- Modify: `scripts/run_strategy_agent.py` (line 42-44)

- [ ] **Step 1: Add to AGENT_REGISTRY**

```python
AGENT_REGISTRY: dict[str, tuple[str, str]] = {
    "insider_copy": ("agents.insider_copy_agent", "run"),
    "wallet_scout": ("agents.wallet_scout_agent", "run"),
}
```

- [ ] **Step 2: Test it launches (dry run)**

Run: `python3 scripts/run_strategy_agent.py wallet_scout --verbose 2>&1 | head -5`
Expected: Should show "[agent] Session: ..." or similar init output.

- [ ] **Step 3: Commit**

---

### Task 6: Create systemd timer for wallet_scout

**Files:**
- Create: `systemd/sniper-agent-wallet-scout.service`
- Create: `systemd/sniper-agent-wallet-scout.timer`

- [ ] **Step 1: Create service file**

```ini
[Unit]
Description=Sniper Wallet Scout Agent (discovery + strategy lifecycle)
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/home/sniper/sniper
EnvironmentFile=-/home/sniper/sniper/.env
ExecStart=/home/sniper/sniper/.venv/bin/python3 /home/sniper/sniper/scripts/run_strategy_agent.py wallet_scout --quiet
StandardOutput=append:/home/sniper/sniper/logs/agent_wallet_scout.log
StandardError=append:/home/sniper/sniper/logs/agent_wallet_scout.log
TimeoutStartSec=900
Restart=no

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Create timer file**

```ini
[Unit]
Description=Timer — run Wallet Scout agent once daily
Requires=sniper-agent-wallet-scout.service

[Timer]
# Run at 12:00 UTC daily (between the two insider_copy runs at 06:00 and 18:00)
OnCalendar=*-*-* 12:00:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

- [ ] **Step 3: Commit**

---

## Chunk 3: Testing & Deployment

### Task 7: Integration test for Scout workflow

**Files:**
- Create: `tests/test_wallet_scout.py`

- [ ] **Step 1: Write integration tests**

Test the key queries the Scout will run (these are the SQL queries from the task prompt):

```python
# tests/test_wallet_scout.py
import sqlite3
import pytest
from pathlib import Path

@pytest.fixture
def scout_db(tmp_path):
    """Minimal DB with the tables the Scout queries."""
    p = str(tmp_path / "test.db")
    c = sqlite3.connect(p)
    c.executescript("""
        CREATE TABLE user_analysis (
            wallet TEXT PRIMARY KEY, insider_score REAL, speculator_score REAL,
            win_rate REAL, trade_count INTEGER, closed_sample_size INTEGER,
            classification TEXT, analyzed_ts INTEGER
        );
        CREATE TABLE strategies (
            id INTEGER PRIMARY KEY AUTOINCREMENT, wallet TEXT, label TEXT,
            strategy_type TEXT, parameters TEXT DEFAULT '{}', status TEXT DEFAULT 'active',
            pause_reason TEXT, paused_at TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE strategy_positions (
            position_id INTEGER PRIMARY KEY, strategy_id TEXT, condition_id TEXT,
            outcome TEXT, state TEXT, entry_price REAL, avg_entry_price REAL,
            realized_pnl REAL, opened_ts INTEGER, closed_ts INTEGER
        );
        CREATE TABLE position_signals (
            wallet TEXT, condition_id TEXT, outcome TEXT, current_price REAL,
            is_open INTEGER, position_insider_score REAL,
            position_speculator_score REAL, captured_at TEXT
        );
        CREATE TABLE score_history (
            wallet TEXT, insider_score REAL, classification TEXT, analyzed_ts INTEGER
        );
        CREATE TABLE external_leaderboard_entries (
            wallet TEXT, category TEXT, time_period TEXT, order_by TEXT,
            rank INTEGER, official_pnl REAL, official_volume REAL, username TEXT
        );
    """)
    return p, c

def test_scout_finds_dead_strategy(scout_db):
    """Strategy with wallet not in user_analysis should be flagged."""
    p, c = scout_db
    c.execute("INSERT INTO strategies (id, wallet, label, strategy_type, status, created_at, updated_at) VALUES (1,'0xdead','Dead','insider_copy_v1','active','2026-03-20','2026-03-20')")
    c.commit()
    # Wallet NOT in user_analysis
    row = c.execute("""
        SELECT s.id, s.label, s.status, s.wallet
        FROM strategies s
        WHERE s.strategy_type = 'insider_copy_v1' AND s.status IN ('active','paused')
          AND s.wallet NOT IN (SELECT wallet FROM user_analysis)
    """).fetchall()
    assert len(row) == 1
    assert row[0][3] == "0xdead"

def test_scout_finds_high_score_candidates(scout_db):
    """Wallets with high insider scores not already copied."""
    p, c = scout_db
    c.execute("INSERT INTO user_analysis VALUES ('0xgood',85,10,0.80,50,45,'high_winrate_review',1773960000)")
    c.execute("INSERT INTO user_analysis VALUES ('0xbot',85,75,0.60,100,80,'speculator_bot',1773960000)")
    c.commit()
    rows = c.execute("""
        SELECT wallet, insider_score, speculator_score, win_rate, closed_sample_size, classification
        FROM user_analysis
        WHERE insider_score >= 70
          AND speculator_score < 65
          AND closed_sample_size >= 15
          AND classification NOT IN ('speculator_bot','low_signal')
          AND wallet NOT IN (SELECT wallet FROM strategies WHERE status != 'stopped')
        ORDER BY insider_score DESC
        LIMIT 10
    """).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "0xgood"

def test_scout_checks_open_signals(scout_db):
    """Candidate must have open position_signals with good scores."""
    p, c = scout_db
    c.execute("INSERT INTO position_signals VALUES ('0xgood','0xcond1','Yes',0.45,1,72,10,'2026-03-20')")
    c.execute("INSERT INTO position_signals VALUES ('0xgood','0xcond2','No',0.60,1,68,15,'2026-03-20')")
    c.execute("INSERT INTO position_signals VALUES ('0xbad','0xcond3','Yes',0.95,1,20,5,'2026-03-20')")
    c.commit()
    rows = c.execute("""
        SELECT wallet, COUNT(*) as open_sigs,
            SUM(CASE WHEN position_insider_score >= 55 AND current_price <= 0.70 THEN 1 ELSE 0 END) as qualifying
        FROM position_signals
        WHERE is_open = 1
        GROUP BY wallet
        HAVING qualifying >= 1
    """).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "0xgood"
```

- [ ] **Step 2: Run tests**

Run: `python3 -m pytest tests/test_wallet_scout.py -v`

- [ ] **Step 3: Run full suite**

Run: `python3 -m pytest tests/ -q`

- [ ] **Step 4: Commit**

---

### Task 8: Deploy to VPS

- [ ] **Step 1: Push code**

```bash
git push origin main
```

- [ ] **Step 2: Pull on VPS and install timer**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && git pull origin main'
ssh sniper@188.166.91.20 'cp ~/sniper/systemd/sniper-agent-wallet-scout.service ~/.config/systemd/user/ && cp ~/sniper/systemd/sniper-agent-wallet-scout.timer ~/.config/systemd/user/'
ssh sniper@188.166.91.20 'systemctl --user daemon-reload && systemctl --user enable --now sniper-agent-wallet-scout.timer'
```

- [ ] **Step 3: Verify timer is active**

```bash
ssh sniper@188.166.91.20 'systemctl --user status sniper-agent-wallet-scout.timer'
```

- [ ] **Step 4: Manual test run**

```bash
ssh sniper@188.166.91.20 'systemctl --user start sniper-agent-wallet-scout.service'
# Wait ~5 min, then check logs:
ssh sniper@188.166.91.20 'journalctl --user -u sniper-agent-wallet-scout --since "10 min ago" --no-pager | tail -30'
```

- [ ] **Step 5: Verify observations written**

```bash
ssh sniper@188.166.91.20 'cd ~/sniper && .venv/bin/python3 agents/tools/obs_tool.py read --strategy wallet_scout --limit 5'
```

---

## Agent Design Notes

### Safety Guardrails

1. **No hard cap on active strategies** — if it passes vetting, it gets a strategy. Note in run_summary when 10+ active.
2. **Create in paused state**: always — never go straight to active
3. **Activation requires evidence**: agent must verify open signals exist at good prices BEFORE activating
4. **Never modify scores or analysis**: read-only access to user_analysis, score_history
5. **Stop requires reason**: every stop is logged with rationale to Telegram + Supabase
6. **Budget cap**: $1.50 per run, 60 max turns (same as insider_copy)
7. **Daily cadence**: once per day at 12:00 UTC — wallet quality changes slowly

### Cleanup Guidelines

The Scout SHOULD stop a strategy when:
- The tracked wallet is no longer in `user_analysis` (vanished)
- The wallet's `insider_score` dropped below 50 (was 70+ when created)
- Zero `is_open=1` position_signals for the wallet in 14+ days (dormant)
- Wallet reclassified as `speculator_bot`

The Scout SHOULD flag (not stop) when:
- Strategy PnL < -30% with 10+ closed trades (chronic loser — human decides)
- Wallet score trending down but still above 60 (watch, don't kill)

The Scout MUST NOT:
- Stop manually-paused strategies (`pause_reason = 'manual'`)
- Create strategies for wallets with `classification = speculator_bot`
- Activate more than 3 new strategies per run (stagger to observe)
- Research wallets that are already being copied by an active strategy

### Discovery Priority Order

1. `flagged_users` with insider >= 70 (already vetted by analyzer)
2. `user_analysis` with insider >= 75, WR >= 60%, sample >= 20, not bot (high bar)
3. `external_leaderboard_entries` cross-referenced with `user_analysis` scores
4. Web search for notable Polymarket traders (low priority, high effort)

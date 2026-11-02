# Stink Bid Paper Deploy — VPS Readiness Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the stink bid tennis paper trader deployable to VPS and visible in the dashboard UI.

**Architecture:** Three gaps to close: (1) systemd service file following existing patterns, (2) strategy DB row so the runner has a `--strategy-id`, (3) UI registration so the Strategies tab renders stink bid with proper badge/label. The data pipeline already works: runner → `paper_limit_orders` → fills → `strategy_positions` → API → UI.

**Tech Stack:** systemd, SQLite, Flask/JS dashboard

---

### Task 1: Create systemd template unit

**Files:**
- Create: `systemd/sniper-stink-bid@.service`

The stink bid runner is a long-running async process (`stink_bid_runner.py --db <path> --strategy-id <N>`). It uses a systemd template unit so the strategy ID is passed as the instance specifier: `systemctl --user enable --now sniper-stink-bid@42.service`.

- [ ] **Step 1: Create the template unit file**

```ini
[Unit]
Description=Sniper Stink Bid Tennis Paper Trader (strategy %i)
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
WorkingDirectory=/home/sniper/sniper
Environment=PYTHONPATH=/home/sniper/sniper
Environment=DB_PATH=sniper_tracker.db
EnvironmentFile=-/home/sniper/sniper/.env
ExecStart=/home/sniper/sniper/.venv/bin/python /home/sniper/sniper/stink_bid_runner.py --db sniper_tracker.db --strategy-id %i
Restart=always
RestartSec=10
TimeoutStopSec=20
MemoryMax=350M
MemorySwapMax=50M

[Install]
WantedBy=default.target
```

The `@` in the filename makes this a template unit. `%i` is the instance specifier — the strategy ID. Deploy with: `systemctl --user enable --now sniper-stink-bid@<ID>.service`.

- [ ] **Step 2: Verify the file parses correctly**

Run: `systemd-analyze verify systemd/sniper-stink-bid@.service 2>&1 || echo "parse check done"`

Expected: No fatal errors (warnings about missing targets are fine on dev machine).

- [ ] **Step 3: Commit**

```bash
git add systemd/sniper-stink-bid@.service
git commit -m "feat: add systemd template unit for stink bid runner"
```

---

### Task 2: Register stink_bid_v1 in the dashboard UI

**Files:**
- Modify: `static/app.js` — three locations:
  1. `STRATEGY_TEMPLATES` object (~line 3015): add `stink_bid_v1` entry
  2. Badge class map (~line 273): add `strategy_stink_bid_v1` → badge class
  3. `_strategyTypeShort()` map (~line 303): add `stink_bid_v1` → short label

These three maps control how a strategy type renders in the Strategies tab sidebar cards, detail view, and operator panel.

- [ ] **Step 1: Add STRATEGY_TEMPLATES entry**

After the `term_structure_v1` entry (~line 3015), before `insider_copy_v1`, add:

```javascript
    stink_bid_v1: {
      label: 'Stink Bid Tennis',
      desc: 'Passive limit orders on tennis match panic dips',
      defaults: {
        execution_mode: 'paper',
        dip_pct: 0.30,
        min_favorite: 0.55,
        min_bid: 0.05,
        min_fill_size: 50,
        max_resting: 3,
        max_daily_fills: 5,
        position_size_usdc: 25,
      }
    },
```

- [ ] **Step 2: Add badge class mapping**

In the badge class map (~line 273), add after the `strategy_term_structure_v1` line:

```javascript
      'strategy_stink_bid_v1': 'badge-strategy-autonomous',
```

- [ ] **Step 3: Add short type label**

In `_strategyTypeShort()` (~line 303), add after the `term_structure_v1` line:

```javascript
      stink_bid_v1: 'Stink',
```

- [ ] **Step 4: Verify the JS has no syntax errors**

Run: `node -c static/app.js`

Expected: No output (clean parse).

- [ ] **Step 5: Commit**

```bash
git add static/app.js
git commit -m "feat: register stink_bid_v1 in dashboard strategy UI"
```

---

### Task 3: Add API endpoint for stink bid order book

**Files:**
- Modify: `routes/api_strategies.py` — add a GET endpoint for paper limit order details

The existing `_strategy_runtime()` in `api_strategies.py` already reads `strategy_positions` which `paper_limit_engine.py` writes to on fills. So filled orders → positions are already visible. But the stink bid has unique data: resting orders, fill rates, ESPN outage status, slippage. The `scripts/stink_bid_report.py` already queries this data. Expose a lightweight version via the API so the strategy detail view can show the order book.

- [ ] **Step 1: Write the test**

Create test in `tests/test_stink_bid_api.py`:

```python
"""Tests for stink bid API endpoint in api_strategies blueprint."""
from __future__ import annotations

import sqlite3

import pytest
from flask import Flask

from db import init_db
from paper_trader import PaperTrader
from routes.api_strategies import create_api_strategies_blueprint


def _get_conn(tmp_path):
    """Create a test DB with full schema and return the connection."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_db(conn)
    # paper_limit_orders/fills are created by PaperTrader, not init_db
    pt = PaperTrader.__new__(PaperTrader)
    pt.conn = conn
    pt._init_tables()
    return conn


def _make_client(conn):
    """Create a Flask test client with the strategies blueprint."""
    app = Flask(__name__)
    bp = create_api_strategies_blueprint({
        "conn": conn,
        "normalize_wallet": lambda w: w,
        "wallet_valid": lambda w: True,
        "safe_int": lambda v, d=0: int(v) if v else d,
        "optional_float": lambda v: float(v) if v else None,
    })
    app.register_blueprint(bp)
    return app.test_client()


@pytest.fixture
def client(tmp_path):
    conn = _get_conn(tmp_path)
    # Seed a stink bid strategy
    conn.execute(
        """INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status)
           VALUES (1, '0x0000000000000000000000000000000000000001',
                   'Stink Bid Tennis', 'stink_bid_v1', '{}', 'active')""",
    )
    # Seed a resting order
    conn.execute(
        """INSERT INTO paper_limit_orders
           (strategy_id, condition_id, market_title, event_slug,
            favorite_token_id, favorite_token_label, favorite_price_at_entry,
            bid_price, dip_pct, min_fill_size, state, tour, player1, player2,
            placed_at, updated_at)
           VALUES (1, 'cond_abc', 'Djokovic vs Nadal - Winner', 'atp-final',
                   'tok_abc', 'Djokovic', 0.85,
                   0.12, 0.30, 50, 'resting', 'atp', 'Djokovic', 'Nadal',
                   '2026-04-08T12:00:00Z', '2026-04-08T12:00:00Z')""",
    )
    # Seed a filled order with a fill
    conn.execute(
        """INSERT INTO paper_limit_orders
           (strategy_id, condition_id, market_title, event_slug,
            favorite_token_id, favorite_token_label, favorite_price_at_entry,
            bid_price, dip_pct, min_fill_size, state, tour, player1, player2,
            placed_at, updated_at, closed_at)
           VALUES (1, 'cond_def', 'Alcaraz vs Sinner - Winner', 'atp-open',
                   'tok_def', 'Alcaraz', 0.80,
                   0.15, 0.30, 50, 'filled', 'atp', 'Alcaraz', 'Sinner',
                   '2026-04-08T10:00:00Z', '2026-04-08T11:00:00Z',
                   '2026-04-08T11:00:00Z')""",
    )
    filled_order_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    # slippage = bid_price - fill_price = 0.15 - 0.14 = +0.01 (positive = better than expected)
    conn.execute(
        """INSERT INTO paper_limit_fills
           (order_id, fill_label, trade_price, trade_size, trade_ts, bid_price, slippage, created_at)
           VALUES (?, 'strict', 0.14, 60.0, '2026-04-08T11:00:00Z', 0.15, 0.01, '2026-04-08T11:00:00Z')""",
        (filled_order_id,),
    )
    conn.commit()
    yield _make_client(conn)
    conn.close()


def test_stink_bid_orders_returns_order_book(client):
    resp = client.get("/api/strategies/1/stink-bid-orders")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "orders" in data
    assert len(data["orders"]) == 2
    resting = [o for o in data["orders"] if o["state"] == "resting"]
    filled = [o for o in data["orders"] if o["state"] == "filled"]
    assert len(resting) == 1
    assert len(filled) == 1
    assert resting[0]["bid_price"] == 0.12
    assert resting[0]["player1"] == "Djokovic"


def test_stink_bid_orders_includes_fills(client):
    resp = client.get("/api/strategies/1/stink-bid-orders")
    data = resp.get_json()
    filled = [o for o in data["orders"] if o["state"] == "filled"]
    assert len(filled[0]["fills"]) == 1
    assert filled[0]["fills"][0]["fill_label"] == "strict"
    assert filled[0]["fills"][0]["trade_price"] == 0.14
    assert filled[0]["fills"][0]["slippage"] == 0.01  # positive = better than expected


def test_stink_bid_orders_summary(client):
    resp = client.get("/api/strategies/1/stink-bid-orders")
    data = resp.get_json()
    assert "summary" in data
    s = data["summary"]
    assert s["total"] == 2
    assert s["resting"] == 1
    assert s["filled"] == 1


def test_stink_bid_orders_wrong_strategy_type(tmp_path):
    """Returns 400 if strategy is not stink_bid_v1."""
    conn = _get_conn(tmp_path)
    conn.execute(
        """INSERT INTO strategies (id, wallet, label, strategy_type, parameters, status)
           VALUES (1, '0x0000000000000000000000000000000000000001',
                   'Other', 'insider_copy_v1', '{}', 'active')""",
    )
    conn.commit()
    c = _make_client(conn)
    resp = c.get("/api/strategies/1/stink-bid-orders")
    assert resp.status_code == 400
    conn.close()


def test_stink_bid_orders_not_found(client):
    resp = client.get("/api/strategies/999/stink-bid-orders")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_stink_bid_api.py -v`

Expected: All 5 tests FAIL (endpoint doesn't exist yet).

- [ ] **Step 3: Implement the endpoint**

Add inside `create_api_strategies_blueprint()` in `routes/api_strategies.py`, after the existing route definitions:

```python
    @bp.get("/api/strategies/<int:strategy_id>/stink-bid-orders")
    def api_stink_bid_orders(strategy_id: int):
        """Return the paper limit order book for a stink bid strategy."""
        conn = ctx["conn"]
        row = conn.execute(
        "SELECT * FROM strategies WHERE id = ?", (strategy_id,)
    ).fetchone()
        if row is None:
            return jsonify({"error": "strategy not found"}), 404
        if str(row["strategy_type"] or "") != "stink_bid_v1":
            return jsonify({"error": "not a stink_bid_v1 strategy"}), 400

    orders = conn.execute(
        """SELECT * FROM paper_limit_orders
           WHERE strategy_id = ?
           ORDER BY placed_at DESC""",
        (strategy_id,),
    ).fetchall()

    result = []
    state_counts = {"resting": 0, "filled": 0, "cancelled": 0, "expired": 0}
    for o in orders:
        state = str(o["state"] or "")
        state_counts[state] = state_counts.get(state, 0) + 1
        oid = int(o["order_id"] or 0)
        fills = conn.execute(
            """SELECT * FROM paper_limit_fills
               WHERE order_id = ?
               ORDER BY created_at DESC""",
            (oid,),
        ).fetchall()
        result.append({
            "order_id": oid,
            "condition_id": str(o["condition_id"] or ""),
            "market_title": str(o["market_title"] or ""),
            "bid_price": float(o["bid_price"] or 0),
            "state": state,
            "tour": str(o["tour"] or ""),
            "player1": str(o["player1"] or ""),
            "player2": str(o["player2"] or ""),
            "event_slug": str(o["event_slug"] or ""),
            "favorite_token_label": str(o["favorite_token_label"] or ""),
            "favorite_price_at_entry": float(o["favorite_price_at_entry"] or 0),
            "placed_at": str(o["placed_at"] or ""),
            "closed_at": str(o["closed_at"] or ""),
            "fills": [
                {
                    "fill_id": int(f["fill_id"] or 0),
                    "fill_label": str(f["fill_label"] or ""),
                    "trade_price": float(f["trade_price"] or 0),
                    "trade_size": float(f["trade_size"] or 0),
                    "bid_price": float(f["bid_price"] or 0),
                    "slippage": float(f["slippage"] or 0),
                    "trade_ts": str(f["trade_ts"] or ""),
                }
                for f in fills
            ],
        })

    return jsonify({
        "orders": result,
        "summary": {
            "total": len(result),
            **state_counts,
        },
    })
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_stink_bid_api.py -v`

Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/test_stink_bid_api.py routes/api_strategies.py
git commit -m "feat: add /api/strategies/<id>/stink-bid-orders endpoint"
```

---

### Task 4: Add stink bid order book to strategy detail view in UI

**Files:**
- Modify: `static/app.js` — extend the strategy detail renderer to show stink bid orders when strategy type is `stink_bid_v1`

The existing strategy detail view (rendered by `selectStrategy()` → detail panel) shows positions, runs, and executions. For stink_bid_v1, we additionally fetch and render the order book from the endpoint created in Task 3.

- [ ] **Step 1: Add the order book fetch and render**

Find the strategy detail rendering section. In `selectStrategy()` (~line 4350), after the existing detail render, add a conditional block that fetches and displays stink bid orders when `strat.strategy_type === 'stink_bid_v1'`.

Add this function to the strategies section of app.js (inside the strategies module, near `selectStrategy`):

```javascript
    async renderStinkBidOrders(strategyId) {
      const container = document.getElementById('stink-bid-orders');
      if (!container) return;
      container.innerHTML = '<p class="text-muted">Loading order book...</p>';
      try {
        const data = await fetchJson(`/api/strategies/${strategyId}/stink-bid-orders`);
        if (!data || !data.orders) { container.innerHTML = ''; return; }
        const s = data.summary;
        let html = `
          <div class="stink-bid-summary">
            <h4>Stink Bid Order Book</h4>
            <div class="strat-perf-strip">
              ${renderStatCard({ label: 'Resting', value: s.resting || 0, type: 'activity' })}
              ${renderStatCard({ label: 'Filled', value: s.filled || 0, type: 'performance', tone: s.filled > 0 ? 'positive' : '' })}
              ${renderStatCard({ label: 'Cancelled', value: s.cancelled || 0, type: 'activity' })}
              ${renderStatCard({ label: 'Expired', value: s.expired || 0, type: 'activity' })}
              ${renderStatCard({ label: 'Total', value: s.total || 0, type: 'activity' })}
            </div>
          </div>`;
        if (data.orders.length > 0) {
          html += `<div class="table-wrapper"><table class="table table-sm">
            <thead><tr>
              <th>Match</th><th>Tour</th><th>Favorite</th><th>Bid</th>
              <th>State</th><th>Fav Price</th><th>Fills</th><th>Placed</th>
            </tr></thead><tbody>`;
          for (const o of data.orders) {
            const stateClass = o.state === 'filled' ? 'text-positive'
              : o.state === 'resting' ? 'text-info'
              : o.state === 'cancelled' ? 'text-muted' : 'text-warning';
            const fillCount = (o.fills || []).length;
            const matchLabel = o.player1 && o.player2
              ? `${esc(o.player1)} vs ${esc(o.player2)}`
              : esc(o.market_title || '--');
            html += `<tr>
              <td title="${esc(o.market_title)}">${matchLabel}</td>
              <td>${esc((o.tour || '').toUpperCase())}</td>
              <td>${esc(o.favorite_token_label)}</td>
              <td>${fmt.price(o.bid_price)}</td>
              <td class="${stateClass}">${esc(o.state)}</td>
              <td>${fmt.price(o.favorite_price_at_entry)}</td>
              <td>${fillCount > 0 ? `<span class="badge badge-xs chip-positive">${fillCount}</span>` : '0'}</td>
              <td><span class="text-xs">${esc((o.placed_at || '').slice(0, 16))}</span></td>
            </tr>`;
            // Show fill rows nested under the order
            for (const f of (o.fills || [])) {
              html += `<tr class="stink-bid-fill-row">
                <td colspan="2" class="text-xs text-muted" style="padding-left:2rem">Fill: ${esc(f.fill_label)}</td>
                <td colspan="2" class="text-xs">$${(f.trade_size || 0).toFixed(0)} @ ${fmt.price(f.trade_price)}</td>
                <td colspan="2" class="text-xs ${f.slippage > 0 ? 'text-positive' : f.slippage < 0 ? 'text-negative' : ''}">slip: ${(f.slippage * 100).toFixed(1)}%</td>
                <td colspan="2" class="text-xs text-muted">${esc((f.trade_ts || '').slice(0, 16))}</td>
              </tr>`;
            }
          }
          html += '</tbody></table></div>';
        }
        container.innerHTML = html;
      } catch (e) {
        container.innerHTML = `<p class="text-muted">Failed to load order book</p>`;
      }
    },
```

- [ ] **Step 2: Add the container div to strategy detail template**

In the strategy detail rendering (inside `selectStrategy()`, where the detail HTML is built), add a `<div id="stink-bid-orders"></div>` after the positions section. Then call `renderStinkBidOrders` if the strategy type is `stink_bid_v1`.

Find the section where `selectStrategy` renders the detail view (around line 4380+). After the detail panel HTML is set, add:

```javascript
      // After setting innerHTML on the detail panel:
      if (strat.strategy_type === 'stink_bid_v1') {
        strategies.renderStinkBidOrders(strat.id);
      }
```

And ensure the detail HTML template includes the container div. Add `<div id="stink-bid-orders"></div>` after the existing positions/executions section in the detail template.

- [ ] **Step 3: Verify JS parses**

Run: `node -c static/app.js`

Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat: render stink bid order book in strategy detail view"
```

---

### Task 5: Create the strategy seed script

**Files:**
- Create: `scripts/seed_stink_bid_strategy.py`

A one-shot script to insert the stink_bid_v1 strategy row into the DB. Idempotent — skips if a stink_bid_v1 strategy already exists. Prints the strategy_id on success (needed for the service file).

- [ ] **Step 1: Write the seed script**

```python
#!/usr/bin/env python3
"""Seed the stink_bid_v1 strategy row.  Idempotent — skips if one already exists.

Usage:
    python3 scripts/seed_stink_bid_strategy.py [db_path]

Prints the strategy ID on success (needed for --strategy-id in the service file).
"""
from __future__ import annotations

import json
import sqlite3
import sys


DEFAULT_PARAMS = {
    "dip_pct": 0.30,
    "min_favorite": 0.55,
    "min_bid": 0.05,
    "min_fill_size": 50.0,
    "max_resting": 3,
    "max_daily_fills": 5,
    "reprice_threshold": 0.02,
    "min_volume_24h": 10000.0,
    "position_size_usdc": 25.0,
    "espn_pause_after": 3,
    "espn_cancel_after_min": 10,
}


def seed(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    existing = conn.execute(
        "SELECT id FROM strategies WHERE strategy_type = 'stink_bid_v1' LIMIT 1"
    ).fetchone()
    if existing:
        sid = int(existing["id"])
        print(f"stink_bid_v1 strategy already exists: id={sid}")
        conn.close()
        return sid

    cur = conn.execute(
        """INSERT INTO strategies
           (wallet, label, strategy_type, parameters, status, lifecycle_stage)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "0x0000000000000000000000000000000000000001",
            "Stink Bid Tennis v1",
            "stink_bid_v1",
            json.dumps(DEFAULT_PARAMS),
            "active",
            "paper",
        ),
    )
    conn.commit()
    sid = int(cur.lastrowid or 0)
    conn.close()
    print(f"Created stink_bid_v1 strategy: id={sid}")
    return sid


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "sniper_tracker.db"
    seed(db)
```

- [ ] **Step 2: Test the script locally**

Run: `python3 scripts/seed_stink_bid_strategy.py sniper_tracker.db`

Expected: `Created stink_bid_v1 strategy: id=<N>` (or "already exists" if re-run).

- [ ] **Step 3: Verify idempotency**

Run again: `python3 scripts/seed_stink_bid_strategy.py sniper_tracker.db`

Expected: `stink_bid_v1 strategy already exists: id=<N>` (same ID).

- [ ] **Step 4: Commit**

```bash
git add scripts/seed_stink_bid_strategy.py
git commit -m "feat: add seed script for stink_bid_v1 strategy row"
```

---

### Task 6: Run full test suite and verify green

- [ ] **Step 1: Run tests**

Run: `python3 -m pytest tests/ -q`

Expected: 2400+ passed, 0 failed.

- [ ] **Step 2: Verify worktree clean**

Run: `git status --short`

Expected: No output (clean).

---

### VPS Deployment Steps (manual, post-merge)

After all tasks are committed and pushed:

```bash
# On VPS
cd ~/sniper && git pull origin main

# Seed the strategy row (idempotent)
.venv/bin/python scripts/seed_stink_bid_strategy.py sniper_tracker.db
# Note the printed strategy ID (e.g. 42)

# Install the template unit
cp systemd/sniper-stink-bid@.service ~/.config/systemd/user/
systemctl --user daemon-reload

# Enable and start with the strategy ID as the instance
systemctl --user enable --now sniper-stink-bid@42.service

# Verify
systemctl --user status sniper-stink-bid@42.service
journalctl --user -u sniper-stink-bid@42 -f --no-pager | head -30

# Restart web to pick up new API endpoint
systemctl --user restart sniper-web
```

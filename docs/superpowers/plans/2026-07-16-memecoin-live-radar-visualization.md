# Memecoin Live Radar Visualization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an embedded, read-only **Radar** subtab to the Memecoin page that renders existing entry-radar telemetry as a live per-`(network_id, mint)` view: episode table, per-coin timeline drill-down, and a bottom analytics panel.

**Architecture:** A new pure read-only projection module (`sniper/services/memecoin_radar_view.py`) queries the already-written telemetry tables (`memecoin_token_episodes`, `memecoin_feed_observations_v2`, `memecoin_quote_observations_v2`, `memecoin_radar_ratings`, `memecoin_holder_evidence`, `memecoin_events`, `memecoin_positions`, `memecoin_strategy_candidates`, `memecoin_candidate_positions`). Three thin Flask endpoints in `sniper/routes/api_memecoin.py` expose it. One `Radar` subtab in `static/app.js` renders it, polling the local API on the existing 4-second subtab cycle. Nothing runs in the funded entry/exit path; the module issues SELECT statements only.

**Tech Stack:** Python 3, SQLite (WAL), Flask blueprint (`create_api_memecoin_blueprint`), vanilla JS (`static/app.js`), pytest.

**Spec:** `docs/superpowers/specs/2026-07-16-memecoin-live-radar-visualization-design.md`

## Global Constraints

- **Read-only.** No radar code issues INSERT/UPDATE/DELETE/CREATE/REPLACE/ALTER/DROP. Verified by a SQL-spy test (Task 4). This supersedes the spec's incidental "own connection / PRAGMA query_only" phrasing with an equivalent, testable guarantee appropriate to the shared-connection blueprint.
- **No new table, column, writer, or migration.** Projection over existing tables only.
- **No new external network request.** Local SQLite + local HTTP API only. No DexScreener/CLOB/fomo/RPC.
- **Nothing in the entry/exit/reservation path.** Radar is a separate route + UI subtab; the funded runner is never touched and needs no restart.
- **Identity is `(network_id, mint)`.** `memecoin_token_episodes` names these `chain_id` / `token_address` (identical values: `solana`, `8453`, `4663`, `56`, `1`, ...). A bare `0x` address on `8453` and `4663` must render as two distinct rows.
- **Honesty labels.** Claimed source time (`source_ts`) shown separately from observed time (`observed_at`), always labeled untrusted. Immutable `attempt_rating` (from `memecoin_radar_ratings`) rendered distinctly from display-only `advisory_rating`. Missing facts render `null` (blank/yellow), never zero-filled. `global_feed` and `follow_activity` never pooled.
- **Timestamps are ISO-8601 UTC strings.** Most use `+00:00`; `memecoin_events.created_at` uses `Z`. Parse with `.replace("Z","+00:00")`. ISO strings sort lexicographically, so window filters use string comparison. "Now" is `sniper.util.clock.real_utc_now_iso()` (host clock runs +7h; this corrects it). Service functions accept an injectable `now_iso` for deterministic tests.
- **Rating rule is reused, never reimplemented.** Advisory rating calls `sniper.services.memecoin_entry_radar.rate_entry_facts(facts, chase_cap_pct=None)`, which returns `(rating, reason_codes)` where rating ∈ `{"green","yellow","red"}`.
- All new tests must pass and the touched suite stays green: `python3 -m pytest tests/test_memecoin_radar_view.py tests/test_memecoin_radar_api.py -q`.

---

### Task 1: Projection module scaffold + pure helpers

**Files:**
- Create: `sniper/services/memecoin_radar_view.py`
- Test: `tests/test_memecoin_radar_view.py`

**Interfaces:**
- Consumes: `sniper.services.memecoin_entry_radar.rate_entry_facts(facts, chase_cap_pct=None) -> (str, list)`
- Produces:
  - `chain_label(network_id) -> str`
  - `drop_top_two(pnls: list[float]) -> float`
  - `advisory_rating(episode, latest_quote, first_source_ts, now_iso, *, chase_cap_pct=None) -> (str, list)`
  - private: `_parse_ts`, `_age_s`, `_pct_change`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_memecoin_radar_view.py
from sniper.services.memecoin_radar_view import (
    chain_label, drop_top_two, advisory_rating,
)


def test_chain_label_known_and_passthrough():
    assert chain_label("solana") == "SOL"
    assert chain_label("8453") == "Base"
    assert chain_label("4663") == "Robinhood"
    assert chain_label("143") == "143"      # unknown passes through raw
    assert chain_label(None) == "?"


def test_drop_top_two_removes_two_largest():
    assert drop_top_two([100.0, 50.0, 5.0, 5.0, -20.0]) == -10.0  # keep 5+5-20
    assert drop_top_two([10.0]) == 0.0                            # fewer than 2 -> empty
    assert drop_top_two([]) == 0.0


def test_advisory_rating_high_mcap_is_yellow():
    ep = {"chain_id": "solana", "token_address": "MINT",
          "first_source_price": 1.0, "first_executable_price": 1.0}
    q = {"id": 7, "price": 1.02, "market_cap": 40_000_000.0, "liquidity": 50_000.0,
         "observed_at": "2026-07-16T10:00:20+00:00", "source_ts": "2026-07-16T10:00:00+00:00"}
    rating, reasons = advisory_rating(ep, q, "2026-07-16T10:00:00+00:00",
                                      "2026-07-16T10:00:20+00:00")
    assert rating == "yellow"
    assert "entry_mcap_ge_30m" in reasons


def test_advisory_rating_clean_is_green():
    ep = {"chain_id": "solana", "token_address": "MINT",
          "first_source_price": 1.0, "first_executable_price": 1.0}
    q = {"id": 7, "price": 1.02, "market_cap": 500_000.0, "liquidity": 50_000.0,
         "observed_at": "2026-07-16T10:00:20+00:00", "source_ts": "2026-07-16T10:00:00+00:00"}
    rating, reasons = advisory_rating(ep, q, "2026-07-16T10:00:10+00:00",
                                      "2026-07-16T10:00:20+00:00")
    assert rating == "green", reasons
    assert reasons == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_radar_view.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sniper.services.memecoin_radar_view'`

- [ ] **Step 3: Write minimal implementation**

```python
# sniper/services/memecoin_radar_view.py
"""Read-only projection of memecoin entry-radar telemetry for the dashboard.

Every function issues SELECT statements only. It never writes and is never
called from the funded entry/exit path. It renders the tables written by the
approved 2026-07-16 entry-radar-coin-state telemetry into a live
per-(network_id, mint) view. See
docs/superpowers/specs/2026-07-16-memecoin-live-radar-visualization-design.md.
"""
import json
from datetime import datetime, timedelta, timezone

from sniper.services.memecoin_entry_radar import rate_entry_facts

_CHAIN_LABELS = {
    "solana": "SOL", "8453": "Base", "4663": "Robinhood", "56": "BSC", "1": "ETH",
}
GIVEBACK_PCT = 40.0  # display-only: latest quote >= this % below peak -> giveback flag


def chain_label(network_id):
    if network_id is None:
        return "?"
    return _CHAIN_LABELS.get(str(network_id), str(network_id))


def _parse_ts(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _age_s(start_iso, now_iso):
    a, b = _parse_ts(start_iso), _parse_ts(now_iso)
    if a is None or b is None:
        return None
    return max(0.0, (b - a).total_seconds())


def _pct_change(a, b):
    """(b - a) / a * 100, or None if a is missing/zero or b is missing."""
    try:
        a = float(a); b = float(b)
    except (TypeError, ValueError):
        return None
    if a == 0:
        return None
    return (b - a) / a * 100.0


def drop_top_two(pnls):
    """Sum after removing the two largest values (tail-dependence check)."""
    vals = sorted((float(x) for x in pnls), reverse=True)
    return round(sum(vals[2:]), 2)


def advisory_rating(episode, latest_quote, first_source_ts, now_iso, *, chase_cap_pct=None):
    """Display-only green/yellow/red for a watch-only coin (no real attempt).

    Reuses the immutable v1 rule against observable episode + latest-quote facts.
    NEVER persisted. Returns (rating, reason_codes).
    """
    ep = dict(episode) if episode is not None else {}
    q = dict(latest_quote) if latest_quote is not None else {}
    first_exec = ep.get("first_executable_price")
    runup = _pct_change(first_exec, q.get("price")) if first_exec else None
    facts = {
        "terminal_status": None,   # no attempt -> never an existing-refusal red
        "terminal_reason": None,
        "network_id": ep.get("chain_id"),
        "mint": ep.get("token_address"),
        "source_price": ep.get("first_source_price"),
        "quote_price": q.get("price"),
        "quote_age_ms": None,
        "quote_id": q.get("id"),
        "entry_mcap": q.get("market_cap"),
        "entry_liquidity": q.get("liquidity"),
        "observed_at": q.get("observed_at"),
        "claimed_source_age_s": _age_s(first_source_ts, now_iso),
        "episode_executable_runup_pct": runup,
    }
    qsrc, qobs = q.get("source_ts"), q.get("observed_at")
    a, b = _parse_ts(qsrc), _parse_ts(qobs)
    if a is not None and b is not None:
        facts["quote_age_ms"] = max(0.0, (b - a).total_seconds() * 1000.0)
    return rate_entry_facts(facts, chase_cap_pct=chase_cap_pct)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_radar_view.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_radar_view.py tests/test_memecoin_radar_view.py
git commit -m "feat(memecoin): radar-view projection scaffold + pure helpers"
```

---

### Task 2: `radar_rows()` — live episode table projection

**Files:**
- Modify: `sniper/services/memecoin_radar_view.py`
- Test: `tests/test_memecoin_radar_view.py`

**Interfaces:**
- Consumes: `chain_label`, `advisory_rating`, `_age_s`, `_pct_change` (Task 1); tables listed in Global Constraints.
- Produces: `radar_rows(conn, *, window_hours=24, chain=None, source=None, rating=None, fast=False, phase=None, limit=200, now_iso=None) -> list[dict]`. Each dict has keys: `episode_id, network_id, mint, chain_label, ticker, phase, giveback, age_s, buyers, consensus_cluster_count, source_quote_drift_pct, runup_pct, mcap, attempt_rating, advisory_rating, advisory_reasons, effective_rating, signal_source, claimed_source_age_s`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_memecoin_radar_view.py
import sqlite3
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_radar_view import radar_rows

NOW = "2026-07-16T10:05:00+00:00"


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _episode(c, ep_id, nid, mint, **kw):
    c.execute(
        "INSERT INTO memecoin_token_episodes(episode_id, chain_id, token_address, "
        "started_ts, ended_ts, status, consensus_cluster_count, first_source_price, "
        "first_executable_price, last_signal_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (ep_id, nid, mint, kw.get("started_ts", "2026-07-16T10:00:00+00:00"),
         kw.get("ended_ts"), kw.get("status", "open"),
         kw.get("consensus", 1), kw.get("first_source_price", 1.0),
         kw.get("first_executable_price", 1.0),
         kw.get("last_signal_ts", "2026-07-16T10:04:00+00:00")))


def _feed(c, nid, mint, uid, **kw):
    c.execute(
        "INSERT INTO memecoin_feed_observations_v2(upstream_event_id, raw_type, "
        "normalized_side, source_user_id, source_handle, network_id, mint, ticker, "
        "their_price, their_size_usd, source_ts, observed_at, lag_s, was_curated, "
        "was_real_approved, raw_json, capture_version, clock_domain) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (kw.get("uev", f"ev-{uid}-{mint}"), "buy", kw.get("side", "buy"), uid,
         kw.get("handle", uid), nid, mint, kw.get("ticker", "TICK"),
         kw.get("their_price", 1.0), kw.get("size", 500.0),
         kw.get("source_ts", "2026-07-16T10:00:00+00:00"),
         kw.get("observed_at", "2026-07-16T10:00:03+00:00"), kw.get("lag_s", 3.0),
         1, 0, "{}", 2, "observed_real_utc"))


def _quote(c, nid, mint, **kw):
    c.execute(
        "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, "
        "observed_at, source_ts, price, liquidity, market_cap, volume_24h, quality, "
        "contexts, capture_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (kw.get("sweep", "sw-1"), nid, mint,
         kw.get("observed_at", "2026-07-16T10:04:30+00:00"),
         kw.get("source_ts", "2026-07-16T10:04:29+00:00"),
         kw.get("price", 1.2), kw.get("liquidity", 60_000.0),
         kw.get("mcap", 800_000.0), kw.get("vol", 0.0), "ok", "[]", 2))


def test_radar_rows_projects_watching_episode():
    c = _conn()
    _episode(c, "ep1", "solana", "MINT", first_source_price=1.0,
             first_executable_price=1.1)
    _feed(c, "solana", "MINT", "u1", ticker="ABC")
    _feed(c, "solana", "MINT", "u2")
    _quote(c, "solana", "MINT", price=1.32, mcap=430_000.0)
    rows = radar_rows(c, now_iso=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["network_id"] == "solana" and r["mint"] == "MINT"
    assert r["chain_label"] == "SOL"
    assert r["ticker"] == "ABC"
    assert r["phase"] == "watching"
    assert r["buyers"] == 2
    assert r["mcap"] == 430_000.0
    assert round(r["runup_pct"], 1) == 20.0          # (1.32-1.1)/1.1*100
    assert r["attempt_rating"] is None                # no radar_ratings row
    assert r["advisory_rating"] in {"green", "yellow", "red"}
    assert r["age_s"] == 300.0                         # 10:05 - 10:00


def test_radar_rows_same_address_two_chains_are_distinct():
    c = _conn()
    _episode(c, "ep-b", "8453", "0xABC")
    _episode(c, "ep-r", "4663", "0xABC")
    _feed(c, "8453", "0xABC", "u1"); _feed(c, "4663", "0xABC", "u2")
    rows = radar_rows(c, now_iso=NOW)
    keys = {(r["network_id"], r["mint"]) for r in rows}
    assert keys == {("8453", "0xABC"), ("4663", "0xABC")}


def test_radar_rows_filters_by_chain_and_fast():
    c = _conn()
    _episode(c, "ep1", "solana", "M1")
    _feed(c, "solana", "M1", "u1", source_ts="2026-07-16T10:04:50+00:00")  # ~10s old -> fast
    _episode(c, "ep2", "8453", "M2")
    _feed(c, "8453", "M2", "u2", source_ts="2026-07-16T09:00:00+00:00")    # >60s old
    assert {r["mint"] for r in radar_rows(c, chain="solana", now_iso=NOW)} == {"M1"}
    assert {r["mint"] for r in radar_rows(c, fast=True, now_iso=NOW)} == {"M1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_radar_view.py -k radar_rows -q`
Expected: FAIL with `ImportError: cannot import name 'radar_rows'`

- [ ] **Step 3: Write minimal implementation**

Append to `sniper/services/memecoin_radar_view.py`:

```python
def _now(now_iso):
    if now_iso:
        return now_iso
    from sniper.util.clock import real_utc_now_iso
    return real_utc_now_iso()


def radar_rows(conn, *, window_hours=24, chain=None, source=None, rating=None,
               fast=False, phase=None, limit=200, now_iso=None):
    now_iso = _now(now_iso)
    now_dt = _parse_ts(now_iso)
    cutoff_iso = None
    if now_dt is not None and window_hours:
        cutoff_iso = (now_dt - timedelta(hours=float(window_hours))).isoformat()

    q = ("SELECT episode_id, chain_id, token_address, started_ts, ended_ts, status, "
         "consensus_cluster_count, first_source_price, first_executable_price, "
         "last_signal_ts FROM memecoin_token_episodes")
    where, params = [], []
    if cutoff_iso:
        where.append("COALESCE(last_signal_ts, started_ts) >= ?"); params.append(cutoff_iso)
    if chain:
        where.append("chain_id = ?"); params.append(str(chain))
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY COALESCE(last_signal_ts, started_ts) DESC LIMIT ?"
    params.append(int(max(1, min(int(limit), 500))))

    rows = []
    for ep in conn.execute(q, params).fetchall():
        nid, mint = ep["chain_id"], ep["token_address"]
        latest_q = conn.execute(
            "SELECT id, price, market_cap, liquidity, observed_at, source_ts "
            "FROM memecoin_quote_observations_v2 WHERE network_id=? AND mint=? "
            "ORDER BY observed_at DESC LIMIT 1", (nid, mint)).fetchone()
        buyers = conn.execute(
            "SELECT COUNT(DISTINCT source_user_id) FROM memecoin_feed_observations_v2 "
            "WHERE network_id=? AND mint=?", (nid, mint)).fetchone()[0]
        first_obs = conn.execute(
            "SELECT ticker, source_ts FROM memecoin_feed_observations_v2 "
            "WHERE network_id=? AND mint=? ORDER BY observed_at ASC LIMIT 1",
            (nid, mint)).fetchone()
        ev = conn.execute(
            "SELECT signal_source FROM memecoin_events WHERE network_id=? AND mint=? "
            "AND signal_source IS NOT NULL ORDER BY created_at ASC LIMIT 1",
            (nid, mint)).fetchone()
        rating_row = conn.execute(
            "SELECT rating FROM memecoin_radar_ratings WHERE episode_id=? "
            "ORDER BY created_at DESC LIMIT 1", (ep["episode_id"],)).fetchone()
        if rating_row is None:
            rating_row = conn.execute(
                "SELECT rating FROM memecoin_radar_ratings WHERE network_id=? AND mint=? "
                "ORDER BY created_at DESC LIMIT 1", (nid, mint)).fetchone()
        open_real = conn.execute(
            "SELECT COUNT(*) FROM memecoin_positions WHERE network_id=? AND mint=? "
            "AND execution_mode='real' AND is_open=1", (nid, mint)).fetchone()[0]
        peak = conn.execute(
            "SELECT MAX(price) FROM memecoin_quote_observations_v2 "
            "WHERE network_id=? AND mint=?", (nid, mint)).fetchone()[0]

        attempt_rating = rating_row["rating"] if rating_row else None
        adv, adv_reasons = advisory_rating(
            ep, latest_q, first_obs["source_ts"] if first_obs else None, now_iso)

        if open_real:
            ph = "owned"
        elif ep["ended_ts"] or str(ep["status"] or "").lower() == "closed":
            ph = "cooldown"
        else:
            ph = "watching"

        latest_price = latest_q["price"] if latest_q else None
        first_exec = ep["first_executable_price"]
        runup = _pct_change(first_exec, latest_price) if first_exec else None
        src_quote_drift = _pct_change(ep["first_source_price"], ep["first_executable_price"])
        giveback = bool(
            peak and latest_price is not None
            and float(latest_price) <= float(peak) * (1 - GIVEBACK_PCT / 100.0)
            and ph in ("owned", "watching"))
        claimed_age = _age_s(first_obs["source_ts"] if first_obs else None, now_iso)
        eff_rating = attempt_rating or adv

        rows.append({
            "episode_id": ep["episode_id"], "network_id": nid, "mint": mint,
            "chain_label": chain_label(nid),
            "ticker": (first_obs["ticker"] if first_obs else None),
            "phase": ph, "giveback": giveback,
            "age_s": _age_s(ep["started_ts"], now_iso),
            "buyers": buyers, "consensus_cluster_count": ep["consensus_cluster_count"],
            "source_quote_drift_pct": src_quote_drift, "runup_pct": runup,
            "mcap": (latest_q["market_cap"] if latest_q else None),
            "attempt_rating": attempt_rating, "advisory_rating": adv,
            "advisory_reasons": adv_reasons, "effective_rating": eff_rating,
            "signal_source": (ev["signal_source"] if ev else None),
            "claimed_source_age_s": claimed_age,
        })

    def keep(r):
        if source and r["signal_source"] != source:
            return False
        if rating and r["effective_rating"] != rating:
            return False
        if phase and r["phase"] != phase:
            return False
        if fast and not (r["claimed_source_age_s"] is not None
                         and r["claimed_source_age_s"] < 60):
            return False
        return True

    return [r for r in rows if keep(r)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_radar_view.py -q`
Expected: PASS (all radar_rows + Task 1 tests)

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_radar_view.py tests/test_memecoin_radar_view.py
git commit -m "feat(memecoin): radar_rows live episode projection + filters"
```

---

### Task 3: `coin_timeline()` — per-coin drill-down projection

**Files:**
- Modify: `sniper/services/memecoin_radar_view.py`
- Test: `tests/test_memecoin_radar_view.py`

**Interfaces:**
- Consumes: `chain_label` (Task 1); seed helpers `_conn/_episode/_feed/_quote` (Task 2).
- Produces: `coin_timeline(conn, network_id, mint, *, episode_id=None, now_iso=None) -> dict` with keys: `network_id, mint, chain_label, episode, signals, decisions, holders, arm_positions, attempt_rating, attempt_reason_codes, price_path`. Each `signals` item carries `claimed_source_ts` and `observed_at` separately plus `lag_s`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_memecoin_radar_view.py
from sniper.services.memecoin_radar_view import coin_timeline


def _position(c, nid, mint, strategy, **kw):
    c.execute(
        "INSERT INTO memecoin_positions(network_id, mint, strategy, execution_mode, "
        "is_open, entry_price, last_quote_price, peak_price, max_gain_pct, realized_pnl, "
        "exit_reason, opened_at, closed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (nid, mint, strategy, kw.get("mode", "paper"), kw.get("is_open", 0),
         kw.get("entry", 1.0), kw.get("last", 1.4), kw.get("peak", 1.8),
         kw.get("gain", 80.0), kw.get("pnl", 12.0), kw.get("reason", "tp"),
         kw.get("opened", "2026-07-16T10:01:00+00:00"),
         kw.get("closed", "2026-07-16T10:03:00+00:00")))


def test_coin_timeline_separates_claimed_and_observed():
    c = _conn()
    _episode(c, "ep1", "solana", "MINT")
    _feed(c, "solana", "MINT", "u1", source_ts="2026-07-16T10:00:00+00:00",
          observed_at="2026-07-16T10:00:03+00:00", lag_s=3.0)
    _quote(c, "solana", "MINT", observed_at="2026-07-16T10:00:05+00:00", price=1.1)
    _quote(c, "solana", "MINT", observed_at="2026-07-16T10:01:05+00:00", price=1.5)
    _position(c, "solana", "MINT", "alpha_b", pnl=25.0)
    tl = coin_timeline(c, "solana", "MINT", now_iso=NOW)
    assert tl["chain_label"] == "SOL"
    assert tl["episode"]["episode_id"] == "ep1"
    s = tl["signals"][0]
    assert s["claimed_source_ts"] == "2026-07-16T10:00:00+00:00"
    assert s["observed_at"] == "2026-07-16T10:00:03+00:00"
    assert s["lag_s"] == 3.0
    assert len(tl["price_path"]) == 2
    assert tl["price_path"][0]["observed_at"] < tl["price_path"][1]["observed_at"]
    assert tl["arm_positions"][0]["strategy"] == "alpha_b"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_radar_view.py -k coin_timeline -q`
Expected: FAIL with `ImportError: cannot import name 'coin_timeline'`

- [ ] **Step 3: Write minimal implementation**

Append to `sniper/services/memecoin_radar_view.py`:

```python
def coin_timeline(conn, network_id, mint, *, episode_id=None, now_iso=None):
    ep = None
    if episode_id:
        ep = conn.execute(
            "SELECT * FROM memecoin_token_episodes WHERE episode_id=?",
            (episode_id,)).fetchone()
    if ep is None:
        ep = conn.execute(
            "SELECT * FROM memecoin_token_episodes WHERE chain_id=? AND token_address=? "
            "ORDER BY started_ts DESC LIMIT 1", (network_id, mint)).fetchone()

    signals = [{
        "kind": "signal", "side": r["normalized_side"],
        "user": r["source_handle"] or r["source_user_id"],
        "their_price": r["their_price"], "their_size_usd": r["their_size_usd"],
        "claimed_source_ts": r["source_ts"], "observed_at": r["observed_at"],
        "lag_s": r["lag_s"], "curated": bool(r["was_curated"]),
        "real_approved": bool(r["was_real_approved"]),
    } for r in conn.execute(
        "SELECT normalized_side, source_user_id, source_handle, their_price, "
        "their_size_usd, source_ts, observed_at, lag_s, was_curated, was_real_approved "
        "FROM memecoin_feed_observations_v2 WHERE network_id=? AND mint=? "
        "ORDER BY observed_at ASC", (network_id, mint))]

    decisions = [dict(r) for r in conn.execute(
        "SELECT signal_source, action, real_decision, real_decision_reason, "
        "real_decision_at, created_at FROM memecoin_events "
        "WHERE network_id=? AND mint=? ORDER BY created_at ASC", (network_id, mint))]

    holders = [dict(r) for r in conn.execute(
        "SELECT source_user_id, source_handle, evidence_state, remaining_usd, "
        "observed_at, confirmation_state, invalidated_at, invalidation_reason "
        "FROM memecoin_holder_evidence WHERE network_id=? AND mint=? "
        "ORDER BY observed_at ASC", (network_id, mint))]

    arm_positions = [dict(r) for r in conn.execute(
        "SELECT strategy, execution_mode, entry_price, last_quote_price, peak_price, "
        "max_gain_pct, realized_pnl, exit_reason, opened_at, closed_at, is_open "
        "FROM memecoin_positions WHERE network_id=? AND mint=? ORDER BY opened_at ASC",
        (network_id, mint))]

    rating_row = None
    if ep is not None:
        rating_row = conn.execute(
            "SELECT rating, reason_codes_json, decided_at FROM memecoin_radar_ratings "
            "WHERE episode_id=? ORDER BY created_at DESC LIMIT 1",
            (ep["episode_id"],)).fetchone()
    if rating_row is None:
        rating_row = conn.execute(
            "SELECT rating, reason_codes_json, decided_at FROM memecoin_radar_ratings "
            "WHERE network_id=? AND mint=? ORDER BY created_at DESC LIMIT 1",
            (network_id, mint)).fetchone()

    price_path = [{
        "observed_at": r["observed_at"], "price": r["price"], "market_cap": r["market_cap"],
    } for r in conn.execute(
        "SELECT observed_at, price, market_cap FROM memecoin_quote_observations_v2 "
        "WHERE network_id=? AND mint=? ORDER BY observed_at ASC LIMIT 2000",
        (network_id, mint))]
    if len(price_path) > 300:
        step = len(price_path) // 300 + 1
        price_path = price_path[::step]

    return {
        "network_id": network_id, "mint": mint, "chain_label": chain_label(network_id),
        "episode": (dict(ep) if ep is not None else None),
        "signals": signals, "decisions": decisions, "holders": holders,
        "arm_positions": arm_positions,
        "attempt_rating": (rating_row["rating"] if rating_row else None),
        "attempt_reason_codes": (rating_row["reason_codes_json"] if rating_row else None),
        "price_path": price_path,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_radar_view.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_radar_view.py tests/test_memecoin_radar_view.py
git commit -m "feat(memecoin): coin_timeline drill-down projection"
```

---

### Task 4: `radar_summary()` — bottom analytics panel + no-write guarantee

**Files:**
- Modify: `sniper/services/memecoin_radar_view.py`
- Test: `tests/test_memecoin_radar_view.py`

**Interfaces:**
- Consumes: `chain_label`, `drop_top_two` (Task 1); all three service functions for the spy test.
- Produces: `radar_summary(conn, *, window_hours=24, chain=None, source=None, now_iso=None) -> dict` with keys: `window_hours, outcomes_by_chain_source, paper_arms, attribution_complete_pct, candidates`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_memecoin_radar_view.py
import json as _json
from sniper.services.memecoin_radar_view import radar_summary, radar_rows, coin_timeline


def _rating(c, nid, mint, rating, source, ep_id=None):
    c.execute(
        "INSERT INTO memecoin_radar_ratings(attempt_id, episode_id, network_id, mint, "
        "signal_source, rating_version, rating, reason_codes_json, facts_json, "
        "facts_hash, created_at, clock_domain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"att-{mint}-{rating}", ep_id, nid, mint, source, 1, rating, "[]", "{}",
         "h", "2026-07-16T10:02:00+00:00", "observed_real_utc"))


def test_radar_summary_arms_outcomes_and_drop_top2():
    c = _conn()
    _episode(c, "ep1", "solana", "MINT")
    _rating(c, "solana", "MINT", "green", "global_feed", "ep1")
    _rating(c, "solana", "MINT", "yellow", "global_feed", "ep1")
    for pnl in (100.0, 60.0, 5.0, -20.0):        # matched arm PnL
        _position(c, "solana", "MINT", "alpha_b", pnl=pnl)
    s = radar_summary(c, now_iso=NOW)
    key = "SOL/global_feed"
    assert s["outcomes_by_chain_source"][key] == {"green": 1, "yellow": 1, "red": 0}
    arm = next(a for a in s["paper_arms"] if a["arm"] == "alpha_b")
    assert arm["closed"] == 4
    assert arm["pnl"] == 145.0
    assert arm["pnl_drop_top2"] == -15.0          # keep 5 + -20


def test_radar_summary_candidate_gate_progress():
    c = _conn()
    c.execute(
        "INSERT INTO memecoin_strategy_candidates(candidate_id, name, status, spawn_index, "
        "generation, predicate_json, exit_policy_json, sizing_json, costs_json, "
        "controls_json, gates_json, params_hash, born_at, discovery_cutoff, "
        "capture_version_floor, execution_mode, timecap_s, alpha_alloc, notes) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("c1", "peak_stall", "active", 0, 0, "{}", "{}", "{}", "{}", "{}",
         _json.dumps({"paired_closes_per_chain": "30-50"}), "p", "2026-07-16T00:00:00+00:00",
         "2026-07-16T00:00:00+00:00", 2, "paper", 600, 1.0, ""))
    c.execute(
        "INSERT INTO memecoin_candidate_positions(candidate_id, signal_id, episode_key, "
        "network_id, mint, evidence_stage, execution_mode, timecap_s, entry_price, "
        "entry_notional, quantity, entry_cost, opened_at, status, stop_hits) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("c1", 1, "solana:MINT", "solana", "MINT", "stage1", "paper", 600, 1.0,
         10.0, 10.0, 10.0, "2026-07-16T10:00:00+00:00", "closed", 0))
    s = radar_summary(c, now_iso=NOW)
    cand = s["candidates"][0]
    assert cand["candidate_id"] == "c1"
    assert cand["paired_closes_target"] == "30-50"
    assert cand["closed_by_chain"] == {"SOL": 1}


class _SpyConn:
    def __init__(self, real):
        self._real = real; self.writes = []
    def execute(self, sql, *a, **k):
        head = sql.lstrip().split(None, 1)[0].upper() if sql.strip() else ""
        if head in {"INSERT", "UPDATE", "DELETE", "REPLACE", "DROP", "ALTER", "CREATE"}:
            self.writes.append(sql)
        return self._real.execute(sql, *a, **k)
    def __getattr__(self, name):
        return getattr(self._real, name)


def test_radar_view_never_writes():
    real = _conn()
    _episode(real, "ep1", "solana", "MINT")
    _feed(real, "solana", "MINT", "u1"); _quote(real, "solana", "MINT")
    _position(real, "solana", "MINT", "alpha_b")
    spy = _SpyConn(real)
    radar_rows(spy, now_iso=NOW)
    radar_summary(spy, now_iso=NOW)
    coin_timeline(spy, "solana", "MINT", now_iso=NOW)
    assert spy.writes == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_radar_view.py -k "summary or never_writes" -q`
Expected: FAIL with `ImportError: cannot import name 'radar_summary'`

- [ ] **Step 3: Write minimal implementation**

Append to `sniper/services/memecoin_radar_view.py`:

```python
def radar_summary(conn, *, window_hours=24, chain=None, source=None, now_iso=None):
    now_iso = _now(now_iso)
    now_dt = _parse_ts(now_iso)
    cutoff_iso = None
    if now_dt is not None and window_hours:
        cutoff_iso = (now_dt - timedelta(hours=float(window_hours))).isoformat()

    ep_q = "SELECT chain_id, token_address FROM memecoin_token_episodes"
    ep_where, ep_params = [], []
    if cutoff_iso:
        ep_where.append("COALESCE(last_signal_ts, started_ts) >= ?"); ep_params.append(cutoff_iso)
    if chain:
        ep_where.append("chain_id = ?"); ep_params.append(str(chain))
    if ep_where:
        ep_q += " WHERE " + " AND ".join(ep_where)
    coins = {(r["chain_id"], r["token_address"]) for r in conn.execute(ep_q, ep_params)}

    r_q = "SELECT network_id, signal_source, rating, COUNT(*) n FROM memecoin_radar_ratings"
    r_where, r_params = [], []
    if cutoff_iso:
        r_where.append("created_at >= ?"); r_params.append(cutoff_iso)
    if chain:
        r_where.append("network_id = ?"); r_params.append(str(chain))
    if source:
        r_where.append("signal_source = ?"); r_params.append(source)
    if r_where:
        r_q += " WHERE " + " AND ".join(r_where)
    r_q += " GROUP BY network_id, signal_source, rating"
    outcomes = {}
    for row in conn.execute(r_q, r_params):
        key = f"{chain_label(row['network_id'])}/{row['signal_source'] or '?'}"
        bucket = outcomes.setdefault(key, {"green": 0, "yellow": 0, "red": 0})
        rk = str(row["rating"] or "").lower()
        if rk in bucket:
            bucket[rk] += row["n"]

    arm_q = ("SELECT strategy, realized_pnl, network_id, mint FROM memecoin_positions "
             "WHERE is_open=0 AND realized_pnl IS NOT NULL")
    arm_params = []
    if cutoff_iso:
        arm_q += " AND closed_at >= ?"; arm_params.append(cutoff_iso)
    if chain:
        arm_q += " AND network_id = ?"; arm_params.append(str(chain))
    per_arm = {}
    for row in conn.execute(arm_q, arm_params):
        if coins and (row["network_id"], row["mint"]) not in coins:
            continue
        per_arm.setdefault(row["strategy"] or "?", []).append(float(row["realized_pnl"]))
    paper_arms = [{
        "arm": name, "closed": len(pnls), "pnl": round(sum(pnls), 2),
        "pnl_drop_top2": drop_top_two(pnls),
    } for name, pnls in sorted(per_arm.items())]

    att_q = ("SELECT COUNT(*) total, SUM(CASE WHEN price_attribution IS NOT NULL "
             "AND quote_id IS NOT NULL AND source_drift_pct IS NOT NULL "
             "AND execution_slippage_pct IS NOT NULL THEN 1 ELSE 0 END) complete "
             "FROM memecoin_positions WHERE execution_mode='real'")
    att_params = []
    if cutoff_iso:
        att_q += " AND opened_at >= ?"; att_params.append(cutoff_iso)
    att = conn.execute(att_q, att_params).fetchone()
    total = att["total"] or 0
    attribution_pct = round(100.0 * (att["complete"] or 0) / total, 1) if total else None

    candidates = []
    for cand in conn.execute(
        "SELECT candidate_id, name, status, gates_json FROM memecoin_strategy_candidates"):
        try:
            gates = json.loads(cand["gates_json"] or "{}")
        except (TypeError, ValueError):
            gates = {}
        closed_by_chain = {}
        for row in conn.execute(
            "SELECT network_id, COUNT(*) n FROM memecoin_candidate_positions "
            "WHERE candidate_id=? AND status='closed' GROUP BY network_id",
            (cand["candidate_id"],)):
            closed_by_chain[chain_label(row["network_id"])] = row["n"]
        candidates.append({
            "candidate_id": cand["candidate_id"], "name": cand["name"],
            "status": cand["status"],
            "paired_closes_target": gates.get("paired_closes_per_chain", "30-50"),
            "closed_by_chain": closed_by_chain,
        })

    return {
        "window_hours": window_hours,
        "outcomes_by_chain_source": outcomes,
        "paper_arms": paper_arms,
        "attribution_complete_pct": attribution_pct,
        "candidates": candidates,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_radar_view.py -q`
Expected: PASS (all Task 1–4 tests, including `test_radar_view_never_writes`)

- [ ] **Step 5: Commit**

```bash
git add sniper/services/memecoin_radar_view.py tests/test_memecoin_radar_view.py
git commit -m "feat(memecoin): radar_summary aggregates + no-write guarantee test"
```

---

### Task 5: Read-only radar endpoints

**Files:**
- Modify: `sniper/routes/api_memecoin.py` (add three routes inside `create_api_memecoin_blueprint`, after the `events_recent` route ~line 201)
- Test: `tests/test_memecoin_radar_api.py`

**Interfaces:**
- Consumes: `sniper.services.memecoin_radar_view.{radar_rows, coin_timeline, radar_summary}`; the blueprint's closure `conn`.
- Produces: `GET /api/memecoin/radar`, `GET /api/memecoin/radar/summary`, `GET /api/memecoin/radar/<network_id>/<path:mint>`.

- [ ] **Step 1: Verify a module logger exists**

Run: `grep -n "^logger\|logger = logging" sniper/routes/api_memecoin.py`
If it prints a `logger = ...` line, reuse it. If it prints nothing, add near the top imports:

```python
import logging
logger = logging.getLogger(__name__)
```

- [ ] **Step 2: Write the failing test**

```python
# tests/test_memecoin_radar_api.py
import sqlite3
from flask import Flask
from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint


def _client():
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute(
        "INSERT INTO memecoin_token_episodes(episode_id, chain_id, token_address, "
        "started_ts, status, consensus_cluster_count, first_source_price, "
        "first_executable_price, last_signal_ts) VALUES('ep1','solana','MINT',"
        "'2026-07-16T10:00:00+00:00','open',2,1.0,1.1,'2026-07-16T10:04:00+00:00')")
    c.execute(
        "INSERT INTO memecoin_feed_observations_v2(upstream_event_id, raw_type, "
        "normalized_side, source_user_id, source_handle, network_id, mint, ticker, "
        "source_ts, observed_at, lag_s, was_curated, was_real_approved, raw_json, "
        "capture_version, clock_domain) VALUES('e1','buy','buy','u1','u1','solana',"
        "'MINT','ABC','2026-07-16T10:00:00+00:00','2026-07-16T10:00:03+00:00',3.0,1,0,"
        "'{}',2,'observed_real_utc')")
    c.execute(
        "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, "
        "observed_at, source_ts, price, liquidity, market_cap, volume_24h, quality, "
        "contexts, capture_version) VALUES('s1','solana','MINT',"
        "'2026-07-16T10:04:30+00:00','2026-07-16T10:04:29+00:00',1.3,60000,430000,0,"
        "'ok','[]',2)")
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    return app.test_client()


def test_radar_list_endpoint():
    r = _client().get("/api/memecoin/radar").get_json()
    assert "episodes" in r and len(r["episodes"]) == 1
    assert r["episodes"][0]["chain_label"] == "SOL"
    assert r["episodes"][0]["phase"] == "watching"


def test_radar_summary_endpoint():
    r = _client().get("/api/memecoin/radar/summary").get_json()
    assert "paper_arms" in r and "outcomes_by_chain_source" in r
    assert "candidates" in r


def test_radar_timeline_endpoint():
    r = _client().get("/api/memecoin/radar/solana/MINT").get_json()
    assert r["chain_label"] == "SOL"
    assert r["signals"][0]["claimed_source_ts"] == "2026-07-16T10:00:00+00:00"
    assert r["signals"][0]["observed_at"] == "2026-07-16T10:00:03+00:00"


def test_radar_list_chain_filter():
    r = _client().get("/api/memecoin/radar?chain=8453").get_json()
    assert r["episodes"] == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_radar_api.py -q`
Expected: FAIL with 404 (routes not registered) → `KeyError`/assertion on `episodes`.

- [ ] **Step 4: Write minimal implementation**

In `sniper/routes/api_memecoin.py`, add near the top-level imports:

```python
from sniper.services import memecoin_radar_view as _radar
```

Then, inside `create_api_memecoin_blueprint`, immediately after the `events_recent` route, add:

```python
    @bp.get("/api/memecoin/radar")
    def radar_list():
        try:
            limit = min(max(int(request.args.get("limit", 200)), 1), 500)
            rows = _radar.radar_rows(
                conn, window_hours=float(request.args.get("hours", 24)),
                chain=request.args.get("chain") or None,
                source=request.args.get("source") or None,
                rating=request.args.get("rating") or None,
                fast=request.args.get("fast") in ("1", "true", "yes"),
                phase=request.args.get("phase") or None, limit=limit)
            return jsonify({"episodes": rows})
        except Exception as e:
            logger.exception("radar_list failed")
            return jsonify({"error": str(e), "episodes": []}), 500

    @bp.get("/api/memecoin/radar/summary")
    def radar_summary_route():
        try:
            return jsonify(_radar.radar_summary(
                conn, window_hours=float(request.args.get("hours", 24)),
                chain=request.args.get("chain") or None,
                source=request.args.get("source") or None))
        except Exception as e:
            logger.exception("radar_summary failed")
            return jsonify({"error": str(e)}), 500

    @bp.get("/api/memecoin/radar/<network_id>/<path:mint>")
    def radar_timeline(network_id, mint):
        try:
            return jsonify(_radar.coin_timeline(
                conn, network_id, mint,
                episode_id=request.args.get("episode_id") or None))
        except Exception as e:
            logger.exception("radar_timeline failed")
            return jsonify({"error": str(e)}), 500
```

Note on routing: `/api/memecoin/radar/summary` is a single trailing segment and cannot collide with `/radar/<network_id>/<path:mint>` (which requires at least two segments). No ordering constraint.

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_memecoin_radar_api.py -q`
Expected: PASS (4 passed)

- [ ] **Step 6: Regression — existing memecoin API tests still green**

Run: `python3 -m pytest tests/test_memecoin_api.py tests/test_memecoin_api_health.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add sniper/routes/api_memecoin.py tests/test_memecoin_radar_api.py
git commit -m "feat(memecoin): read-only radar endpoints (list/summary/timeline)"
```

---

### Task 6: Radar subtab — episode table, filter chips, bottom summary panel

**Files:**
- Modify: `static/app.js` (subtab button ~line 2530; section div ~line 2582 after the `decisions` section; loader map ~line 2694; new methods)

**Interfaces:**
- Consumes: `GET /api/memecoin/radar`, `GET /api/memecoin/radar/summary`; existing helpers `fetchJson`, `esc`, `chainFilterChips`.
- Produces: `Radar` subtab wired into `_SECTION_LOADERS` as `radar: '_loadRadar'`; methods `_loadRadar`, `_radarRow`, `_radarSummary`; state `this._radarFilters`. Row markup exposes `data-radar-coin="<network_id>|<mint>"` and `data-radar-ep="<episode_id>"` for Task 7.

No pytest harness exists for `app.js`; verification is by curling the endpoints (Task 5, green) and a manual browser render check. Do not fabricate a JS test framework.

- [ ] **Step 1: Add the subtab button**

In the `#mc-subtabs` strip (after the `decisions` button, `static/app.js:2530`), add:

```javascript
            <button class="mc-subtab" data-subtab="radar" title="live per-coin entry-radar telemetry (read-only)">Radar</button>
```

- [ ] **Step 2: Add the section container**

After the `decisions` section `</div></div>` (`static/app.js:2582`), add:

```javascript
          <div class="mc-section" data-section="radar">
          <div class="card" style="padding:12px 16px;margin-bottom:14px">
            <div style="font-weight:600;font-size:14px;margin-bottom:2px">Entry radar <span class="text-muted" style="font-size:11px;font-weight:400">— live per-coin telemetry (read-only projection; not a buy signal). Claimed source time is untrusted and shown separately from our observed time.</span></div>
            <div id="mc-radar-filters" style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0"></div>
            <div id="mc-radar-table" class="table-wrapper"></div>
            <div id="mc-radar-drawer" style="margin-top:10px"></div>
            <div id="mc-radar-summary" style="margin-top:14px"></div>
          </div></div>
```

- [ ] **Step 3: Register the loader**

In `_SECTION_LOADERS` (`static/app.js:2694`), add the `radar` entry:

```javascript
      performance: '_loadTraders', radar: '_loadRadar',
```

- [ ] **Step 4: Add the loader + render methods**

Add these methods to the memecoin board object (near `_loadDecisions`, `static/app.js:~3590`):

```javascript
    _radarFilters: { chain: '', source: '', rating: '', fast: false },

    _radarQuery() {
      const f = this._radarFilters, p = new URLSearchParams();
      if (f.chain) p.set('chain', f.chain);
      if (f.source) p.set('source', f.source);
      if (f.rating) p.set('rating', f.rating);
      if (f.fast) p.set('fast', '1');
      return p.toString();
    },

    _radarDot(rating, advisory) {
      const c = { green: '#3fb950', yellow: '#d29922', red: '#f85149' }[rating] || '#8b949e';
      const mark = advisory ? '~' : '';
      const op = advisory ? '0.55' : '1';
      return `<span title="${advisory ? 'advisory (no real attempt)' : 'immutable attempt rating'}" style="opacity:${op}">
        <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${c};margin-right:4px"></span>${mark}${esc(rating || '—')}</span>`;
    },

    _fmtPct(v) { return (v === null || v === undefined) ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`; },
    _fmtMcap(v) { return (v === null || v === undefined) ? '—' : '$' + Math.round(v).toLocaleString(); },
    _fmtAge(s) { return (s === null || s === undefined) ? '—' : (s < 90 ? `${Math.round(s)}s` : `${Math.round(s / 60)}m`); },

    _radarRow(r) {
      const rating = r.attempt_rating
        ? this._radarDot(r.attempt_rating, false)
        : this._radarDot(r.advisory_rating, true);
      const phase = esc(r.phase) + (r.giveback ? ' <span class="text-muted">· giveback</span>' : '');
      return `<tr class="mc-radar-row" style="cursor:pointer" data-radar-coin="${esc(r.network_id)}|${esc(r.mint)}" data-radar-ep="${esc(r.episode_id || '')}">
        <td>${esc(r.chain_label)}</td>
        <td>${esc(r.ticker || r.mint.slice(0, 8))}</td>
        <td>${phase}</td>
        <td class="num">${this._fmtAge(r.age_s)}</td>
        <td class="num">${r.buyers}${r.consensus_cluster_count > 1 ? ` <span class="text-muted">(c${r.consensus_cluster_count})</span>` : ''}</td>
        <td class="num">${this._fmtPct(r.source_quote_drift_pct)}</td>
        <td class="num">${this._fmtPct(r.runup_pct)}</td>
        <td class="num">${this._fmtMcap(r.mcap)}</td>
        <td>${rating}</td>
      </tr>`;
    },

    async _loadRadar() {
      const fEl = document.getElementById('mc-radar-filters');
      if (fEl && !fEl.dataset.wired) {
        fEl.dataset.wired = '1';
        fEl.innerHTML = [
          ['chain', '', 'ALL'], ['chain', 'solana', 'SOL'], ['chain', '8453', 'Base'],
          ['chain', '4663', 'Robinhood'], ['source', 'global_feed', 'GLOBAL'],
          ['source', 'follow_activity', 'FOLLOW'], ['fast', '1', '<60s'],
          ['rating', 'green', 'G'], ['rating', 'yellow', 'Y'], ['rating', 'red', 'R'],
        ].map(([k, v, label]) =>
          `<button class="btn btn-sm mc-radar-chip" data-rk="${k}" data-rv="${v}">${label}</button>`).join('');
        fEl.addEventListener('click', (e) => {
          const b = e.target.closest('.mc-radar-chip'); if (!b) return;
          const k = b.dataset.rk, v = b.dataset.rv;
          if (k === 'fast') this._radarFilters.fast = !this._radarFilters.fast;
          else this._radarFilters[k] = (this._radarFilters[k] === v ? '' : v);
          this._loadRadar();
        });
      }

      const tEl = document.getElementById('mc-radar-table');
      let data;
      try {
        const qs = this._radarQuery();
        data = await fetchJson('/api/memecoin/radar' + (qs ? '?' + qs : ''), { suppressErrorToast: true });
      } catch (e) {
        if (tEl) tEl.innerHTML = `<div class="text-muted">Radar unavailable: ${esc(String(e && e.message || e))}</div>`;
        return;
      }
      const eps = data.episodes || [];
      if (tEl) {
        tEl.innerHTML = eps.length ? `<table class="data-table"><thead><tr>
          <th>Chain</th><th>Token</th><th>Phase</th><th class="num">Age</th>
          <th class="num">Buyers</th><th class="num">Src→Q</th><th class="num">Runup</th>
          <th class="num">Mcap</th><th>Rating</th></tr></thead>
          <tbody>${eps.map(r => this._radarRow(r)).join('')}</tbody></table>`
          : `<div class="text-muted">No episodes in window.</div>`;
      }
      this._radarSummary();
      if (this._radarOpenCoin) this._openRadarTimeline(this._radarOpenCoin, true);
    },

    async _radarSummary() {
      const el = document.getElementById('mc-radar-summary');
      if (!el) return;
      let s;
      try { s = await fetchJson('/api/memecoin/radar/summary', { suppressErrorToast: true }); }
      catch (e) { el.innerHTML = ''; return; }
      const arms = (s.paper_arms || []).map(a =>
        `<tr><td>${esc(a.arm)}</td><td class="num">${a.closed}</td>
         <td class="num">$${a.pnl}</td><td class="num">$${a.pnl_drop_top2}</td></tr>`).join('');
      const outs = Object.entries(s.outcomes_by_chain_source || {}).map(([k, v]) =>
        `<span class="pill" style="margin-right:6px">${esc(k)} <span style="color:#3fb950">${v.green}G</span>/<span style="color:#d29922">${v.yellow}Y</span>/<span style="color:#f85149">${v.red}R</span></span>`).join('') || '<span class="text-muted">no rated attempts yet</span>';
      const cands = (s.candidates || []).map(c =>
        `<div class="text-muted" style="font-size:11px">${esc(c.name)} (${esc(c.status)}) — closed ${JSON.stringify(c.closed_by_chain)} / target ${esc(String(c.paired_closes_target))} per chain</div>`).join('');
      el.innerHTML = `<div class="card" style="padding:10px 14px">
        <div style="font-weight:600;font-size:13px;margin-bottom:6px">Outcomes (immutable attempt ratings)</div>
        <div style="margin-bottom:10px">${outs}</div>
        <div style="font-weight:600;font-size:13px;margin-bottom:4px">Paper-arm PnL on matched episodes</div>
        <table class="data-table" style="margin-bottom:10px"><thead><tr><th>Arm</th>
          <th class="num">Closed</th><th class="num">PnL</th>
          <th class="num" title="PnL after removing each arm's 2 largest winners">Drop top-2</th></tr></thead>
          <tbody>${arms || '<tr><td colspan="4" class="text-muted">none</td></tr>'}</tbody></table>
        <div style="font-weight:600;font-size:13px">Attribution complete: ${s.attribution_complete_pct === null || s.attribution_complete_pct === undefined ? '—' : s.attribution_complete_pct + '%'}</div>
        <div style="margin-top:6px">${cands}</div>
      </div>`;
    },
```

- [ ] **Step 5: Manual verification**

Restart the web server so the new route + JS load, then verify data + render:

```bash
curl -s 'http://127.0.0.1:5000/api/memecoin/radar?limit=5' | python3 -m json.tool | head -30
curl -s 'http://127.0.0.1:5000/api/memecoin/radar/summary' | python3 -m json.tool | head -30
```

Expected: `episodes` array with `chain_label`, `phase`, `rating` fields; summary with `paper_arms`. Then open the dashboard → Memecoin → Radar subtab: table renders, filter chips toggle and refilter, bottom panel shows arms/outcomes. (Port per your web server; check `ps aux | grep sniper.web.server`.)

- [ ] **Step 6: Commit**

```bash
git add static/app.js
git commit -m "feat(memecoin): Radar subtab — episode table, filters, summary panel"
```

---

### Task 7: Radar timeline drawer (per-coin drill-down)

**Files:**
- Modify: `static/app.js` (row-click handler + `_openRadarTimeline` method)

**Interfaces:**
- Consumes: `GET /api/memecoin/radar/<network_id>/<path:mint>`; the `data-radar-coin` / `data-radar-ep` markup from Task 6.
- Produces: `_openRadarTimeline(coinKey, keepOpen)`; state `this._radarOpenCoin`. Renders into `#mc-radar-drawer`.

- [ ] **Step 1: Wire the row click**

In `_loadRadar` (Task 6), after building the table HTML, attach a one-time delegated click handler. Add this block at the end of `_loadRadar`, before the `this._radarSummary()` call:

```javascript
      if (tEl && !tEl.dataset.wired) {
        tEl.dataset.wired = '1';
        tEl.addEventListener('click', (e) => {
          const row = e.target.closest('.mc-radar-row'); if (!row) return;
          const key = row.dataset.radarCoin;
          this._radarOpenCoin = (this._radarOpenCoin === key) ? null : key;
          if (this._radarOpenCoin) this._openRadarTimeline(this._radarOpenCoin, false);
          else document.getElementById('mc-radar-drawer').innerHTML = '';
        });
      }
```

- [ ] **Step 2: Add the timeline renderer**

Add to the memecoin board object:

```javascript
    _sparkline(path) {
      const pts = (path || []).map(p => p.price).filter(v => typeof v === 'number');
      if (pts.length < 2) return '';
      const min = Math.min(...pts), max = Math.max(...pts), w = 260, h = 40;
      const span = (max - min) || 1;
      const d = pts.map((v, i) =>
        `${i === 0 ? 'M' : 'L'}${(i / (pts.length - 1) * w).toFixed(1)},${(h - (v - min) / span * h).toFixed(1)}`).join(' ');
      return `<svg width="${w}" height="${h}" style="display:block"><path d="${d}" fill="none" stroke="#58a6ff" stroke-width="1.5"/></svg>`;
    },

    async _openRadarTimeline(coinKey, keepOpen) {
      const el = document.getElementById('mc-radar-drawer');
      if (!el) return;
      const [nid, mint] = coinKey.split('|');
      let t;
      try { t = await fetchJson(`/api/memecoin/radar/${encodeURIComponent(nid)}/${encodeURIComponent(mint)}`, { suppressErrorToast: true }); }
      catch (e) { if (!keepOpen) el.innerHTML = `<div class="text-muted">Timeline unavailable: ${esc(String(e && e.message || e))}</div>`; return; }

      const sig = (t.signals || []).map(s =>
        `<tr><td>${esc(s.side || '')}</td><td>${esc(s.user || '')}</td>
         <td class="text-muted" title="claimed by source — untrusted">${esc(s.claimed_source_ts || '—')}</td>
         <td title="our observed time">${esc(s.observed_at || '—')}</td>
         <td class="num">${s.lag_s === null || s.lag_s === undefined ? '—' : s.lag_s.toFixed(1) + 's'}</td>
         <td>${s.real_approved ? '⚡' : ''}</td></tr>`).join('');
      const arms = (t.arm_positions || []).map(p =>
        `<tr><td>${esc(p.strategy || '')}</td><td>${p.is_open ? 'open' : esc(p.exit_reason || 'closed')}</td>
         <td class="num">${p.entry_price ?? '—'}</td><td class="num">${p.peak_price ?? '—'}</td>
         <td class="num">${p.realized_pnl === null || p.realized_pnl === undefined ? '—' : '$' + p.realized_pnl}</td></tr>`).join('');
      const dec = (t.decisions || []).map(d =>
        `<div class="text-muted" style="font-size:11px">${esc(d.created_at || '')} · ${esc(d.signal_source || '?')} · paper=${esc(d.action || '—')} · real=${esc(d.real_decision || '—')}${d.real_decision_reason ? ' (' + esc(d.real_decision_reason) + ')' : ''}</div>`).join('');

      el.innerHTML = `<div class="card" style="padding:10px 14px;border:1px solid #30363d">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div style="font-weight:600">${esc(t.chain_label)} · ${esc(mint)}</div>
          <div>${t.attempt_rating ? 'attempt rating: ' + esc(t.attempt_rating) : '<span class="text-muted">no real attempt</span>'}</div>
        </div>
        ${this._sparkline(t.price_path)}
        <div style="font-weight:600;font-size:12px;margin:8px 0 4px">Signals — claimed vs observed</div>
        <table class="data-table"><thead><tr><th>Side</th><th>Wallet</th>
          <th>Claimed (untrusted)</th><th>Observed</th><th class="num">Lag</th><th>Armed</th></tr></thead>
          <tbody>${sig || '<tr><td colspan="6" class="text-muted">none</td></tr>'}</tbody></table>
        <div style="font-weight:600;font-size:12px;margin:8px 0 4px">Real-path decisions</div>
        ${dec || '<div class="text-muted" style="font-size:11px">none</div>'}
        <div style="font-weight:600;font-size:12px;margin:8px 0 4px">Paper-arm positions</div>
        <table class="data-table"><thead><tr><th>Arm</th><th>State</th>
          <th class="num">Entry</th><th class="num">Peak</th><th class="num">PnL</th></tr></thead>
          <tbody>${arms || '<tr><td colspan="5" class="text-muted">none</td></tr>'}</tbody></table>
      </div>`;
    },
```

- [ ] **Step 3: Manual verification**

With the web server restarted: open Memecoin → Radar → click a coin row. Expected: the drawer opens below the table showing the price sparkline, the signals table with **separate** Claimed and Observed columns, the real-path decisions, and paper-arm positions. Click the same row again → drawer closes. Confirm the 4-second auto-refresh keeps the drawer open (state `this._radarOpenCoin`) and does not error.

- [ ] **Step 4: Commit**

```bash
git add static/app.js
git commit -m "feat(memecoin): Radar timeline drawer — claimed-vs-observed drill-down"
```

---

## Self-Review

**1. Spec coverage:**
- Embedded Radar subtab, one row per `(network_id, mint)` episode → Tasks 2, 6. ✓
- Filter chips (GLOBAL/FOLLOW/SOL/BASE/4663/<60s/rating) → Task 6. ✓
- Timeline drill-down (claimed vs observed, armed buys/sells, quote drift, consensus, real decision, terminal reason, paper-arm exits overlaid) → Tasks 3, 7. ✓
- Bottom panel (outcomes by chain/source, paper-arm PnL matched, drop-top-2, attribution %, candidate-gate progress) → Tasks 4, 6. ✓
- Read-only, no new table, nothing in entry path → Global Constraints + Task 4 no-write test. ✓
- Advisory vs immutable rating distinction → Tasks 1, 2, 6 (`_radarDot` dims advisory). ✓
- `giveback` display flag ≠ reserved `decaying` coin-state → Task 2. ✓
- Identity `(network_id, mint)`, 4663 ≠ bare 0x → Task 2 test `..._two_chains_are_distinct`. ✓
- Poll local API on existing 4s cycle → Task 6 (auto-poll via `_SECTION_LOADERS`). ✓

**2. Placeholder scan:** No TBD/TODO. Every code step shows full code. Manual-verification steps are explicit curl + browser checks (JS has no pytest harness — stated, not hidden).

**3. Type consistency:** `radar_rows`/`coin_timeline`/`radar_summary` signatures and dict keys match between service (Tasks 1–4), routes (Task 5), and UI (Tasks 6–7). `advisory_rating` returns `(str, list)` consistently. `data-radar-coin="net|mint"` produced in Task 6, consumed in Task 7. `drop_top_two` semantics identical in Task 1 impl and Task 4 assertion.

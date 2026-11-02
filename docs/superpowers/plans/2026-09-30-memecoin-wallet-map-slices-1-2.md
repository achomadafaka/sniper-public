# Memecoin Wallet Map, slices 1–2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. In this repo the crew builds inline (no subagents, pytest `-n 6` max).

**Goal:** A "Wallet Map" tab under the Memecoin venue that draws our wallets and their links (Solana today) as a d3 force graph, with client-side knobs.

**Architecture:** A pure builder `sniper/services/memecoin_wallet_map.py` turns the link-graph DB rows plus a few read-only ledger rows into `{stamp, nodes, edges}`. One GET route in `sniper/routes/api_memecoin.py` opens both databases read-only per request and returns the builder's payload. `static/wallet_map.js` holds pure helpers (`WalletMapCore`, tested under node) and the d3 renderer (`window.walletMap`).

**Tech Stack:** Python 3.13, Flask blueprint, sqlite3 (read-only URIs), d3 v7 (vendored `static/vendor/d3.min.js`), node 24 for JS unit checks.

**Spec:** `docs/superpowers/specs/2026-09-30-memecoin-wallet-map-design.md`

## Global Constraints

- Read-only. No import of `evm_real_seams`, `solana_real_seams`, `_quote_with_fallback`, Jupiter or any price helper in the builder or route. No chain, price or HTTP call.
- Ledger reads through `sniper.persistence.db_core.get_readonly_connection()`, never the blueprint's shared `conn`.
- Graph DB path = `sniper.util.state_paths.state_path("data", "memecoin_wallet_graph.db")` (state root, not the release dir), opened `file:<path>?mode=ro`.
- Chains are never pooled: the route requires one chain, resolved by `sniper.services.memecoin_chains.resolve_chain`; rows match `network_ids_for(key)`.
- Solid edge = `n_coins >= 5`; everything else the graph emitted is `weak`. Knobs hide, never add.
- Missing graph DB → `stamp.error = "graph_unavailable"`; unreadable → `"graph_unreadable"`. Never an empty map without an error.
- Our custody addresses: `MEMECOIN_OWN_ADDRESSES` (comma list, public keys only; Solana exact case, EVM lowercased). Any edge touching one is dropped. It is set in `.env` (operator YES 2026-09-30).
- Stranger `activity` is `null` (unknown), never 0.
- Covering set runs with `-n 6` max; exclude watchdog/deploy script tests; include `tests/test_memecoin_runner_health.py`.

---

### Task 1: Setting + pure payload builder

**Files:**
- Modify: `sniper/settings/memecoin.py` (append one setting)
- Create: `sniper/services/memecoin_wallet_map.py`
- Test: `tests/test_memecoin_wallet_map.py`

**Interfaces:**
- Produces: `MEMECOIN_OWN_ADDRESSES: str`; `own_address_set(raw: str) -> set[str]`; `open_graph_readonly(path) -> sqlite3.Connection | None`; `build_wallet_map(graph, ledger, *, chain: str, network_ids: tuple[str, ...], days: int, own_addresses: set[str], now_iso: str) -> dict` returning `{"stamp": {...}, "nodes": [...], "edges": [...]}`.

- [ ] **Step 1: Write the failing tests**

```python
"""Wallet map payload (spec 2026-09-30-memecoin-wallet-map-design.md)."""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m

NOW = "2026-09-30T10:00:00+00:00"
OWN = "OurCustody111111111111111111111111111111111"


def _graph(rows, finished_at="2026-09-30T09:35:00+00:00"):
    g = sqlite3.connect(":memory:")
    g.execute("CREATE TABLE memecoin_wallet_edges (id INTEGER PRIMARY KEY, network_id TEXT, "
              "edge_type TEXT, src TEXT, dst TEXT, src_user_id TEXT, dst_user_id TEXT, "
              "n_coins INTEGER, n_consistent INTEGER, lag_p50_s REAL, lag_p90_s REAL, "
              "null_p99 REAL, null_p REAL, flag TEXT, first_seen TEXT, last_seen TEXT, "
              "coverage TEXT, evidence TEXT, method_version TEXT, computed_at TEXT)")
    g.execute("CREATE TABLE memecoin_wallet_graph_runs (id INTEGER PRIMARY KEY, "
              "method_version TEXT, started_at TEXT, finished_at TEXT, window_from TEXT, "
              "edges INTEGER, helius_calls INTEGER, note TEXT)")
    g.execute("INSERT INTO memecoin_wallet_graph_runs (method_version, finished_at) VALUES (?, ?)",
              ("wallet-graph-v3", finished_at))
    for r in rows:
        g.execute("INSERT INTO memecoin_wallet_edges (network_id, edge_type, src, dst, src_user_id, "
                  "dst_user_id, n_coins, null_p, lag_p50_s, first_seen, last_seen, coverage, "
                  "evidence, method_version, computed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (r.get("net", "solana"), r["type"], r["src"], r["dst"], r.get("su"), r.get("du"),
                   r["n"], r.get("p", 0.001), r.get("lag"), "2026-09-20T00:00:00+00:00",
                   "2026-09-29T00:00:00+00:00", json.dumps({"sample": "J7 roster only"}),
                   json.dumps(r.get("ev", {})), "wallet-graph-v3", "2026-09-30T09:35:00+00:00"))
    return g


def _ledger():
    c = sqlite3.connect(":memory:")
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_wallets (source_user_id, handle, real_approved, real_chains) "
              "VALUES ('uA', 'alice', 1, '4663,solana'), ('uB', 'bob', 0, NULL)")
    c.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, created_at, "
              "network_id) VALUES ('e1','uA','M1','buy','2026-09-29T00:00:00Z','solana'), "
              "('e2','uA','M2','buy','2026-09-29T01:00:00Z','solana'), "
              "('e3','uA','M2','buy','2026-09-29T02:00:00Z','solana'), "
              "('e4','uB','M9','buy','2026-08-01T00:00:00Z','solana')")
    c.execute("INSERT INTO memecoin_address_registry (source_user_id, network_id, address, status, "
              "evidence_kind, first_seen_at, status_at) VALUES "
              "('uB','solana','BobAddr','verified','j7_trader_receipt','t','t')")
    return c


def _build(graph, ledger, own=()):
    from sniper.services.memecoin_wallet_map import build_wallet_map
    return build_wallet_map(graph, ledger, chain="solana", network_ids=("solana",), days=14,
                            own_addresses=set(own), now_iso=NOW)


def test_strength_direction_and_registry_merge():
    g = _graph([
        dict(type="follows", src="uA", dst="uB", su="uA", du="uB", n=6),
        dict(type="co_buy_slot", src="uA", dst="BobAddr", su="uA", n=3),
        dict(type="slot_follower", src="uA", dst="Stranger1", su="uA", n=4),
    ])
    out = _build(g, _ledger())
    e = {(x["type"], x["dst"]): x for x in out["edges"]}
    assert e[("follows", "uB")]["strength"] == "solid" and e[("follows", "uB")]["directed"] is True
    # BobAddr is uB's verified address: the stranger merges into the source node.
    assert e[("co_buy_slot", "uB")]["strength"] == "weak"
    assert e[("co_buy_slot", "uB")]["directed"] is False
    nodes = {n["id"]: n for n in out["nodes"]}
    assert nodes["uA"]["kind"] == "source" and nodes["uA"]["armed"] is True
    assert nodes["uA"]["activity"] == 2                 # distinct mints, not events
    assert nodes["uB"]["armed"] is False and nodes["uB"]["activity"] == 0
    assert nodes["Stranger1"]["kind"] == "stranger" and nodes["Stranger1"]["activity"] is None
    assert out["stamp"]["observed_wallets"] == 1 and out["stamp"]["stale"] is False
    assert out["stamp"]["error"] is None


def test_own_custody_edges_are_dropped():
    g = _graph([dict(type="slot_follower", src="uA", dst=OWN, su="uA", n=9),
                dict(type="slot_follower", src="uA", dst="pump:" + OWN, su="uA", n=9)])
    out = _build(g, _ledger(), own={OWN})
    assert out["edges"] == []
    assert OWN not in {n["id"] for n in out["nodes"]}


def test_hub_funder_is_flagged_and_unknown_degree_is_none():
    g = _graph([
        dict(type="shared_funder", src="uA", dst="uB", su="uA", du="uB", n=1,
             ev={"funder": "F", "funder_degree": 11}),
        dict(type="shared_funder", src="uB", dst="uA", su="uB", du="uA", n=1,
             ev={"funder": "G", "funder_degree": 2}),
        dict(type="shared_funder", src="uA", dst="X", su="uA", n=1, ev={}),
    ])
    hub = [x["hub_funder"] for x in _build(g, _ledger())["edges"]]
    assert hub == [True, False, None]


def test_other_chain_rows_are_never_pooled():
    g = _graph([dict(type="follows", src="uA", dst="uB", su="uA", du="uB", n=6, net="4663")])
    assert _build(g, _ledger())["edges"] == []


def test_missing_and_unreadable_graph_say_so():
    from sniper.services.memecoin_wallet_map import build_wallet_map, open_graph_readonly
    assert open_graph_readonly("/nonexistent/graph.db") is None
    out = build_wallet_map(None, _ledger(), chain="solana", network_ids=("solana",), days=14,
                           own_addresses=set(), now_iso=NOW)
    assert out["stamp"]["error"] == "graph_unavailable" and out["edges"] == []
    broken = sqlite3.connect(":memory:")                 # no tables at all
    out = build_wallet_map(broken, _ledger(), chain="solana", network_ids=("solana",), days=14,
                           own_addresses=set(), now_iso=NOW)
    assert out["stamp"]["error"] == "graph_unreadable" and out["edges"] == []


def test_stale_graph_is_flagged():
    out = _build(_graph([], finished_at="2026-09-30T05:00:00+00:00"), _ledger())
    assert out["stamp"]["stale"] is True


def test_own_address_set_normalizes_evm_case_only():
    from sniper.services.memecoin_wallet_map import own_address_set
    assert own_address_set(" SoLaNa1 ,0xAbC, ") == {"SoLaNa1", "0xabc"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_memecoin_wallet_map.py -q -p no:randomly`
Expected: FAIL with `ModuleNotFoundError: No module named 'sniper.services.memecoin_wallet_map'`

- [ ] **Step 3: Add the setting** (end of `sniper/settings/memecoin.py`)

```python
# Our funded custody PUBLIC addresses, comma separated (Solana exact case; EVM any case). Read by
# the wallet map to drop edges that are really our own copying (spec 2026-09-30 wallet map).
# Public keys only: nothing here can sign. Empty = no exclusion.
MEMECOIN_OWN_ADDRESSES = os.getenv("MEMECOIN_OWN_ADDRESSES", "").strip()
```

- [ ] **Step 4: Write the builder** (`sniper/services/memecoin_wallet_map.py`)

```python
"""Wallet map payload (spec docs/superpowers/specs/2026-09-30-memecoin-wallet-map-design.md).

Read-only: link-graph DB rows plus a few ledger rows become {stamp, nodes, edges}. No chain, price
or funded-seam call. The map is centred on our roster; it is not a market map.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

SOLID_MIN_COINS = 5                 # spec rule 4
HUB_FUNDER_DEGREE = 10              # a funder of more wallets than this is an exchange-like hub
STALE_AFTER_S = 3 * 3600            # the graph job runs hourly
DIRECTED = {"follows": True, "dumps_on": True, "slot_follower": True,
            "co_buy_slot": False, "shared_funder": False}
_EDGE_SQL = ("SELECT edge_type, src, dst, src_user_id, dst_user_id, n_coins, null_p, lag_p50_s, "
             "first_seen, last_seen, coverage, evidence, method_version, computed_at "
             "FROM memecoin_wallet_edges WHERE network_id IN (%s) ORDER BY id")


def _parse(ts):
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def _addr(node_id):
    """Chain address of a node id: strips the pump: profile prefix; EVM lowercased."""
    s = str(node_id or "").strip()
    s = s[5:] if s.startswith("pump:") else s
    return s.lower() if s.lower().startswith("0x") else s


def own_address_set(raw):
    return {_addr(a) for a in str(raw or "").split(",") if a.strip()}


def open_graph_readonly(path):
    """None when the file is absent (graph_unavailable); a read-only connection otherwise."""
    p = Path(path)
    if not p.is_file():
        return None
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True)


def _hub(edge_type, evidence):
    if edge_type != "shared_funder":
        return None
    try:
        degree = json.loads(evidence or "{}").get("funder_degree")
    except (TypeError, ValueError):
        return None
    return None if degree is None else int(degree) > HUB_FUNDER_DEGREE


def build_wallet_map(graph, ledger, *, chain, network_ids, days, own_addresses, now_iso):
    now = _parse(now_iso)
    stamp = {"chain": chain, "window_days": days, "observed_wallets": 0, "method_version": None,
             "computed_at": None, "coverage": [], "stale": None, "error": None}
    out = {"stamp": stamp, "nodes": [], "edges": []}
    nets = tuple(network_ids)
    marks = ",".join("?" * len(nets))

    alias = {_addr(a): uid for a, uid in ledger.execute(
        "SELECT address, source_user_id FROM memecoin_address_registry "
        "WHERE network_id IN (%s) AND status='verified'" % marks, nets)}
    sources = {r[0]: r for r in ledger.execute(
        "SELECT source_user_id, handle, label, paused, excluded, real_approved, real_chains "
        "FROM memecoin_wallets")}
    since = (now - timedelta(days=int(days))).strftime("%Y-%m-%dT%H:%M:%S")
    activity = {}
    for uid in sources:             # indexed per source: (source_user_id, created_at)
        activity[uid] = ledger.execute(
            "SELECT COUNT(DISTINCT mint) FROM memecoin_events WHERE source_user_id=? "
            "AND created_at>=? AND side='buy' AND network_id IN (%s)" % marks,
            (uid, since) + nets).fetchone()[0]
    stamp["observed_wallets"] = sum(1 for v in activity.values() if v)

    if graph is None:
        stamp["error"] = "graph_unavailable"
        return out
    try:
        rows = graph.execute(_EDGE_SQL % marks, nets).fetchall()
        finished = graph.execute("SELECT MAX(finished_at) FROM memecoin_wallet_graph_runs").fetchone()[0]
    except sqlite3.Error:
        stamp["error"] = "graph_unreadable"
        return out
    stamp["stale"] = finished is None or (now - _parse(finished)).total_seconds() > STALE_AFTER_S

    def canon(node_id, user_id):
        return user_id or alias.get(_addr(node_id), node_id)

    ids, methods, computed, coverage = set(), set(), [], []
    for (etype, src, dst, su, du, n, p, lag, first, last, cov, ev, mv, cat) in rows:
        a, b = canon(src, su), canon(dst, du)
        if {_addr(src), _addr(dst), _addr(a), _addr(b)} & own_addresses:
            continue                                      # our own copying, spec rule 3
        out["edges"].append({
            "src": a, "dst": b, "type": etype, "directed": DIRECTED.get(etype, False),
            "strength": "solid" if (n or 0) >= SOLID_MIN_COINS else "weak",
            "n_coins": n, "null_p": p, "lag_p50_s": lag, "first_seen": first, "last_seen": last,
            "hub_funder": _hub(etype, ev)})
        ids.update((a, b))
        methods.add(mv)
        computed.append(cat)
        if cov and cov not in coverage:
            coverage.append(cov)
    stamp["method_version"] = ",".join(sorted(x for x in methods if x)) or None
    stamp["computed_at"] = max(computed) if computed else None
    stamp["coverage"] = [json.loads(c) for c in coverage]

    ids.update(uid for uid, v in activity.items() if v)
    for nid in sorted(ids):
        src_row = sources.get(nid)
        if src_row is None:
            out["nodes"].append({"id": nid, "kind": "stranger", "label": None, "handle": None,
                                 "armed": None, "paused": None, "excluded": None,
                                 "activity": None})
            continue
        chains = {c.strip() for c in str(src_row[6] or "").split(",") if c.strip()}
        out["nodes"].append({
            "id": nid, "kind": "source", "label": src_row[2], "handle": src_row[1],
            "armed": bool(src_row[5]) and bool(chains & set(nets)),
            "paused": bool(src_row[3]), "excluded": bool(src_row[4]),
            "activity": activity.get(nid, 0)})
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_wallet_map.py -q -p no:randomly`
Expected: 7 passed

- [ ] **Step 6: Prove the tests bite** — apply each mutant, run, confirm a failure, revert:
  - `SOLID_MIN_COINS = 5` → `3` (strength test fails)
  - drop the `own_addresses` check line (own-custody test fails)
  - `return None if degree is None` → `return False if degree is None` (hub test fails)

- [ ] **Step 7: Commit**

```bash
git add sniper/settings/memecoin.py sniper/services/memecoin_wallet_map.py tests/test_memecoin_wallet_map.py
git commit -m "feat(wallet-map): read-only payload builder over the link graph (slice 1)"
```

### Task 2: GET route

**Files:**
- Modify: `sniper/routes/api_memecoin.py` (add one route after `traders_arm_matrix`)
- Test: `tests/test_memecoin_wallet_map.py` (append)

**Interfaces:**
- Consumes: Task 1 `build_wallet_map`, `open_graph_readonly`, `own_address_set`, `MEMECOIN_OWN_ADDRESSES`.
- Produces: `GET /api/memecoin/wallet-map?chain=<key>&days=<1..30>` → builder payload; 400 `{"error": "chain_required"}` for an unknown chain; 400 `{"error": "<message>"}` for bad `days`.

- [ ] **Step 1: Write the failing tests** (append)

```python
def _client(tmp_path, monkeypatch, ledger, graph_rows=None):
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    import sniper.persistence.db_core as db_core
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    if graph_rows is not None:
        disk = sqlite3.connect(tmp_path / "data" / "memecoin_wallet_graph.db")
        _graph(graph_rows).backup(disk)
        disk.close()
    monkeypatch.setattr(db_core, "get_readonly_connection", lambda *a, **k: ledger)
    shared = sqlite3.connect(":memory:", check_same_thread=False)
    shared.row_factory = sqlite3.Row
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": shared}))
    return app.test_client()


def test_route_serves_the_payload(tmp_path, monkeypatch):
    cl = _client(tmp_path, monkeypatch, _ledger(),
                 [dict(type="follows", src="uA", dst="uB", su="uA", du="uB", n=6)])
    body = cl.get("/api/memecoin/wallet-map?chain=solana&days=14").get_json()
    assert body["stamp"]["error"] is None and body["edges"][0]["type"] == "follows"


def test_route_missing_graph_is_an_error_state_not_an_empty_map(tmp_path, monkeypatch):
    cl = _client(tmp_path, monkeypatch, _ledger(), None)
    body = cl.get("/api/memecoin/wallet-map?chain=solana").get_json()
    assert body["stamp"]["error"] == "graph_unavailable"


def test_route_refuses_unknown_chain_and_bad_days(tmp_path, monkeypatch):
    cl = _client(tmp_path, monkeypatch, _ledger(), [])
    assert cl.get("/api/memecoin/wallet-map?chain=nope").status_code == 400
    assert cl.get("/api/memecoin/wallet-map?chain=solana&days=99").status_code == 400


def test_wallet_map_imports_no_funded_seam():
    import ast
    from pathlib import Path
    tree = ast.parse(Path("sniper/services/memecoin_wallet_map.py").read_text())
    names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names} | {n.module for n in ast.walk(tree)
                                  if isinstance(n, ast.ImportFrom) and n.module}
    banned = ("real_seams", "jupiter", "quote", "price", "requests", "urllib", "http")
    assert not [x for x in names if any(b in x.lower() for b in banned)], names
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/test_memecoin_wallet_map.py -q -p no:randomly -k route`
Expected: FAIL, 404 on `/api/memecoin/wallet-map`

- [ ] **Step 3: Add the route** (inside `create_api_memecoin_blueprint`, after `traders_arm_matrix`)

```python
    @bp.get("/api/memecoin/wallet-map")
    def wallet_map():
        # Read-only wallet map (spec 2026-09-30). Its own read-only connections per request:
        # never the shared writable `conn`, never a funded seam, never a network call.
        from sniper.services.memecoin_chains import network_ids_for, resolve_chain
        from sniper.services.memecoin_wallet_map import (build_wallet_map, open_graph_readonly,
                                                         own_address_set)
        from sniper.persistence import db_core
        from sniper.util.state_paths import state_path
        import sniper.settings.memecoin as mc_settings
        chain = resolve_chain(request.args.get("chain"))
        if chain is None:
            return jsonify({"error": "chain_required",
                            "detail": "chains are never pooled; name one"}), 400
        try:
            days = _bounded_query_number(request.args, "days", cast=int, default=14,
                                         minimum=1, maximum=30)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        graph = ledger = None
        try:
            try:
                graph = open_graph_readonly(state_path("data", "memecoin_wallet_graph.db"))
            except sqlite3.Error:
                logger.exception("wallet map: graph DB open failed")
                graph = sqlite3.connect(":memory:")          # reads as graph_unreadable
            ledger = db_core.get_readonly_connection()
            payload = build_wallet_map(
                graph, ledger, chain=chain, network_ids=tuple(network_ids_for(chain)),
                days=days, own_addresses=own_address_set(mc_settings.MEMECOIN_OWN_ADDRESSES),
                now_iso=_now_iso())
        finally:
            if graph is not None:
                graph.close()
        return jsonify(payload)
```

Note: `ledger` is not closed when the test double returns a shared connection; `get_readonly_connection()` connections are per call in production and are closed by GC. If `get_readonly_connection` documents an explicit close contract, close it in the `finally` too (check `sniper/persistence/db_core.py:61`).

- [ ] **Step 4: Run to verify they pass**

Run: `python3 -m pytest tests/test_memecoin_wallet_map.py -q -p no:randomly`
Expected: 11 passed

- [ ] **Step 5: Measure** the live route once (read-only) and record p50/p90 of 20 calls in the commit message:

```bash
python3 - <<'EOF'
import time, statistics
from flask import Flask
from sniper.util.envfiles import load_env_files; load_env_files(".")
from sniper.persistence.db_core import get_readonly_connection
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
app = Flask(__name__); app.register_blueprint(create_api_memecoin_blueprint({"conn": get_readonly_connection()}))
cl = app.test_client(); ts = []
for _ in range(20):
    t = time.perf_counter(); r = cl.get("/api/memecoin/wallet-map?chain=solana"); ts.append((time.perf_counter()-t)*1000)
ts.sort(); print("status", r.status_code, "p50 %.0f ms p90 %.0f ms" % (statistics.median(ts), ts[17]))
EOF
```

If `ensure_memecoin_tables` refuses a read-only connection in the blueprint factory, pass a scratch in-memory connection instead; the route itself never touches `conn`.

- [ ] **Step 6: Commit**

```bash
git add sniper/routes/api_memecoin.py tests/test_memecoin_wallet_map.py
git commit -m "feat(wallet-map): GET /api/memecoin/wallet-map, read-only, one chain (slice 1)"
```

### Task 3: Tab, renderer and core helpers (slice 1 UI)

**Files:**
- Create: `static/wallet_map.js`
- Modify: `templates/index.html` (Solana section nav items, `tab-wallet-map` section, script tag)
- Modify: `static/app.js` (`VALID_TABS`, `TAB_VENUE`, titles, both load dispatches)
- Modify: `sniper/web/server.py:852-854` (`_asset_versions` adds `wallet_map`)
- Test: `tests/test_memecoin_wallet_map_ui.py`

**Interfaces:**
- Consumes: Task 2 payload.
- Produces: `WalletMapCore.edgeStyle(edge) -> {cls, dash, marker, label}`; `WalletMapCore.filter(payload, knobs) -> {nodes, edges}` (Task 4 fills in knobs; Task 3 ships it with defaults); `window.walletMap.load(signal)`.

- [ ] **Step 1: Write the failing test** (`tests/test_memecoin_wallet_map_ui.py`)

```python
"""Wallet map UI helpers run under node against real payload shapes."""
import json
import subprocess
from pathlib import Path


def _core(expr):
    js = ("const C = require('./static/wallet_map.js');"
          "process.stdout.write(JSON.stringify(%s));" % expr)
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def test_edge_style_per_type():
    s = _core("['follows','dumps_on','co_buy_slot','slot_follower','shared_funder','mystery']"
              ".map(t => C.edgeStyle({type: t, strength: 'solid', hub_funder: false}))")
    assert [x["marker"] for x in s] == [True, True, False, True, False, False]
    assert s[2]["label"].startswith("same slot")
    assert s[4]["dash"] == "2,3" and s[5]["cls"] == "wm-edge-unknown"
    weak = _core("C.edgeStyle({type: 'follows', strength: 'weak'})")
    assert weak["dash"] == "5,4"


def test_hub_and_unknown_hub_funders_are_hidden_by_default():
    payload = {"nodes": [{"id": "a"}, {"id": "b"}], "edges": [
        {"src": "a", "dst": "b", "type": "shared_funder", "strength": "weak", "hub_funder": True},
        {"src": "a", "dst": "b", "type": "shared_funder", "strength": "weak", "hub_funder": None},
        {"src": "a", "dst": "b", "type": "shared_funder", "strength": "weak", "hub_funder": False}]}
    out = _core("C.filter(%s, C.DEFAULT_KNOBS)" % json.dumps(payload))
    assert [e["hub_funder"] for e in out["edges"]] == [False]


def test_tab_is_wired():
    html = Path("templates/index.html").read_text()
    app = Path("static/app.js").read_text()
    assert 'data-tab="wallet-map"' in html and 'id="tab-wallet-map"' in html
    assert "/static/wallet_map.js" in html
    assert "'wallet-map': 'solana'" in app and "walletMap.load" in app
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_memecoin_wallet_map_ui.py -q -p no:randomly`
Expected: FAIL, node cannot find `./static/wallet_map.js`

- [ ] **Step 3: Write `static/wallet_map.js`**

```javascript
/* Wallet Map (spec docs/superpowers/specs/2026-09-30-memecoin-wallet-map-design.md).
   WalletMapCore = pure helpers (node-tested). window.walletMap = d3 renderer. Read-only. */
(function (root) {
  'use strict';
  const STYLE = {
    follows:       { cls: 'wm-edge-follows', marker: true,  label: 'follows (leader → follower)' },
    dumps_on:      { cls: 'wm-edge-dumps',   marker: true,  label: 'dumps on' },
    slot_follower: { cls: 'wm-edge-slotf',   marker: true,  label: 'buys after in the same slot' },
    co_buy_slot:   { cls: 'wm-edge-coslot',  marker: false, label: 'same slot (bot crowd, not identity)' },
    shared_funder: { cls: 'wm-edge-funder',  marker: false, label: 'shared funder' },
  };
  const DEFAULT_KNOBS = {
    types: { follows: true, dumps_on: true, slot_follower: true, co_buy_slot: true, shared_funder: true },
    minCoins: 0, armedOnly: false, hideStrangers: false, sizeBy: 'activity', focus: null, search: '',
  };

  function edgeStyle(e) {
    const base = STYLE[e.type] || { cls: 'wm-edge-unknown', marker: false, label: String(e.type) };
    let dash = e.strength === 'solid' ? null : '5,4';
    if (e.type === 'shared_funder') dash = '2,3';
    if (e.type === 'co_buy_slot') dash = '3,3';
    return { cls: base.cls, marker: base.marker, label: base.label, dash };
  }

  function filter(payload, knobs) {
    const k = Object.assign({}, DEFAULT_KNOBS, knobs || {});
    const byId = new Map((payload.nodes || []).map(n => [n.id, n]));
    const keepNode = n => n && !(k.hideStrangers && n.kind === 'stranger')
      && !(k.armedOnly && n.kind === 'source' && !n.armed);
    let edges = (payload.edges || []).filter(e =>
      (k.types[e.type] !== false)
      && (e.type !== 'shared_funder' || e.hub_funder === false)   // hub or unknown: hidden
      && ((e.n_coins || 0) >= k.minCoins)
      && keepNode(byId.get(e.src)) && keepNode(byId.get(e.dst)));
    if (k.focus) {
      const near = new Set([k.focus]);
      for (let hop = 0; hop < 2; hop++) {              // snapshot per hop: exactly 2 hops, not a chain
        const add = [];
        edges.forEach(e => { if (near.has(e.src)) add.push(e.dst); if (near.has(e.dst)) add.push(e.src); });
        add.forEach(x => near.add(x));
      }
      edges = edges.filter(e => near.has(e.src) && near.has(e.dst));
    }
    const ids = new Set();
    edges.forEach(e => { ids.add(e.src); ids.add(e.dst); });
    const nodes = (payload.nodes || []).filter(n => keepNode(n)
      && (ids.has(n.id) || (!k.focus && n.kind === 'source')));
    const q = (k.search || '').trim().toLowerCase();
    nodes.forEach(n => { n._hit = !!q && [n.id, n.handle, n.label].some(v => v && String(v).toLowerCase().includes(q)); });
    return { nodes, edges };
  }

  const WalletMapCore = { edgeStyle, filter, DEFAULT_KNOBS, STYLE };
  if (typeof module !== 'undefined' && module.exports) { module.exports = WalletMapCore; return; }
  root.WalletMapCore = WalletMapCore;

  // ---------------------------------------------------------------- renderer (browser only)
  const state = { payload: null, knobs: JSON.parse(JSON.stringify(DEFAULT_KNOBS)), chain: 'solana', days: 14 };
  const esc = s => String(s == null ? '—' : s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const short = id => (id && id.length > 14 ? id.slice(0, 6) + '…' + id.slice(-4) : id);

  function stampLine(st) {
    if (st.error) return `<span class="wm-error">Graph ${esc(st.error)} — no links shown; this is NOT "no links".</span>`;
    return `Observed set: ${st.observed_wallets} wallets · ${esc(st.chain)} · ${st.window_days} d · method ${esc(st.method_version)}`
      + ` · computed ${esc(st.computed_at)}${st.stale ? ' · <span class="wm-error">STALE (graph job late)</span>' : ''}`
      + ' · roster-centred, not a market map';
  }

  function tooltipNode(n) {
    return `<b>${esc(n.handle || n.label || short(n.id))}</b><br>${esc(n.kind)}`
      + (n.kind === 'source' ? `<br>armed here: ${n.armed ? 'yes' : 'no'}${n.paused ? ' (paused)' : ''}${n.excluded ? ' (excluded)' : ''}`
        + `<br>distinct coins bought (${state.days} d): ${n.activity}` : '<br>activity: unknown (not on our roster)')
      + `<br><code>${esc(n.id)}</code>`;
  }

  function tooltipEdge(e) {
    const st = edgeStyle(e);
    return `<b>${esc(st.label)}</b> · ${esc(e.strength)}<br>shared coins: ${e.n_coins} · null p: ${esc(e.null_p)}`
      + (e.lag_p50_s != null ? `<br>lag p50: ${Math.round(e.lag_p50_s)} s` : '')
      + `<br>seen ${esc(e.first_seen)} → ${esc(e.last_seen)}`;
  }

  function draw() {
    const host = document.getElementById('wallet-map-canvas');
    const tip = document.getElementById('wallet-map-tip');
    if (!host || !root.d3 || !state.payload) return;
    const d3 = root.d3;
    document.getElementById('wallet-map-stamp').innerHTML = stampLine(state.payload.stamp);
    const { nodes, edges } = filter(state.payload, state.knobs);
    const width = host.clientWidth || 900, height = Math.max(520, Math.round(width * 0.6));
    host.innerHTML = '';
    const svg = d3.select(host).append('svg').attr('width', width).attr('height', height).attr('class', 'wm-svg');
    svg.append('defs').append('marker').attr('id', 'wm-arrow').attr('viewBox', '0 -4 8 8')
      .attr('refX', 14).attr('markerWidth', 7).attr('markerHeight', 7).attr('orient', 'auto')
      .append('path').attr('d', 'M0,-4L8,0L0,4').attr('class', 'wm-arrowhead');
    const g = svg.append('g');
    svg.call(d3.zoom().scaleExtent([0.2, 6]).on('zoom', ev => g.attr('transform', ev.transform)));
    const nodeData = nodes.map(n => Object.assign({}, n));
    const linkData = edges.map(e => Object.assign({}, e, { source: e.src, target: e.dst }));
    const size = n => (n.activity == null ? 4 : 5 + Math.sqrt(n.activity) * 2.2);
    const sim = d3.forceSimulation(nodeData)
      .force('link', d3.forceLink(linkData).id(d => d.id).distance(70))
      .force('charge', d3.forceManyBody().strength(-90))
      .force('center', d3.forceCenter(width / 2, height / 2))
      .force('collide', d3.forceCollide(d => size(d) + 2));
    const show = (html, ev) => { tip.innerHTML = html; tip.style.display = 'block'; tip.style.left = (ev.offsetX + 14) + 'px'; tip.style.top = (ev.offsetY + 10) + 'px'; };
    const hide = () => { tip.style.display = 'none'; };
    const link = g.append('g').selectAll('line').data(linkData).join('line')
      .attr('class', d => 'wm-edge ' + edgeStyle(d).cls + (d.strength === 'weak' ? ' wm-weak' : ''))
      .attr('stroke-dasharray', d => edgeStyle(d).dash)
      .attr('marker-end', d => (d.directed && edgeStyle(d).marker ? 'url(#wm-arrow)' : null))
      .on('mousemove', (ev, d) => show(tooltipEdge(d), ev)).on('mouseleave', hide);
    const node = g.append('g').selectAll('circle').data(nodeData).join('circle')
      .attr('r', size)
      .attr('class', d => 'wm-node ' + (d.kind === 'stranger' ? 'wm-stranger' : d.armed ? 'wm-armed' : 'wm-source') + (d._hit ? ' wm-hit' : ''))
      .on('mousemove', (ev, d) => show(tooltipNode(d), ev)).on('mouseleave', hide)
      .on('click', (ev, d) => { state.knobs.focus = state.knobs.focus === d.id ? null : d.id; draw(); })
      .call(d3.drag()
        .on('start', (ev, d) => { if (!ev.active) sim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
        .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y; })
        .on('end', (ev, d) => { if (!ev.active) sim.alphaTarget(0); d.fx = null; d.fy = null; }));
    const label = g.append('g').selectAll('text').data(nodeData.filter(n => n.kind === 'source')).join('text')
      .attr('class', 'wm-label').text(d => d.handle || d.label || short(d.id));
    sim.on('tick', () => {
      link.attr('x1', d => d.source.x).attr('y1', d => d.source.y).attr('x2', d => d.target.x).attr('y2', d => d.target.y);
      node.attr('cx', d => d.x).attr('cy', d => d.y);
      label.attr('x', d => d.x + size(d) + 3).attr('y', d => d.y + 3);
    });
  }

  async function load(signal) {
    const stampEl = document.getElementById('wallet-map-stamp');
    try {
      const r = await fetch(`/api/memecoin/wallet-map?chain=${encodeURIComponent(state.chain)}&days=${state.days}`, { signal });
      const body = await r.json();
      if (!r.ok) throw new Error(body.error || ('HTTP ' + r.status));
      state.payload = body;
      draw();
    } catch (err) {
      if (err && err.name === 'AbortError') return;
      if (stampEl) stampEl.innerHTML = `<span class="wm-error">Wallet map unavailable: ${esc(err.message)}</span>`;
    }
  }

  root.walletMap = { load, draw, state };
})(typeof window !== 'undefined' ? window : globalThis);
```

- [ ] **Step 4: Wire the tab**
  - `templates/index.html`, in `#section-nav` after the Wallet Research item:
    ```html
    <a href="#" class="nav-item" data-tab="memecoin" data-venue="solana"><span class="nav-label">Copy</span></a>
    <a href="#" class="nav-item" data-tab="wallet-map" data-venue="solana"><span class="nav-label">Wallet Map</span></a>
    ```
  - after the closing tag of `<section id="tab-memecoin" ...>` (index.html:678 block):
    ```html
    <section id="tab-wallet-map" class="tab-panel">
        <div class="wm-controls" id="wallet-map-controls"></div>
        <div class="wm-stamp text-muted" id="wallet-map-stamp">Loading wallet map…</div>
        <div class="wm-wrap" style="position:relative">
            <div id="wallet-map-canvas"></div>
            <div id="wallet-map-tip" class="wm-tip" style="display:none;position:absolute"></div>
        </div>
    </section>
    ```
  - script tag after `mc-range.js`: `<script defer src="/static/wallet_map.js?v={{ asset_v.wallet_map }}"></script>`
  - `static/app.js`: add `'wallet-map'` to `VALID_TABS`; `'wallet-map': 'solana'` to `TAB_VENUE`; `'wallet-map': 'Wallet Map'` to the titles map; in BOTH load dispatches add `else if (tabId === 'wallet-map') window.walletMap && window.walletMap.load(sig);` (use `window.walletMap.load()` in the refresh dispatch that has no `sig`).
  - `sniper/web/server.py` `_asset_versions`: add `("wallet_map", "wallet_map.js")` to the tuple.
  - `static/styles.css`: append the `wm-*` rules (edges: follows `#4aa3ff`, dumps `#e5484d`, slotf `#8b8b8b`, coslot `#b0b0b0` 0.6 opacity, funder `#c9a227`, unknown `#777`; `.wm-weak{opacity:.45}`; nodes: armed `#2fbf71`, source `#6c8cff`, stranger `#9a9a9a`; `.wm-hit{stroke:#fff;stroke-width:3}`; `.wm-tip` dark card, 12px; `.wm-error{color:#e5484d}`), using existing CSS variables where they exist.

- [ ] **Step 5: Run the UI tests**

Run: `python3 -m pytest tests/test_memecoin_wallet_map_ui.py -q -p no:randomly`
Expected: 3 passed

- [ ] **Step 6: Mutants** — (a) make `filter` keep `hub_funder === null` (hub test fails); (b) give `co_buy_slot` `marker: true` (edge-style test fails). Revert both.

- [ ] **Step 7: Commit**

```bash
git add static/wallet_map.js static/app.js static/styles.css templates/index.html sniper/web/server.py tests/test_memecoin_wallet_map_ui.py
git commit -m "feat(wallet-map): Wallet Map tab under Memecoin, d3 force graph (slice 1)"
```

### Task 4: Knobs (slice 2)

**Files:**
- Modify: `static/wallet_map.js` (controls renderer; `filter` already implements the knobs)
- Test: `tests/test_memecoin_wallet_map_ui.py` (append)

**Interfaces:**
- Consumes: `WalletMapCore.filter`, `window.walletMap.state`.
- Produces: `renderControls()` inside the renderer; knob state in `state.knobs` / `state.chain` / `state.days`.

- [ ] **Step 1: Write the failing tests** (append)

```python
PAYLOAD = {"nodes": [
    {"id": "a", "kind": "source", "armed": True, "handle": "alice", "activity": 5},
    {"id": "b", "kind": "source", "armed": False, "handle": "bob", "activity": 1},
    {"id": "s", "kind": "stranger", "activity": None},
    {"id": "c", "kind": "source", "armed": True, "handle": "carol", "activity": 2}],
    "edges": [
    {"src": "a", "dst": "b", "type": "follows", "strength": "solid", "n_coins": 6},
    {"src": "a", "dst": "s", "type": "co_buy_slot", "strength": "weak", "n_coins": 3},
    {"src": "b", "dst": "c", "type": "follows", "strength": "weak", "n_coins": 3}]}


def _f(knobs):
    return _core("C.filter(%s, %s)" % (json.dumps(PAYLOAD), json.dumps(knobs)))


def test_knobs_hide_but_never_add():
    base = _f({})
    assert len(base["edges"]) == 3
    assert len(_f({"minCoins": 5})["edges"]) == 1
    assert len(_f({"hideStrangers": True})["edges"]) == 2
    # b is unarmed, so a-b and b-c go; strangers are not sources, so a-s stays
    assert [e["dst"] for e in _f({"armedOnly": True})["edges"]] == ["s"]
    no_follows = _f({"types": {"follows": False, "co_buy_slot": True}})
    assert [e["type"] for e in no_follows["edges"]] == ["co_buy_slot"]


def test_focus_keeps_two_hops_and_search_marks_hits():
    f = _f({"focus": "s"})
    assert {n["id"] for n in f["nodes"]} == {"s", "a", "b"}            # s-a (1 hop), a-b (2 hops)
    hits = [n["id"] for n in _f({"search": "CAR"})["nodes"] if n["_hit"]]
    assert hits == ["c"]
```

- [ ] **Step 2: Run to verify** — the two tests should already PASS against Task 3's `filter` (it ships the knob logic). If either fails, fix `filter`, not the test. Then add the controls UI (Step 3); the controls have no unit test beyond `test_tab_is_wired`, and are verified in the browser (Task 5).

- [ ] **Step 3: Add `renderControls()`** in the renderer, and make `renderControls();` the first line of `load()` (it is idempotent via `dataset.ready`):

```javascript
  function renderControls() {
    const el = document.getElementById('wallet-map-controls');
    if (!el || el.dataset.ready) return;
    el.dataset.ready = '1';
    const types = Object.keys(STYLE).map(t =>
      `<label class="wm-knob"><input type="checkbox" data-type="${t}" checked> ${esc(STYLE[t].label)}</label>`).join('');
    el.innerHTML = `
      <label class="wm-knob">Chain <select id="wm-chain"><option value="solana">Solana</option></select></label>
      <label class="wm-knob">Window <select id="wm-days"><option>7</option><option selected>14</option><option>30</option></select> d</label>
      <label class="wm-knob">Min shared coins <input id="wm-min" type="range" min="0" max="10" value="0"> <span id="wm-min-v">0</span></label>
      <label class="wm-knob"><input id="wm-armed" type="checkbox"> armed only</label>
      <label class="wm-knob"><input id="wm-strangers" type="checkbox"> hide strangers</label>
      <input id="wm-search" class="wm-knob" placeholder="handle or address">
      <button id="wm-refresh" class="btn btn-sm">Refresh</button>
      <div class="wm-types">${types}</div>`;
    const redraw = () => draw();
    el.querySelectorAll('input[data-type]').forEach(cb => cb.addEventListener('change', () => { state.knobs.types[cb.dataset.type] = cb.checked; redraw(); }));
    el.querySelector('#wm-min').addEventListener('input', ev => { state.knobs.minCoins = +ev.target.value; el.querySelector('#wm-min-v').textContent = ev.target.value; redraw(); });
    el.querySelector('#wm-armed').addEventListener('change', ev => { state.knobs.armedOnly = ev.target.checked; redraw(); });
    el.querySelector('#wm-strangers').addEventListener('change', ev => { state.knobs.hideStrangers = ev.target.checked; redraw(); });
    el.querySelector('#wm-search').addEventListener('input', ev => { state.knobs.search = ev.target.value; redraw(); });
    el.querySelector('#wm-days').addEventListener('change', ev => { state.days = +ev.target.value; load(); });
    el.querySelector('#wm-chain').addEventListener('change', ev => { state.chain = ev.target.value; load(); });
    el.querySelector('#wm-refresh').addEventListener('click', () => load());
  }
```

Chain select lists Solana only until slice 5 adds EVM edges. Size-by has one option (activity) until slice 3; it is not rendered yet.

- [ ] **Step 4: Run all wallet-map tests**

Run: `python3 -m pytest tests/test_memecoin_wallet_map.py tests/test_memecoin_wallet_map_ui.py -q -p no:randomly`
Expected: 16 passed

- [ ] **Step 5: Commit**

```bash
git add static/wallet_map.js tests/test_memecoin_wallet_map_ui.py
git commit -m "feat(wallet-map): knobs - edge types, min coins, armed only, strangers, focus, search (slice 2)"
```

### Task 5: Covering set, browser check, hand-off

- [ ] **Step 1: Covering set** (grep-built, watchdog/deploy tests excluded):

```bash
F=$( (grep -rlE "api_memecoin|memecoin_wallet_map|_asset_versions|index\.html|static/app\.js|settings\.memecoin|settings import memecoin" tests --include=*.py; echo tests/test_memecoin_runner_health.py) | sort -u | xargs grep -LE "memecoin_watchdog|deploy_memecoin_runtime|deploy_web_runtime|\.sh[\"']")
python3 -m pytest $F -q -n 6 -p no:randomly
```
Expected: all pass. Paste the pass count into the hand-off message.

- [ ] **Step 2: Browser check** on a local dev server of the branch (not the live web): open the Memecoin venue → Wallet Map; confirm the stamp line, bubbles, arrows on follows, dashed same-slot lines, hover cards, each knob, focus on click; read the console for errors. Screenshot it for the hand-off.

- [ ] **Step 3: Hand-off** to Alpha: branch tip SHA, covering-set count, route p50/p90 from Task 2 Step 5, screenshot path. Gamma reviews; Alpha merges and deploys the web only (`scripts/deploy_web_runtime.sh <sha>`). The runner is untouched by this change.

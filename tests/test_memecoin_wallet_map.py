"""Wallet Map herders table (spec docs/superpowers/specs/2026-09-30-memecoin-wallet-map-overhaul-design.md,
Gamma CLEAN d16c454df; pre-registration docs/audits/2026-09-30-wallet-map-reads-preregistration.md).

- One read per (herder, coin): the unit read (unit_reads, Gamma CLEAN 4fb73b112).
- A regular: k >= 3 of n read coins and k/n >= 10%; none named under n = 10 (None, never 0).
- A probe: a regular of 2+ herders; never a roster wallet or ours. Shared counts are shared regulars.
- Coverage per herder: read first buys over first buys, uuid-only apart, "undercounted" under 80%.
- Read-only: missing / unreadable graph and no finished run are three named states.
"""
import json
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_wallet_map as wm

NOW = "2026-09-30T10:00:00+00:00"
T0 = 1790700000.0                                         # 2026-09-29T16:40:00Z


def _iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _graph(reads, *, edges=(), finished_at="2026-09-30T09:35:00+00:00", window_from="2026-09-16T09:35:00+00:00",
           impact=None, intake_col=True):
    g = sqlite3.connect(":memory:")
    g.execute("CREATE TABLE memecoin_wallet_graph_runs (id INTEGER PRIMARY KEY, method_version TEXT, started_at TEXT, "
              "finished_at TEXT, window_from TEXT, edges INTEGER, helius_calls INTEGER, note TEXT)")
    g.execute("INSERT INTO memecoin_wallet_graph_runs (method_version, finished_at, window_from) VALUES ('v', ?, ?)",
              (finished_at, window_from))
    g.execute("CREATE TABLE memecoin_wallet_edges (id INTEGER PRIMARY KEY, network_id TEXT, edge_type TEXT, src TEXT, "
              "dst TEXT, src_user_id TEXT, dst_user_id TEXT, n_coins INTEGER, n_consistent INTEGER, lag_p50_s REAL, "
              "lag_p90_s REAL, null_p99 REAL, null_p REAL, flag TEXT, first_seen TEXT, last_seen TEXT, coverage TEXT, "
              "evidence TEXT, method_version TEXT, computed_at TEXT)")
    for etype, su, du in edges:
        g.execute("INSERT INTO memecoin_wallet_edges (network_id, edge_type, src, dst, src_user_id, dst_user_id, "
                  "n_coins, lag_p50_s) VALUES ('solana',?,?,?,?,?,3,40.0)", (etype, su, du, su, du))
    g.execute("CREATE TABLE memecoin_wallet_slot_reads (signature TEXT PRIMARY KEY, source_user_id TEXT, network_id TEXT, "
              "mint TEXT, detected_at TEXT, status TEXT NOT NULL, slot INTEGER, idx INTEGER, companions TEXT, "
              "undecoded INTEGER, truncated INTEGER, read_at TEXT NOT NULL%s)" % (", intake TEXT" if intake_col else ""))
    for r in reads:
        cols = "signature, source_user_id, network_id, mint, detected_at, status, idx, companions, truncated, read_at"
        vals = [r["sig"], r["h"], "solana", r["mint"], _iso(r["t"]), r.get("status", "ok"), r.get("idx", 10),
                json.dumps([{"trader": t, "index": i} for t, i in r.get("comp", ())]), int(r.get("trunc", False)), "x"]
        if intake_col:
            cols, vals = cols + ", intake", vals + [r.get("intake", "detection")]
        g.execute("INSERT INTO memecoin_wallet_slot_reads (%s) VALUES (%s)" % (cols, ",".join("?" * len(vals))), vals)
    if impact is not None:
        g.execute("CREATE TABLE memecoin_wallet_impact (source_user_id TEXT, network_id TEXT, n INTEGER, "
                  "median_delta_sol REAL, verdict TEXT, k INTEGER, k_needed INTEGER, computed_at TEXT)")
        for uid, n, med, verdict, at in impact:
            g.execute("INSERT INTO memecoin_wallet_impact VALUES (?, 'solana', ?, ?, ?, NULL, NULL, ?)",
                      (uid, n, med, verdict, at))
    return g


def _ledger():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row             # as get_readonly_connection (the route's ledger) sets it
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_wallets (source_user_id, handle, real_approved, real_chains, paused, excluded) VALUES "
              "('A', 'alpha_h', 1, '4663,solana', 0, 0), ('B', 'bravo_h', 0, NULL, 0, 0), ('D', 'delta_h', 1, '4663', 0, 0), "
              "('C', 'charlie_h', 1, 'solana', 0, 1), ('R', 'roster_bot', 0, NULL, 0, 0)")
    c.execute("INSERT INTO memecoin_address_registry (source_user_id, network_id, address, status, evidence_kind, "
              "first_seen_at, status_at) VALUES ('R','solana','RosterAddr','verified','j7_trader_receipt','t','t')")
    c.execute("INSERT INTO memecoin_wallet_approvals (source_user_id, action, actor, reason, copy_pnl, copy_closes, "
              "created_at, chain) VALUES ('B','chain_arm','ui','solana: arm', 0.0, 0, '2026-09-20T00:00:00Z', 'solana'), "
              "('B','chain_revoke','auto_demote','solana: cohort realized $-137.12', -137.12, 1, "
              "'2026-09-22T17:28:41Z', 'solana')")                        # append-only: id order is time order
    return c


def _herder(h, n, *, members=(), start=0):
    """n read coins for herder h; `members` = [(bot, on_how_many_coins)] behind it (index 20 > 10)."""
    reads, first = [], {}
    for i in range(n):
        mint = "%s-m%d" % (h, i)
        comp = [(bot, 20) for bot, k in members if i < k]
        reads.append({"sig": "%s-s%d" % (h, i), "h": h, "mint": mint, "t": T0 + start + i, "comp": comp})
        first[(h, mint)] = ("%s-s%d" % (h, i), T0 + start + i)
    return reads, first


def _build(reads, first, *, ledger=None, own=(), **graph_kw):
    return wm.build_herders(_graph(reads, **graph_kw), ledger or _ledger(), now_iso=NOW, own_addresses=set(own),
                            first_sigs=first)


def _row(out, h):
    return next(r for r in out["herders"] if r["id"] == h)


# ---------------------------------------------------------------- the rules
def test_one_unit_read_per_pair_names_the_regulars():
    reads, first = _herder("A", 10, members=[("X", 4), ("Y", 1)])
    reads[0]["comp"].append(("Z", 5))                                   # ahead of A in its slot: not a member
    reads.append({"sig": "A-later", "h": "A", "mint": "A-m1", "t": T0 + 500, "comp": [("LATE", 20)]})
    out = _build(reads, first)
    a = _row(out, "A")
    assert (a["n"], a["regulars"], [r["id"] for r in a["regular_list"]]) == (10, 1, ["X"])
    assert a["members"] == 2 and a["drew"] == 4                         # X and Y; LATE is on a later read only


def test_under_ten_read_coins_no_regular_is_named_and_a_measured_zero_is_zero():
    small, f1 = _herder("A", 5, members=[("X", 5)])
    big, f2 = _herder("B", 12, members=[("W", 1)], start=100)
    out = _build(small + big, {**f1, **f2})
    assert _row(out, "A")["regulars"] is None and _row(out, "A")["few_reads"]
    assert _row(out, "B")["regulars"] == 0


def test_a_probe_is_a_regular_of_two_herders_and_never_a_roster_wallet():
    a, fa = _herder("A", 10, members=[("X", 4), ("RosterAddr", 4), ("W", 3)])
    b, fb = _herder("B", 10, members=[("X", 3), ("RosterAddr", 3), ("W", 3)], start=100)
    c, fc = _herder("C", 5, members=[("X", 3)], start=200)
    out = _build(a + b + c, {**fa, **fb, **fc})
    assert [(p["id"], [h["id"] for h in p["herds"]]) for p in out["probes"]] == [("W", ["A", "B"]), ("X", ["A", "B"])]
    assert "R" in [r["id"] for r in _row(out, "A")["regular_list"]]     # the roster wallet, named by its id
    assert out["shared"] == [{"a": "A", "a_handle": "alpha_h", "b": "B", "b_handle": "bravo_h", "count": 3}]
    assert _row(out, "A")["shares"] == [{"id": "B", "handle": "bravo_h", "count": 3}]


def test_our_own_address_is_never_a_member():
    reads, first = _herder("A", 10, members=[("OurWallet", 5), ("X", 3)])
    out = _build(reads, first, own={"OurWallet"})
    assert [r["id"] for r in _row(out, "A")["regular_list"]] == ["X"]


def test_coverage_is_read_first_buys_over_first_buys_with_uuid_only_apart():
    reads, first = _herder("A", 10, members=[("X", 3)])
    for i in range(10, 16):
        first[("A", "unread-%d" % i)] = ("never-%d" % i, T0 + i)       # not read yet
    for i in range(4):
        first[("A", "uuid-%d" % i)] = (None, T0 + i)                    # can never be read
    reads.append({"sig": "A-trunc", "h": "A", "mint": "A-t", "t": T0, "trunc": True, "comp": [("X", 20)]})
    first[("A", "A-t")] = ("A-trunc", T0)
    reads.append({"sig": "A-rebuy", "h": "A", "mint": "A-nf", "t": T0 + 900, "comp": []})
    first[("A", "A-nf")] = ("A-first", T0)                           # only a later buy read: still pending
    out = _build(reads, first)
    a = _row(out, "A")
    assert (a["units"], a["read"], a["uuid_only"], a["n"]) == (22, 10, 4, 10)
    assert a["out_of_n"] == {"truncated": 1, "not_first": 1} and a["left_out"] == 1   # truncated is final
    assert a["undercounted"] and abs(a["coverage"] - 10 / 22) < 1e-12
    st = out["stamp"]
    assert (st["units"], st["read"], st["uuid_only"], st["left_out"], st["not_read_yet"]) == (22, 10, 4, 1, 7)
    assert st["window_from"] == _iso(T0) and st["window_to"] == _iso(T0 + 9)   # the unit reads' own span


def test_do_we_follow_it_comes_from_the_roster_and_the_approvals_ledger():
    a, fa = _herder("A", 10)
    b, fb = _herder("B", 10, start=100)
    c, fc = _herder("C", 10, start=200)
    s, fs = _herder("Stranger", 10, start=300)
    out = _build(a + b + c + s, {**fa, **fb, **fc, **fs})
    assert _row(out, "A")["follow"]["state"] == "Real money on Solana"
    b_row = _row(out, "B")["follow"]
    assert b_row["state"] == "Paper only" and b_row["last"]["action"] == "chain_revoke"
    assert b_row["last"]["copy_pnl"] == -137.12 and b_row["last"]["actor"] == "auto_demote"
    assert _row(out, "C")["follow"]["state"] == "Excluded"
    d, fd = _herder("D", 10, start=400)
    assert _row(_build(d, fd), "D")["follow"]["state"] == "Paper only"   # armed on 4663 is not armed here
    assert _row(out, "Stranger")["follow"] == {"state": "Not watched", "last": None}


def test_a_zero_copy_pnl_over_zero_closes_is_unknown():
    c = _ledger()
    c.execute("INSERT INTO memecoin_wallet_approvals (source_user_id, action, actor, reason, copy_pnl, copy_closes, "
              "created_at, chain) VALUES ('B','chain_revoke','ui','solana: revoke', 0.0, 0, '2026-09-29T00:00:00Z', 'solana')")
    b, fb = _herder("B", 10)
    assert _row(_build(b, fb, ledger=c), "B")["follow"]["last"]["copy_pnl"] is None


def test_links_are_drawn_between_herders_only():
    a, fa = _herder("A", 10)
    b, fb = _herder("B", 10, start=100)
    out = _build(a + b, {**fa, **fb}, edges=[("follows", "A", "B"), ("dumps_on", "A", "Outsider")])
    assert [(x["type"], x["other"], x["role"]) for x in _row(out, "A")["links"]] == [("follows", "B", "src")]
    assert [(x["type"], x["other"], x["role"]) for x in _row(out, "B")["links"]] == [("follows", "A", "dst")]


def test_flow_is_not_measured_until_slice_3_writes_rows():
    a, fa = _herder("A", 10)
    out = _build(a, fa)
    assert _row(out, "A")["flow"] is None and out["stamp"]["flow"] == "not_run"
    out = _build(a, fa, impact=[("A", 21, 0.8, "mover", "2026-09-30T09:00:00Z"),
                                ("A", 20, 0.1, "not_distinguished", "2026-09-30T08:00:00Z")])
    assert _row(out, "A")["flow"]["verdict"] == "mover" and out["stamp"]["flow"] == "ok"


def test_herders_sort_by_regulars_then_draw_share_and_too_few_last():
    a, fa = _herder("A", 10, members=[("X", 3)])
    b, fb = _herder("B", 10, members=[("X", 3), ("W", 3)], start=100)
    c, fc = _herder("C", 5, members=[("X", 5)], start=200)
    d, fd = _herder("D", 10, members=[("X", 3)], start=300)
    for r in d[3:8]:
        r["comp"] = [("ONCE-%s" % r["mint"], 20)]                       # D drew a follower on 8 of 10, A on 3
    out = _build(a + b + c + d, {**fa, **fb, **fc, **fd})
    assert [r["id"] for r in out["herders"]] == ["B", "D", "A", "C"]


def test_a_pair_that_shares_one_regular_is_not_listed_as_sharing():
    a, fa = _herder("A", 10, members=[("X", 3)])
    b, fb = _herder("B", 10, members=[("X", 3)], start=100)
    out = _build(a + b, {**fa, **fb})
    assert out["shared"] == [] and _row(out, "A")["shares"] == [{"id": "B", "handle": "bravo_h", "count": 1}]


def test_missing_unreadable_and_unfinished_graphs_are_named_states():
    missing = wm.build_herders(None, _ledger(), now_iso=NOW, own_addresses=set(), first_sigs={})
    assert missing["stamp"]["error"] == "graph_unavailable" and missing["herders"] == []
    broken = wm.build_herders(sqlite3.connect(":memory:"), _ledger(), now_iso=NOW, own_addresses=set(), first_sigs={})
    assert broken["stamp"]["error"] == "graph_unreadable"
    g = _graph([], finished_at=None)
    assert wm.build_herders(g, _ledger(), now_iso=NOW, own_addresses=set())["stamp"]["error"] == "no_finished_run"


def test_a_stale_graph_is_flagged_and_an_older_reads_table_still_reads():
    a, fa = _herder("A", 10, members=[("X", 3)])
    out = _build(a, fa, finished_at="2026-09-30T05:00:00+00:00", intake_col=False)
    assert out["stamp"]["stale"] and _row(out, "A")["regulars"] == 1


def test_first_buys_come_from_the_jobs_own_coverage_units_over_its_window():
    led = _ledger()
    sig = "5" * 88
    led.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, created_at, received_at, "
                "network_id, their_size_usd) VALUES (?, 'A', 'M1', 'buy', ?, ?, 'solana', 100.0)",
                ("swap:raybot:%s:owner:M1" % sig, _iso(T0), _iso(T0)))
    g = _graph([{"sig": sig, "h": "A", "mint": "M1", "t": T0, "comp": [("X", 20)]}])
    out = wm.build_herders(g, led, now_iso=NOW, own_addresses=set())
    assert (_row(out, "A")["units"], _row(out, "A")["read"]) == (1, 1)


# ---------------------------------------------------------------- the route
def _client(tmp_path, monkeypatch, ledger, graph=None):
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    import sniper.persistence.db_core as db_core
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    if graph is not None:
        disk = sqlite3.connect(tmp_path / "data" / "memecoin_wallet_graph.db")
        graph.commit()
        graph.backup(disk)
        disk.close()
    monkeypatch.setattr(db_core, "get_readonly_connection", lambda *a, **k: ledger)
    shared = sqlite3.connect(":memory:", check_same_thread=False)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": shared}))
    return app.test_client()


def test_the_route_serves_the_herders_and_the_force_view_route_is_gone(tmp_path, monkeypatch):
    reads, _first = _herder("A", 2)
    cl = _client(tmp_path, monkeypatch, _ledger(), _graph(reads))
    body = cl.get("/api/memecoin/wallet-map/herders?chain=solana").get_json()
    assert body["stamp"]["error"] is None and set(body) == {"stamp", "herders", "shared", "probes"}
    assert cl.get("/api/memecoin/wallet-map?chain=solana").status_code == 404
    other = cl.get("/api/memecoin/wallet-map/herders?chain=4663").get_json()
    assert other["herders"] == [] and other["stamp"]["note"] == "no link data for this chain yet"


def test_the_route_says_when_the_graph_is_missing(tmp_path, monkeypatch):
    cl = _client(tmp_path, monkeypatch, _ledger(), None)
    assert cl.get("/api/memecoin/wallet-map/herders").get_json()["stamp"]["error"] == "graph_unavailable"


def test_the_service_imports_no_funded_seam():
    import ast
    from pathlib import Path
    tree = ast.parse(Path("sniper/services/memecoin_wallet_map.py").read_text())
    names = {a.name for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))
             for a in n.names} | {n.module for n in ast.walk(tree)
                                  if isinstance(n, ast.ImportFrom) and n.module}
    banned = ("real_seams", "jupiter", "quote", "price", "requests", "urllib", "http")
    assert not [x for x in names if any(b in x.lower() for b in banned)], names


def test_serving_the_page_loads_no_funded_or_network_module():
    """Gamma LOW 2: first_sigs_for imports the graph job at request time; pin the boundary, not the file."""
    import subprocess
    import sys
    code = ("import sys, sqlite3\n"
            "from sniper.persistence import db_memecoin as m\n"
            "from sniper.services import memecoin_wallet_map as wm\n"
            "c = sqlite3.connect(':memory:'); m.ensure_memecoin_tables(c)\n"
            "before = set(sys.modules)\n"
            "wm.first_sigs_for(c, '2026-09-16T00:00:00+00:00')\n"
            "new = sorted(set(sys.modules) - before)\n"
            "bad = [x for x in new if any(b in x.lower() for b in "
            "('real_seams', 'jupiter', 'quote', 'price', 'requests', 'urllib', 'http', 'funded_exit_truth'))]\n"
            "print(bad)\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]", out.stdout


def test_own_address_set_normalizes_evm_case_only():
    assert wm.own_address_set("0xAbC,Sol1, pump:Sol2") == {"0xabc", "Sol1", "Sol2"}


# ---------------------------------------------------------------- one herder (slice 3)
def _view(reads, first, wallet, *, ledger=None, **graph_kw):
    return wm.build_wallet_view(_graph(reads, **graph_kw), ledger or _ledger(), wallet=wallet, now_iso=NOW,
                                own_addresses=set(), first_sigs=first)


def test_the_herder_view_lists_its_regulars_with_their_other_herds_and_its_coins():
    a, fa = _herder("A", 10, members=[("X", 4), ("W", 3), ("ONCE", 1)])
    a[0]["comp"].append(("AHEAD", 5))                                   # ahead of A in its slot: not a follower
    b, fb = _herder("B", 10, members=[("X", 3)], start=100)
    v = _view(a + b, {**fa, **fb}, "A")
    assert v["herd"]["regulars"] == [{"id": "X", "handle": None, "k": 4, "n": 10, "other_herds": 1},
                                     {"id": "W", "handle": None, "k": 3, "n": 10, "other_herds": 0}]
    assert v["herd"]["others"] == 1 and v["before"] == {"state": "not_measured_yet"}
    assert [c["mint"] for c in v["coins"]][:2] == ["A-m9", "A-m8"]      # newest first
    first_coin = next(c for c in v["coins"] if c["mint"] == "A-m0")
    assert {k: first_coin[k] for k in ("mint", "at", "followers", "regulars")} == {
        "mint": "A-m0", "at": _iso(T0), "followers": 3, "regulars": ["W", "X"]}   # D2 adds source/after
    assert v["wallet"]["id"] == "A" and v["wallet"]["regulars"] == 2


def test_the_herder_view_shows_who_sells_into_it_and_whom_it_sells_into():
    a, fa = _herder("A", 10)
    v = _view(a, fa, "A", edges=[("dumps_on", "Seller", "A"), ("dumps_on", "A", "Buyer"), ("follows", "A", "F")])
    assert [x["id"] for x in v["sells_into_it"]] == ["Seller"] and [x["id"] for x in v["it_sells_into"]] == ["Buyer"]


def test_the_pattern_table_uses_each_coins_first_relay_buy_and_the_herders_own_age_clock():
    led = _ledger()
    rows = [("e1", "M1", "2026-09-29T01:00:00Z", 50000.0, 200.0, "j7_wallets"),
            ("e2", "M1", "2026-09-29T02:00:00Z", 90000.0, 999.0, "j7_wallets"),     # a later buy of M1: not a first buy
            ("e3", "M2", "2026-09-29T13:00:00Z", 70000.0, None, "j7_pump"),         # an unknown size is left out
            ("e4", "M3", "2026-09-10T13:00:00Z", 1.0, 1.0, "j7_wallets"),           # before the window
            ("e5", "M4", "2026-09-29T20:00:00Z", 0.0, 0.0, "j7_wallets")]           # 0 is the relay's "unknown"
    for fid, mint, at, mcap, usd, lane in rows:
        led.execute("INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, created_at, network_id, "
                    "their_market_cap, their_size_usd, delivery_lane) VALUES (?, 'A', ?, 'buy', ?, 'solana', ?, ?, ?)",
                    (fid, mint, at, mcap, usd, lane))
    a, fa = _herder("A", 10)
    v = _view(a, fa, "A", ledger=led)
    pt = v["patterns"]
    assert pt["first_buys"] == 3 and pt["mcap_usd"]["n"] == 2 and pt["mcap_usd"]["median"] == 60000.0
    assert pt["size_usd"] == {"n": 1, "median": 200.0, "p25": 200.0, "p75": 200.0}
    assert pt["hours_utc"] == {"00-06": 1, "06-12": 0, "12-18": 1, "18-24": 1}
    assert pt["lanes"] == {"j7_wallets": 2, "j7_pump": 1} and pt["age_s"] is None     # slice 3 has no table yet


def test_a_wallet_with_no_read_first_buy_still_gets_its_follow_state_and_patterns():
    a, fa = _herder("A", 10)
    v = _view(a, fa, "B")
    assert v["wallet"]["follow"]["state"] == "Paper only" and v["herd"] is None and v["coins"] == []
    assert v["patterns"]["first_buys"] == 0


def test_the_view_route_needs_an_id_and_serves_the_view(tmp_path, monkeypatch):
    reads, _first = _herder("A", 2)
    cl = _client(tmp_path, monkeypatch, _ledger(), _graph(reads))
    assert cl.get("/api/memecoin/wallet-map/wallet?chain=solana").status_code == 400
    body = cl.get("/api/memecoin/wallet-map/wallet?chain=solana&id=A").get_json()
    assert body["stamp"]["error"] is None and body["before"]["state"] == "not_measured_yet"


def test_entry_order_counts_unique_decoded_peers_and_only_first_good_coin_reads():
    reads, first = _herder('A', 3)
    reads[0]['comp'] = [('X', 5), ('X', 20), ('Y', 30), ('A', 1)]
    reads[1]['comp'] = [('Z', 20)]
    # No other decoded buyer: not a zero-order sample.
    reads[2]['comp'] = []
    reads.append(dict(sig='later', h='A', mint='A-m0', t=T0+500, comp=[('W', 1)]))
    result = _row(_build(reads, first), 'A')
    assert result.get('entry_order') == dict(n=2, share_before=.25,
        scope='decoded buyers in the same sampled first-buy slot')


def test_entry_order_unknown_source_or_empty_peers_never_becomes_early_zero():
    reads, first = _herder('A', 2)
    reads[0].update(idx=None, comp=[('X', 1)])
    assert _row(_build(reads, first), 'A').get('entry_order') == dict(n=0, share_before=None,
        scope='decoded buyers in the same sampled first-buy slot')

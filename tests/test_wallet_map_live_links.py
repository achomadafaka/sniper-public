"""Wallet Map D1 (spec docs/superpowers/specs/2026-10-07-wallet-map-overhaul-design.md): the universe
route reads the live link graph, never the retired wallet_universe_<chain>.json, and each wallet
carries its funded record on the chain. A graph that cannot be read is `unavailable`, never an empty
measured graph; a chain the graph does not cover is `not_measured`, never a measured zero."""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_wallet_map as wm

LEADER, FOLLOWER = "uid-leader", "uid-follower"
COMPANION = "Companion1111111111111111111111111111111111"
MV = "wallet-graph-v3:test"


def _now():
    return datetime.now(timezone.utc)


def _graph(path, *, finished_ago=timedelta(minutes=10), edges=(), method_version=MV, runs=True):
    from scripts.research import wallet_link_graph as g
    conn = sqlite3.connect(str(path))
    conn.executescript(g.SCHEMA + g.SLOT_SCHEMA)
    at = (_now() - finished_ago).isoformat()
    if runs:
        conn.execute("INSERT INTO memecoin_wallet_graph_runs (method_version, started_at, finished_at, "
                     "window_from, edges) VALUES (?,?,?,?,?)", (method_version, at, at, at, len(edges)))
    for e in edges:
        row = dict(network_id="solana", n_consistent=e.get("n_coins", 3), lag_p50_s=None, lag_p90_s=None,
                   null_p99=None, null_p=0.001, flag=None, first_seen=at, last_seen=at,
                   coverage=json.dumps({"source": "test", "clock": "slot order"}),
                   method_version=method_version, computed_at=at, src_user_id=None, dst_user_id=None)
        row.update(e)
        if not isinstance(row.get("evidence"), str):
            row["evidence"] = json.dumps(row.get("evidence") or {"coins": []})
        cols = sorted(row)
        conn.execute("INSERT INTO memecoin_wallet_edges (%s) VALUES (%s)" % (",".join(cols), ",".join("?" * len(cols))),
                     [row[c] for c in cols])
    conn.commit()
    conn.close()


def _edge(kind, src, dst, *, src_uid=None, dst_uid=None, mints=("M1", "M2", "M3"), n_coins=None, **kw):
    return dict(edge_type=kind, src=src, dst=dst, src_user_id=src_uid, dst_user_id=dst_uid,
                n_coins=n_coins if n_coins is not None else len(mints),
                evidence={"coins": [{"mint": x, "slot": 1} for x in mints]}, **kw)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from flask import Flask
    from sniper.routes.api_memecoin import create_api_memecoin_blueprint
    from sniper.routes import api_memecoin
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    # THE ROUTES' READ-ONLY LEDGER IS THIS TEST'S OWN FILE (2026-10-15, Gamma full run). herders and
    # wallet open db_core.get_readonly_connection(), i.e. DB_PATH. An earlier test in the same xdist
    # worker can leave DB_PATH unset: the default "sniper_tracker.db" then fails to open in a
    # worktree (TypeError on a 500 body) and is the REAL ledger in the live checkout.
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ledger.db"))
    monkeypatch.setattr(api_memecoin, "_notify_operator", lambda *a, **k: None)
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(str(tmp_path / "ledger.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, LEADER, "leader", "")
    m.add_wallet(conn, FOLLOWER, "follower", "")
    conn.commit()
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": conn}))
    return app.test_client(), conn, tmp_path


@pytest.fixture
def db_path_unset(monkeypatch):
    """What an earlier test in the worker can leave behind; must precede `client`."""
    monkeypatch.delenv("DB_PATH", raising=False)


def test_the_routes_read_this_tests_own_ledger_whatever_db_path_was(db_path_unset, client):
    cl, _conn, tmp_path = client
    _graph(GRAPH(tmp_path))
    assert os.environ["DB_PATH"] == str(tmp_path / "ledger.db")
    for route in ("/api/memecoin/wallet-map/herders?chain=solana",
                  "/api/memecoin/wallet-map/wallet?chain=solana&id=" + LEADER):
        assert cl.get(route).get_json()["stamp"]["graph_state"] == "ok"


def _universe(cl, chain="solana"):
    r = cl.get("/api/memecoin/wallet-map/universe?chain=" + chain)
    assert r.status_code == 200
    return r.get_json()


def _wallet(body, uid):
    return next(w for w in body["wallets"] if w["uid"] == uid)


GRAPH = lambda tmp_path: tmp_path / "data" / "memecoin_wallet_graph.db"   # noqa: E731


def test_live_edges_come_from_the_graph_with_their_measured_fields(client):
    cl, _conn, tmp_path = client
    _graph(GRAPH(tmp_path), edges=[
        _edge("follows", LEADER, FOLLOWER, src_uid=LEADER, dst_uid=FOLLOWER, lag_p50_s=12.5),
        _edge("slot_follower", LEADER, COMPANION, src_uid=LEADER, mints=("A", "B", "C"), n_coins=14),
        _edge("co_buy_slot", LEADER, COMPANION, src_uid=LEADER)])
    body = _universe(cl)
    assert body["graph_state"] == "ok" and body["graph"]["reason"] is None
    assert body["graph"]["method_version"] == MV and body["graph"]["edges_served"] == 3
    by_kind = {e["kind"]: e for e in body["edges"]}
    follows = by_kind["follows"]
    assert (follows["src_uid"], follows["dst_uid"], follows["direction"]) == (LEADER, FOLLOWER, "ordered")
    assert follows["lag_p50_s"] == 12.5 and follows["coins"] == ["M1", "M2", "M3"]
    assert body["coverage_by_ref"][follows["coverage_ref"]] == {"source": "test", "clock": "slot order"}
    assert follows["provenance"]["method_version"] == MV
    slot = by_kind["slot_follower"]
    assert slot["dst_uid"] == COMPANION and slot["dst_in_roster"] is False
    assert slot["coins_omitted"] == 11, "the job stores at most 10 coins; the rest are counted, not hidden"
    assert by_kind["co_buy_slot"]["direction"] == "association"
    assert _wallet(body, LEADER)["links"] == {"state": "ok", "by_type": {
        "follows": 1, "dumps_on": 0, "shared_funder": 0, "co_buy_slot": 1, "slot_follower": 1}}
    assert _wallet(body, FOLLOWER)["links"]["by_type"]["follows"] == 1


def test_the_retired_universe_file_is_never_read(client):
    cl, _conn, tmp_path = client
    (tmp_path / "data" / "wallet_universe_solana.json").write_text(json.dumps(dict(
        version=1, chain="solana", wallets=[dict(uid="ghost", chain="solana")],
        edges=[dict(src_uid=LEADER, dst_uid=FOLLOWER, kind="follows")], graph_state="ok")))
    _graph(GRAPH(tmp_path))
    body = _universe(cl)
    assert "ghost" not in {w["uid"] for w in body["wallets"]}
    assert body["edges"] == [] and body["graph_state"] == "ok", "a measured graph with no edges is a zero"
    assert _wallet(body, LEADER)["links"]["by_type"]["follows"] == 0


def test_a_missing_graph_is_unavailable_not_an_empty_graph(client):
    cl, _conn, _tmp = client
    body = _universe(cl)
    assert (body["graph_state"], body["graph"]["reason"]) == ("unavailable", "absent")
    assert _wallet(body, LEADER)["links"] == {"state": "unavailable", "by_type": None}


def test_a_corrupt_graph_is_unavailable(client):
    cl, _conn, tmp_path = client
    GRAPH(tmp_path).write_bytes(b"this is not a sqlite database" * 100)
    body = _universe(cl)
    assert (body["graph_state"], body["graph"]["reason"]) == ("unavailable", "unreadable")
    assert body["edges"] == []


def test_a_graph_without_a_finished_run_is_unavailable(client):
    cl, _conn, tmp_path = client
    _graph(GRAPH(tmp_path), runs=False, edges=[_edge("follows", LEADER, FOLLOWER, src_uid=LEADER)])
    body = _universe(cl)
    assert (body["graph_state"], body["graph"]["reason"]) == ("unavailable", "no_finished_run")
    assert body["edges"] == []


def test_an_old_run_is_stale_and_still_served(client):
    cl, _conn, tmp_path = client
    _graph(GRAPH(tmp_path), finished_ago=timedelta(hours=4),
           edges=[_edge("follows", LEADER, FOLLOWER, src_uid=LEADER, dst_uid=FOLLOWER)])
    body = _universe(cl)
    assert body["graph_state"] == "stale" and len(body["edges"]) == 1
    assert body["graph"]["age_s"] > 3 * 3600


def test_another_method_versions_rows_are_not_served(client):
    cl, _conn, tmp_path = client
    _graph(GRAPH(tmp_path), edges=[_edge("follows", LEADER, FOLLOWER, src_uid=LEADER)])
    conn = sqlite3.connect(str(GRAPH(tmp_path)))
    conn.execute("UPDATE memecoin_wallet_edges SET method_version='wallet-graph-v2:old'")
    conn.execute("INSERT INTO memecoin_wallet_edges (network_id, edge_type, src, dst, src_user_id, n_coins, "
                 "first_seen, last_seen, coverage, evidence, method_version, computed_at) SELECT network_id, "
                 "'co_buy_slot', src, dst, src_user_id, n_coins, first_seen, last_seen, coverage, evidence, "
                 "?, computed_at FROM memecoin_wallet_edges", (MV + ":cov1",))
    conn.commit()
    conn.close()
    body = _universe(cl)
    assert [e["kind"] for e in body["edges"]] == ["co_buy_slot"], "the run's version and its :suffix family only"


def test_a_chain_the_graph_does_not_cover_is_not_measured_and_the_graph_is_not_opened(client, monkeypatch):
    cl, _conn, tmp_path = client
    _graph(GRAPH(tmp_path), edges=[_edge("follows", LEADER, FOLLOWER, src_uid=LEADER)])
    opened = []
    monkeypatch.setattr(wm, "open_graph_readonly", lambda p: opened.append(p))
    body = _universe(cl, "1")
    assert (body["graph_state"], body["graph"]["reason"]) == ("not_measured", "chain_not_measured")
    assert body["edges"] == [] and opened == []
    assert _wallet(body, LEADER)["links"] == {"state": "not_measured", "by_type": None}


def test_each_wallet_keeps_its_strongest_edges_and_counts_the_rest(client, monkeypatch):
    cl, _conn, tmp_path = client
    monkeypatch.setattr(wm, "UNIVERSE_WALLET_EDGE_CAP", 2)
    _graph(GRAPH(tmp_path), edges=[
        _edge("co_buy_slot", LEADER, "D%d" % i, src_uid=LEADER, null_p=p) for i, p in enumerate((0.5, 0.001, 0.01))])
    body = _universe(cl)
    assert sorted(e["null_p"] for e in body["edges"]) == [0.001, 0.01]
    assert body["graph"]["omitted_by_wallet"] == {LEADER: 1, "D0": 1}, "missing from both endpoints' views"
    assert body["graph"]["edges_total"] == 3
    assert _wallet(body, LEADER)["links"]["by_type"]["co_buy_slot"] == 3, "counts are of the graph, not the page"


def _position(conn, uid, *, mode, pnl, notional, net="solana", is_open=0):
    at = _now().isoformat()
    conn.execute("INSERT INTO memecoin_positions(source_user_id, mint, network_id, execution_mode, is_open, "
                 "realized_pnl, opened_at, closed_at, strategy, signal_source, entry_notional) "
                 "VALUES (?,?,?,?,?,?,?,?,'alpha_b','global_feed',?)",
                 (uid, "mint-%s-%s-%s" % (uid, mode, pnl), net, mode, is_open, pnl, at, at, notional))
    conn.commit()


def test_each_wallet_carries_its_funded_record_on_the_chain_and_paper_never_counts(client):
    cl, conn, tmp_path = client
    _graph(GRAPH(tmp_path))
    _position(conn, LEADER, mode="real", pnl=5.0, notional=50.0)
    _position(conn, LEADER, mode="paper", pnl=500.0, notional=50.0)
    _position(conn, LEADER, mode="real", pnl=99.0, notional=10.0, net="8453")
    body = _universe(cl)
    funded = _wallet(body, LEADER)["funded"]
    assert funded["state"] == "ok" and funded["closed_count"] == 1
    assert funded["roi_pct"] == pytest.approx(10.0) and funded["realized_pnl"] == pytest.approx(5.0)
    assert _wallet(body, FOLLOWER)["funded"] == {"state": "none", "open_count": 0, "closed_count": 0}
    assert body["hypothesis_export"] == {"state": "not_available", "reason": "legacy setups retired"}
    assert _wallet(body, LEADER)["paper"] is None


def test_an_unreadable_ledger_makes_funded_unknown_not_none(client, monkeypatch):
    cl, _conn, tmp_path = client
    _graph(GRAPH(tmp_path))

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(m, "real_stats_by_wallet", boom)
    body = _universe(cl)
    assert _wallet(body, LEADER)["funded"] == {"state": "unavailable"}


# --- the :memory: substitution is gone ------------------------------------------------------------

@pytest.mark.parametrize("route", ["/api/memecoin/wallet-map/herders?chain=solana",
                                   "/api/memecoin/wallet-map/wallet?chain=solana&id=" + LEADER])
def test_a_graph_open_failure_reads_as_unavailable_never_an_empty_graph(client, monkeypatch, route):
    cl, _conn, tmp_path = client
    _graph(GRAPH(tmp_path))

    def boom(_p):
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(wm, "open_graph_readonly", boom)
    real_connect = sqlite3.connect
    memory = []
    monkeypatch.setattr(sqlite3, "connect",
                        lambda target, *a, **k: memory.append(target) or real_connect(target, *a, **k))
    stamp = cl.get(route).get_json()["stamp"]
    assert (stamp["error"], stamp["graph_state"]) == ("graph_unavailable", "unavailable")
    assert ":memory:" not in memory


def test_a_readable_graph_stamps_its_state(client):
    cl, _conn, tmp_path = client
    _graph(GRAPH(tmp_path))
    assert cl.get("/api/memecoin/wallet-map/herders?chain=solana").get_json()["stamp"]["graph_state"] == "ok"
    assert cl.get("/api/memecoin/wallet-map/herders?chain=1").get_json()["stamp"]["graph_state"] == "not_measured"


# --- the brain projection bound -------------------------------------------------------------------

def test_an_oversized_shared_funder_edge_projects_with_a_truncation_marker():
    from scripts.research import wallet_link_graph as g
    hubs = {"F%04d" % i: i for i in range(400)}
    e = dict(network_id="solana", edge_type="shared_funder", src="A", dst="B", n_coins=1, n_consistent=1,
             lag_p50_s=None, lag_p90_s=None, null_p=None, flag=None, first_seen="t", last_seen="t",
             coverage={"source": "Helius", "hub_funders": hubs}, evidence={"funder": "F", "funder_degree": 3},
             method_version=g.METHOD_VERSION)
    assert len(json.dumps(e["coverage"], separators=(",", ":"))) > 4096
    text = g.detail_for(e, "2026-10-07T10:35:01+00:00")
    assert len(text) <= 4096
    d = json.loads(text)
    assert "truncated, " in d["coverage"] and d["coverage"].startswith('{"source":"Helius"')
    assert d["edge_type"] == "shared_funder" and d["src"] == "A"


def test_an_edge_under_the_bound_is_unchanged():
    from scripts.research import wallet_link_graph as g
    e = dict(network_id="solana", edge_type="follows", src="A", dst="B", n_coins=3, n_consistent=3,
             lag_p50_s=1.0, lag_p90_s=2.0, null_p=0.01, flag=None, first_seen="t", last_seen="t",
             coverage={"source": "j7"}, evidence={"coins": [{"mint": "M"}], "null": {"per_seed": [1]}},
             method_version=g.METHOD_VERSION)
    d = json.loads(g.detail_for(e, "t"))
    assert d["coverage"] == {"source": "j7"} and d["coins"] == ["M"] and d["null"] == {"per_seed": [1]}


def test_coverage_is_served_once_per_distinct_value_not_on_every_edge(client):
    cl, _conn, tmp_path = client
    other = json.dumps({"source": "a different intake"})
    _graph(GRAPH(tmp_path), edges=[
        _edge("co_buy_slot", LEADER, "D1", src_uid=LEADER), _edge("co_buy_slot", LEADER, "D2", src_uid=LEADER),
        _edge("co_buy_slot", LEADER, "D3", src_uid=LEADER, coverage=other),
        _edge("follows", LEADER, FOLLOWER, src_uid=LEADER, dst_uid=FOLLOWER)])
    body = _universe(cl)
    assert all("coverage" not in e for e in body["edges"]), "no per-edge copy"
    refs = {e["dst"]: e["coverage_ref"] for e in body["edges"]}
    assert refs["D1"] == refs["D2"] == "co_buy_slot" and refs["D3"] == "co_buy_slot#2"
    assert refs[FOLLOWER] == "follows"
    assert body["coverage_by_ref"]["co_buy_slot#2"] == {"source": "a different intake"}
    assert set(body["coverage_by_ref"]) == set(refs.values())

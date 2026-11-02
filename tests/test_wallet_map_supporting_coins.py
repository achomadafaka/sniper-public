"""Wallet Map D2 (spec docs/superpowers/specs/2026-10-07-wallet-map-overhaul-design.md): the wallet
view names each supporting coin's source first buy and the buyers that landed after it in that slot;
`&with=<other>` returns the coins two wallets share under each measured edge type; a chain the graph
does not cover answers 200 `not_measured`, not a 400."""
import json

from sniper.services import memecoin_wallet_map as wm
from test_memecoin_wallet_map import NOW, T0, _client, _graph, _herder, _iso, _ledger, _view

COV = {"source": "onchain_wallet_detections + Helius", "clock": "slot order (transactionIndex)"}


def _edge(g, kind, src, dst, coins, *, src_uid=None, dst_uid=None, mv="v", n=None, cov=COV):
    g.execute("INSERT INTO memecoin_wallet_edges (network_id, edge_type, src, dst, src_user_id, dst_user_id, n_coins, "
              "n_consistent, first_seen, last_seen, coverage, evidence, method_version, computed_at) "
              "VALUES ('solana',?,?,?,?,?,?,?,'t','t',?,?,?,?)",
              (kind, src, dst, src_uid, dst_uid, n if n is not None else len(coins), len(coins), json.dumps(cov),
               json.dumps({"coins": coins}), mv, NOW))


def test_each_coin_names_its_source_first_buy_and_who_landed_after_it():
    a, fa = _herder("A", 10, members=[("X", 10)])        # n >= 10, so X is a repeat buyer (a regular)
    a[0]["comp"] += [("AHEAD", 5), ("RosterAddr", 30)]
    g = _graph(a)
    g.execute("UPDATE memecoin_wallet_slot_reads SET slot = 451000000 + rowid")
    v = wm.build_wallet_view(g, _ledger(), wallet="A", now_iso=NOW, own_addresses=set(), first_sigs=fa)
    coin = next(c for c in v["coins"] if c["mint"] == "A-m0")
    assert coin["source"] == {"signature": "A-s0", "slot": 451000001, "index": 10, "at": _iso(T0),
                              "detected_at": _iso(T0), "source_at": None, "source_time_basis": "unknown"}
    assert coin["after"] == [
        {"trader": "X", "address": "X", "index": 20, "signature": None, "repeat_buyer": True},
        {"trader": "R", "address": "RosterAddr", "index": 30, "signature": None, "repeat_buyer": False}]
    assert coin["followers"] == 2 and coin["regulars"] == ["X"]
    assert v["coins_total"] == 10 and v["coins_omitted"] == 0
    assert v["notes"]["after_signature"] == "not stored by the slot read"


def test_a_source_buy_with_no_slot_index_has_unknown_followers_not_zero():
    a, fa = _herder("A", 2, members=[("X", 2)])
    a[0]["idx"] = None
    v = _view(a, fa, "A")
    coin = next(c for c in v["coins"] if c["mint"] == "A-m0")
    assert coin["after"] is None and coin["followers"] is None and coin["source"]["index"] is None
    assert coin["regulars"] is None, "unknown order never reads as 'no repeat buyers'"
    other = next(c for c in v["coins"] if c["mint"] == "A-m1")
    assert other["followers"] == 1


def test_the_coin_list_is_bounded_to_the_newest_and_says_how_many_it_left_out(monkeypatch):
    monkeypatch.setattr(wm, "COINS_MAX", 2)
    a, fa = _herder("A", 5)
    v = _view(a, fa, "A")
    assert [c["mint"] for c in v["coins"]] == ["A-m4", "A-m3"]
    assert (v["coins_total"], v["coins_omitted"]) == (5, 3)


def test_shared_coins_with_a_linked_wallet_come_from_each_edges_stored_evidence():
    g = _graph([])
    _edge(g, "follows", "A", "B", [{"mint": "M1", "leader_at": "t1", "lag_s": 9.1}], src_uid="A", dst_uid="B",
          cov={"source": "memecoin_events j7_wallets"})
    _edge(g, "co_buy_slot", "B", "A", [{"mint": "M1", "slot": 7, "src_index": 3, "dst_index": 9},
                                       {"mint": "M2", "slot": 8, "src_index": 1, "dst_index": 2}],
          src_uid="B", dst_uid="A", n=14)
    _edge(g, "follows", "A", "C", [{"mint": "M9"}], src_uid="A", dst_uid="C")
    _edge(g, "follows", "A", "B", [{"mint": "OLD"}], src_uid="A", dst_uid="B", mv="older")
    out = wm.shared_with(g, "A", "B", now_iso=NOW)
    assert out["state"] == "ok" and out["measured_link"] is True
    by = {e["kind"]: e for e in out["edges"]}
    assert set(by) == {"follows", "co_buy_slot"}, "only this pair, only the run's method version"
    assert by["follows"]["coins"] == [{"mint": "M1", "leader_at": "t1", "lag_s": 9.1}]
    assert (by["co_buy_slot"]["src_uid"], by["co_buy_slot"]["coins_omitted"]) == ("B", 12)
    assert out["shared_coins"] == ["M1", "M2"]
    assert out["coverage_by_ref"][by["follows"]["coverage_ref"]] == {"source": "memecoin_events j7_wallets"}


def test_no_measured_link_is_an_empty_list_with_the_coverage_never_a_bare_zero():
    g = _graph([])
    _edge(g, "follows", "A", "C", [{"mint": "M9"}], src_uid="A", dst_uid="C", cov={"source": "j7"})
    out = wm.shared_with(g, "A", "B", now_iso=NOW)
    assert (out["state"], out["measured_link"], out["edges"], out["shared_coins"]) == ("ok", False, [], [])
    assert out["coverage_by_ref"] == {"follows": {"source": "j7"}}, "what was measured, so the empty list has a scope"


def test_an_unreadable_graph_makes_the_comparison_unavailable():
    assert wm.shared_with(None, "A", "B", now_iso=NOW)["state"] == "unavailable"


def test_the_wallet_route_serves_the_comparison_and_a_non_solana_chain_is_not_measured(tmp_path, monkeypatch):
    g = _graph([])
    _edge(g, "follows", "A", "B", [{"mint": "M1"}], src_uid="A", dst_uid="B")
    cl = _client(tmp_path, monkeypatch, _ledger(), g)
    body = cl.get("/api/memecoin/wallet-map/wallet?chain=solana&id=A&with=B").get_json()
    assert body["graph_state"] == "stale" and body["with"]["shared_coins"] == ["M1"]
    r = cl.get("/api/memecoin/wallet-map/wallet?chain=56&id=A")
    assert r.status_code == 200
    assert r.get_json()["graph_state"] == "not_measured" and r.get_json()["coins"] == []
    assert cl.get("/api/memecoin/wallet-map/wallet?chain=typo&id=A").status_code == 400


def test_the_wallet_route_without_with_has_no_comparison(tmp_path, monkeypatch):
    cl = _client(tmp_path, monkeypatch, _ledger(), _graph([]))     # the fixture's ledger serves one request
    body = cl.get("/api/memecoin/wallet-map/wallet?chain=solana&id=A").get_json()
    assert "with" not in body and body["graph_state"] == "stale"

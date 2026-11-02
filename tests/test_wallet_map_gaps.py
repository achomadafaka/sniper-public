"""Wallet Map D4 (spec docs/superpowers/specs/2026-10-07-wallet-map-overhaul-design.md): the wallet view
lists each gap in its evidence with what would fill it, from fields D1-D3 already return. A gap is
named, never implied by a zero."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_wallet_map as wm
from test_memecoin_wallet_map import NOW, _client, _graph, _herder, _ledger, _view
from test_wallet_map_funded_coins import _attempt, _origin, _pos


def _keys(view):
    return [g["key"] for g in view["gaps"]]


def test_a_measured_wallet_with_a_small_sample_and_no_funded_closes_names_both():
    a, fa = _herder("A", 3)
    v = _view(a, fa, "A")
    assert _keys(v) == ["small_sample", "no_funded_closes", "coins_without_attempt"]
    small = v["gaps"][0]
    assert small["n"] == 3 and "10" in small["fill"]
    assert v["gaps"][2]["n"] == 3
    assert all(g["what"] and g["fill"] for g in v["gaps"])


def test_a_wallet_with_enough_coins_closes_and_attempts_has_no_gap():
    led = _ledger()
    a, fa = _herder("A", 10)
    for i in range(10):
        mint = "A-m%d" % i
        _attempt(led, mint, _origin(mint), status="refused", reason="not_approved")
    _pos(led, "A-m0", pnl=5.0)
    v = _view(a, fa, "A", ledger=led)
    assert v["gaps"] == []


def test_a_stale_graph_and_an_unreadable_ledger_are_gaps_not_silence(monkeypatch):
    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(m, "real_positions_for_wallet", boom)
    a, fa = _herder("A", 10)
    v = wm.build_wallet_view(_graph(a, finished_at="2026-09-30T05:00:00+00:00"), _ledger(), wallet="A",
                             now_iso=NOW, own_addresses=set(), first_sigs=fa)
    assert _keys(v) == ["graph_stale", "funded_unavailable"]


def test_an_unavailable_graph_is_the_only_gap_it_can_name():
    v = wm.build_wallet_view(None, _ledger(), wallet="A", now_iso=NOW, own_addresses=set())
    assert _keys(v) == ["graph_unavailable"]


def test_a_chain_the_graph_does_not_cover_names_that_gap(tmp_path, monkeypatch):
    cl = _client(tmp_path, monkeypatch, _ledger())
    body = cl.get("/api/memecoin/wallet-map/wallet?chain=56&id=A").get_json()
    assert _keys(body) == ["graph_not_measured"]


def test_a_coin_whose_only_attempt_was_shadow_counts_as_without_attempt():
    led = _ledger()
    a, fa = _herder("A", 10)
    for i in range(9):
        mint = "A-m%d" % i
        _attempt(led, mint, _origin(mint), reason="not_approved")
    _attempt(led, "A-m9", _origin("A-m9"), status="shadow", lane="j7_shadow", reason="j7_shadow: checks passed")
    _pos(led, "A-m0", pnl=5.0)
    v = _view(a, fa, "A", ledger=led)
    assert [(g["key"], g.get("n")) for g in v["gaps"]] == [("coins_without_attempt", 1)]


def test_the_attempt_gap_names_its_population_when_coins_were_left_out(monkeypatch):
    monkeypatch.setattr(wm, "COINS_MAX", 2)
    a, fa = _herder("A", 5)
    gap = next(g for g in _view(a, fa, "A")["gaps"] if g["key"] == "coins_without_attempt")
    assert gap["n"] == 2 and "of the newest 2" in gap["what"]

"""Live Wallet Signals board v2: joins analyzer reads (wallet_analysis_latest) × open positions
into explainable LANES (directional_edge / fresh_watch / algo), leads with the WHY (headline),
pins favorites, excludes soft-deleted wallets, flags strategy candidates. No insider / no score."""
from __future__ import annotations

import json
import sqlite3

import pytest
from flask import Flask

from sniper.routes import api_live_signals as M
from sniper.persistence import db_wallet_signals as DB
from sniper.persistence import db_wallet_reads as RD
from sniper.persistence import db_wallet_flags as FL
from sniper.analysis.winning_wallet_thesis.synthesis import WalletRead

A = "0x" + "a" * 40   # established directional
B = "0x" + "b" * 40   # structural / algo
FR = "0x" + "f" * 40  # fresh_high_surprise


def _read(verdict, route, *, domains=(), warnings=(), archetype="x"):
    return WalletRead(
        primary_verdict=verdict, operational_route=route, headline=f"{verdict} — why one-liner",
        reasons=["r"], warnings=list(warnings),
        edge_location={"domains": list(domains), "loss_centers": [], "entry_shape": {}},
        evidence_profile={"history_depth": "deep", "archetype": archetype, "intent": "i",
                          "fresh": {"ex_ante_level": "none", "ex_post_level": "none",
                                    "coordination_level": "none"}, "recent_anomaly_level": "none"},
        provenance={"analyzer_version": 1, "rule_version": 1, "data_as_of": 100,
                    "source_snapshot_hash": "h"})


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    DB.ensure_wallet_signals_tables(c)
    RD.ensure_wallet_reads_tables(c)
    FL.ensure_wallet_flags_tables(c)
    RD.append_read(c, A, _read("established_directional_edge", "expedited_review",
                               domains=["Basketball"], archetype="directional_discretionary"), now_ts=1)
    RD.append_read(c, B, _read("structural_non_directional", "ignore", archetype="paired_outcome_arb"), now_ts=1)
    RD.append_read(c, FR, _read("fresh_high_surprise", "expedited_review",
                                warnings=["WATCH ONLY — high false-positive shape; validation required"],
                                archetype="directional_discretionary"), now_ts=1)
    DB.upsert_live_position(c, {"wallet": A, "condition_id": "0x1", "outcome_index": 0, "outcome": "LAL",
                               "title": "Lakers ML", "league": "Basketball", "size": 70000,
                               "avg_price": 0.6, "cur_price": 0.62, "current_value": 42000, "cash_pnl": 1000})
    DB.upsert_live_position(c, {"wallet": B, "condition_id": "0x9", "outcome_index": 0,
                               "league": "Basketball", "cur_price": 0.5, "current_value": 50000})
    DB.upsert_live_position(c, {"wallet": FR, "condition_id": "0xf1", "outcome_index": 0,
                               "league": "Politics", "cur_price": 0.3, "current_value": 9000})
    return c


def _client(conn):
    app = Flask(__name__)
    app.register_blueprint(M.create_api_live_signals_blueprint({"conn": conn}))
    return app.test_client()


def test_board_assigns_lanes(conn):
    d = _client(conn).get("/api/live-wallet-signals").get_json()
    assert d["count"] == 3 and "in-sample" in d["disclaimer"].lower()
    by = {r["wallet"]: r for r in d["items"]}
    assert by[A]["lane"] == "directional_edge" and by[A]["strategy_candidate"] is True
    assert ("forward" + "_candidate") not in by[A]
    assert ("forward" + "_test_required") not in by[A]
    assert by[B]["lane"] == "algo"
    assert by[FR]["lane"] == "fresh_watch"
    assert by[A]["headline"]                      # the why, present
    assert by[A]["edge_domain_match"] is True      # Basketball position ∈ edge domains
    assert d["lanes"]["directional_edge"] == 1 and d["lanes"]["algo"] == 1


def test_fresh_high_conviction_routes_to_fresh_watch(conn):
    # gardenshed/GRIMDRIP: a thin-history big-conviction whale surfaces in fresh_watch (UNVERIFIED) as a
    # strategy candidate, carrying the survivorship caveat — not buried like a no_supported wallet.
    HC = "0x" + "7" * 40
    RD.append_read(conn, HC, _read("fresh_high_conviction", "strategy_candidate",
                                   warnings=["HIGH false-positive base rate — survivorship; paper-test only"],
                                   archetype="mixed_unresolved"), now_ts=1)
    DB.upsert_live_position(conn, {"wallet": HC, "condition_id": "0xhc", "outcome_index": 0,
                                   "league": "Soccer", "cur_price": 0.4, "current_value": 50000})
    r = next(x for x in _client(conn).get("/api/live-wallet-signals").get_json()["items"]
             if x["wallet"] == HC)
    assert r["lane"] == "fresh_watch" and r["strategy_candidate"] is True
    assert r["warnings"]   # survivorship caveat carried on the fresh_watch lane


def test_fresh_watch_carries_warning_no_insider(conn):
    d = _client(conn).get("/api/live-wallet-signals").get_json()
    fr = next(r for r in d["items"] if r["wallet"] == FR)
    assert fr["warnings"]
    assert "insider" not in json.dumps(d).lower()


def test_favorites_pinned_to_top(conn):
    FL.set_favorite(conn, B, now=5)               # algo wallet, but favorited
    d = _client(conn).get("/api/live-wallet-signals").get_json()
    assert d["items"][0]["wallet"] == B and d["items"][0]["is_favorite"] is True


def test_soft_deleted_wallet_excluded(conn):
    FL.soft_delete(conn, A, now=5)
    d = _client(conn).get("/api/live-wallet-signals").get_json()
    assert all(r["wallet"] != A for r in d["items"])


def test_lane_filter(conn):
    d = _client(conn).get("/api/live-wallet-signals?lane=algo").get_json()
    assert d["count"] == 1 and all(r["lane"] == "algo" for r in d["items"])


def test_strategy_candidates(conn):
    c = _client(conn)
    assert c.get("/api/live-wallet-signals/" + "forward" + "-candidates").status_code == 404
    d = c.get("/api/live-wallet-signals/strategy-candidates").get_json()
    assert d["count"] == 2 and all(r["strategy_candidate"] for r in d["items"])
    low = d["disclaimer"].lower()
    assert "derived strategy" in low
    assert "enroll" not in low
    assert ("paper" + "-watch") not in low


def test_legacy_unknown_strategy_route_sanitized_from_verdict(conn):
    legacy = "0x" + "6" * 40
    RD.append_read(conn, legacy, _read("emerging_directional_evidence", "legacy_route_label"),
                   now_ts=1)
    DB.upsert_live_position(conn, {"wallet": legacy, "condition_id": "0xlg", "outcome_index": 0,
                                   "league": "Soccer", "cur_price": 0.4, "current_value": 5000})
    row = next(r for r in _client(conn).get("/api/live-wallet-signals").get_json()["items"]
               if r["wallet"] == legacy)
    assert row["strategy_candidate"] is True
    assert "operational_route" not in row


def test_no_insider_or_overall_score(conn):
    item = _client(conn).get("/api/live-wallet-signals").get_json()["items"][0]
    assert "insider_score" not in item and "score" not in item and "grade" not in item


def test_unsupported_verdict_hidden_unless_favorited(conn):
    # a no_supported_edge wallet with an open position is not surfaced...
    NS = "0x" + "e" * 40
    RD.append_read(conn, NS, _read("no_supported_edge", "ignore"), now_ts=1)
    DB.upsert_live_position(conn, {"wallet": NS, "condition_id": "0xe1", "outcome_index": 0,
                                   "league": "Soccer", "cur_price": 0.4, "current_value": 1000})
    d = _client(conn).get("/api/live-wallet-signals").get_json()
    assert all(r["wallet"] != NS for r in d["items"])
    # ...until favorited, then it shows (pinned) with its verdict in the "other" lane
    FL.set_favorite(conn, NS, now=9)
    d2 = _client(conn).get("/api/live-wallet-signals").get_json()
    ns = next((r for r in d2["items"] if r["wallet"] == NS), None)
    assert ns is not None and ns["primary_verdict"] == "no_supported_edge"


def test_board_tolerates_missing_flags_table():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    DB.ensure_wallet_signals_tables(c)
    RD.ensure_wallet_reads_tables(c)
    RD.append_read(c, A, _read("established_directional_edge", "expedited_review",
                               domains=["Basketball"]), now_ts=1)
    DB.upsert_live_position(c, {"wallet": A, "condition_id": "0x1", "outcome_index": 0,
                                "league": "Basketball", "cur_price": 0.5, "current_value": 1000})
    r = _client(c).get("/api/live-wallet-signals")
    assert r.status_code == 200
    assert r.get_json()["count"] == 1


def test_domain_match_normalizes_nba_to_basketball(conn):
    assert M._domain_match("Basketball", ["NBA"]) is True

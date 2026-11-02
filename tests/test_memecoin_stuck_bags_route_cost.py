"""/api/memecoin/stuck-bags: what it costs, and where it gets liquidity.

`consensus_setups` aggregates all-time copy-PnL per wallet (a full scan of memecoin_fills, ~34ms on
the live DB) and the report reads it ONLY to name the setup a full book is blocking. This endpoint is
one of five parallel reads the funded cockpit's first paint waits on, and it was the slowest of them.
"""
import sqlite3
from datetime import datetime, timedelta

import pytest
from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from sniper.services import memecoin_entry_signals as ES
from sniper.settings import memecoin as cfg
from sniper.util.clock import real_utc_now_iso

NOW = "2026-07-19T06:00:00+00:00"
HOT = [{"ticker": "GEM", "mint": "0xgem", "armed": 3, "hot": True, "setup_score": 80}]


@pytest.fixture
def route(monkeypatch):
    c = sqlite3.connect(":memory:", check_same_thread=False)
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({"conn": c}))
    calls = []
    monkeypatch.setattr(ES, "consensus_setups",
                        lambda *a, **k: calls.append(1) or list(HOT))
    return app.test_client(), c, calls


def _open(c, pid, mint, *, stuck=0):
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, entry_notional, "
              "network_id, is_open, execution_mode, opened_at, stuck_bag) "
              "VALUES(?,?,?,?,?,'solana',1,'real',?,?)", (pid, mint, "T" + str(pid), 0.001, 50.0,
                                                          NOW, stuck))
    c.commit()


def test_a_free_slot_never_grades_the_wallets(route, monkeypatch):
    cl, c, calls = route
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    _open(c, 1, "AAA", stuck=1)                 # stuck, but the book has room

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["slots_free"] == 6 and len(j["stuck"]) == 1
    assert j["recommendation"] is None
    assert calls == []                          # nothing to redeploy into a free slot


def test_a_full_book_with_no_stuck_bag_never_grades_the_wallets(route, monkeypatch):
    cl, c, calls = route
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)
    _open(c, 1, "AAA")                          # no radar quote -> liquidity unknown -> not DEAD

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["slots_free"] == 0 and j["stuck"] == []
    assert j["recommendation"] is None
    assert calls == []                          # no bag to cut, so no setup to name


def test_a_full_book_with_a_stuck_bag_still_names_the_blocked_setup(route, monkeypatch):
    cl, c, calls = route
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 1)
    _open(c, 1, "AAA", stuck=1)

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert calls == [1]                         # computed exactly once, not twice
    assert j["blocked_setup"]["ticker"] == "GEM"
    assert j["recommendation"] and "#1" in j["recommendation"]


def _mark(c, mint, sweep_ts, liq, *, chain="solana", mid=0.0005):
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, "
              "executable_exit_value, liquidity, mcap, source, quality) "
              "VALUES(?,?,?,?,?,?,?,'geckoterminal','ok')",
              (chain, mint, sweep_ts, mid, mid, liq, 1e5))
    c.commit()


def _ago(seconds):
    return (datetime.fromisoformat(real_utc_now_iso())
            - timedelta(seconds=seconds)).isoformat()


def test_a_fresh_mark_gives_the_detector_its_liquidity_back(route, monkeypatch):
    # radar_quote_map has returned {} since the quote daemon stopped on 2026-08-21, so this is the
    # only liquidity the detector can see. The funded runner writes it for every open real position.
    cl, c, _ = route
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    _open(c, 1, "THIN")
    _mark(c, "THIN", _ago(60), 100.0)          # known, tiny -> untradeable

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert [s["id"] for s in j["stuck"]] == [1]
    assert j["stuck"][0]["liquidity"] == 100.0
    assert j["unpriced"] == []


def test_a_stale_mark_can_never_justify_a_write_off(route, monkeypatch):
    cl, c, _ = route
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    monkeypatch.setattr(cfg, "MEMECOIN_STUCK_MARK_MAX_AGE_S", 600.0)
    _open(c, 1, "THIN")
    _mark(c, "THIN", _ago(3600), 100.0)        # an hour old -> unknown, not "untradeable"

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["stuck"] == []
    assert j["unpriced"] == [1]                # named as unknown, never counted healthy


def test_a_fresh_depth_cache_row_prices_a_coin_with_no_provider_mark(route, monkeypatch):
    # On 4663 the fast-lane marks never carry liquidity and 29 of 49 funded positions in the week to
    # 2026-09-23 had no DexScreener listing at all, so the banner "untradeable cannot be ruled out"
    # fired on every one of them while the impact-gate depth cache held a fresh number for the coin.
    cl, c, _ = route
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    monkeypatch.setattr(cfg, "MEMECOIN_POOL_DEPTH_MAX_AGE_S", 3600.0)
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, entry_notional, "
              "network_id, is_open, execution_mode, opened_at, stuck_bag) "
              "VALUES(1,'0xdoai','DOAI',0.0001,50.0,'4663',1,'real',?,0)", (NOW,))
    _mark(c, "0xdoai", _ago(30), None, chain="4663")          # fast-lane mark, liquidity unknown
    m.record_pool_depth(c, "4663", "0xdoai", depth_usd=39035.8, pool="0xpool", quote="0xq",
                        pools_read=12, now_iso=_ago(120))
    c.commit()

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["unpriced"] == []
    assert j["stuck"] == []


def test_a_stale_depth_cache_row_leaves_the_coin_unpriced(route, monkeypatch):
    cl, c, _ = route
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    monkeypatch.setattr(cfg, "MEMECOIN_POOL_DEPTH_MAX_AGE_S", 3600.0)
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, entry_notional, "
              "network_id, is_open, execution_mode, opened_at, stuck_bag) "
              "VALUES(1,'0xdoai','DOAI',0.0001,50.0,'4663',1,'real',?,0)", (NOW,))
    m.record_pool_depth(c, "4663", "0xdoai", depth_usd=39035.8, pool="0xpool", quote="0xq",
                        pools_read=12, now_iso=_ago(7200))
    c.commit()

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["unpriced"] == [1]


def _open_56(c, pid, mint, *, stuck=0):
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, entry_notional, "
              "network_id, is_open, execution_mode, opened_at, stuck_bag) "
              "VALUES(?,?,?,0.0002,90.0,'56',1,'real',?,?)", (pid, mint, "T" + str(pid), NOW, stuck))
    c.commit()


def _exact_mark(c, mint, sweep_ts, *, chain="56", mid=0.00023):
    # The funded fast lane's whole-balance route quote: mid == executable value, no liquidity.
    c.execute("INSERT INTO memecoin_position_marks(chain_id, mint, sweep_ts, mid, "
              "executable_exit_value, liquidity, mcap, source, quality) "
              "VALUES(?,?,?,?,?,NULL,NULL,'fastlane_rpc','exact_route')",
              (chain, mint, sweep_ts, mid, mid))
    c.commit()


def test_a_fresh_exact_route_quote_counts_as_tradeable(route, monkeypatch):
    # Position 68538 (56, 2026-10-02): the runner quoted the whole bag every 2 s on its exact route
    # (fastlane_rpc / exact_route, liquidity NULL), yet the banner called it "no live liquidity".
    cl, c, _ = route
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    monkeypatch.setattr(cfg, "MEMECOIN_STUCK_MARK_MAX_AGE_S", 600.0)
    _open_56(c, 1, "0xbe14")
    _exact_mark(c, "0xbe14", _ago(20))

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["unpriced"] == []
    assert j["stuck"] == []


def test_a_stale_or_empty_exact_route_quote_keeps_the_warning(route, monkeypatch):
    cl, c, _ = route
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_CONCURRENT", 7)
    monkeypatch.setattr(cfg, "MEMECOIN_STUCK_MARK_MAX_AGE_S", 600.0)
    _open_56(c, 1, "0xold")
    _exact_mark(c, "0xold", _ago(3600))              # older than the max age
    _open_56(c, 2, "0xzero")
    _exact_mark(c, "0xzero", _ago(20), mid=0.0)      # a route row with no price is no quote
    _open_56(c, 3, "0xdaodun", stuck=1)              # 68544: stuck, no mark of any kind

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["unpriced"] == [1, 2, 3]
    assert [s["id"] for s in j["stuck"]] == [3]


def _position_exact_quote(c, pid, price, observed_at):
    c.execute("UPDATE memecoin_positions SET last_executable_price=?, last_executable_at=? "
              "WHERE id=?", (price, observed_at, pid))
    c.commit()


@pytest.mark.parametrize("observed_at", [
    NOW, "2026-07-19T05:50:00+00:00", "2026-07-19T02:00:00-04:00",
])
def test_fresh_position_exact_quote_proves_sellability_without_liquidity(
        route, monkeypatch, observed_at):
    from sniper.routes import api_memecoin
    cl, c, calls = route
    monkeypatch.setattr(api_memecoin, "_now_iso", lambda: NOW)
    monkeypatch.setattr(cfg, "MEMECOIN_STUCK_MARK_MAX_AGE_S", 600.0)
    _open(c, 73731, "PQC")
    _position_exact_quote(c, 73731, 0.000515, observed_at)
    # Solana swap/state marks are live mids; the position row holds the actual whole-bag quote.
    _mark(c, "PQC", NOW, None)

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["unpriced"] == []
    assert j["stuck"] == []
    assert calls == []


@pytest.mark.parametrize("price,observed_at", [
    (None, NOW), (0.0, NOW), (-1.0, NOW), (float("inf"), NOW), (float("nan"), NOW),
    ("bad", NOW), (0.000515, None), (0.000515, "bad"),
    (0.000515, "2026-07-19T05:49:59+00:00"),
    (0.000515, "2026-07-19T06:00:01+00:00"), (0.000515, "2026-07-19T06:00:00"),
])
def test_unknown_or_not_current_position_exact_quote_keeps_warning(
        route, monkeypatch, price, observed_at):
    from sniper.routes import api_memecoin
    cl, c, _ = route
    monkeypatch.setattr(api_memecoin, "_now_iso", lambda: NOW)
    monkeypatch.setattr(cfg, "MEMECOIN_STUCK_MARK_MAX_AGE_S", 600.0)
    _open(c, 73731, "PQC")
    _position_exact_quote(c, 73731, price, observed_at)
    _mark(c, "PQC", NOW, None)

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["unpriced"] == [73731]


def test_position_exact_quote_is_bound_to_its_bag_and_closed_bags_are_excluded(route, monkeypatch):
    from sniper.routes import api_memecoin
    cl, c, _ = route
    monkeypatch.setattr(api_memecoin, "_now_iso", lambda: NOW)
    _open(c, 1, "PQC")
    _open(c, 2, "PQC")
    _open(c, 3, "CLOSED")
    _position_exact_quote(c, 1, 0.000515, NOW)
    c.execute("UPDATE memecoin_positions SET is_open=0 WHERE id=3")
    c.commit()

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["unpriced"] == [2]


def test_position_exact_quote_uses_existing_freshness_setting_and_never_clears_stuck(route, monkeypatch):
    from sniper.routes import api_memecoin
    cl, c, _ = route
    monkeypatch.setattr(api_memecoin, "_now_iso", lambda: NOW)
    monkeypatch.setattr(cfg, "MEMECOIN_STUCK_MARK_MAX_AGE_S", 60.0)
    _open(c, 1, "OLD")
    _open(c, 2, "STUCK", stuck=1)
    _open(c, 3, "MIDONLY")
    _position_exact_quote(c, 1, 0.000515, "2026-07-19T05:58:00+00:00")
    _position_exact_quote(c, 2, 0.000515, NOW)
    c.execute("UPDATE memecoin_positions SET last_quote_price=.000515, last_quote_ts=? WHERE id=3",
              (NOW,))
    c.commit()

    j = cl.get("/api/memecoin/stuck-bags").get_json()

    assert j["unpriced"] == [1, 3]
    assert [p["id"] for p in j["stuck"]] == [2]
    assert c.execute("SELECT stuck_bag FROM memecoin_positions WHERE id=2").fetchone()[0] == 1

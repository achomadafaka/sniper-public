"""Stuck-bag sweep — detection + conditional redeploy recommendation."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_stuck_bags import stuck_bag_report

NOW = "2026-07-19T06:00:00+00:00"


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, pid, mint, *, net="solana", stuck=0):
    c.execute("INSERT INTO memecoin_positions(id, mint, ticker, entry_price, entry_notional, network_id, "
              "is_open, execution_mode, opened_at, stuck_bag) VALUES(?,?,?,?,?,?,1,'real',?,?)",
              (pid, mint, "T"+str(pid), 0.001, 50.0, net, NOW, stuck))


def test_detects_dead_and_runner_stuck_bags():
    c = _c()
    _open(c, 1, "DEADLIQ")                          # dead liquidity -> DEAD
    _open(c, 2, "STUCKFLAG", stuck=1)               # runner-flagged stuck
    _open(c, 3, "HEALTHY")                          # healthy -> not stuck
    c.commit()
    qmap = {("solana", "DEADLIQ"): {"price": 0.0005, "liquidity": 100.0, "market_cap": 1e5},
            ("solana", "STUCKFLAG"): {"price": 0.001, "liquidity": 40000.0, "market_cap": 5e5},
            ("solana", "HEALTHY"): {"price": 0.001, "liquidity": 80000.0, "market_cap": 5e5}}
    rep = stuck_bag_report(c, quote_map=qmap, max_slots=7, hot_setups=[], now_iso=NOW)
    assert {s["id"] for s in rep["stuck"]} == {1, 2}
    assert rep["slots_used"] == 3 and rep["slots_free"] == 4
    assert rep["recommendation"] is None            # slots free -> no cut urgency


def test_recommends_cut_only_when_slots_full_and_hot_setup_waiting():
    c = _c()
    for i in range(1, 8):                            # 7 open -> full
        _open(c, i, f"M{i}")
    c.commit()
    qmap = {("solana", "M1"): {"price": 0.0005, "liquidity": 100.0, "market_cap": 1e5}}  # M1 DEAD
    qmap.update({("solana", f"M{i}"): {"price": 0.001, "liquidity": 80000.0, "market_cap": 5e5}
                 for i in range(2, 8)})
    hot = [{"ticker": "GEM", "mint": "0xgem", "armed": 3, "hot": True, "setup_score": 80}]
    rep = stuck_bag_report(c, quote_map=qmap, max_slots=7, hot_setups=hot, now_iso=NOW)
    assert rep["slots_free"] == 0 and rep["blocked_setup"]["ticker"] == "GEM"
    assert rep["recommendation"] and "#1" in rep["recommendation"]   # cut the deadest (M1)


def test_no_recommendation_when_no_hot_setup_even_if_full():
    c = _c()
    for i in range(1, 8):
        _open(c, i, f"N{i}")
    c.commit()
    qmap = {("solana", "N1"): {"price": 0.0005, "liquidity": 100.0, "market_cap": 1e5}}
    rep = stuck_bag_report(c, quote_map=qmap, max_slots=7, hot_setups=[], now_iso=NOW)
    assert rep["slots_free"] == 0 and rep["stuck"] and rep["recommendation"] is None  # nothing to redeploy into


def test_reports_positions_it_cannot_price_instead_of_calling_them_healthy():
    # The radar quote daemon stopped 2026-08-21 and radar_quote_map went empty. DEAD fires only on a
    # KNOWN positive-but-tiny liquidity, so every position read "not stuck" and the endpoint answered
    # 200 with an empty list for two days. An unpriceable bag must be named, not counted healthy.
    c = _c()
    _open(c, 1, "NOQUOTE")
    _open(c, 2, "PRICED")
    c.commit()
    qmap = {("solana", "PRICED"): {"price": 0.001, "liquidity": 80000.0, "market_cap": 5e5}}

    rep = stuck_bag_report(c, quote_map=qmap, max_slots=7, hot_setups=[], now_iso=NOW)

    assert rep["stuck"] == []            # neither is DEAD on what we know
    assert rep["unpriced"] == [1]        # ...but #1 is unknown, not healthy


def test_a_known_liquidity_leaves_the_unpriced_list_empty():
    c = _c()
    _open(c, 1, "DEADLIQ")
    c.commit()
    qmap = {("solana", "DEADLIQ"): {"price": 0.0005, "liquidity": 100.0, "market_cap": 1e5}}

    rep = stuck_bag_report(c, quote_map=qmap, max_slots=7, hot_setups=[], now_iso=NOW)

    assert rep["unpriced"] == []
    assert [s["id"] for s in rep["stuck"]] == [1]   # known-tiny liquidity IS a write-off

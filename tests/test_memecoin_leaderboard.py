"""Leaderboard ingest: fomo's server-side lifetime totalPnL (top-50, full history,
NOT cap-limited) -> memecoin_leaderboard + analytics stamp + discovery candidates."""
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.feeds.fomo_account import fetch_leaderboard


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _entry(uid, handle, pnl, holdings_pnl, rank_vol=0):
    return {"id": uid, "userHandle": handle, "totalPnL": pnl,
            "totalVolume": rank_vol, "totalHoldings": len(holdings_pnl),
            "numTrades": 10, "swapCount": 20, "followers": 100,
            "topHoldings": [{"pnl": p} for p in holdings_pnl]}


def test_fetch_leaderboard_unwraps_response_object():
    def fetch(auth, path):
        assert path == "/v2/leaderboard?limit=50"
        return 200, {"responseObject": {"leaderboard": [{"id": "a"}, {"id": "b"}]}}, "d"
    lb = fetch_leaderboard(None, fetch=fetch)
    assert [e["id"] for e in lb] == ["a", "b"]


def test_fetch_leaderboard_empty_on_error():
    assert fetch_leaderboard(None, fetch=lambda a, p: (500, {}, "d")) == []


def test_ingest_writes_rows_stamps_analytics_and_seeds_candidates():
    c = _conn()
    entries = [_entry("u1", "change", 1_000_000.0, [500.0, -100.0]),   # unrealized = 400
               _entry("u2", "poorgoat", 500_000.0, [200.0])]
    res = m.ingest_leaderboard(c, entries, "2026-07-07T00:00:00Z")
    assert res["ranked"] == 2 and res["new_candidates"] == 2

    lb = {r["source_user_id"]: r for r in m.list_leaderboard(c)}
    assert lb["u1"]["rank"] == 1 and abs(lb["u1"]["total_pnl"] - 1_000_000.0) < 1e-6
    assert abs(lb["u1"]["unrealized_pnl"] - 400.0) < 1e-6      # sum of topHoldings pnl

    a = m.get_trader_analytics(c, "u1")
    assert a["on_leaderboard"] == 1 and a["leaderboard_rank"] == 1
    assert abs(a["fomo_total_pnl"] - 1_000_000.0) < 1e-6
    assert abs(a["fomo_unrealized_pnl"] - 400.0) < 1e-6

    cand = {r["source_user_id"]: r for r in m.list_candidates(c)}
    assert cand["u1"]["source"] == "leaderboard"


def test_ingest_resets_stale_leaderboard_flag():
    # a trader who drops out of the top-N must not keep on_leaderboard=1 / an old rank.
    c = _conn()
    m.ingest_leaderboard(c, [_entry("u1", "alice", 9.0, [1.0])], "2026-07-07T00:00:00Z")
    assert m.get_trader_analytics(c, "u1")["on_leaderboard"] == 1
    m.ingest_leaderboard(c, [_entry("u2", "bob", 9.0, [1.0])], "2026-07-08T00:00:00Z")  # u1 gone
    a = m.get_trader_analytics(c, "u1")
    assert a["on_leaderboard"] == 0 and a["leaderboard_rank"] is None
    assert m.get_trader_analytics(c, "u2")["on_leaderboard"] == 1


def test_ingest_skips_already_curated_trader_as_candidate():
    c = _conn()
    m.add_wallet(c, "u1", "change")                     # already curated
    res = m.ingest_leaderboard(c, [_entry("u1", "change", 9.0, [1.0])], "2026-07-07T00:00:00Z")
    # analytics still stamped, but NOT surfaced as a discovery candidate
    assert res["new_candidates"] == 0
    assert m.get_candidate(c, "u1") is None
    assert m.get_trader_analytics(c, "u1")["on_leaderboard"] == 1


def test_ingest_preserves_existing_candidate_status():
    c = _conn()
    # a large-buy candidate already ignored -> re-seeing on leaderboard must NOT reset to 'new'
    c.execute("INSERT INTO memecoin_candidates(source_user_id, handle, status, source) "
              "VALUES ('u1','change','ignored','large_buy')")
    m.ingest_leaderboard(c, [_entry("u1", "change", 9.0, [1.0])], "2026-07-07T00:00:00Z")
    assert m.get_candidate(c, "u1")["status"] == "ignored"
    assert m.get_candidate(c, "u1")["source"] == "large_buy"   # first-seen provenance kept


def test_ingest_flags_partial_unrealized():
    # trader holds 10 tokens but only 2 are in topHoldings -> unrealized sum is partial.
    c = _conn()
    e = {"id": "u1", "userHandle": "alice", "totalPnL": 100000.0, "totalHoldings": 10,
         "topHoldings": [{"pnl": 500.0}, {"pnl": 300.0}]}
    m.ingest_leaderboard(c, [e], "2026-07-07T00:00:00Z")
    a = m.get_trader_analytics(c, "u1")
    assert a["fomo_unrealized_partial"] == 1
    from sniper.analysis.fomo_account.selection_gate import selection_gate
    g = selection_gate(a)
    assert g["lifetime"]["unrealized_partial"] == 1   # caveat surfaced, not hidden

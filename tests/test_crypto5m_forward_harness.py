"""Phase 1D #5 — forward harness pure decision logic: build the known-at-time context, run all 5 rules,
execute the selected side on ITS OWN ask ladder (never 1-opposite), gate on book quality."""
import os

from sniper.algos.polymarket_crypto5m import forward_harness as FH
from sniper.algos.polymarket_crypto5m import forward_persistence as FPP
from sniper.algos.polymarket_crypto5m import persistence as P


UP_BOOK = {"best_bid": 0.60, "best_ask": 0.62, "bids": [[0.60, 800]], "asks": [[0.62, 1000]],
           "book_hash": "u", "book_integrity_status": "usable"}
DOWN_BOOK = {"best_bid": 0.38, "best_ask": 0.40, "bids": [[0.38, 200]], "asks": [[0.40, 1000]],
             "book_hash": "d", "book_integrity_status": "usable"}


def test_build_context():
    ctx = FH.build_context(binance_now=101.0, binance_30s_ago=100.0, up_book=UP_BOOK, down_book=DOWN_BOOK)
    assert ctx["up_midpoint"] == 0.61 and ctx["down_midpoint"] == 0.39
    assert ctx["binance_price_now"] == 101.0 and ctx["binance_price_30s_ago"] == 100.0
    assert ctx["up_bid_notional"] == 0.60 * 800 and ctx["up_ask_notional"] == 0.62 * 1000


def _records(ctx, *, book_quality="usable"):
    return {r["rule_id"]: r for r in FH.decide_market(
        condition_id="0x1", ctx=ctx, up_book=UP_BOOK, down_book=DOWN_BOOK, up_token="UP", down_token="DOWN",
        decision_ts=1240, fill_book_ts=1241, gross=10.0, min_order=5, latency_ms=500,
        book_quality=book_quality)}


def test_decide_market_entry_and_side_uses_own_ladder():
    ctx = FH.build_context(binance_now=101.0, binance_30s_ago=100.0, up_book=UP_BOOK, down_book=DOWN_BOOK)
    recs = _records(ctx)
    # R1 momentum up -> Up entry, filled on the UP token's asks (0.62), NOT 1-0.40
    assert recs["R1"]["status"] == "paper_entry" and recs["R1"]["selected_outcome"] == "Up"
    assert recs["R1"]["selected_token_id"] == "UP"
    assert abs(recs["R1"]["fill"]["best_ask"] - 0.62) < 1e-9
    # every rule produced exactly one record with a valid status
    assert set(recs) == {"C0", "C1", "R1", "R2", "R3"}
    for r in recs.values():
        assert r["status"] in ("paper_entry", "paper_no_trade", "blocked")


def test_c0_always_decides_a_side():
    ctx = FH.build_context(binance_now=None, binance_30s_ago=None, up_book=UP_BOOK, down_book=DOWN_BOOK)
    recs = _records(ctx)
    assert recs["C0"]["selected_outcome"] in ("Up", "Down")    # control trades regardless of signals
    assert recs["R1"]["status"] == "paper_no_trade"            # momentum unavailable -> no trade


def test_insufficient_depth_is_no_trade():
    thin_up = {**UP_BOOK, "asks": [[0.62, 5]]}                  # ~$3.1 of depth < $10
    ctx = FH.build_context(binance_now=101.0, binance_30s_ago=100.0, up_book=thin_up, down_book=DOWN_BOOK)
    recs = {r["rule_id"]: r for r in FH.decide_market(
        condition_id="0x1", ctx=ctx, up_book=thin_up, down_book=DOWN_BOOK, up_token="UP", down_token="DOWN",
        decision_ts=1240, fill_book_ts=1241, gross=10.0, min_order=5, latency_ms=500, book_quality="usable")}
    assert recs["R1"]["status"] == "paper_no_trade" and recs["R1"]["reason"] == "insufficient_depth"


def test_bad_book_quality_blocks_entries():
    ctx = FH.build_context(binance_now=101.0, binance_30s_ago=100.0, up_book=UP_BOOK, down_book=DOWN_BOOK)
    recs = _records(ctx, book_quality="quarantined")
    assert recs["R1"]["status"] == "blocked" and "quarantined" in recs["R1"]["reason"]


def _fwd_conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "f.sqlite"))
    FPP.ensure_forward_schema(conn)
    return conn


def _decide(ctx):
    return FH.decide_market(condition_id="0x1", ctx=ctx, up_book=UP_BOOK, down_book=DOWN_BOOK,
                            up_token="UP", down_token="DOWN", decision_ts=1240, fill_book_ts=1241,
                            gross=10.0, min_order=5, latency_ms=500, book_quality="usable")


def test_persist_and_settle_end_to_end(tmp_path):
    conn = _fwd_conn(tmp_path)
    ctx = FH.build_context(binance_now=101.0, binance_30s_ago=100.0, up_book=UP_BOOK, down_book=DOWN_BOOK)
    recs = _decide(ctx)
    FH.persist_records(conn, campaign_id="c", condition_id="0x1", rule_version="v0", checkpoint=240,
                       decision_ts=1240, records=recs, now_ts=1)
    FH.persist_records(conn, campaign_id="c", condition_id="0x1", rule_version="v0", checkpoint=240,
                       decision_ts=1240, records=recs, now_ts=2)   # idempotent re-run
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_forward_decisions").fetchone()[0] == 5
    FH.settle_market(conn, campaign_id="c", condition_id="0x1", winning_outcome="Up",
                     reconciliation_status="gamma_only", now_ts=3)
    row = conn.execute(
        "SELECT fs.pnl, fs.won FROM crypto5m_forward_fee_scenarios fs "
        "JOIN crypto5m_forward_entries e ON e.id=fs.entry_id "
        "WHERE e.rule_id='R1' AND fs.scenario='F1_documented'").fetchone()
    assert row["won"] == 1 and row["pnl"] > 0          # R1 bought Up, Up won, net positive at documented fee


def test_settle_quarantines_conflict(tmp_path):
    conn = _fwd_conn(tmp_path)
    ctx = FH.build_context(binance_now=101.0, binance_30s_ago=100.0, up_book=UP_BOOK, down_book=DOWN_BOOK)
    FH.persist_records(conn, campaign_id="c", condition_id="0x1", rule_version="v0", checkpoint=240,
                       decision_ts=1240, records=_decide(ctx), now_ts=1)
    FH.settle_market(conn, campaign_id="c", condition_id="0x1", winning_outcome=None,
                     reconciliation_status="conflict", now_ts=3)
    assert conn.execute("SELECT COUNT(*) FROM crypto5m_forward_fee_scenarios").fetchone()[0] == 0  # quarantined

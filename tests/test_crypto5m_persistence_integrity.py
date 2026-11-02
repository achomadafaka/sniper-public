"""Codex review fixes: settlement must not be downgraded/overwritten (label integrity), and a captured
priceToBeat oracle must not be erased by a later null upsert."""
import os

from sniper.algos.polymarket_crypto5m import persistence as P
from sniper.algos.polymarket_crypto5m.models import MarketSpec, Settlement


def _conn(tmp_path):
    conn = P.connect(os.path.join(str(tmp_path), "p.sqlite"))
    P.ensure_schema(conn)
    return conn


def _settle(cid, winner, status):
    tok = "UP" if winner == "Up" else "DN" if winner == "Down" else None
    op = ("1", "0") if winner == "Up" else ("0", "1") if winner == "Down" else ()
    return Settlement(cid, tok, winner, None, None, op, "gamma", status, None)


def _row(conn, cid):
    return conn.execute("SELECT winning_outcome, reconciliation_status FROM crypto5m_settlements "
                        "WHERE condition_id=?", (cid,)).fetchone()


def test_conflict_settlement_is_sticky(tmp_path):
    conn = _conn(tmp_path)
    P.record_settlement(conn, _settle("c", None, "conflict"), code_version="t", now_ts=1)
    P.record_settlement(conn, _settle("c", "Up", "gamma_only"), code_version="t", now_ts=2)
    r = _row(conn, "c")
    assert r["reconciliation_status"] == "conflict" and r["winning_outcome"] is None


def test_resolved_not_downgraded_to_unresolved(tmp_path):
    conn = _conn(tmp_path)
    P.record_settlement(conn, _settle("c", "Up", "gamma_only"), code_version="t", now_ts=1)
    P.record_settlement(conn, _settle("c", None, "unresolved"), code_version="t", now_ts=2)
    assert _row(conn, "c")["winning_outcome"] == "Up"


def test_differing_winners_become_conflict(tmp_path):
    conn = _conn(tmp_path)
    P.record_settlement(conn, _settle("c", "Up", "gamma_only"), code_version="t", now_ts=1)
    P.record_settlement(conn, _settle("c", "Down", "gamma_only"), code_version="t", now_ts=2)
    r = _row(conn, "c")
    assert r["reconciliation_status"] == "conflict" and r["winning_outcome"] is None


def test_same_winner_refresh_ok(tmp_path):
    conn = _conn(tmp_path)
    P.record_settlement(conn, _settle("c", "Up", "gamma_only"), code_version="t", now_ts=1)
    P.record_settlement(conn, _settle("c", "Up", "agree"), code_version="t", now_ts=2)
    assert _row(conn, "c")["winning_outcome"] == "Up"


def test_unresolved_upgrades_to_resolved(tmp_path):
    conn = _conn(tmp_path)
    P.record_settlement(conn, _settle("c", None, "unresolved"), code_version="t", now_ts=1)
    P.record_settlement(conn, _settle("c", "Up", "gamma_only"), code_version="t", now_ts=2)
    assert _row(conn, "c")["winning_outcome"] == "Up"


def _spec(cid="c", p2b=None):
    return MarketSpec(
        event_id="e", market_id="m", condition_id=cid, slug="btc-updown-5m-1000", question="q",
        raw_rule_text="r", rule_text_hash="h", resolution_source="src", series_id="s",
        market_created_ts=None, window_start_ts=1000, window_end_ts=1300, up_token_id="U",
        down_token_id="D", outcomes=("Up", "Down"), price_to_beat=p2b, fees_enabled=False,
        fee_type=None, taker_base_fee=None, maker_base_fee=None, tick_size=None, min_order_size=None)


def test_upsert_market_preserves_oracle_price_to_beat(tmp_path):
    conn = _conn(tmp_path)
    P.upsert_market(conn, _spec(p2b=63456.0), code_version="t", source_lineage="gamma", now_ts=1)
    P.upsert_market(conn, _spec(p2b=None), code_version="t", source_lineage="gamma", now_ts=2)
    val = conn.execute("SELECT price_to_beat FROM crypto5m_markets WHERE condition_id='c'").fetchone()[0]
    assert val == 63456.0          # a null incoming must not erase the captured oracle


def test_upsert_market_sets_oracle_when_provided(tmp_path):
    conn = _conn(tmp_path)
    P.upsert_market(conn, _spec(p2b=None), code_version="t", source_lineage="gamma", now_ts=1)
    P.upsert_market(conn, _spec(p2b=63456.0), code_version="t", source_lineage="gamma", now_ts=2)
    val = conn.execute("SELECT price_to_beat FROM crypto5m_markets WHERE condition_id='c'").fetchone()[0]
    assert val == 63456.0

"""The censored *_noquote paper closes carry a labelled mark-space bound beside the censor.

Coverage measured on the live ledger, 3,006 censored closes since 2026-08-29: the mark prices 91.5%
of them on 4663 and 1.7% on solana, and 65.3% of all rows have no price of any kind. The per-chain
split is asserted here because a single overall percentage hides the asymmetry that matters.
"""
import sqlite3

import pytest

from sniper.services import memecoin_paper_report as rep


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE memecoin_route_series (
        id INTEGER PRIMARY KEY AUTOINCREMENT, position_id INTEGER, mint TEXT, network_id TEXT,
        seam TEXT NOT NULL, observed_at TEXT NOT NULL, quoted_at TEXT,
        expected_usdc REAL NOT NULL, open_qty REAL, unit_price REAL)""")
    return conn


def _row(**fields):
    base = dict(id=1, network_id="solana", mint="M", closed_at="2026-09-22T12:00:00+00:00",
                last_quote_price=None, last_quote_ts=None,
                report_outcome_state="censored_noquote", report_conservative_pnl=-100.0,
                report_observed_open_qty=1000.0, report_pnl=None)
    base.update(fields)
    return base


def test_a_censored_row_with_a_mark_gets_a_labelled_bound_and_keeps_its_none_outcome():
    conn = _conn()
    row = _row(last_quote_price=0.05, last_quote_ts="2026-09-22T12:00:00+00:00")
    rep._price_censored(conn, [row], None)
    # conservative writes the bag off in full; the bound values it at the last price seen.
    assert row["report_priced_pnl"] == pytest.approx(-100.0 + 0.05 * 1000.0)
    assert row["report_priced_source"] == "mark"
    assert row["report_priced_age_s"] == 0.0
    assert row["report_pnl"] is None, "the measured outcome stays censored"
    assert row["report_conservative_pnl"] == -100.0, "the conservative end is untouched"


def test_a_stale_mark_reports_its_age_positive_means_older_than_the_close():
    conn = _conn()
    row = _row(last_quote_price=0.05, last_quote_ts="2026-09-22T11:58:20+00:00")
    rep._price_censored(conn, [row], None)
    assert row["report_priced_age_s"] == 100.0


def test_a_censored_row_with_no_mark_is_left_unpriced():
    conn = _conn()
    row = _row(last_quote_price=0.0)
    rep._price_censored(conn, [row], None)
    assert row["report_priced_pnl"] is None
    assert row["report_priced_source"] is None


def test_a_route_observation_is_a_signed_distance_and_never_a_price():
    """Alpha's scope call: a funded exit read of a different bag must not price this row."""
    conn = _conn()
    conn.execute("INSERT INTO memecoin_route_series(mint,network_id,seam,observed_at,expected_usdc,"
                 "open_qty,unit_price) VALUES('M','solana','exit_decision',"
                 "'2026-09-22T12:00:30+00:00',50.0,1000.0,0.05)")
    row = _row()
    rep._price_censored(conn, [row], None)
    assert row["report_route_observed_within_s"] == 30.0, "positive means after the close"
    assert row["report_priced_pnl"] is None, "a route quote must never become a price"
    assert row["report_priced_source"] is None


def test_a_route_observation_on_another_chain_does_not_cover_this_row():
    """Codex review 2026-09-23 R5: the join was by mint alone, so a Base quote for the same
    address made a Robinhood row read as covered. Same address, different chain, no coverage."""
    conn = _conn()
    conn.execute("INSERT INTO memecoin_route_series(mint,network_id,seam,observed_at,expected_usdc,"
                 "open_qty,unit_price) VALUES('0xsame','8453','exit_decision',"
                 "'2026-09-22T12:00:30+00:00',50.0,1000.0,0.05)")
    row = _row(network_id="4663", mint="0xsame")
    rep._price_censored(conn, [row], None)
    assert row["report_route_observed_within_s"] is None
    same = _row(network_id="8453", mint="0xsame")
    rep._price_censored(conn, [same], None)
    assert same["report_route_observed_within_s"] == 30.0


def test_the_nearest_route_observation_wins_even_when_it_precedes_the_close():
    conn = _conn()
    for stamp in ("2026-09-22T11:59:50+00:00", "2026-09-22T12:05:00+00:00"):
        conn.execute("INSERT INTO memecoin_route_series(mint,network_id,seam,observed_at,"
                     "expected_usdc,open_qty,unit_price) VALUES('M','solana','exit_decision',?,"
                     "50.0,1000.0,0.05)", (stamp,))
    row = _row()
    rep._price_censored(conn, [row], None)
    assert row["report_route_observed_within_s"] == -10.0


def test_a_route_row_without_a_unit_price_or_on_another_seam_is_not_an_observation():
    conn = _conn()
    conn.execute("INSERT INTO memecoin_route_series(mint,network_id,seam,observed_at,expected_usdc,"
                 "open_qty,unit_price) VALUES('M','solana','exit_audit',"
                 "'2026-09-22T12:00:05+00:00',50.0,1000.0,0.05)")
    conn.execute("INSERT INTO memecoin_route_series(mint,network_id,seam,observed_at,expected_usdc,"
                 "open_qty,unit_price) VALUES('M','solana','exit_decision',"
                 "'2026-09-22T12:00:06+00:00',50.0,1000.0,NULL)")
    row = _row()
    rep._price_censored(conn, [row], None)
    assert row["report_route_observed_within_s"] is None


def test_only_censored_noquote_rows_are_touched():
    conn = _conn()
    closed = _row(report_outcome_state="closed", report_pnl=12.0, last_quote_price=0.05)
    open_row = _row(report_outcome_state="marked_open", last_quote_price=0.05)
    rep._price_censored(conn, [closed, open_row], None)
    for row in (closed, open_row):
        assert "report_priced_pnl" not in row
    assert closed["report_pnl"] == 12.0


def test_coverage_reports_the_per_chain_split_not_only_a_total():
    conn = _conn()
    conn.execute("INSERT INTO memecoin_route_series(mint,network_id,seam,observed_at,expected_usdc,"
                 "open_qty,unit_price) VALUES('S2','solana','exit_decision',"
                 "'2026-09-22T12:00:30+00:00',50.0,1000.0,0.05)")
    rows = [
        _row(id=1, network_id="4663", mint="E1", last_quote_price=0.05,
             last_quote_ts="2026-09-22T12:00:00+00:00"),
        _row(id=2, network_id="4663", mint="E2", last_quote_price=0.05,
             last_quote_ts="2026-09-22T12:00:00+00:00"),
        _row(id=3, network_id="solana", mint="S1"),
        _row(id=4, network_id="solana", mint="S2"),
    ]
    rep._price_censored(conn, rows, None)
    coverage = rep._censored_price_coverage(rows)
    assert coverage["censored_noquote"] == 4
    assert coverage["censored_priced_mark"] == 2
    assert coverage["censored_route_observed"] == 1
    assert coverage["censored_unpriced"] == 1
    assert coverage["censored_priced_by_chain"] == {
        "4663": {"censored": 2, "priced_mark": 2, "route_observed": 0, "unpriced": 0},
        "solana": {"censored": 2, "priced_mark": 0, "route_observed": 1, "unpriced": 1},
    }


def test_the_batched_read_issues_one_query_per_chunk_not_one_per_row():
    """The research-load rule: no per-row query against the route series."""
    conn = _conn()
    queries = []
    real = conn.execute

    class Counting:
        def execute(self, sql, *args):
            queries.append(sql)
            return real(sql, *args)

    rows = [_row(id=i, mint="M%d" % i) for i in range(50)]
    rep._price_censored(Counting(), rows, None)
    assert len(queries) == 1, "50 censored rows must cost one read, not 50"

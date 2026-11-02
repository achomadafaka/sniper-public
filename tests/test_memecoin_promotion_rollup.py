"""The promotion rollup counts what the gates saw, not what we kept."""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_promotion_lane_table(conn)
    return conn


def _row(**over):
    base = dict(attempt_id="a", network_id="solana", mint="M", source_user_id="u1",
                source_handle="alice", verdict="admitted",
                decision_started_at="2026-09-23T12:00:00+00:00",
                evaluated_at="2026-09-23T12:01:00+00:00", eval_lag_s=60.0,
                created_at="2026-09-23T12:01:00+00:00")
    base.update(over)
    return base


def test_unreadable_is_never_counted_as_refused():
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(attempt_id="a1", verdict="refused",
                                           refused_family="source_buy_floor"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a2", verdict="unreadable"))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["seen"] == 2
    assert wallet["refused"] == 1
    assert wallet["unreadable"] == 1


def test_a_censored_row_counts_as_admitted_and_never_as_a_close():
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(attempt_id="a1", exit_reason="open_at_horizon"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a2", exit_reason="tp", pnl_usd=10.0))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["admitted"] == 2
    assert wallet["censored"] == 1
    assert wallet["closed"] == 1
    assert wallet["sum_pnl"] == pytest.approx(10.0)


def test_settled_unquoted_row_is_censored_not_pending():
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(
        fill_basis="unquoted", settled_at="2026-09-23T13:00:00+00:00"))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert (wallet["censored"], wallet["pending"], wallet["closed"]) == (1, 0, 0)


def test_rollup_names_the_model_basis_of_closed_pnl():
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(
        exit_reason="tp", pnl_usd=10, pnl_basis="buy_mark_model"))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["pnl_bases"] == {"buy_mark_model": 1}


def test_the_denominator_is_every_evaluated_row():
    conn = _conn()
    for i, verdict in enumerate(("admitted", "refused", "unreadable", "refused")):
        m.record_promotion_lane_row(conn, _row(attempt_id="a%d" % i, verdict=verdict,
                                               refused_family="source_buy_floor"))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["seen"] == 4
    assert wallet["admit_pct"] == pytest.approx(25.0)


def test_the_median_leads_and_the_mean_is_beside_it():
    conn = _conn()
    for i, pnl in enumerate((-1.0, 2.0, 300.0)):   # three DISTINCT coins (per-coin grain)
        m.record_promotion_lane_row(conn, _row(attempt_id="a%d" % i, mint="M%d" % i,
                                               exit_reason="tp", pnl_usd=pnl))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["median_pnl"] == pytest.approx(2.0)
    assert wallet["mean_pnl"] == pytest.approx(100.33333, rel=1e-4)


def test_the_family_panel_pools_refusals_across_wallets():
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(attempt_id="a1", source_user_id="u1",
                                           verdict="refused", refused_family="coin_band_unreservable"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a2", source_user_id="u2",
                                           verdict="refused", refused_family="coin_band_unreservable"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a3", source_user_id="u2",
                                           verdict="refused", refused_family="source_buy_floor"))
    families = {f["family"]: f for f in m.promotion_lane_rollup(conn)["families"]}
    assert families["coin_band_unreservable"]["n"] == 2
    assert families["source_buy_floor"]["n"] == 1


def test_the_lag_is_reported_so_no_gate_result_is_quoted_without_it():
    conn = _conn()
    for i, lag in enumerate((10.0, 60.0, 900.0)):
        m.record_promotion_lane_row(conn, _row(attempt_id="a%d" % i, eval_lag_s=lag))
    lag = m.promotion_lane_rollup(conn)["lag"]
    assert lag["n"] == 3
    assert lag["p50_s"] == pytest.approx(60.0)


def test_rollup_names_when_its_latest_evaluation_happened():
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(attempt_id="a1",
                                           evaluated_at="2026-09-23T12:01:00+00:00"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a2",
                                           evaluated_at="2026-09-23T12:05:00+00:00"))
    assert m.promotion_lane_rollup(conn)["window"]["latest_evaluated_at"] == \
        "2026-09-23T12:05:00+00:00"


def test_an_empty_table_reports_nothing_rather_than_zeroes_that_read_as_a_measurement():
    out = m.promotion_lane_rollup(_conn())
    assert out["wallets"] == [] and out["families"] == []
    assert out["lag"]["n"] == 0 and out["lag"]["p50_s"] is None


# Two departures from the plan's draft implementation, each asserted rather than claimed.


def test_an_unsettled_row_is_pending_and_not_a_censoring():
    """`censored` is a DECIDED fact; an unpriced row is an unknown.

    The draft counted any admitted row with no `pnl_usd` as censored, which makes every row the
    settle pass has not reached yet look like a horizon that expired with the position open. The
    reach value here is the exit_reason: `None` (never settled) against `open_at_horizon`. Give
    both rows the same exit_reason and the two rules agree, and the test proves nothing.
    """
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(attempt_id="a1", exit_reason=None, pnl_usd=None))
    m.record_promotion_lane_row(conn, _row(attempt_id="a2", exit_reason="open_at_horizon"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a3", exit_reason="tp", pnl_usd=4.0))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["admitted"] == 3
    assert wallet["pending"] == 1
    assert wallet["censored"] == 1
    assert wallet["closed"] == 1


def test_one_wallet_on_two_chains_is_two_rows():
    """Chains are never pooled, so the key is (wallet, chain) and not the wallet alone."""
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(attempt_id="a1", network_id="solana"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a2", network_id="4663"))
    wallets = m.promotion_lane_rollup(conn)["wallets"]
    assert len(wallets) == 2
    assert {w["network_id"] for w in wallets} == {"solana", "4663"}
    assert all(w["seen"] == 1 for w in wallets)
    only_solana = m.promotion_lane_rollup(conn, chain="solana")["wallets"]
    assert len(only_solana) == 1 and only_solana[0]["network_id"] == "solana"


def test_canonical_chain_includes_its_stored_network_ids():
    conn = _conn()
    m.record_promotion_lane_row(conn, _row(attempt_id="a1", network_id="4663"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a2", network_id="robin"))
    m.record_promotion_lane_row(conn, _row(attempt_id="a3", network_id="solana"))
    out = m.promotion_lane_rollup(conn, chain="robin")
    assert len(out["wallets"]) == 1
    assert out["wallets"][0]["seen"] == 2
    assert out["wallets"][0]["network_id"] == "4663"


def test_read_failure_is_not_reported_as_an_empty_cohort():
    conn = _conn()
    conn.execute("DROP TABLE memecoin_promotion_lane")
    with pytest.raises(sqlite3.OperationalError):
        m.promotion_lane_rollup(conn)


def test_promotion_reads_legacy_rows_before_the_writer_migrates_optional_columns():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_promotion_lane ("
                 "attempt_id TEXT, source_user_id TEXT, source_handle TEXT, network_id TEXT, "
                 "mint TEXT, ticker TEXT, decision_started_at TEXT, evaluated_at TEXT, "
                 "eval_lag_s REAL, verdict TEXT, refused_family TEXT, refused_reason TEXT, "
                 "fill_basis TEXT, exit_reason TEXT, censored_by TEXT, pnl_usd REAL)")
    conn.execute("INSERT INTO memecoin_promotion_lane "
                 "(attempt_id, source_user_id, network_id, mint, decision_started_at, "
                 "evaluated_at, verdict, fill_basis) VALUES "
                 "('a1','u1','solana','M','2026-09-23T12:00:00+00:00',"
                 "'2026-09-23T12:01:00+00:00','admitted','none')")
    wallet = m.promotion_lane_rollup(conn, chain="solana")["wallets"][0]
    assert (wallet["admitted"], wallet["pending"], wallet["closed"]) == (1, 1, 0)
    signals = m.promotion_lane_signals(conn, "u1", chain="solana",
                                       since="2026-09-23T00:00:00+00:00")
    assert len(signals) == 1
    assert signals[0]["pnl_basis"] is None and signals[0]["settled_at"] is None


def test_the_top_family_does_not_depend_on_insertion_order():
    conn = _conn()
    for i, family in enumerate(("zeta", "alpha", "alpha", "zeta")):
        m.record_promotion_lane_row(conn, _row(attempt_id="a%d" % i, verdict="refused",
                                               refused_family=family))
    assert m.promotion_lane_rollup(conn)["wallets"][0]["top_family"] == "alpha"


def test_repeated_signals_on_one_coin_are_one_outcome():
    """The promotion unit is one distinct coin per wallet and chain: three closes on one mint
    are summed into ONE outcome, so one coin cannot dominate the median or the win rate."""
    conn = _conn()
    for i, pnl in enumerate((5.0, 5.0, -1.0)):
        m.record_promotion_lane_row(conn, _row(attempt_id="a%d" % i, mint="M1", exit_reason="tp",
                                               pnl_usd=pnl))
    m.record_promotion_lane_row(conn, _row(attempt_id="b", mint="M2", exit_reason="stop",
                                           pnl_usd=-2.0,
                                           decision_started_at="2026-09-24T12:00:00+00:00"))
    wallet = m.promotion_lane_rollup(conn)["wallets"][0]
    assert wallet["closed"] == 2
    assert wallet["sum_pnl"] == pytest.approx(7.0)
    assert wallet["win_pct"] == pytest.approx(50.0)
    assert (wallet["coins_admitted"], wallet["days_admitted"]) == (2, 2)

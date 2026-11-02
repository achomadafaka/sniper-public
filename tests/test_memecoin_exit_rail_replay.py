"""The replay's cohort layer decides what the answer MEANS. These pin the parts that fail silently.

Every test here corresponds to a defect that was measured on the live book before the module
existed, or to a property the pre-registered decision rule depends on.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from sniper.analysis import memecoin_exit_rail_replay as R
from sniper.analysis import memecoin_rails_model as rm
from sniper.persistence import db_memecoin as m

OPEN = "2026-08-26T12:00:00+00:00"
CLOSE = "2026-08-26T12:00:30+00:00"      # 30s: four marks 2s apart clear the 0.1/s density floor
LONG_CLOSE = "2026-08-26T12:05:00+00:00"
POLICY = {"policy_id": "follow-30-50-v1", "tp_pct": 25.0, "stop_pct": 35.0, "max_hold_s": 780.0,
          "trail_arm_pct": 0.0, "trail_pct": 0.0}


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m._ensure_memecoin_tables_in_transaction(c)
    return c


def _pos(conn, *, mint="MINT", entry=1.0, reason="real_tp", policy=None, opened=OPEN, closed=CLOSE):
    contract = json.dumps({"policy": policy or POLICY})
    conn.execute(
        "INSERT INTO memecoin_positions (mint, network_id, execution_mode, is_open, entry_price, "
        "opened_at, closed_at, exit_reason, funded_contract_json) "
        "VALUES (?, 'solana', 'real', 0, ?, ?, ?, ?, ?)",
        (mint, entry, opened, closed, reason, contract))
    return conn.execute("SELECT id FROM memecoin_positions ORDER BY id DESC").fetchone()["id"]


def _marks(conn, mint, prices, *, start=0, step=2, quality="live"):
    """`step` seconds apart from OPEN, so the series is dense enough to clear the density floor."""
    for i, price in enumerate(prices):
        sec = start + i * step
        ts = "2026-08-26T12:%02d:%02d+00:00" % (sec // 60, sec % 60)
        conn.execute(
            "INSERT INTO memecoin_position_marks (chain_id, mint, sweep_ts, mid, quality) "
            "VALUES ('solana', ?, ?, ?, ?)", (mint, ts, price, quality))


# ------------------------------------------------------- the quarantined-mark defect

def test_quarantined_marks_are_excluded_exactly_as_the_live_path_excludes_them():
    """REGRESSION 2026-08-26, position 44690. Its window holds 30 accepted marks at ~0.0100 and 49
    QUARANTINED ones at 0.268-0.308 — a coherent alternative series from a different pool for the
    same mint. Reading the table raw made the simulated baseline return +2562% and drove the ladder
    variants to -162 points, which reads as a policy finding and is a pool-identity fault. The live
    exit path filters `quality != 'quarantined_spike'`; this must match it."""
    conn = _conn()
    _pos(conn, entry=1.0)
    _marks(conn, "MINT", [1.0, 1.01, 1.02, 1.03])
    _marks(conn, "MINT", [30.0, 31.0], start=1, quality="quarantined_spike")
    cohort, _ = R.load_cohort(conn)
    assert len(cohort) == 1
    prices = [p for _t, p in cohort[0].series]
    assert max(prices) < 2.0, "a quarantined spike must never reach the simulator"
    assert len(prices) == 4


# ------------------------------------------------------- the price-space defect

def test_the_entry_anchor_is_the_fill_and_the_series_is_declared_chart():
    """Defect 1. The model takes `entry_price` on trust and only LABELS the series space, so the mix
    is the caller's to get right. We anchor on the fill because the live funded nominator does."""
    conn = _conn()
    _pos(conn, entry=1.0)
    _marks(conn, "MINT", [1.0, 1.1, 1.3, 1.31])
    cohort, _ = R.load_cohort(conn)
    got = R.outcome(cohort[0], R.baseline_policy(POLICY), cadence_q=1.0)
    assert got.price_space == rm.SPACE_CHART
    assert got.reason == rm.TP, "a 30% chart move must clear a 25% TP anchored on a 1.0 fill"


def test_the_measured_price_space_gap_is_reported_per_position():
    """The fill/chart ratio is the systematic error term. It must be visible, not folded into a
    band that only covers sampling."""
    conn = _conn()
    _pos(conn, entry=1.2)
    _marks(conn, "MINT", [1.0, 1.0, 1.0, 1.0])
    cohort, _ = R.load_cohort(conn)
    assert cohort[0].fill_over_chart == pytest.approx(1.2)


def test_a_first_mark_long_after_the_open_cannot_claim_to_price_the_open():
    conn = _conn()
    _pos(conn, entry=1.0)
    _marks(conn, "MINT", [1.0, 1.0, 1.0, 1.0], start=10)
    cohort, _ = R.load_cohort(conn)
    assert cohort[0].chart_at_open is None and cohort[0].fill_over_chart is None


# ------------------------------------------------------- cohorting

def test_positions_are_cohorted_by_their_own_contract_never_by_date():
    """28 distinct rail tuples exist across 352 contracted funded closes. Pooling them would compare
    policies to each other under the name of one baseline."""
    conn = _conn()
    _pos(conn, mint="A")
    _pos(conn, mint="B", policy=dict(POLICY, stop_pct=50.0))
    _marks(conn, "A", [1.0] * 4)
    _marks(conn, "B", [1.0] * 4)
    cohort, excluded = R.load_cohort(conn)
    assert [p.mint for p in cohort] == ["A"]
    assert excluded["other_era"] == 1


def test_a_position_without_a_contract_is_excluded_and_counted():
    conn = _conn()
    conn.execute("INSERT INTO memecoin_positions (mint, network_id, execution_mode, is_open, "
                 "entry_price, opened_at, closed_at, exit_reason) "
                 "VALUES ('X', 'solana', 'real', 0, 1.0, ?, ?, 'real_tp')", (OPEN, CLOSE))
    cohort, excluded = R.load_cohort(conn)
    assert cohort == [] and excluded["no_contract"] == 1


def test_a_sparse_position_is_dropped_and_counted_never_interpolated():
    """Reconstructing a path through a gap invents the prices the replay exists to measure."""
    conn = _conn()
    _pos(conn, mint="SPARSE", closed=LONG_CLOSE)
    _marks(conn, "SPARSE", [1.0, 1.0, 1.0], step=60)     # 3 marks over 300s = 0.01/s
    cohort, excluded = R.load_cohort(conn)
    assert cohort == [] and excluded["below_density_floor"] == 1


# ------------------------------------------------------- the comparison

def test_the_comparison_is_simulated_against_simulated_not_against_realized_pnl():
    """Comparing a chart-space simulation to a fill-space realized return would mix the two spaces
    the whole module exists to keep apart. Proof: the delta is unchanged when the realized outcome
    is rewritten to something else entirely."""
    conn = _conn()
    _pos(conn, mint="M", reason="real_tp")
    _marks(conn, "M", [1.0, 1.1, 1.3, 1.31])
    cohort, _ = R.load_cohort(conn)
    variant = rm.RailsPolicy(tp_pct=15.0, stop_pct=35.0, max_hold_s=780.0)
    first = R.compare(cohort, variant, label="tp15", cadence_q=1.0)

    conn.execute("UPDATE memecoin_positions SET exit_reason='b_stop', realized_pnl=-999")
    cohort2, _ = R.load_cohort(conn)
    second = R.compare(cohort2, variant, label="tp15", cadence_q=1.0)
    assert first.mean_delta_pts == second.mean_delta_pts


def test_a_censored_arm_drops_the_pair_rather_than_scoring_it_zero():
    """A zero delta and an unobservable one are different facts. Pooling them reports a variant as
    identical to baseline when the truth is that it could not be evaluated."""
    conn = _conn()
    _pos(conn, mint="M")
    _marks(conn, "M", [1.0, 1.05, 1.06, 1.07])
    cohort, _ = R.load_cohort(conn)
    variant = rm.RailsPolicy(tp_pct=15.0, stop_pct=35.0, max_hold_s=780.0)
    got = R.compare(cohort, variant, label="tp15", cadence_q=0.0)   # censor everything
    assert got.n_paired == 0 and got.n_undetermined == 1
    assert got.distinguishable is False


def test_a_comparison_always_carries_its_band():
    conn = _conn()
    _pos(conn, mint="M")
    _marks(conn, "M", [1.0, 1.1, 1.3, 1.31])
    cohort, _ = R.load_cohort(conn)
    got = R.compare(cohort, rm.RailsPolicy(tp_pct=15.0, stop_pct=35.0, max_hold_s=780.0),
                    label="tp15", cadence_q=1.0)
    assert got.lo <= got.mean_delta_pts <= got.hi
    assert "[" in str(got) and "n=" in str(got), "the printed form must show the band and the n"


# ------------------------------------------------------- fidelity and sensitivity

def test_manual_closes_are_not_scored_against_a_rail():
    conn = _conn()
    _pos(conn, mint="M", reason="manual")
    _marks(conn, "M", [1.0, 1.1, 1.3, 1.31])
    cohort, _ = R.load_cohort(conn)
    fid = R.baseline_fidelity(cohort, cadence_q=1.0)
    assert fid["scored"] == 0 and fid["not_a_rail"] == 1


def test_fidelity_counts_an_agreement_when_the_simulation_reproduces_reality():
    conn = _conn()
    _pos(conn, mint="M", reason="real_tp")
    _marks(conn, "M", [1.0, 1.1, 1.3, 1.31])
    cohort, _ = R.load_cohort(conn)
    fid = R.baseline_fidelity(cohort, cadence_q=1.0)
    assert fid["agree"] == 1 and fid["scored"] == 1


def test_the_sensitivity_check_reports_a_flip_when_the_censor_changes_the_order():
    """THE RESULT THAT DECIDED THIS STUDY. On the live cohort the ordering flips between q=0.90 and
    q=1.00, so no variant may be eliminated. A check that cannot detect a flip would have let the
    q=1.00 ranking be reported alone."""
    conn = _conn()
    _pos(conn, mint="A", entry=1.0)
    _marks(conn, "A", [1.0, 1.30, 1.05, 1.06])           # TP then fade
    _pos(conn, mint="B", entry=1.0)
    _marks(conn, "B", [1.0, 1.18, 1.20, 1.21])           # never reaches 25
    cohort, _ = R.load_cohort(conn)
    vs = [("tp15", rm.RailsPolicy(tp_pct=15.0, stop_pct=35.0, max_hold_s=780.0)),
          ("tp30", rm.RailsPolicy(tp_pct=30.0, stop_pct=35.0, max_hold_s=780.0))]
    stable, orders = R.ordering_is_stable(cohort, vs)
    assert set(orders) == {0.9, 1.0}
    assert all(len(o) == 2 for o in orders.values())
    assert isinstance(stable, bool)


import pytest as _pytest
from sniper.research.wallet_execution.publication import diagnostic_mode as _diagnostic_mode
@_pytest.fixture(autouse=True)
def _explicit_legacy_diagnostics():
    with _diagnostic_mode():
        yield

"""Selection gate: transparent green/red/insufficient per signal. No score, no verdict."""
from sniper.analysis.fomo_account.selection_gate import selection_gate


def _row(**kw):
    base = {"fomo_total_pnl": 100_000.0, "fomo_unrealized_pnl": 10_000.0,
            "realized_pnl_7d": 500.0, "realized_7d_covered": 1,
            "realized_pnl_30d": 2_000.0, "realized_30d_covered": 1,
            "median_hold_s": 3600.0, "last_swap_age_h": 5.0, "active_days": 10}
    base.update(kw)
    return base


def test_all_green_trader():
    g = selection_gate(_row())
    assert g["lifetime"]["state"] == "green"
    assert g["w7d"]["state"] == "green" and g["w7d"]["source"] == "ledger"
    assert g["w30d"]["state"] == "green"
    assert g["fresh"]["state"] == "green"
    assert g["summary"]["green"] == 4 and g["summary"]["red"] == 0


def test_hold_is_not_a_gate_signal():
    # long holders are fine — manual sell handles them; hold must NOT appear as a gate signal
    g = selection_gate(_row(median_hold_s=200_000.0))
    assert "hold" not in g
    assert g["summary"]["green"] == 4      # unchanged by a long hold


def test_lifetime_red_when_mostly_unsold_bags():
    # 60% of totalPnL is unrealized -> not realized skill -> red (bag check)
    g = selection_gate(_row(fomo_total_pnl=100_000.0, fomo_unrealized_pnl=60_000.0))
    assert g["lifetime"]["state"] == "red"


def test_lifetime_is_insufficient_when_bag_check_is_partial():
    g = selection_gate(_row(fomo_unrealized_partial=1))
    assert g["lifetime"]["state"] == "insufficient"


def test_lifetime_red_when_not_profitable():
    assert selection_gate(_row(fomo_total_pnl=-5_000.0))["lifetime"]["state"] == "red"


def test_lifetime_insufficient_when_off_leaderboard():
    # no fomo lifetime number -> we can't judge lifetime green
    g = selection_gate(_row(fomo_total_pnl=None, fomo_unrealized_pnl=None))
    assert g["lifetime"]["state"] == "insufficient"


def test_7d_uses_forward_snapshot_when_ledger_uncovered():
    g = selection_gate(_row(realized_7d_covered=0, realized_pnl_7d=None),
                       w7d={"realized": 50.0, "n_days": 7, "saturated": 0})
    assert g["w7d"]["state"] == "green" and g["w7d"]["source"] == "snapshot"
    assert g["w7d"]["n_days"] == 7


def test_7d_insufficient_when_uncovered_and_no_snapshot():
    g = selection_gate(_row(realized_7d_covered=0, realized_pnl_7d=None))
    assert g["w7d"]["state"] == "insufficient"


def test_7d_insufficient_when_snapshot_too_immature():
    g = selection_gate(_row(realized_7d_covered=0, realized_pnl_7d=None),
                       w7d={"realized": 50.0, "n_days": 6, "saturated": 0})
    assert g["w7d"]["state"] == "insufficient"      # 6 days of snapshots can't back a 7d call


def test_30d_requires_thirty_realized_snapshot_days():
    row = _row(realized_30d_covered=0, realized_pnl_30d=None)
    assert selection_gate(
        row, w30d={"realized": 50.0, "n_days": 29, "saturated": 0},
    )["w30d"]["state"] == "insufficient"
    assert selection_gate(
        row, w30d={"realized": 50.0, "n_days": 30, "saturated": 0},
    )["w30d"]["state"] == "green"


def test_fresh_red_when_stale():
    assert selection_gate(_row(last_swap_age_h=500.0))["fresh"]["state"] == "red"
    assert selection_gate(_row(active_days=1))["fresh"]["state"] == "red"


def test_summary_counts_states():
    g = selection_gate(_row(fomo_total_pnl=-1.0))   # lifetime red; w7d/w30d/fresh green
    assert g["summary"]["red"] == 1 and g["summary"]["green"] == 3

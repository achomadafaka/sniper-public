"""Two-stage (calibration/holdout) boundary-rule selection — no in-sample selection on the holdout."""
from __future__ import annotations

from decimal import Decimal

from sniper.algos.polymarket_crypto5m import calibration as CAL
from sniper.algos.polymarket_crypto5m import price_to_beat_resolver as P2B


def _row(errs):
    """A per-window comparison: {rule: abs_error(Decimal) or None-if-missing}."""
    return {rule: {"missing": errs.get(rule) is None, "abs_error": errs.get(rule)}
            for rule in P2B.CANDIDATE_RULES}


def test_match_rates_per_precision():
    rows = [_row({"last_at_or_before": Decimal("0"), "first_at_or_after": Decimal("0.005"),
                  "nearest": Decimal("0")}) for _ in range(10)]
    tight = CAL.match_rates(rows, precision=Decimal("0.001"))
    assert tight["last_at_or_before"] == 1.0
    assert tight["first_at_or_after"] == 0.0           # 0.005 > 0.001
    loose = CAL.match_rates(rows, precision=Decimal("0.01"))
    assert loose["first_at_or_after"] == 1.0           # 0.005 <= 0.01


def test_match_rates_ignores_missing_rows():
    rows = [_row({"last_at_or_before": Decimal("0")}),          # only last present
            _row({"last_at_or_before": None})]                  # missing
    r = CAL.match_rates(rows, precision=Decimal("0.01"))
    assert r["last_at_or_before"] == 1.0                # 1 of 1 usable matched (missing excluded)


def test_select_rule_picks_strictest_precision_that_clears_gate():
    rows = [_row({"last_at_or_before": Decimal("0"), "nearest": Decimal("0.005")}) for _ in range(100)]
    rule, prec = CAL.select_rule(rows, gate=0.99)
    assert rule == "last_at_or_before"                 # clears at the strictest precision
    assert prec == Decimal("0.0001")


def test_two_stage_locks_when_holdout_passes():
    cal = [_row({"last_at_or_before": Decimal("0"), "nearest": Decimal("0"),
                 "first_at_or_after": Decimal("0.005")}) for _ in range(3)]
    holdout = [_row({"last_at_or_before": Decimal("0")}) for _ in range(3)]
    out = CAL.two_stage(cal + holdout, cal_size=3, gate=0.99)
    assert out["status"] == "locked"
    assert out["selected_rule"] == "last_at_or_before"
    assert out["holdout"]["match_rate"] == 1.0
    # the module constant is NOT auto-mutated — locking is an operator action
    assert P2B.LOCKED_BOUNDARY_RULE is None


def test_two_stage_rejects_when_holdout_fails():
    cal = [_row({"last_at_or_before": Decimal("0")}) for _ in range(3)]       # looks perfect in-sample
    holdout = [_row({"last_at_or_before": Decimal("0.5")}) for _ in range(3)] # fails out-of-sample
    out = CAL.two_stage(cal + holdout, cal_size=3, gate=0.99)
    assert out["status"] == "rejected"
    assert out["holdout"]["match_rate"] == 0.0


def test_two_stage_insufficient_windows():
    rows = [_row({"last_at_or_before": Decimal("0")}) for _ in range(5)]
    out = CAL.two_stage(rows, cal_size=100, gate=0.99)
    assert out["status"] == "insufficient_windows"
    assert out["selected_rule"] is None


def test_two_stage_ignores_all_missing_windows_in_split():
    # all-missing (no candidate at all) windows must NOT consume calibration/holdout slots
    u = _row({"last_at_or_before": Decimal("0")})
    x = _row({})   # all three rules missing
    out = CAL.two_stage([u, u] + [x] * 100 + [u, u], cal_size=2, gate=0.99)
    assert out["status"] == "locked"                # the 4 usable windows form cal+holdout, not the x's


def test_two_stage_min_usable_floor_blocks_degenerate_lock():
    # a holdout whose SELECTED rule has too few usable windows must not lock (no n=1 lock)
    cal = [_row({"last_at_or_before": Decimal("0")}) for _ in range(3)]
    holdout = [_row({"last_at_or_before": Decimal("0")}),    # 1 usable for last_at_or_before
               _row({"nearest": Decimal("0")}),              # present (survives filter) but last missing
               _row({"nearest": Decimal("0")})]
    out = CAL.two_stage(cal + holdout, cal_size=3, gate=0.99)
    assert out["status"] != "locked"                # floor (>=2 usable) not met on holdout
    assert out["holdout"]["n_usable"] == 1


def test_two_stage_sorts_by_window_start_ts_when_present():
    # when rows carry window_start_ts the split must be time-ordered regardless of input order
    def row(ts, err):
        r = _row({"last_at_or_before": Decimal(err)})
        r["window_start_ts"] = ts
        return r
    # shuffled in: a bad early window + good later ones
    rows = [row(50, "5"), row(10, "0"), row(20, "0"), row(40, "0"), row(30, "0")]
    out = CAL.two_stage(rows, cal_size=2, gate=0.99)
    # sorted by ts: [10,20 | 30,40] cal=ts10/20 (err0), holdout=ts30/40 (err0); the ts=50 err=5 is last
    assert out["status"] == "locked"


def test_multi_precision_report_covers_all_three():
    rows = [_row({"last_at_or_before": Decimal("0.0005")}) for _ in range(4)]
    rep = CAL.multi_precision_report(rows)
    assert set(rep.keys()) == {Decimal("0.0001"), Decimal("0.001"), Decimal("0.01")}
    assert rep[Decimal("0.0001")]["last_at_or_before"] == 0.0   # 0.0005 > 0.0001
    assert rep[Decimal("0.001")]["last_at_or_before"] == 1.0    # 0.0005 <= 0.001

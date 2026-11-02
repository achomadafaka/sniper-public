"""TDD for the drift-reach validation methods: the window-clustered Spearman that decides whether the
measured-drift expected move predicts the realized move (where the driftless gate scored rho~0)."""
import random
import sqlite3

import pytest

from sniper.algos.polymarket_crypto5m import drift_reach_log as DL
from scripts import crypto5m_drift_reach_validation as V


def test_spearman_monotone_and_null():
    assert V.spearman([1, 2, 3, 4, 5], [2, 4, 6, 8, 10]) == pytest.approx(1.0)      # perfect monotone
    assert V.spearman([1, 2, 3, 4, 5], [10, 8, 6, 4, 2]) == pytest.approx(-1.0)
    assert V.spearman([1, 1, 1], [2, 3, 4]) == 0.0                                  # no variance -> 0
    assert V.spearman([1, 2], [1, 2]) is None                                       # too few


def test_clustered_bootstrap_detects_positive_signal():
    # 12 windows, each with a clean positive exp_fav_move -> realized relationship
    by_window = {}
    rng = random.Random(0)
    for w in range(12):
        rows = []
        for _ in range(8):
            x = rng.uniform(-0.02, 0.02)
            rows.append({"exp_fav_move": x, "realized_mid_move": x + rng.uniform(-0.002, 0.002)})
        by_window[w] = rows
    rho, lo, hi = V.clustered_bootstrap_rho(by_window, random.Random(1))
    assert rho > 0.5 and lo > 0                                                     # CI excludes 0, positive


def test_clustered_bootstrap_null_includes_zero():
    by_window = {}
    rng = random.Random(2)
    for w in range(12):
        rows = [{"exp_fav_move": rng.uniform(-1, 1), "realized_mid_move": rng.uniform(-1, 1)} for _ in range(8)]
        by_window[w] = rows
    rho, lo, hi = V.clustered_bootstrap_rho(by_window, random.Random(3))
    assert lo <= 0 <= hi                                                            # null -> CI spans 0


def test_gate_accruing_below_milestone(tmp_path):
    db = str(tmp_path / "d.sqlite")
    c = sqlite3.connect(db); c.row_factory = sqlite3.Row
    DL.ensure_schema(c)
    # one complete row -> far below the 150 milestone -> accruing, never crashes at low n
    c.execute("INSERT INTO crypto5m_drift_reach (window_start_ts, exp_fav_move, realized_mid_move, status) "
              "VALUES (1, 0.01, 0.012, 'complete')")
    c.commit(); c.close()
    g = V.gate(sqlite3.connect(f"file:{db}?mode=ro", uri=True), milestone=150)
    assert g["status"] == "accruing" and g["n"] == 1

"""Phase 2A — dynamic timing policies T0 (threshold cross) and T1 (edge persistence). Pure."""
from sniper.algos.polymarket_crypto5m import quant_timing as QT


def test_t0_enters_when_edge_clears_threshold_and_gates_ok():
    r = QT.threshold_cross(net_edge=0.04, threshold=0.02, gates_ok=True)
    assert r["action"] == "enter"


def test_t0_waits_below_threshold():
    r = QT.threshold_cross(net_edge=0.01, threshold=0.02, gates_ok=True)
    assert r["action"] == "wait" and "below_threshold" in r["reason"]


def test_t0_blocked_when_gates_fail_even_with_edge():
    r = QT.threshold_cross(net_edge=0.10, threshold=0.02, gates_ok=False)
    assert r["action"] == "wait" and r["reason"] == "gates_failed"


def test_t1_requires_persistence():
    # edge must hold above threshold for the last N updates
    flicker = [0.05, 0.01, 0.05]                       # not persistent
    steady = [0.01, 0.05, 0.05, 0.05]                  # last 3 persist
    assert QT.edge_persistence(flicker, threshold=0.02, persist_n=3, gates_ok=True)["action"] == "wait"
    assert QT.edge_persistence(steady, threshold=0.02, persist_n=3, gates_ok=True)["action"] == "enter"


def test_t1_not_enough_history_waits():
    r = QT.edge_persistence([0.05], threshold=0.02, persist_n=3, gates_ok=True)
    assert r["action"] == "wait" and r["reason"] == "insufficient_history"

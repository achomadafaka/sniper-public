"""Phase 2A — dynamic timing policies (pure).

Fixed checkpoints stay as controls elsewhere. These decide WHEN to act within a window from the evolving
uncertainty-adjusted net edge. T0 = threshold cross; T1 = edge persistence (kills one-tick noise). T2
(value-of-wait dynamic program) and T3 (event-driven re-evaluation) are specified in the design and
deferred behind the data gate.
"""
from __future__ import annotations


def threshold_cross(*, net_edge, threshold, gates_ok) -> dict:
    """T0 — enter the moment the uncertainty-adjusted net edge clears the threshold and all gates pass."""
    if not gates_ok:
        return {"action": "wait", "reason": "gates_failed", "net_edge": net_edge, "threshold": threshold}
    if net_edge is None:
        return {"action": "wait", "reason": "edge_unavailable", "net_edge": None, "threshold": threshold}
    if net_edge >= threshold:
        return {"action": "enter", "reason": "edge_cleared_threshold", "net_edge": net_edge,
                "threshold": threshold}
    return {"action": "wait", "reason": "below_threshold", "net_edge": net_edge, "threshold": threshold}


def edge_persistence(edge_history, *, threshold, persist_n, gates_ok) -> dict:
    """T1 — enter only if the last ``persist_n`` net-edge updates all sit at/above threshold. Avoids acting
    on a single noisy tick. ``edge_history`` is oldest->newest."""
    if not gates_ok:
        return {"action": "wait", "reason": "gates_failed"}
    hist = [e for e in (edge_history or []) if e is not None]
    if len(hist) < persist_n:
        return {"action": "wait", "reason": "insufficient_history",
                "have": len(hist), "need": persist_n}
    window = hist[-persist_n:]
    if all(e >= threshold for e in window):
        return {"action": "enter", "reason": "edge_persisted", "window": window, "threshold": threshold}
    return {"action": "wait", "reason": "edge_not_persistent", "window": window, "threshold": threshold}

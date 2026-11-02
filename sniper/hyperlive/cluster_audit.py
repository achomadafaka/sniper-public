"""Liquidation-cascade edge audit. The thesis: a dense liquidation cluster is a MAGNET — price
gravitates toward it to trigger forced fills, and once triggered the cascade can overshoot. If
that magnet pull, conditioned on a NEAR + BIG cluster, moves price toward the cluster by more
than the ~9bps round-trip taker fee, it's a 24/7 crypto edge that clears the cost wall where
micro-momentum could not.

Reads the hyperlive_cluster_history time series (perishable, forward-accruing). The signal:
side ('long' = cluster above mid, 'short' = below), distance_bps (proximity), notional (size).
aligned move = toward-cluster direction × forward move. Edge requires EV-after-fee > 0 AND the
mean aligned move significant (t>2). Read-only; selection/entry labels, never PnL."""
from __future__ import annotations

import math
import sqlite3

TOL_MS = 90_000               # ~half the 180s poll cadence
FEE_BPS = 9.0                 # HL round-trip taker (models.ROUND_TRIP_FEE_BPS)
MIN_N = 30                    # don't trust a verdict below this many near+big-cluster signals


def _toward(side: str) -> float:
    return 1.0 if side == "long" else -1.0   # 'long' cluster is ABOVE mid -> toward = up


def cluster_cascade_edge(conn: sqlite3.Connection, *, horizon_ms: int = 180_000,
                         max_distance_bps: float = 50.0, min_notional: float = 1_000_000.0,
                         tol_ms: int = TOL_MS, fee_bps: float = FEE_BPS, min_n: int = MIN_N) -> dict:
    """Conditioned on a NEAR (distance<=max_distance_bps) + BIG (notional>=min_notional) cluster,
    does price move TOWARD the cluster by more than the fee within horizon_ms? Forward move is
    measured from the same asset's later cluster-history mid."""
    rows = conn.execute(
        "SELECT asset, ts_ms, mid, side, distance_bps, notional FROM hyperlive_cluster_history "
        "WHERE mid IS NOT NULL AND mid > 0 ORDER BY asset, ts_ms").fetchall()
    by_asset: dict = {}
    for r in rows:
        by_asset.setdefault(r["asset"], []).append(
            (int(r["ts_ms"]), float(r["mid"]), r["side"], float(r["distance_bps"] or 1e9),
             float(r["notional"] or 0.0)))
    aligned = []
    for ars in by_asset.values():
        ars.sort(key=lambda x: x[0])
        ts = [a[0] for a in ars]
        for i, (t0, mid0, side, dist, notional) in enumerate(ars):
            if dist > max_distance_bps or notional < min_notional or side not in ("long", "short"):
                continue
            target = t0 + horizon_ms
            best = None
            for j in range(i + 1, len(ars)):
                if ts[j] > target + tol_ms:
                    break
                if abs(ts[j] - target) <= tol_ms and (best is None or abs(ts[j] - target) < abs(ts[best] - target)):
                    best = j
            if best is None:
                continue
            move_bps = (ars[best][1] - mid0) / mid0 * 1e4
            aligned.append(_toward(side) * move_bps)
    n = len(aligned)
    horizon_s = horizon_ms // 1000
    if n < min_n:
        return {"status": "insufficient", "n": n, "min_n": min_n, "horizon_s": horizon_s,
                "max_distance_bps": max_distance_bps, "min_notional": min_notional}
    mean = sum(aligned) / n
    std = math.sqrt(sum((a - mean) ** 2 for a in aligned) / (n - 1)) if n > 1 else 0.0
    hits = sum(1 for a in aligned if a > 0)
    after_fee = mean - fee_bps
    t_stat = (mean / (std / math.sqrt(n))) if std > 0 else (math.inf if mean > 0 else 0.0)
    return {
        "status": "ok", "n": n, "horizon_s": horizon_s,
        "max_distance_bps": max_distance_bps, "min_notional": min_notional,
        "hit_rate": round(hits / n, 3), "mean_aligned_bps": round(mean, 2),
        "after_fee_bps": round(after_fee, 2), "fee_bps": fee_bps,
        "t_stat": round(t_stat, 2) if t_stat != math.inf else 999.0,
        "edge": bool(after_fee > 0 and t_stat > 2.0),
    }

"""Read-only MFE/MAE exit replay for the Hyperliquid microstructure paper lane.

Reconstructs each closed fusion_radar position's forward mid path from
``hlm_feature_snapshots`` and replays alternative stop/take/time-stop exits over it.
This measures the exit hypothesis ("would a different stop have helped?") WITHOUT
changing live paper behavior. It does not validate a deployable rule: replaying an
exit grid over realized paths is in-sample and overfit by construction.
"""
from __future__ import annotations

import itertools
import json
import sqlite3
import statistics
from bisect import bisect_right
from collections import defaultdict


MAX_REPLAY_HORIZON_MS = 600_000
STOP_GRID = (0.001, 0.0015, 0.003)
TAKE_GRID = (0.002, 0.004, 0.008)
TIME_STOP_GRID = (120_000, 300_000)

# Cost models for sensitivity only. The exit moves are mid-based and identical across
# models; only the charged cost differs. taker_actual is the real lane cost. maker and
# frictionless are HYPOTHETICAL lower-cost bounds — public data cannot prove maker fills
# or queue priority, so they are diagnostics, never evidence of a deployable edge.
COST_MODELS = (
    {"name": "taker_actual", "fee_rate": None, "hypothetical": False,
     "note": "per-position taker fee on both legs plus full entry-spread cross"},
    {"name": "maker_hypothetical", "fee_rate": 0.0001, "hypothetical": True,
     "note": "HYPOTHETICAL: assumes passive fills both legs, no spread cross; unprovable on public data"},
    {"name": "frictionless_bound", "fee_rate": 0.0, "hypothetical": True,
     "note": "HYPOTHETICAL upper bound: zero fees, no spread"},
)


def build_exit_replay(conn: sqlite3.Connection) -> dict:
    snapshots = _snapshot_index(conn)
    positions = _replay_positions(conn)
    mfe, mae = [], []
    grids: dict[tuple, dict] = {
        cfg: {"positions": 0, "net_sum": 0.0, "net_wins": 0, "total_net_usdc": 0.0,
              "gross_sum": 0.0, "spread_sum": 0.0, "fee_sum": 0.0, "reasons": defaultdict(int)}
        for cfg in itertools.product(STOP_GRID, TAKE_GRID, TIME_STOP_GRID)
    }
    realized = []
    with_path = 0
    for pos in positions:
        notional = pos["notional_usdc"]
        if notional and notional > 0 and pos["realized_pnl"] is not None:
            realized.append(pos["realized_pnl"] / notional)
        path = snapshots.forward_path(pos["asset"], pos["opened_ts_ms"], MAX_REPLAY_HORIZON_MS)
        if not path:
            continue
        with_path += 1
        exc = reconstruct_excursions(pos["entry_mid"], pos["side"], [m for _ts, m in path])
        if exc["mfe_pct"] is not None:
            mfe.append(exc["mfe_pct"])
            mae.append(exc["mae_pct"])
        cost = _round_trip_cost(pos["fee_rate"], pos["entry_spread_bps"])
        for (stop, take, time_stop), acc in grids.items():
            outcome = replay_exit(pos["entry_mid"], pos["opened_ts_ms"], pos["side"], path,
                                  stop_loss_pct=stop, take_profit_pct=take, time_stop_ms=time_stop)
            if outcome is None:
                continue
            net = outcome["exit_move_pct"] - cost
            acc["positions"] += 1
            acc["net_sum"] += net
            acc["net_wins"] += 1 if net > 0 else 0
            acc["total_net_usdc"] += net * float(notional or 0.0)
            acc["gross_sum"] += outcome["exit_move_pct"]
            acc["spread_sum"] += max(0.0, float(pos["entry_spread_bps"] or 0.0)) / 10_000.0
            acc["fee_sum"] += 2.0 * float(pos["fee_rate"] or 0.0)
            acc["reasons"][outcome["exit_reason"]] += 1
    return {
        "summary": {
            "positions": len(positions),
            "with_path": with_path,
            "max_replay_horizon_ms": MAX_REPLAY_HORIZON_MS,
            "realized_net_bps": round(10000 * statistics.fmean(realized), 4) if realized else None,
        },
        "mfe_mae": _mfe_mae(mfe, mae),
        "exit_grid": _grid_rows(grids),
        "cost_sensitivity": _cost_sensitivity(grids),
        "data_gaps": {
            "exit_grid_inference": "in_sample_overfit_grid_fit_to_realized_paths_not_a_deployable_rule",
            "fill_model": "mid_to_mid_threshold_fills_no_gap_slippage",
            "right_censoring": "forward_path_truncates_when_asset_deselected_path_end_exits_are_censored",
            "cost_model": "taker_round_trip_plus_entry_spread_estimate",
        },
    }


def _mfe_mae(mfe: list[float], mae: list[float]) -> dict:
    if not mfe:
        return {"positions": 0, "avg_mfe_bps": None, "avg_mae_bps": None,
                "median_mfe_bps": None, "median_mae_bps": None}
    return {
        "positions": len(mfe),
        "avg_mfe_bps": round(10000 * statistics.fmean(mfe), 4),
        "avg_mae_bps": round(10000 * statistics.fmean(mae), 4),
        "median_mfe_bps": round(10000 * statistics.median(mfe), 4),
        "median_mae_bps": round(10000 * statistics.median(mae), 4),
    }


def _grid_rows(grids: dict) -> list[dict]:
    rows = []
    for (stop, take, time_stop), acc in grids.items():
        n = acc["positions"]
        if n <= 0:
            continue
        rows.append({
            "stop_loss_pct": stop,
            "take_profit_pct": take,
            "time_stop_ms": time_stop,
            "positions": n,
            "avg_net_bps": round(10000 * acc["net_sum"] / n, 4),
            "net_win_rate": round(acc["net_wins"] / n, 4),
            "total_net_usdc": round(acc["total_net_usdc"], 4),
            "exit_reason_mix": dict(acc["reasons"]),
        })
    rows.sort(key=lambda r: (-r["avg_net_bps"], r["stop_loss_pct"], r["take_profit_pct"], r["time_stop_ms"]))
    return rows


def _cost_sensitivity(grids: dict) -> list[dict]:
    out = []
    for model in COST_MODELS:
        best = None
        positive = 0
        for cfg, acc in grids.items():
            n = acc["positions"]
            if n <= 0:
                continue
            cost_total = _model_cost_total(model, acc, n)
            avg_net = (acc["gross_sum"] - cost_total) / n
            if avg_net > 0:
                positive += 1
            if best is None or avg_net > best["avg"]:
                best = {"avg": avg_net, "cfg": cfg}
        out.append({
            "name": model["name"],
            "hypothetical": model["hypothetical"],
            "note": model["note"],
            "best_avg_net_bps": round(10000 * best["avg"], 4) if best else None,
            "best_cell": _cfg_dict(best["cfg"]) if best else None,
            "positive_cells": positive,
        })
    return out


def _model_cost_total(model: dict, acc: dict, n: int) -> float:
    if model.get("fee_rate") is None:  # taker_actual: real per-position fee + spread cross
        return acc["fee_sum"] + acc["spread_sum"]
    return n * 2.0 * float(model["fee_rate"])  # maker/frictionless: passive, no spread cross


def _cfg_dict(cfg: tuple) -> dict:
    stop, take, time_stop = cfg
    return {"stop_loss_pct": stop, "take_profit_pct": take, "time_stop_ms": time_stop}


def format_exit_replay_markdown(report: dict) -> str:
    s = report["summary"]
    m = report["mfe_mae"]
    lines = [
        "# Hyperliquid Microstructure Exit Replay",
        "",
        f"- Closed fusion positions: {s['positions']}",
        f"- Positions with a forward path: {s['with_path']}",
        f"- Max replay horizon: {s['max_replay_horizon_ms'] // 1000}s",
        f"- Realized net (actual closes): {_bps(s['realized_net_bps'])}",
        "- WARNING: the exit grid is in-sample and overfit — it fits exits to realized "
        "paths and is NOT a deployable rule. Use it only to size the exit hypothesis.",
        "",
        "## MFE / MAE",
        f"- Avg MFE: {_bps(m['avg_mfe_bps'])}  |  Avg MAE: {_bps(m['avg_mae_bps'])}",
        f"- Median MFE: {_bps(m['median_mfe_bps'])}  |  Median MAE: {_bps(m['median_mae_bps'])}",
        "",
        "## Exit Grid (top 20 by avg net bps — in-sample)",
    ]
    columns = ["stop_loss_pct", "take_profit_pct", "time_stop_ms", "positions", "avg_net_bps", "net_win_rate", "total_net_usdc"]
    rows = report["exit_grid"]
    if not rows:
        lines.append("- No replayable positions.")
    else:
        lines.append("| " + " | ".join(columns) + " |")
        lines.append("| " + " | ".join(["---"] * len(columns)) + " |")
        for row in rows[:20]:
            lines.append("| " + " | ".join(str(row[c]) for c in columns) + " |")
    lines.extend(["", "## Cost Sensitivity (maker/frictionless are HYPOTHETICAL — not evidence)"])
    cs_cols = ["name", "hypothetical", "best_avg_net_bps", "positive_cells"]
    cs_rows = report.get("cost_sensitivity") or []
    if not cs_rows:
        lines.append("- No replayable positions.")
    else:
        lines.append("| " + " | ".join(cs_cols) + " |")
        lines.append("| " + " | ".join(["---"] * len(cs_cols)) + " |")
        for row in cs_rows:
            lines.append("| " + " | ".join(str(row[c]) for c in cs_cols) + " |")
        for row in cs_rows:
            if row["hypothetical"]:
                lines.append(f"- {row['name']}: {row['note']}")
    lines.extend(["", "## Caveats"])
    for key, value in report["data_gaps"].items():
        lines.append(f"- {key}: {value}")
    lines.append("")
    return "\n".join(lines)


def _replay_positions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""
        SELECT p.id AS position_id, p.asset, p.side, p.opened_ts_ms, p.notional_usdc,
               p.realized_pnl, d.fee_rate, fs.features_json
        FROM hlm_positions p
        JOIN hlm_decisions d ON d.position_id = p.id AND d.status='entered' AND d.action='enter'
        JOIN hlm_feature_snapshots fs ON fs.id = d.feature_snapshot_id
        WHERE p.policy_id='fusion_radar' AND p.state='closed'
        ORDER BY p.opened_ts_ms, p.id
    """).fetchall()
    out = []
    seen = set()
    for row in rows:
        pid = int(row["position_id"])
        if pid in seen:
            continue
        seen.add(pid)
        features = _json_dict(row["features_json"])
        entry_mid = _float(features.get("mid"))
        if entry_mid is None or entry_mid <= 0:
            continue
        out.append({
            "position_id": pid,
            "asset": row["asset"],
            "side": row["side"],
            "opened_ts_ms": int(row["opened_ts_ms"]),
            "notional_usdc": _float(row["notional_usdc"]) or 0.0,
            "realized_pnl": _float(row["realized_pnl"]),
            "fee_rate": _float(row["fee_rate"]) or 0.0,
            "entry_mid": entry_mid,
            "entry_spread_bps": _float(features.get("spread_bps")),
        })
    return out


class _SnapshotIndex:
    def __init__(self, by_asset: dict[str, list[tuple[int, float]]]):
        self._by_asset = by_asset
        self._times = {asset: [ts for ts, _mid in rows] for asset, rows in by_asset.items()}

    def forward_path(self, asset: str, after_ts_ms: int, horizon_ms: int) -> list[tuple[int, float]]:
        rows = self._by_asset.get(asset) or []
        times = self._times.get(asset) or []
        start = bisect_right(times, int(after_ts_ms))
        ceiling = int(after_ts_ms) + int(horizon_ms)
        path = []
        for i in range(start, len(rows)):
            ts, mid = rows[i]
            if ts > ceiling:
                break
            path.append((ts, mid))
        return path


def _snapshot_index(conn: sqlite3.Connection) -> _SnapshotIndex:
    rows = conn.execute("""
        SELECT asset, decision_ts_ms, features_json
        FROM hlm_feature_snapshots
        ORDER BY asset, decision_ts_ms, id
    """).fetchall()
    by_asset = defaultdict(list)
    seen = set()
    for row in rows:
        asset = row["asset"]
        ts = int(row["decision_ts_ms"])
        key = (asset, ts)
        if key in seen:
            continue
        seen.add(key)
        mid = _float(_json_dict(row["features_json"]).get("mid"))
        if mid is None or mid <= 0:
            continue
        by_asset[asset].append((ts, mid))
    return _SnapshotIndex(dict(by_asset))


def _round_trip_cost(fee_rate, spread_bps) -> float:
    spread_cost = max(0.0, float(spread_bps or 0.0)) / 10_000.0
    return 2.0 * float(fee_rate or 0.0) + spread_cost


def _json_dict(raw) -> dict:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bps(value) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.1f} bps"


def reconstruct_excursions(entry_mid: float, side: str, path_mids: list[float]) -> dict:
    """Side-aware maximum favorable / adverse excursion of mid vs entry mid.

    Returns fractional moves (e.g. 0.02 == +2%). ``mfe_pct``/``mae_pct`` are ``None``
    when there is no usable forward path.
    """
    entry = _float(entry_mid)
    moves = []
    if entry is not None and entry > 0:
        for mid in path_mids or []:
            value = _float(mid)
            if value is None or value <= 0:
                continue
            moves.append(_signed_move(entry, value, side))
    if not moves:
        return {"mfe_pct": None, "mae_pct": None, "points": 0}
    return {"mfe_pct": max(moves), "mae_pct": min(moves), "points": len(moves)}


def replay_exit(entry_mid: float, entry_ts_ms: int, side: str, path,
                *, stop_loss_pct: float, take_profit_pct: float, time_stop_ms: int) -> dict | None:
    """Replay a single stop/take/time-stop exit over a forward ``(ts_ms, mid)`` path.

    First trigger wins. Take-profit and stop-loss fill at the threshold (no overshoot
    credit, no gap slippage modeled); time-stop and path-end use the actual mid move.
    Returns ``None`` when the path is empty.
    """
    entry = _float(entry_mid)
    if entry is None or entry <= 0:
        return None
    last = None
    for ts_ms, mid in path or []:
        value = _float(mid)
        if value is None or value <= 0:
            continue
        move = _signed_move(entry, value, side)
        elapsed = int(ts_ms) - int(entry_ts_ms)
        last = (move, elapsed)
        if move >= take_profit_pct:
            return _exit(take_profit_pct, "take_profit", elapsed, value)
        if move <= -stop_loss_pct:
            return _exit(-stop_loss_pct, "stop_loss", elapsed, value)
        if elapsed >= time_stop_ms:
            return _exit(move, "time_stop", elapsed, value)
    if last is None:
        return None
    return _exit(last[0], "path_end", last[1], None)


def _exit(move_pct: float, reason: str, elapsed_ms: int, exit_mid) -> dict:
    return {
        "exit_move_pct": float(move_pct),
        "exit_reason": reason,
        "elapsed_ms": int(elapsed_ms),
        "exit_mid": exit_mid,
    }


def _signed_move(entry_mid: float, mid: float, side: str) -> float:
    raw = (mid - entry_mid) / entry_mid
    return raw if side == "long" else -raw


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

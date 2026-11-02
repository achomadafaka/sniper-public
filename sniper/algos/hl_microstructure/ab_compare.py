"""Read-only per-policy_id A/B readout for the Hyperliquid microstructure forward test.

Segments closed positions by arm (control fusion_radar vs candidates fusion_gated_b1/b2),
reports realized net bps with an honest one-sided, entry-clustered Student-t test of
mean net > 0, Holm-corrected across the candidate arms. Realized net per position is
already taker fee + book-fill inclusive (from PaperEngine._close). No policy or schema
change. See docs/superpowers/specs/2026-06-21-hl-microstructure-entry-cost-gate-forward-ab-design.md.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict

from sniper.algos.hl_microstructure.evidence_audit import _one_sided_mean_p_value


CONTROL_ARM = "fusion_radar"
CANDIDATE_ARMS = ("fusion_gated_b1", "fusion_gated_b2")
CLUSTER_WIDTH_MS = 60_000
MIN_CLOSED = 200
ALPHA = 0.05
MAX_ASSET_PNL_SHARE = 0.40


def build_ab_compare(conn: sqlite3.Connection) -> dict:
    by_arm = _closed_by_arm(conn)
    candidates_present = [a for a in CANDIDATE_ARMS if by_arm.get(a)]
    raw_p = {a: _arm_stats(by_arm[a])["p_value"] for a in candidates_present}
    holm = _holm(raw_p)

    arms = []
    for arm in [CONTROL_ARM, *CANDIDATE_ARMS]:
        rows = by_arm.get(arm) or []
        if not rows:
            continue
        stats = _arm_stats(rows)
        is_candidate = arm in CANDIDATE_ARMS
        holm_p = holm.get(arm) if is_candidate else None
        stats.update({
            "policy_id": arm,
            "is_candidate": is_candidate,
            "holm_p": holm_p,
            "passes": _passes(stats, holm_p) if is_candidate else None,
        })
        arms.append(stats)
    return {
        "control_arm": CONTROL_ARM,
        "candidate_arms": candidates_present,
        "alpha": ALPHA,
        "min_closed": MIN_CLOSED,
        "arms": arms,
    }


def _closed_by_arm(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    rows = conn.execute(
        """SELECT policy_id, asset, opened_ts_ms, realized_pnl, notional_usdc
           FROM hlm_positions
           WHERE state='closed' AND policy_id IN (?,?,?)
           ORDER BY opened_ts_ms, id""",
        (CONTROL_ARM, *CANDIDATE_ARMS),
    ).fetchall()
    out = defaultdict(list)
    for r in rows:
        notional = _float(r["notional_usdc"])
        pnl = _float(r["realized_pnl"])
        if notional is None or notional <= 0 or pnl is None:
            continue
        out[r["policy_id"]].append({
            "asset": r["asset"],
            "opened_ts_ms": int(r["opened_ts_ms"]),
            "net": pnl / notional,
            "pnl": pnl,
        })
    return out


def _arm_stats(rows: list[dict]) -> dict:
    nets = [r["net"] for r in rows]
    n = len(nets)
    clusters = _cluster_means(rows)
    return {
        "closed": n,
        "realized_net_bps": round(10000 * sum(nets) / n, 4) if n else 0.0,
        "net_win_rate": round(sum(1 for v in nets if v > 0) / n, 4) if n else 0.0,
        "effective_observations": len(clusters),
        "p_value": _one_sided_mean_p_value(clusters),
        "max_asset_pnl_share": _max_asset_share(rows),
        "positive_thirds": _positive_thirds(rows),
    }


def _cluster_means(rows: list[dict]) -> list[float]:
    buckets = defaultdict(list)
    for r in rows:
        buckets[(r["asset"], int(r["opened_ts_ms"]) // CLUSTER_WIDTH_MS)].append(r["net"])
    return [sum(v) / len(v) for v in buckets.values() if v]


def _max_asset_share(rows: list[dict]) -> float:
    by_asset = defaultdict(float)
    for r in rows:
        by_asset[r["asset"]] += abs(r["pnl"])
    total = sum(by_asset.values())
    if total <= 0:
        return 0.0
    return round(max(by_asset.values()) / total, 4)


def _positive_thirds(rows: list[dict]) -> int:
    ordered = sorted(rows, key=lambda r: int(r["opened_ts_ms"]))
    n = len(ordered)
    if n < 3:
        return 1 if n and sum(r["net"] for r in ordered) / n > 0 else 0
    size = n // 3
    thirds = [ordered[0:size], ordered[size:2 * size], ordered[2 * size:]]
    return sum(1 for block in thirds if block and sum(r["net"] for r in block) / len(block) > 0)


def _passes(stats: dict, holm_p) -> bool:
    return bool(
        stats["closed"] >= MIN_CLOSED
        and stats["realized_net_bps"] > 0
        and holm_p is not None and holm_p <= ALPHA
        and stats["max_asset_pnl_share"] <= MAX_ASSET_PNL_SHARE
        and stats["positive_thirds"] >= 2
    )


def _holm(pvals: dict[str, float]) -> dict[str, float]:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    out = {}
    running = 0.0
    for i, (key, p) in enumerate(items):
        running = max(running, min(1.0, (m - i) * float(p)))
        out[key] = round(running, 6)
    return out


def format_ab_compare_markdown(report: dict) -> str:
    lines = [
        "# Hyperliquid Microstructure A/B Compare",
        "",
        f"- Control: {report['control_arm']}  |  Candidates: {', '.join(report['candidate_arms']) or 'none'}",
        f"- Pass bar: closed >= {report['min_closed']}, net > 0, Holm-adjusted one-sided "
        f"clustered-t p <= {report['alpha']}, max-asset PnL share <= {MAX_ASSET_PNL_SHARE}, "
        "net positive in >= 2 of 3 chronological thirds.",
        "- Realized net is taker fee + book-fill inclusive; arms differ in ENTRY only.",
        "",
    ]
    cols = ["policy_id", "closed", "realized_net_bps", "net_win_rate", "effective_observations",
            "p_value", "holm_p", "max_asset_pnl_share", "positive_thirds", "passes"]
    arms = report["arms"]
    if not arms:
        lines.append("- No closed positions in any arm yet.")
        return "\n".join(lines) + "\n"
    lines.append("| " + " | ".join(cols) + " |")
    lines.append("| " + " | ".join(["---"] * len(cols)) + " |")
    for a in arms:
        lines.append("| " + " | ".join(str(a.get(c)) for c in cols) + " |")
    lines.append("")
    return "\n".join(lines)


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

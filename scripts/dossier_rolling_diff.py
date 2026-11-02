#!/usr/bin/env python3
"""Dossier rolling-diff — compare two time windows on the same wallet.

Builds two dossiers from the same fixture using ``filter_fixture_by_since_days``
at different window sizes (e.g. last 30 days vs last 90 days), then reports the
deltas in:

- grain_quality distribution
- mechanism_evidence strength distribution
- thesis_candidates count
- descriptor table coverage

The purpose is drift detection on copy targets: if a wallet shipped a
promoted thesis a month ago but its recent 30-day grain has shifted
underpowered, the cross-wallet thesis may no longer apply.

Usage:
    python3 scripts/dossier_rolling_diff.py \\
        --wallet 0xdbade4c82fb72780a0db9a38f821d8671aba9c95 \\
        --fixture tests/calibration/fixtures/0xdbade4c82fb72780a0db9a38f821d8671aba9c95_2026-05-13.json \\
        --protocol-report /tmp/swissmiss_protocol.json \\
        --recent-days 30 --baseline-days 120

Output is markdown on stdout plus optional JSON via ``--out-json``.

No DB writes. No live API calls.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.wallet_dossier.builder import (  # noqa: E402
    build_dossier,
    filter_fixture_by_since_days,
)


def _build_window_dossier(
    *,
    wallet: str,
    fixture_path: Path,
    protocol_report_path: Path,
    since_days: int,
) -> Any:
    """Build a dossier restricted to the last ``since_days`` of activity.

    Returns the in-memory DossierTables; no SQLite/JSON sink.
    """
    return build_dossier(
        wallet=wallet,
        fixture_path=fixture_path,
        protocol_report_path=protocol_report_path,
        since_days=since_days,
    )


def _grain_quality_dist(tables) -> dict[str, int]:
    return dict(Counter(r["grain_quality"] for r in tables.result_semantics))


def _mechanism_evidence_dist(tables) -> dict[str, int]:
    return dict(
        Counter(
            f"{r['mechanism']}.{r['evidence_strength']}"
            for r in tables.mechanism_evidence
        )
    )


def _thesis_candidate_count(tables) -> int:
    return len(tables.thesis_candidates)


def _descriptor_row_counts(tables) -> dict[str, int]:
    return {
        "hour_distribution": len(tables.descriptor_hour_distribution),
        "cadence": len(tables.descriptor_cadence),
        "sizing": len(tables.descriptor_sizing),
        "time_in_position": len(tables.descriptor_time_in_position),
        "fills_per_position": len(tables.descriptor_fills_per_position),
    }


def _meta_value(tables, key: str) -> str | None:
    for row in tables.dossier_meta:
        if row["key"] == key:
            return row["value"]
    return None


def diff_window_dossiers(recent_tables, baseline_tables) -> dict[str, Any]:
    """Return a structured diff between two window-restricted dossiers."""
    return {
        "schema_version": _meta_value(recent_tables, "schema_version"),
        "wallet": _meta_value(recent_tables, "wallet"),
        "recent": {
            "n_positions": int(_meta_value(recent_tables, "n_positions") or 0),
            "n_grains": int(_meta_value(recent_tables, "n_grains") or 0),
            "since_days_filter": _meta_value(recent_tables, "since_days_filter"),
            "grain_quality_dist": _grain_quality_dist(recent_tables),
            "mechanism_evidence_dist": _mechanism_evidence_dist(recent_tables),
            "thesis_candidate_count": _thesis_candidate_count(recent_tables),
            "descriptor_row_counts": _descriptor_row_counts(recent_tables),
        },
        "baseline": {
            "n_positions": int(_meta_value(baseline_tables, "n_positions") or 0),
            "n_grains": int(_meta_value(baseline_tables, "n_grains") or 0),
            "since_days_filter": _meta_value(baseline_tables, "since_days_filter"),
            "grain_quality_dist": _grain_quality_dist(baseline_tables),
            "mechanism_evidence_dist": _mechanism_evidence_dist(baseline_tables),
            "thesis_candidate_count": _thesis_candidate_count(baseline_tables),
            "descriptor_row_counts": _descriptor_row_counts(baseline_tables),
        },
        "deltas": _compute_deltas(recent_tables, baseline_tables),
    }


def _compute_deltas(recent_tables, baseline_tables) -> dict[str, Any]:
    """Compute key drift signals."""
    rec_q = _grain_quality_dist(recent_tables)
    base_q = _grain_quality_dist(baseline_tables)
    interp_drift = rec_q.get("interpretable", 0) - base_q.get("interpretable", 0)

    rec_t = _thesis_candidate_count(recent_tables)
    base_t = _thesis_candidate_count(baseline_tables)

    return {
        "interpretable_grain_delta": interp_drift,
        "thesis_candidate_delta": rec_t - base_t,
        "n_positions_delta": (
            int(_meta_value(recent_tables, "n_positions") or 0)
            - int(_meta_value(baseline_tables, "n_positions") or 0)
        ),
        "drift_flag": _classify_drift(interp_drift, rec_t, base_t),
    }


def _classify_drift(
    interp_delta: int, rec_thesis: int, base_thesis: int
) -> str:
    """Coarse classification for operator triage."""
    if base_thesis > 0 and rec_thesis == 0:
        return "thesis_disappeared"
    if interp_delta < 0 and base_thesis > 0:
        return "grain_quality_degraded"
    if interp_delta > 0 and rec_thesis > base_thesis:
        return "thesis_strengthened"
    if rec_thesis == 0 and base_thesis == 0:
        return "no_thesis_either_window"
    return "stable"


def _render_markdown(diff: dict[str, Any]) -> str:
    lines = []
    lines.append(f"# Rolling diff — `{diff['wallet']}`")
    lines.append("")
    lines.append(f"**Schema:** `{diff['schema_version']}`")
    lines.append(f"**Recent window:** `{diff['recent']['since_days_filter']}`")
    lines.append(f"**Baseline window:** `{diff['baseline']['since_days_filter']}`")
    lines.append("")
    lines.append("## Headline deltas")
    lines.append("")
    lines.append("| Metric | Recent | Baseline | Delta |")
    lines.append("|---|---:|---:|---:|")
    lines.append(
        f"| n_positions | {diff['recent']['n_positions']} | "
        f"{diff['baseline']['n_positions']} | "
        f"{diff['deltas']['n_positions_delta']:+d} |"
    )
    lines.append(
        f"| n_grains | {diff['recent']['n_grains']} | "
        f"{diff['baseline']['n_grains']} | "
        f"{diff['recent']['n_grains'] - diff['baseline']['n_grains']:+d} |"
    )
    lines.append(
        f"| interpretable_grains | "
        f"{diff['recent']['grain_quality_dist'].get('interpretable', 0)} | "
        f"{diff['baseline']['grain_quality_dist'].get('interpretable', 0)} | "
        f"{diff['deltas']['interpretable_grain_delta']:+d} |"
    )
    lines.append(
        f"| thesis_candidates | {diff['recent']['thesis_candidate_count']} | "
        f"{diff['baseline']['thesis_candidate_count']} | "
        f"{diff['deltas']['thesis_candidate_delta']:+d} |"
    )
    lines.append("")
    lines.append(f"**Drift flag:** `{diff['deltas']['drift_flag']}`")
    lines.append("")
    lines.append("## Grain quality distribution")
    lines.append("")
    lines.append("| Quality | Recent | Baseline |")
    lines.append("|---|---:|---:|")
    qualities = sorted(
        set(diff["recent"]["grain_quality_dist"].keys())
        | set(diff["baseline"]["grain_quality_dist"].keys())
    )
    for q in qualities:
        r = diff["recent"]["grain_quality_dist"].get(q, 0)
        b = diff["baseline"]["grain_quality_dist"].get(q, 0)
        lines.append(f"| {q} | {r} | {b} |")
    lines.append("")
    lines.append("## Descriptor row counts (v5)")
    lines.append("")
    lines.append("| Surface | Recent | Baseline |")
    lines.append("|---|---:|---:|")
    for k in sorted(diff["recent"]["descriptor_row_counts"].keys()):
        r = diff["recent"]["descriptor_row_counts"][k]
        b = diff["baseline"]["descriptor_row_counts"][k]
        lines.append(f"| {k} | {r} | {b} |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Compare two time-window dossiers of the same wallet for drift "
            "detection."
        )
    )
    p.add_argument("--wallet", required=True, help="Wallet address.")
    p.add_argument(
        "--fixture", required=True, type=Path,
        help="WalletData fixture JSON.",
    )
    p.add_argument(
        "--protocol-report", required=True, type=Path,
        help="ProtocolReport JSON.",
    )
    p.add_argument(
        "--recent-days", type=int, required=True,
        help="Window size for the recent dossier (e.g. 30).",
    )
    p.add_argument(
        "--baseline-days", type=int, required=True,
        help="Window size for the baseline dossier (e.g. 120). Must be > recent-days.",
    )
    p.add_argument(
        "--out-json", type=Path, default=None,
        help="If set, write the structured diff as JSON to this path.",
    )

    args = p.parse_args(argv)

    if args.recent_days <= 0 or args.baseline_days <= 0:
        print("# error: --recent-days and --baseline-days must be positive", file=sys.stderr)
        return 2
    if args.baseline_days <= args.recent_days:
        print(
            "# error: --baseline-days must be > --recent-days "
            f"(got {args.baseline_days} <= {args.recent_days})",
            file=sys.stderr,
        )
        return 2
    if not args.fixture.exists():
        print(f"# error: fixture not found: {args.fixture}", file=sys.stderr)
        return 2
    if not args.protocol_report.exists():
        print(f"# error: protocol report not found: {args.protocol_report}", file=sys.stderr)
        return 2

    recent_tables = _build_window_dossier(
        wallet=args.wallet,
        fixture_path=args.fixture,
        protocol_report_path=args.protocol_report,
        since_days=args.recent_days,
    )
    baseline_tables = _build_window_dossier(
        wallet=args.wallet,
        fixture_path=args.fixture,
        protocol_report_path=args.protocol_report,
        since_days=args.baseline_days,
    )

    diff = diff_window_dossiers(recent_tables, baseline_tables)
    print(_render_markdown(diff))

    if args.out_json:
        args.out_json.parent.mkdir(parents=True, exist_ok=True)
        args.out_json.write_text(
            json.dumps(diff, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

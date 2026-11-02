#!/usr/bin/env python3
"""W6.4 — calibration report generator.

Walks ``tests/calibration/labels.yaml``, runs the wallet protocol from
each wallet's fixture (W5 fixture replay path), and writes a markdown
report comparing actual verdicts against labels.

Usage:

    python3 scripts/calibration_report.py \
        --labels tests/calibration/labels.yaml \
        --out /tmp/calibration_report.md

Match semantics mirror ``tests/calibration/test_calibration_set.py``:
* ``exact`` labels match iff actual verdict == expected.
* ``band`` labels match iff actual verdict ∈ accepted_band.
* Missing fixture → ``fixture_missing`` row; treated as non-match.

Exit codes:
    0  10/10 fixtures present AND agreement ≥ 9/10.
    1  any fixture missing OR agreement < 9/10.
    2  malformed labels file.

Plan ref: docs/superpowers/plans/2026-04-30-analyzer-completion-phase-1.md W6.4.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import yaml

# Make repo importable when running this script directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--labels", default="tests/calibration/labels.yaml",
        help="Path to labels YAML.",
    )
    ap.add_argument(
        "--fixture-dir", default="tests/calibration/fixtures",
        help="Directory containing per-wallet fetch fixtures.",
    )
    ap.add_argument(
        "--fixture-suffix", default="_2026-04-30.json",
        help="Filename suffix appended to <wallet> for fixture lookup.",
    )
    ap.add_argument(
        "--seed", type=int, default=1,
    )
    ap.add_argument(
        "--n-iter", type=int, default=2000,
    )
    ap.add_argument(
        "--out", required=True,
        help="Path to write the markdown report.",
    )
    return ap


def _fixture_path(fixture_dir: str, wallet: str, suffix: str) -> Path:
    return Path(fixture_dir) / f"{wallet.lower()}{suffix}"


def _run_protocol_from_fixture(
    wallet: str, fixture: Path, *, seed: int, n_iter: int,
) -> dict:
    import scripts.run_wallet_protocol as mod
    argv = [
        wallet,
        "--from-fixture", str(fixture),
        "--no-tags", "--no-peers", "--no-exec", "--no-history", "--no-archetype",
        "--seed", str(seed), "--n-iter", str(n_iter),
        "--json", "--no-persist",
    ]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(argv)
    if rc != 0:
        raise RuntimeError(f"run_wallet_protocol rc={rc} for {wallet}")
    return json.loads(buf.getvalue().strip())


def _classify(label: dict, actual: str | None) -> str:
    if actual is None:
        return "missing"
    if label["label_type"] == "exact":
        return "match" if actual == label.get("expected") else "miss"
    band = list(label.get("accepted_band") or [])
    return "match" if actual in band else "miss"


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        payload = yaml.safe_load(Path(args.labels).read_text())
        labels = list(payload.get("labels", []))
    except Exception as e:
        print(f"# error: malformed labels file: {e!r}", file=sys.stderr)
        return 2

    rows: list[dict] = []
    n_match = 0
    n_miss = 0
    n_missing = 0

    for lab in labels:
        wallet = lab["wallet"]
        fx = _fixture_path(args.fixture_dir, wallet, args.fixture_suffix)
        if not fx.exists():
            row = {
                "wallet": wallet,
                "label_type": lab["label_type"],
                "label": (
                    lab.get("expected")
                    if lab["label_type"] == "exact"
                    else lab.get("accepted_band")
                ),
                "actual": None,
                "status": "fixture_missing",
                "evidence": [ev.get("source") for ev in lab.get("evidence", [])],
                "fixture_path": str(fx),
            }
            n_missing += 1
            rows.append(row)
            continue
        try:
            report = _run_protocol_from_fixture(
                wallet, fx, seed=args.seed, n_iter=args.n_iter,
            )
            actual = report.get("overall_verdict")
        except Exception as e:
            actual = None
            row_status = f"error: {type(e).__name__}: {e!s}"[:120]
            rows.append({
                "wallet": wallet,
                "label_type": lab["label_type"],
                "label": (
                    lab.get("expected")
                    if lab["label_type"] == "exact"
                    else lab.get("accepted_band")
                ),
                "actual": None,
                "status": row_status,
                "evidence": [ev.get("source") for ev in lab.get("evidence", [])],
                "fixture_path": str(fx),
            })
            n_miss += 1
            continue
        status = _classify(lab, actual)
        if status == "match":
            n_match += 1
        elif status == "miss":
            n_miss += 1
        else:
            n_missing += 1
        rows.append({
            "wallet": wallet,
            "label_type": lab["label_type"],
            "label": (
                lab.get("expected")
                if lab["label_type"] == "exact"
                else lab.get("accepted_band")
            ),
            "actual": actual,
            "status": status,
            "evidence": [ev.get("source") for ev in lab.get("evidence", [])],
            "fixture_path": str(fx),
        })

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        f.write(f"# Calibration Report\n\n")
        f.write(
            f"**Labels:** `{args.labels}`  •  **Fixtures:** "
            f"`{args.fixture_dir}/<wallet>{args.fixture_suffix}`\n"
        )
        f.write(
            f"**Seed:** {args.seed}  •  **n_iter:** {args.n_iter}\n\n"
        )
        f.write(
            f"**Result:** {n_match}/{len(labels)} agreement  "
            f"(miss: {n_miss}, fixture_missing: {n_missing})\n\n"
        )
        f.write("| # | Wallet | Label | Actual | Status | Evidence |\n")
        f.write("|---|---|---|---|---|---|\n")
        for i, row in enumerate(rows, 1):
            wallet_short = f"{row['wallet'][:10]}…{row['wallet'][-4:]}"
            label_str = (
                row["label"] if row["label_type"] == "exact"
                else f"band({row['label']})"
            )
            actual_str = row["actual"] or "—"
            ev_str = "; ".join(s for s in (row.get("evidence") or []) if s)
            f.write(
                f"| {i} | `{wallet_short}` | `{label_str}` | "
                f"`{actual_str}` | **{row['status']}** | {ev_str} |\n"
            )

    print(
        f"# wrote {out_path} (match={n_match} miss={n_miss} "
        f"missing={n_missing})",
        file=sys.stderr,
    )
    if n_missing > 0:
        return 1
    if n_match < len(labels) - 1:  # allow at most one miss (≥ 9/10)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

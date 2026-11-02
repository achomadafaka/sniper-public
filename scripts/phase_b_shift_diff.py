#!/usr/bin/env python3
"""Phase B B4 — verdict/surface shift-diff harness.

Compares a v3-fixture replay against a v4-fixture replay, both under the
*current* analyzer code, so the diff isolates DATA changes (v3 capture vs
Phase-A re-capture) from analyzer-code changes. The labels.yaml `expected`
verdict is used as an analyzer-drift sanity check on the v3 replay.

Pipeline position: B4 in docs/superpowers/specs/2026-05-17-phase-b-fixture-
recapture.md. Reads fixtures only — no live API, no DB writes, no
persistence.

Usage:
    # diff every labels.yaml wallet that has both v3 + v4 fixtures
    python3 scripts/phase_b_shift_diff.py

    # diff one wallet
    python3 scripts/phase_b_shift_diff.py --wallet 0x8493...

    # emit per-wallet report JSON alongside the table
    python3 scripts/phase_b_shift_diff.py --json-out /tmp/phase-b/

The shift table is markdown on stdout; the `classification` column is left
PENDING for the operator (B5).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "calibration" / "fixtures"
LABELS_PATH = REPO_ROOT / "tests" / "calibration" / "labels.yaml"
V3_DATE = "2026-05-13"
V4_DATE = "2026-05-17"

# Matches scripts/calibration_report_card.py HARNESS_ARGS — bounded replay
# so the diff doesn't couple to local-DB tag state or slow surfaces.
HARNESS_ARGS = [
    "--no-tags", "--no-peers", "--no-exec", "--no-history", "--no-archetype",
    "--seed", "1", "--n-iter", "2000", "--json", "--no-persist",
]


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def _branch_fields(branch: dict | None) -> tuple[bool, dict]:
    """Return (is_active, {hypothesis_name: verdict}) for a branch dict."""
    if not isinstance(branch, dict):
        return False, {}
    is_active = bool(branch.get("is_active"))
    hyps = {}
    for h in branch.get("hypotheses") or []:
        if isinstance(h, dict) and "name" in h:
            hyps[h["name"]] = h.get("verdict")
    return is_active, hyps


def _flag_type(flag: str) -> str:
    """Normalize a data_quality_flag to its type prefix, dropping any value
    payload. Value-bearing flags embed live numbers that drift between
    captures — e.g. ``ACCOUNTING_TIE_UNSUPPORTED: lifecycle_estimate=$X...``
    or ``F3_recent_regime (last_90d_share=0.91)``. The B4 diff cares whether
    a flag TYPE appeared/disappeared (a real surface/verdict shift), not
    whether embedded numbers moved (expected capture-date drift). The type
    is everything before the first ``:`` or `` (``."""
    s = str(flag)
    cuts = [i for i in (s.find(":"), s.find(" (")) if i != -1]
    return (s[: min(cuts)] if cuts else s).strip()


def extract_shift_fields(report: dict) -> dict:
    """Pull the B4 comparison fields from a ProtocolReport JSON dict.

    Pure — no I/O. Tolerates missing nested keys (a bare report dict
    constructed without analyze_wallet still extracts cleanly).
    """
    dc = report.get("data_completeness") or {}
    cpc = dc.get("closed_positions_capture") or {}
    osb = report.get("outcome_side_breakdown") or {}
    clusters = osb.get("category_clusters") or []

    held_active, held_hyps = _branch_fields(report.get("held_analyst_branch"))
    flipper_active, flipper_hyps = _branch_fields(report.get("flipper_branch"))

    raw_flags = sorted(report.get("data_quality_flags") or [])
    return {
        "overall_verdict": report.get("overall_verdict"),
        "gate_result": report.get("gate_result"),
        "primary_edge_type": report.get("primary_edge_type"),
        "held_active": held_active,
        "held_hypotheses": held_hyps,
        "flipper_active": flipper_active,
        "flipper_hypotheses": flipper_hyps,
        "tag_lookup_state": dc.get("tag_lookup_state"),
        "closed_positions_cap_hit": cpc.get("closed_positions_cap_hit"),
        "n_category_clusters": len(clusters),
        "primary_category": (clusters[0].get("category") if clusters else None),
        "data_quality_flags": raw_flags,
        # Type-normalized set — diffed by diff_shift_fields so value-bearing
        # flags don't register as spurious shifts.
        "data_quality_flag_types": sorted({_flag_type(f) for f in raw_flags}),
    }


_SCALAR_FIELDS = (
    "overall_verdict", "gate_result", "primary_edge_type",
    "held_active", "flipper_active", "tag_lookup_state",
    "closed_positions_cap_hit", "n_category_clusters", "primary_category",
)


def diff_shift_fields(v3: dict, v4: dict) -> dict:
    """Per-field {v3, v4, changed}. data_quality_flags additionally carries
    added/removed lists. `any_changed` is a top-level rollup."""
    diff: dict = {}
    any_changed = False

    for field in _SCALAR_FIELDS:
        a, b = v3.get(field), v4.get(field)
        changed = a != b
        any_changed = any_changed or changed
        diff[field] = {"v3": a, "v4": b, "changed": changed}

    for field in ("held_hypotheses", "flipper_hypotheses"):
        a, b = v3.get(field) or {}, v4.get(field) or {}
        changed = a != b
        any_changed = any_changed or changed
        diff[field] = {"v3": a, "v4": b, "changed": changed}

    # Diff on type-normalized flags: value-bearing flags (embedded $/%/ts
    # that drift between capture dates) must not register as a shift. A real
    # shift is a flag TYPE appearing or disappearing. Raw flag lists are
    # retained for operator reference.
    a_types = set(v3.get("data_quality_flag_types") or [])
    b_types = set(v4.get("data_quality_flag_types") or [])
    added = sorted(b_types - a_types)
    removed = sorted(a_types - b_types)
    flags_changed = bool(added or removed)
    any_changed = any_changed or flags_changed
    diff["data_quality_flags"] = {
        "v3_types": sorted(a_types),
        "v4_types": sorted(b_types),
        "added": added,
        "removed": removed,
        "changed": flags_changed,
        "v3_raw": sorted(v3.get("data_quality_flags") or []),
        "v4_raw": sorted(v4.get("data_quality_flags") or []),
    }

    diff["any_changed"] = any_changed
    return diff


def load_expected_verdict(labels: dict, wallet: str) -> str | None:
    """Return the labels.yaml `expected` verdict for a wallet, or None.

    Wallet comparison is case-insensitive (0x addresses)."""
    w = wallet.lower()
    for entry in labels.get("labels") or []:
        if str(entry.get("wallet", "")).lower() == w:
            return entry.get("expected")
    return None


# ---------------------------------------------------------------------------
# Replay (side-effecting — fixture-only, no network)
# ---------------------------------------------------------------------------

def replay_fixture(fixture_path: str) -> dict:
    """Replay a fetch-snapshot fixture through run_wallet_protocol and return
    the parsed report JSON. Fixture-only: no live API, no DB writes."""
    path = Path(fixture_path)
    # Derive the wallet from the filename: <wallet>_<date>.json
    wallet = path.stem.rsplit("_", 1)[0]
    cmd = [
        sys.executable, str(REPO_ROOT / "scripts" / "run_wallet_protocol.py"),
        wallet, "--from-fixture", str(path), *HARNESS_ARGS,
    ]
    result = subprocess.run(
        cmd, check=False, capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"replay failed (rc={result.returncode}) for {fixture_path}: "
            f"{result.stderr[-500:]}"
        )
    # stdout carries the JSON; stderr carries the "# ..." breadcrumbs.
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"replay produced non-JSON stdout for {fixture_path}: {e}; "
            f"first 300 chars: {result.stdout[:300]!r}"
        ) from e


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_labels() -> dict:
    import yaml
    return yaml.safe_load(LABELS_PATH.read_text()) or {}


def _wallets_from_labels(labels: dict) -> list[str]:
    return [str(e["wallet"]) for e in (labels.get("labels") or []) if e.get("wallet")]


def _fixture_path(wallet: str, date: str) -> Path:
    return FIXTURE_DIR / f"{wallet}_{date}.json"


def _shift_row(wallet: str, diff: dict, expected: str | None,
               v3_verdict: str | None) -> str:
    drift = "" if v3_verdict == expected else f" ⚠drift(exp={expected})"
    v = diff["overall_verdict"]
    gate = diff["gate_result"]
    new_surfaces = []
    # primary_edge_type + hypothesis shifts must be surfaced — a B5
    # adversarial check (2026-05-22) caught primary_edge_type flipping on a
    # wallet while the table showed only verdict/gate/flags.
    if diff["primary_edge_type"]["changed"]:
        new_surfaces.append("edge_type")
    if diff["overall_verdict"]["changed"]:
        new_surfaces.append("verdict")
    if diff["held_hypotheses"]["changed"]:
        new_surfaces.append("held_hyp")
    if diff["flipper_hypotheses"]["changed"]:
        new_surfaces.append("flip_hyp")
    if diff["tag_lookup_state"]["changed"]:
        new_surfaces.append("tag_state")
    if diff["closed_positions_cap_hit"]["changed"]:
        new_surfaces.append("closed_cap")
    if diff["n_category_clusters"]["changed"]:
        new_surfaces.append("cat_clusters")
    if diff["data_quality_flags"]["changed"]:
        new_surfaces.append("dq_flags")
    surf = ",".join(new_surfaces) or "—"
    gate_cell = (
        f"{gate['v3']}→{gate['v4']}" if gate["changed"] else gate["v4"]
    )
    return (
        f"| {wallet[:10]}… | {v['v3']} | {v['v4']}{drift} | "
        f"{gate_cell} | {surf} | PENDING |"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Phase B B4 shift-diff harness.")
    ap.add_argument("--wallet", action="append", dest="wallets", default=[],
                    help="Wallet to diff (repeatable; default: all labels.yaml).")
    ap.add_argument("--json-out", default=None,
                    help="Directory for per-wallet diff JSON output.")
    args = ap.parse_args(argv)

    labels = _load_labels()
    wallets = args.wallets or _wallets_from_labels(labels)
    if not wallets:
        print("ERROR: no wallets to diff", file=sys.stderr)
        return 2

    json_out_dir = Path(args.json_out) if args.json_out else None
    if json_out_dir:
        json_out_dir.mkdir(parents=True, exist_ok=True)

    rows: list[str] = []
    skipped: list[str] = []
    for wallet in wallets:
        v3_path = _fixture_path(wallet, V3_DATE)
        v4_path = _fixture_path(wallet, V4_DATE)
        if not v3_path.exists():
            skipped.append(f"{wallet}: no v3 fixture")
            continue
        if not v4_path.exists():
            skipped.append(f"{wallet}: no v4 fixture (B1 not yet captured)")
            continue
        v3_report = replay_fixture(str(v3_path))
        v4_report = replay_fixture(str(v4_path))
        v3_fields = extract_shift_fields(v3_report)
        v4_fields = extract_shift_fields(v4_report)
        diff = diff_shift_fields(v3_fields, v4_fields)
        expected = load_expected_verdict(labels, wallet)
        rows.append(_shift_row(wallet, diff, expected,
                               v3_fields["overall_verdict"]))
        if json_out_dir:
            (json_out_dir / f"{wallet}_shift.json").write_text(
                json.dumps({
                    "wallet": wallet,
                    "expected_verdict": expected,
                    "v3_fields": v3_fields,
                    "v4_fields": v4_fields,
                    "diff": diff,
                }, indent=2, default=str)
            )

    print("## Phase B — verdict/surface shift table\n")
    print("| wallet | v3 verdict | v4 verdict | gate | new surfaces | classification |")
    print("|---|---|---|---|---|---|")
    for row in rows:
        print(row)
    if skipped:
        print("\n### Skipped\n")
        for s in skipped:
            print(f"- {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

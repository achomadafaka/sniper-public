#!/usr/bin/env python3
"""Backfill capital_ledger surface on existing v3 fixtures.

Task #58. Option B captured wallets 3-10 without `--capital-ledger`
because the Goldsky walk was the wall-clock bottleneck and the calibration
verdict didn't need it. This script restores the surface independently:
load each fixture, fetch capital_ledger via the same path that
record_fetch_snapshot.py uses, write back to the fixture in-place.

Safety:
* Idempotent: skips a fixture whose capital_ledger is already populated
  (`n_events > 0` in any collateral bucket OR n_splits+n_merges+n_redemptions > 0)
* No-silent-overwrite: writes the merged fixture to a staging path
  `<wallet>_<date>.recapture.json` first, then compares verdict +
  position counts via protocol replay. Promotes only if those match the
  original; surfaces a delta otherwise and skips promotion.
* Per-wallet status log at `/tmp/v3_capital_ledger_backfill_status.log`
  with per-event progress.

Usage:
    python3 scripts/backfill_capital_ledger.py            # all wallets in DEFAULT_WALLETS
    python3 scripts/backfill_capital_ledger.py <wallet>   # single wallet

Note: each wallet's Goldsky walk is multi-hour for heavy wallets.
"""
from __future__ import annotations

import io
import json
import sys
import time
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path


def _detect_repo_root() -> Path:
    here = Path(__file__).resolve()
    for ancestor in [here.parent, *here.parents]:
        if (ancestor / "tests" / "calibration" / "fixtures").is_dir():
            return ancestor
    raise RuntimeError("Could not detect repo root; expected tests/calibration/fixtures/")


REPO_ROOT = _detect_repo_root()
sys.path.insert(0, str(REPO_ROOT))

FIXTURE_DIR = REPO_ROOT / "tests" / "calibration" / "fixtures"
FIXTURE_DATE = "2026-05-13"
STATUS_LOG = Path("/tmp/v3_capital_ledger_backfill_status.log")

# Wallets 3-10 of the v3 calibration set (Option B was wallets 3-10).
# Wallets 1+2 already have capital_ledger and are NOT re-fetched.
DEFAULT_WALLETS = [
    "0x5bffcf561bcae83af680ad600cb99f1184d6ffbe",  # YatSen
    "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",  # exec-bot
    "0x22e4248bdb066f65c9f11cd66cdd3719a28eef1c",  # hwr 22e4
    "0x8493374739b45eb032382857feeb7ed025215366",  # insider 8493
    "0x1eb06435d217aafbc511e5e0225b46c6e2dbd25b",  # spec-bot
    "0x58f8f1138be2192696378629fc9aa23c7910dc70",  # arb_fund
    "0x2b46bb71681777202368d095196f266944a1684d",  # low_signal
    "0xb3b50ef66acf4beefafa754a4e4b8b5cd5b6bef4",  # hwr b3b5
]


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    with STATUS_LOG.open("a") as f:
        f.write(line + "\n")


def _fixture_path(wallet: str) -> Path:
    return FIXTURE_DIR / f"{wallet.lower()}_{FIXTURE_DATE}.json"


def _has_capital_ledger_already(payload: dict) -> bool:
    """True if the fixture's capital_ledger has any events populated."""
    cl = payload.get("capital_ledger")
    if not isinstance(cl, dict):
        return False
    if any(int(cl.get(k, 0) or 0) > 0 for k in ("n_splits", "n_merges", "n_redemptions")):
        return True
    summary = cl.get("collateral_summary") or {}
    if isinstance(summary, dict):
        for bucket in summary.values():
            if isinstance(bucket, dict) and float(bucket.get("n_events") or 0) > 0:
                return True
    return False


def _replay_verdict(wallet: str, fixture: Path) -> dict:
    """Run the protocol against the given fixture and return key fields."""
    import scripts.run_wallet_protocol as mod
    argv = [
        wallet, "--from-fixture", str(fixture),
        "--no-tags", "--no-peers", "--no-exec", "--no-history", "--no-archetype",
        "--seed", "1", "--n-iter", "2000",
        "--json", "--no-persist",
    ]
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = mod.main(argv)
    if rc != 0:
        raise RuntimeError(f"replay rc={rc}")
    r = json.loads(buf.getvalue().strip())
    return {
        "verdict": r.get("overall_verdict"),
        "primary_edge_type": r.get("primary_edge_type"),
        "gate_result": r.get("gate_result"),
        "n_closed": r.get("n_closed_positions"),
        "n_open": r.get("n_open_positions"),
        "n_activity": r.get("activity_rows_fetched"),
        "n_flags": len(r.get("data_quality_flags") or []),
    }


def _diffable_dict(a: dict, b: dict) -> dict:
    """Return per-key (a, b) tuples for keys where a != b."""
    diffs = {}
    for k in sorted(set(a) | set(b)):
        if a.get(k) != b.get(k):
            diffs[k] = (a.get(k), b.get(k))
    return diffs


def backfill_wallet(wallet: str) -> str:
    """Run capital_ledger backfill for one wallet. Returns status string."""
    wallet = wallet.lower()
    fixture = _fixture_path(wallet)
    if not fixture.exists():
        return f"FAIL: fixture not found at {fixture.name}"

    payload = json.loads(fixture.read_text())
    if _has_capital_ledger_already(payload):
        return f"SKIP: {wallet[:12]}... already has capital_ledger (idempotent)"

    _log(f"backfill {wallet[:14]}... starting")
    t0 = time.time()

    # Pre-replay verdict + counts on the un-augmented fixture (for diff).
    before = _replay_verdict(wallet, fixture)
    _log(f"  pre-backfill: {before}")

    # Run the actual capital_ledger fetch. Hours possible for heavy wallets.
    from sniper.analysis.wallet_protocol.capital_ledger import compute_capital_ledger
    try:
        ledger = compute_capital_ledger(wallet)
    except Exception as e:
        return f"FAIL: {wallet[:12]}... compute_capital_ledger raised: {type(e).__name__}: {e}"

    elapsed = time.time() - t0
    _log(
        f"  fetched n_splits={ledger.n_splits} n_merges={ledger.n_merges} "
        f"n_redemptions={ledger.n_redemptions} elapsed={elapsed:.0f}s"
    )

    # Inject into a STAGED copy of the fixture, never the original yet.
    staged = payload.copy()
    staged["capital_ledger"] = asdict(ledger)
    stage_path = fixture.with_suffix(".recapture.json")
    stage_path.write_text(json.dumps(staged, indent=2, default=str) + "\n")
    _log(f"  staged to {stage_path.name}")

    # Replay staged copy and confirm the verdict + counts haven't drifted
    # in any way that capital_ledger shouldn't cause. Position counts and
    # the gate verdict are independent of capital_ledger; data_quality_flags
    # n is allowed to grow because we expect new accounting tie + possibly
    # CAPITAL_LEDGER_COLLATERAL_MIXED flags. Diff is informational.
    after = _replay_verdict(wallet, stage_path)
    _log(f"  post-backfill: {after}")

    blocking_keys = {"verdict", "primary_edge_type", "gate_result", "n_closed", "n_open"}
    diffs = _diffable_dict(before, after)
    blocking_diff = {k: v for k, v in diffs.items() if k in blocking_keys}
    if blocking_diff:
        # Do not promote — surface for human review.
        _log(f"  ✗ BLOCKING DIFF: {blocking_diff}")
        return (
            f"BLOCK: {wallet[:12]}... blocking diff between pre/post: "
            f"{blocking_diff}; staged fixture at {stage_path.name} retained"
        )

    # All blocking fields unchanged — promote staged → final.
    bytes_before = fixture.stat().st_size
    bytes_after = stage_path.stat().st_size
    fixture.write_text(stage_path.read_text())
    stage_path.unlink()
    _log(
        f"  ✓ promoted: bytes {bytes_before:,} → {bytes_after:,} "
        f"(+{bytes_after - bytes_before:,}); flag count {before['n_flags']} → {after['n_flags']}"
    )
    return f"OK: {wallet[:12]}... elapsed={elapsed:.0f}s flags={before['n_flags']}→{after['n_flags']}"


def main(argv: list[str]) -> int:
    wallets = argv if argv else DEFAULT_WALLETS
    STATUS_LOG.write_text("")  # reset on each run
    _log(f"backfill start: {len(wallets)} wallets")
    results: list[tuple[str, str]] = []
    for w in wallets:
        try:
            status = backfill_wallet(w)
        except Exception as e:
            status = f"CRASH: {w[:12]}... {type(e).__name__}: {e}"
        _log(status)
        results.append((w, status))
    _log("backfill complete")
    Path("/tmp/v3_capital_ledger_backfill_done").touch()
    # Summary
    print("\n=== SUMMARY ===")
    for w, s in results:
        print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

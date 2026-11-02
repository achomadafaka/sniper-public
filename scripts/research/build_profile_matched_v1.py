#!/usr/bin/env python3
"""Build the frozen `profile_matched_v1` artifacts, one per (wallet, chain).

    docs/superpowers/specs/2026-08-29-profile-matched-v1-fitting-addendum.md

Deterministic by construction: the same `fit_window_end` over the same input rows reproduces every
artifact byte for byte, and tests/test_memecoin_profile_fit.py asserts it. The artifact records the
script's own sha256 and a hash of the exact rows the fit consumed, so a later reader can tell
whether a difference came from the code or the data.

Writes ONE file. It never touches the funded ledger, never resolves a live policy, and never runs
at decision time — the arm reads what this leaves behind.
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import hashlib
import json
import sqlite3
import sys
from math import log1p
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from sniper.services.memecoin_profile_fit import (      # noqa: E402
    build_artifact, load_observations, observations_sql, shrinkage_ratio)

OUT = REPO / "data" / "profile_matched_v1.json"


def cohort_members(conn, fit_window_end):
    """Every (uid, chain) with at least one comparable close before the cutoff, plus the roster
    wallets that have none — those need a fallback artifact rather than no artifact, or the arm
    would silently have nothing to load for them."""
    rows = conn.execute(
        "SELECT DISTINCT source_user_id, network_id FROM memecoin_positions "
        "WHERE closed_at < ? AND " + observations_sql() + " ORDER BY source_user_id, network_id",
        (fit_window_end,)).fetchall()
    return [(r[0], r[1]) for r in rows if r[0]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fit-window-end", required=True,
                    help="the GLOBAL cohort cutoff (A6). One value for every artifact.")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args()

    script_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    conn = sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"), uri=True)
    conn.row_factory = sqlite3.Row

    members = cohort_members(conn, a.fit_window_end)
    per_member = {(uid, chain): load_observations(
        conn, fit_window_end=a.fit_window_end, source_user_id=uid, chain=chain)
        for uid, chain in members}

    # The cohort statistics are computed ONCE over every member, so each wallet shrinks toward the
    # same target. Computing them per wallet would shrink each toward its own mean, which is not
    # shrinkage at all.
    ratio, estimated = shrinkage_ratio(list(per_member.values()))
    everything = [o for obs in per_member.values() for o in obs]
    cohort_mean_log = (sum(log1p(max(o.peak, -0.99)) for o in everything) / len(everything)
                       if everything else 0.0)

    artifacts = []
    for (uid, chain), obs in sorted(per_member.items()):
        artifacts.append(build_artifact(
            source_user_id=uid, chain=chain, fit_window_end=a.fit_window_end,
            observations=obs, cohort_mean_log=cohort_mean_log, ratio=ratio,
            ratio_estimated=estimated,
            stop_at_fit=stop_at_fit(conn, uid, chain, a.fit_window_end),
            script_sha256=script_sha))

    doc = {
        "spec": "docs/superpowers/specs/2026-08-29-profile-matched-v1-fitting-addendum.md",
        "fit_window_end": a.fit_window_end,
        "peak_space": "executable",
        "shrinkage_ratio": round(ratio, 6),
        "shrinkage_ratio_estimated": estimated,
        "cohort_mean_log_peak": round(cohort_mean_log, 8),
        "cohort_observations": len(everything),
        "script_sha256": script_sha,
        "artifacts": artifacts,
    }
    body = json.dumps(doc, indent=1, sort_keys=True)
    fitted = [x for x in artifacts if not x["fallback"]]
    print("profile_matched_v1  fit_window_end=%s" % a.fit_window_end)
    print("  members            %d  (%d fitted, %d fallback)"
          % (len(artifacts), len(fitted), len(artifacts) - len(fitted)))
    print("  observations       %d  (%d censored)"
          % (len(everything), sum(1 for o in everything if o.censored)))
    print("  shrinkage ratio    %.3f  (%s)"
          % (ratio, "estimated at this fit" if estimated else "DEFAULT, not estimable"))
    if fitted:
        tps = sorted(x["tp_pct"] for x in fitted)
        print("  tp_pct             min %.1f  median %.1f  max %.1f"
              % (tps[0], tps[len(tps) // 2], tps[-1]))
    if a.write:
        Path(a.out).write_text(body)
        print("  wrote %s (sha256 %s)"
              % (a.out, hashlib.sha256(body.encode()).hexdigest()[:16]))
    return 0


def stop_at_fit(conn, uid, chain, fit_window_end):
    """The stop this origin's last prior funded position actually ran under (A5, audit only).

    THE STOP IS NOT A COLUMN. It lives inside the position's immutable funded contract, and an
    earlier version of this read `memecoin_positions.stop_pct`, which does not exist — the schema
    validator caught it. That read was already guarded by a column-existence check, so it would
    never have raised: every artifact would simply have carried `stop_at_fit: null`, and A5's
    divergence audit would have been silently dead while looking implemented.
    """
    from sniper.services.memecoin_funded_policy import frozen_policy_from_position
    row = conn.execute(
        "SELECT * FROM memecoin_positions WHERE source_user_id=? AND network_id=? "
        "AND closed_at < ? AND execution_mode='real' ORDER BY closed_at DESC LIMIT 1",
        (uid, chain, fit_window_end)).fetchone()
    if row is None:
        return None
    try:
        policy = frozen_policy_from_position(dict(row))
    except Exception:
        return None          # a legacy or unparseable contract is not an audit failure
    return float(policy.stop_pct) if policy is not None else None



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""THE preregistered selection rule for `radar-realistic-v3`'s MAX_BASELINE_AGE_MS.

Frozen by operator decision 2026-08-11, BEFORE any qualifying window exists. This file
is the rule. The later commit substitutes only the measured integer into
`paper_lifecycle_realistic_v3.MAX_BASELINE_AGE_MS`; it must not touch the rule.

Why the rule had to be frozen first: the number decides how much of the cohort survives,
and picking it after seeing outcomes would tune the censor on results.

  1. Read a `paper-sampler-v2` window. That identity exists because a sampler window
     SEALS permanently (`paper_sampler.persistence_due` returns False once `_sealed` is
     set) and because v1 measured the load from BEFORE v3. Under v3 the v2 lifecycle
     keeps quoting a decided position to the 90 s terminal age, so v1 round trips are
     not production-shaped. v1's 5.2-21 s range CANNOT set this number. The real v1 row
     never sealed either: the a893d40e cutover cut it off mid-window at 176 requests, so
     it is an incomplete historical row, not a completed measurement.
  2. `MAX_BASELINE_AGE_MS` stays None until this script reports ACTIVATE.
  3. `radar-realistic-v3` stays inactive until then.
  4. Count only Pump receipts with a full 90 s of terminal age inside the window. A
     position the window cut short has a censored baseline, not a late one.
  5. Require at least 25 eligible Pump positions.
  6. Use only the scheduled 5,000 ms executable observation. No other bucket is a
     baseline.
  7. A missing baseline counts as INFINITE latency. Dropping those rows would measure
     the providers that answered and call it coverage.
  8. Latency is `quote_completed_at - receipt_at`.
  9. Round p90 up to the next whole 1,000 ms.
 10. Require at least 90% baseline coverage.
 11. Hard maximum 15,000 ms.
 12. p90 above 15,000 ms means DO NOT ACTIVATE.

Rule 11 is not a preference. A 60 s policy with a 15 s baseline still measures 45 s of
the path. A later baseline forgives too much of it.

Usage:
  python3 scripts/radar_v3_baseline_age_preregistration.py
  python3 scripts/radar_v3_baseline_age_preregistration.py --db data/memecoin_opportunity.db
"""
import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, __file__.rsplit("/", 2)[0])

from sniper.research.memecoin_opportunity.paper_lifecycle import (  # noqa: E402
    LIFECYCLE_VERSION as SOURCE_LIFECYCLE_VERSION,
    TERMINAL_AGE_MS,
)
from sniper.research.memecoin_opportunity.paper_sampler import (  # noqa: E402
    DETAIL_VERSION as REQUIRED_SAMPLER_VERSION,
    SOURCE_KEY as SAMPLER_SOURCE_KEY,
)

BASELINE_AGE_MS = 5_000
MIN_ELIGIBLE_POSITIONS = 25
MIN_COVERAGE_BPS = 9_000
HARD_MAX_BASELINE_AGE_MS = 15_000
ROUNDING_MS = 1_000
VENUE_ID = "pumpfun_solana"


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _p90(values: list[float | None]) -> float | None:
    """Nearest-rank p90 over a list in which None means infinite latency.

    The missing baselines stay in the DENOMINATOR, which is what rule 7 buys: with 76
    positions at 1,000 ms, 5 at 9,000 ms and 9 missing, p90 is 9,000 ms. Drop the missing
    rows and it collapses to 1,000 ms -- the providers that answered, reported as if they
    were the coverage.

    Returning None (p90 itself lands among the missing) cannot happen downstream of
    rule 10: at 90% coverage or better the 90th percentile is always a real measurement.
    The branch stays as a fail-closed guard, not as a live path.
    """
    if not values:
        return None
    ordered = sorted(v for v in values if v is not None)
    index = max(0, math.ceil(0.90 * len(values)) - 1)
    if index >= len(ordered):
        return None  # p90 lands inside the missing baselines
    return ordered[index]


def evaluate(conn) -> dict[str, object]:
    window = conn.execute(
        "SELECT heartbeat_at,detail_json FROM op_runtime_status WHERE source_key=?",
        (SAMPLER_SOURCE_KEY,),
    ).fetchone()
    if window is None:
        return {"verdict": "NO WINDOW", "reason": f"{SAMPLER_SOURCE_KEY} has no row yet"}
    detail = json.loads(window["detail_json"] or "{}")
    if detail.get("detail_version") != REQUIRED_SAMPLER_VERSION:
        return {
            "verdict": "WRONG WINDOW",
            "reason": (
                f"found {detail.get('detail_version')!r}, need "
                f"{REQUIRED_SAMPLER_VERSION!r} recorded under v3 collection behavior"
            ),
        }
    started_at = detail.get("window_started_at")
    ends_at = detail.get("window_ends_at")
    if not detail.get("window_complete"):
        return {
            "verdict": "WINDOW OPEN",
            "reason": f"{started_at} .. {ends_at} has not closed",
        }

    # Rule 4: the receipt needs a full terminal age inside the window.
    latest_receipt = (_parse(ends_at) - timedelta(milliseconds=TERMINAL_AGE_MS)).isoformat()
    latest_receipt = latest_receipt.replace("+00:00", "Z")
    rows = conn.execute(
        "SELECT f.request_id,f.receipt_at,o.quote_completed_at "
        "FROM op_paper_fill_requests r JOIN op_paper_fills f USING(request_id) "
        "LEFT JOIN op_paper_exit_observations o "
        "  ON o.request_id=r.request_id AND o.lifecycle_version=? "
        "  AND o.scheduled_age_ms=? AND o.status='executable' "
        "WHERE f.status='filled' AND r.venue_id=? "
        "AND f.receipt_at>=? AND f.receipt_at<=?",
        (SOURCE_LIFECYCLE_VERSION, BASELINE_AGE_MS, VENUE_ID, started_at, latest_receipt),
    ).fetchall()
    eligible = len(rows)
    if eligible < MIN_ELIGIBLE_POSITIONS:
        return {
            "verdict": "TOO FEW POSITIONS",
            "reason": f"{eligible} eligible Pump positions, rule 5 needs "
                      f"{MIN_ELIGIBLE_POSITIONS}",
            "eligible": eligible,
        }

    # Rules 6, 7 and 8.
    latencies: list[float | None] = []
    for row in rows:
        if row["quote_completed_at"] is None:
            latencies.append(None)
            continue
        delta = _parse(row["quote_completed_at"]) - _parse(row["receipt_at"])
        latencies.append(max(0.0, delta.total_seconds() * 1_000))
    covered = sum(1 for value in latencies if value is not None)
    coverage_bps = round(10_000 * covered / eligible)

    result: dict[str, object] = {
        "window": f"{started_at} .. {ends_at}",
        "eligible": eligible,
        "with_baseline": covered,
        "coverage_bps": coverage_bps,
    }
    if coverage_bps < MIN_COVERAGE_BPS:
        result["verdict"] = "DO NOT ACTIVATE"
        result["reason"] = (
            f"baseline coverage {coverage_bps / 100:.1f}% is below the 90% rule 10 floor"
        )
        return result

    raw_p90 = _p90(latencies)
    if raw_p90 is None:
        result["verdict"] = "DO NOT ACTIVATE"
        result["reason"] = "p90 falls inside the missing baselines (rule 7: infinite)"
        return result
    rounded = int(math.ceil(raw_p90 / ROUNDING_MS) * ROUNDING_MS)  # rule 9
    result["p90_ms"] = round(raw_p90)
    result["max_baseline_age_ms"] = rounded
    if rounded > HARD_MAX_BASELINE_AGE_MS:
        result["verdict"] = "DO NOT ACTIVATE"
        result["reason"] = (
            f"p90 {rounded} ms is above the {HARD_MAX_BASELINE_AGE_MS} ms hard maximum "
            "(rule 12); a later baseline forgives too much of the 60 s path"
        )
        return result
    result["verdict"] = "ACTIVATE"
    result["reason"] = (
        f"set MAX_BASELINE_AGE_MS = {rounded} in paper_lifecycle_realistic_v3.py, "
        "commit it as the measured integer, and change nothing else"
    )
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/memecoin_opportunity.db")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    result = evaluate(conn)
    print(f"VERDICT: {result.pop('verdict')}")
    for key, value in result.items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()

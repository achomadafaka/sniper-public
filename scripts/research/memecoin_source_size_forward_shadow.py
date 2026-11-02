#!/usr/bin/env python3
"""Report the sealed Solana source-size forward cohort without changing entry authority."""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import hashlib
import json
import math
import os
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sniper.persistence import db_memecoin as m  # noqa: E402
from sniper.services import memecoin_paper_report as paper  # noqa: E402
from sniper.util.clock import real_utc_now  # noqa: E402


ARMS = (
    "alpha_b",
    "combo_f",
    "control",
    "momentum_a",
    "peak_lock_e",
    "real_shadow",
    "scalp_c2",
    "trail_d",
)
CHAIN = "solana"
COHORT_ID = "0e8fc8e02bac8bbe472d18597d18d63de09dd7af81455ed29e11c78baaf83cda"
CUTOFF = "2026-08-19T17:33:35.131664+00:00"
SOURCE_SIZE_FLOOR_USD = 500.0
PAPER_NOTIONAL_USD = 100.0
PRIMARY_ARM = "alpha_b"
SECONDARY_ARM = "combo_f"
MIN_ORIGINS_PER_GROUP = 30
MIN_MINTS_PER_GROUP = 20
MIN_DAYS_PER_GROUP = 7
BOOTSTRAP_ITERATIONS = 10_000
BOOTSTRAP_SEED = 20260819


def _aware(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("the cohort cutoff must include a UTC offset")
    return parsed


def _canonical_json(payload):
    try:
        value = json.loads(payload or "")
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _frozen_policy_is_valid(conn, row, cohort_id):
    """Verify the cohort and its immutable source-episode policy assignment."""
    cohort_json = _canonical_json(row.get("paper_cohort_json"))
    policy_json = _canonical_json(row.get("assigned_policy_json"))
    if cohort_json is None or policy_json is None:
        return False
    if hashlib.sha256(cohort_json.encode()).hexdigest() != cohort_id:
        return False
    assigned = conn.execute(
        "SELECT p.assigned_policy_id,p.assigned_policy_version,"
        "a.policy_id,a.policy_version,v.parameters_json,v.immutable_hash "
        "FROM memecoin_positions p "
        "JOIN memecoin_entry_snapshots s ON s.upstream_event_id=p.origin_event_id "
        "JOIN memecoin_policy_assignments a "
        "ON a.episode_id=s.episode_id AND a.arm_id=p.strategy "
        "JOIN memecoin_policy_versions v "
        "ON v.policy_id=a.policy_id AND v.version=a.policy_version "
        "WHERE p.id=?",
        (row["id"],),
    ).fetchone()
    if assigned is None:
        return False
    version_json = _canonical_json(assigned[4])
    return (
        (assigned[0], assigned[1]) == (assigned[2], assigned[3])
        and policy_json == version_json
        and hashlib.sha256(str(assigned[4]).encode()).hexdigest() == assigned[5]
    )


def _source_event(conn, origin_event_id):
    """Return the exact source event time and its positive source size."""
    rows = conn.execute(
        "SELECT created_at,their_size_usd FROM memecoin_events WHERE fomo_event_id=?",
        (str(origin_event_id),),
    ).fetchall()
    if len(rows) != 1:
        return None
    event_at = paper._parse(rows[0][0])
    if event_at is None:
        return None
    try:
        size = float(rows[0][1]) if rows[0][1] is not None else None
    except (TypeError, ValueError):
        size = None
    return event_at, size if size is not None and size > 0 else None


def load_forward_origins(conn, *, arms=ARMS, chain=CHAIN, cohort_id=COHORT_ID,
                         cutoff=CUTOFF):
    """Return complete measurable origins from the frozen cohort after the sealed cutoff."""
    if chain not in paper.CHAIN_IDS:
        raise ValueError(f"invalid chain: {chain}")
    arms = tuple(arms)
    cutoff_at = _aware(cutoff)
    now = datetime.fromtimestamp(real_utc_now(), timezone.utc)
    rows = [paper._add_outcome(row, now) for row in paper._project_rows(conn, chain)]
    origins = []
    for (cohort, event), group in paper._exact_groups(rows, arms, forward=True):
        if cohort != cohort_id:
            continue
        if any(row.get("is_open") or row.get("report_pnl") is None for row in group):
            continue
        if any(
            not _frozen_policy_is_valid(conn, row, cohort_id)
            for row in group
        ):
            continue
        source_event = _source_event(conn, event)
        if source_event is None or source_event[0] < cutoff_at:
            continue
        opened = [paper._parse(row.get("opened_at")) for row in group]
        if any(value is None for value in opened):
            continue
        mints = {str(row.get("mint") or "") for row in group}
        if len(mints) != 1 or not next(iter(mints)):
            continue
        returns = {}
        valid = True
        for row in group:
            notional = float(row.get("entry_notional") or 0.0)
            if not math.isclose(
                notional, PAPER_NOTIONAL_USD, rel_tol=0.0, abs_tol=1e-9,
            ):
                valid = False
                break
            returns[row["strategy"]] = 100.0 * float(row["report_pnl"]) / notional
        if not valid:
            continue
        origins.append({
            "origin_event_id": event,
            "mint": next(iter(mints)),
            "opened_at": min(value for value in opened if value is not None).isoformat(),
            "source_size_usd": source_event[1],
            "returns_pct": returns,
        })
    return sorted(origins, key=lambda row: (row["opened_at"], row["origin_event_id"]))


def _group_summary(rows):
    arms = sorted({arm for row in rows for arm in row["returns_pct"]})
    by_arm = {}
    for arm in arms:
        values = [float(row["returns_pct"][arm]) for row in rows]
        by_arm[arm] = {
            "n": len(values),
            "median_return_pct": statistics.median(values),
            "mean_return_pct": statistics.mean(values),
            "win_rate_pct": 100.0 * sum(value > 0 for value in values) / len(values),
        }
    return {
        "origins": len(rows),
        "mints": len({row["mint"] for row in rows}),
        "utc_days": len({
            paper._parse(row["opened_at"]).astimezone(timezone.utc).date().isoformat()
            for row in rows
        }),
        "arms": by_arm,
    }


def _percentile(values, q):
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _mint_bootstrap(rows, *, arm, iterations, seed):
    by_mint = defaultdict(list)
    for row in rows:
        by_mint[row["mint"]].append(row)
    mints = sorted(by_mint)
    rng = random.Random(seed)
    deltas = []
    for _ in range(max(0, int(iterations))):
        below = []
        above = []
        for _sample in mints:
            mint = rng.choice(mints)
            for row in by_mint[mint]:
                target = below if row["source_size_usd"] < SOURCE_SIZE_FLOOR_USD else above
                target.append(float(row["returns_pct"][arm]))
        if below and above:
            deltas.append(statistics.median(above) - statistics.median(below))
    if not deltas:
        return None
    return [_percentile(deltas, 0.025), _percentile(deltas, 0.975)]


def _arm_contrast(groups, known, *, arm, iterations, seed):
    contrast = {
        "arm": arm,
        "median_delta_pct_points": None,
        "win_delta_pct_points": None,
        "mint_bootstrap_95_pct_points": None,
    }
    low_arm = groups["below_500"]["arms"].get(arm)
    high_arm = groups["at_least_500"]["arms"].get(arm)
    if low_arm is None or high_arm is None:
        return contrast
    contrast["median_delta_pct_points"] = (
        high_arm["median_return_pct"] - low_arm["median_return_pct"]
    )
    contrast["win_delta_pct_points"] = (
        high_arm["win_rate_pct"] - low_arm["win_rate_pct"]
    )
    contrast["mint_bootstrap_95_pct_points"] = _mint_bootstrap(
        known, arm=arm, iterations=iterations, seed=seed,
    )
    return contrast


def summarize_forward_origins(origins, *, bootstrap_iterations=BOOTSTRAP_ITERATIONS,
                              seed=BOOTSTRAP_SEED):
    """Apply the fixed threshold, sample floors, and mint-cluster verdict contract."""
    known = [row for row in origins if row.get("source_size_usd") is not None]
    below = [row for row in known if row["source_size_usd"] < SOURCE_SIZE_FLOOR_USD]
    above = [row for row in known if row["source_size_usd"] >= SOURCE_SIZE_FLOOR_USD]
    groups = {
        "below_500": _group_summary(below),
        "at_least_500": _group_summary(above),
    }
    ready = all(
        group["origins"] >= MIN_ORIGINS_PER_GROUP
        and group["mints"] >= MIN_MINTS_PER_GROUP
        and group["utc_days"] >= MIN_DAYS_PER_GROUP
        for group in groups.values()
    )
    primary = _arm_contrast(
        groups, known, arm=PRIMARY_ARM, iterations=bootstrap_iterations, seed=seed,
    )
    secondary = _arm_contrast(
        groups, known, arm=SECONDARY_ARM, iterations=bootstrap_iterations, seed=seed,
    )
    verdict = None
    if ready:
        interval = primary["mint_bootstrap_95_pct_points"]
        if interval and interval[0] > 0 and primary["win_delta_pct_points"] > 0:
            verdict = "ESTABLISHED"
        elif interval and interval[1] <= 0:
            verdict = "REFUTED"
        else:
            verdict = "WEAK"
    return {
        "contract": {
            "chain": CHAIN,
            "cohort_id": COHORT_ID,
            "cutoff": CUTOFF,
            "source_size_floor_usd": SOURCE_SIZE_FLOOR_USD,
            "paper_notional_usd": PAPER_NOTIONAL_USD,
            "primary_arm": PRIMARY_ARM,
            "secondary_arm": SECONDARY_ARM,
            "arms": list(ARMS),
            "min_origins_per_group": MIN_ORIGINS_PER_GROUP,
            "min_mints_per_group": MIN_MINTS_PER_GROUP,
            "min_days_per_group": MIN_DAYS_PER_GROUP,
        },
        "coverage": {
            "strict_origins": len(origins),
            "known_size": len(known),
            "unknown_size": len(origins) - len(known),
        },
        "groups": groups,
        "primary": primary,
        "secondary": secondary,
        "status": "READY" if ready else "COLLECTING",
        "verdict": verdict,
    }


def load_funded_actuals(conn, *, chain=CHAIN, cutoff=CUTOFF):
    """Return measurable funded outcomes for description only."""
    if chain not in paper.CHAIN_IDS or not paper.CHAIN_IDS[chain]:
        raise ValueError(f"invalid funded chain: {chain}")
    cutoff_time = _aware(cutoff)
    chain_ids = paper.CHAIN_IDS[chain]
    marks = ",".join("?" for _ in chain_ids)
    rows = conn.execute(
        "SELECT p.id,p.origin_event_id,p.mint,p.opened_at,p.entry_notional,"
        "p.realized_pnl FROM memecoin_positions p "
        "WHERE p.execution_mode='real' "
        f"AND LOWER(COALESCE(p.network_id,'')) IN ({marks}) AND "
        + m.measurable_outcome_sql("p"),
        chain_ids,
    ).fetchall()
    by_origin = defaultdict(list)
    for raw in rows:
        row = dict(raw)
        if row.get("origin_event_id"):
            by_origin[row["origin_event_id"]].append(row)
    actuals = []
    for event, group in by_origin.items():
        if len(group) != 1:
            continue
        row = group[0]
        notional = float(row.get("entry_notional") or 0.0)
        if notional <= 0:
            continue
        source_event = _source_event(conn, event)
        if source_event is None or source_event[0] < cutoff_time:
            continue
        size = source_event[1]
        actuals.append({
            "origin_event_id": event,
            "mint": row["mint"],
            "opened_at": row["opened_at"],
            "source_size_usd": size,
            "return_pct": 100.0 * float(row["realized_pnl"]) / notional,
            "pnl_usd": float(row["realized_pnl"]),
        })
    return sorted(actuals, key=lambda row: (row["opened_at"], row["origin_event_id"]))


def _funded_group(rows):
    returns = [row["return_pct"] for row in rows]
    return {
        "origins": len(rows),
        "mints": len({row["mint"] for row in rows}),
        "median_return_pct": statistics.median(returns) if returns else None,
        "mean_return_pct": statistics.mean(returns) if returns else None,
        "win_rate_pct": (
            100.0 * sum(value > 0 for value in returns) / len(returns) if returns else None
        ),
        "pnl_usd": sum(row["pnl_usd"] for row in rows),
    }


def summarize_funded_actuals(rows):
    """Describe funded outcomes without producing a promotion verdict."""
    known = [row for row in rows if row.get("source_size_usd") is not None]
    below = [row for row in known if row["source_size_usd"] < SOURCE_SIZE_FLOOR_USD]
    above = [row for row in known if row["source_size_usd"] >= SOURCE_SIZE_FLOOR_USD]
    return {
        "origins": len(rows),
        "known_size": len(known),
        "unknown_size": len(rows) - len(known),
        "groups": {
            "below_500": _funded_group(below),
            "at_least_500": _funded_group(above),
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=os.getenv("DB_PATH", "sniper_tracker.db"))
    args = parser.parse_args(argv)
    conn = sqlite3.connect(f"file:{Path(args.db).resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        report = summarize_forward_origins(load_forward_origins(conn))
        report["funded_descriptive"] = summarize_funded_actuals(load_funded_actuals(conn))
    finally:
        conn.close()
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('summarize_forward_origins', 'main'))

if __name__ == "__main__":
    raise SystemExit(main())

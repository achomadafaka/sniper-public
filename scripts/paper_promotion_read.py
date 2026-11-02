#!/usr/bin/env python3
"""The wallet-promotion study's single registered read.

    docs/superpowers/specs/2026-08-28-wallet-promotion-prereg.md, section 7a

It computes the registered estimand and nothing else: realized PnL per dollar deployed,
`profile_matched_v1` MINUS `funded_clone`, paired on `origin_event_id`, with the uncertainty from a
bootstrap over `(wallet, UTC day)` clusters.

THE CENSOR IS CALLED, NEVER RESTATED. `measurable_outcome_sql()` is the registered predicate and it
already carries `HONEST_COPY_SQL`. A copy of it here could drift from the one the report uses, and
then the study and the report would censor different populations while both claimed section 4.

THE PAPER-PEAK CLAUSE OF SECTION 4 IS NOT APPLIED. It is scoped to funded rows in the prereg: on
paper the column is missing universally, so inheriting it would file every paper row as
never-touched.

READ-ONLY. It opens the funded ledger through `get_readonly_connection()` and writes nothing.
`--dry-run` prints the row counts and the dataset hash and NO estimand, so the population can be
checked before the number exists.
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[1]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import collections
import contextlib
import hashlib
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from sniper.persistence.db_core import get_readonly_connection  # noqa: E402
from sniper.persistence.db_memecoin import measurable_outcome_sql  # noqa: E402

ARMS = ("funded_clone", "profile_matched_v1")
CONTROL, TREATMENT = ARMS
SEED = 20260922          # section 7a
DRAWS = 10000            # section 7a
OUTLIER_RET = 3.0        # closes above +300%, reported both ways


def rows_for(conn, cutoff):
    """Censor-surviving closed paper rows for both arms, before `cutoff`."""
    sql = (
        "SELECT id, strategy, origin_event_id, network_id, source_user_id, "
        "       substr(opened_at,1,10) AS utc_day, realized_pnl, entry_notional "
        "FROM memecoin_positions "
        "WHERE execution_mode='paper' AND strategy IN (?,?) "
        "  AND COALESCE(experiment_status,'') = 'eligible' "
        "  AND opened_at < ? "
        "  AND origin_event_id IS NOT NULL AND entry_notional > 0 "
        f"  AND {measurable_outcome_sql('memecoin_positions')}"
    )
    return [dict(r) for r in conn.execute(sql, (CONTROL, TREATMENT, cutoff))]


def pair(rows):
    """One observation per origin where BOTH arms survived the censor."""
    by_origin = collections.defaultdict(dict)
    for r in rows:
        by_origin[r["origin_event_id"]][r["strategy"]] = r
    pairs = []
    for origin, arms in by_origin.items():
        if CONTROL not in arms or TREATMENT not in arms:
            continue
        c, t = arms[CONTROL], arms[TREATMENT]
        pairs.append({
            "origin": origin,
            "network_id": c["network_id"],
            "wallet": c["source_user_id"],
            "utc_day": c["utc_day"],
            "pnl_c": float(c["realized_pnl"]), "notional_c": float(c["entry_notional"]),
            "pnl_t": float(t["realized_pnl"]), "notional_t": float(t["entry_notional"]),
        })
    pairs.sort(key=lambda p: (str(p["origin"]), str(p["network_id"])))
    return pairs


def dataset_hash(pairs):
    h = hashlib.sha256()
    for p in pairs:
        h.update(("%s|%s|%s|%s|%.10f|%.10f|%.10f|%.10f\n" % (
            p["origin"], p["network_id"], p["wallet"], p["utc_day"],
            p["pnl_c"], p["notional_c"], p["pnl_t"], p["notional_t"])).encode())
    return h.hexdigest()


def estimand(pairs):
    """Per dollar deployed, treatment minus control. None when a denominator is empty."""
    nc = sum(p["notional_c"] for p in pairs)
    nt = sum(p["notional_t"] for p in pairs)
    if nc <= 0 or nt <= 0:
        return None
    return (sum(p["pnl_t"] for p in pairs) / nt) - (sum(p["pnl_c"] for p in pairs) / nc)


def cluster_bootstrap(pairs, *, draws=DRAWS, seed=SEED):
    """Resample (wallet, UTC day) cells with replacement. Percentile interval at 95%."""
    cells = collections.defaultdict(list)
    for p in pairs:
        cells[(p["wallet"], p["utc_day"])].append(p)
    keys = list(cells)
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        sample = []
        for _ in range(len(keys)):
            sample.extend(cells[keys[rng.randrange(len(keys))]])
        value = estimand(sample)
        if value is not None:
            out.append(value)
    out.sort()
    if not out:
        return None, None, 0
    lo = out[int(0.025 * (len(out) - 1))]
    hi = out[int(0.975 * (len(out) - 1))]
    return lo, hi, len(keys)


def report(pairs, label):
    value = estimand(pairs)
    if value is None or not pairs:
        print("  %-22s n=%-5d NO ESTIMAND (empty denominator)" % (label, len(pairs)))
        return
    lo, hi, cells = cluster_bootstrap(pairs)
    wallets = len({p["wallet"] for p in pairs})
    days = len({p["utc_day"] for p in pairs})
    print("  %-22s n=%-5d wallets=%-4d days=%-3d clusters=%-4d  %+.3f%% per $  CI95 [%+.3f%%, %+.3f%%]"
          % (label, len(pairs), wallets, days, cells, 100 * value, 100 * lo, 100 * hi))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cutoff", required=True, help="ISO timestamp; positions opened before it")
    ap.add_argument("--dry-run", action="store_true", help="counts and hash only, no estimand")
    a = ap.parse_args()

    # closing(), not a bare `with`. sqlite3's own context manager ends the TRANSACTION and
    # leaves the handle OPEN, so the read connection outlived this block for the whole run.
    with contextlib.closing(get_readonly_connection()) as conn:
        rows = rows_for(conn, a.cutoff)
    pairs = pair(rows)
    digest = dataset_hash(pairs)
    per_arm = collections.Counter(r["strategy"] for r in rows)

    print("wallet-promotion read — prereg section 7a")
    print("cutoff            %s" % a.cutoff)
    print("censor            measurable_outcome_sql('memecoin_positions')  (carries HONEST_COPY_SQL)")
    print("rows surviving    %s=%d  %s=%d" % (CONTROL, per_arm[CONTROL], TREATMENT, per_arm[TREATMENT]))
    print("matched pairs     %d" % len(pairs))
    print("wallets / days    %d / %d" % (len({p["wallet"] for p in pairs}),
                                         len({p["utc_day"] for p in pairs})))
    by_chain = collections.Counter(p["network_id"] for p in pairs)
    print("pairs by chain    %s" % json.dumps(dict(by_chain.most_common())))
    print("dataset sha256    %s" % digest)
    print("seed / draws      %d / %d" % (SEED, DRAWS))

    if a.dry_run:
        print("\n--dry-run: no estimand computed.")
        return 0

    print("\nESTIMAND — realized PnL per dollar, %s MINUS %s" % (TREATMENT, CONTROL))
    report(pairs, "all pairs")
    solana = [p for p in pairs if p["network_id"] == "solana"]
    report(solana, "solana")
    for chain in [c for c in by_chain if c != "solana"]:
        report([p for p in pairs if p["network_id"] == chain], "chain %s" % chain)

    kept = [p for p in pairs
            if p["pnl_c"] / p["notional_c"] <= OUTLIER_RET and p["pnl_t"] / p["notional_t"] <= OUTLIER_RET]
    print("\nwithout closes above +%d%% in either arm (%d pairs dropped)"
          % (int(100 * OUTLIER_RET), len(pairs) - len(kept)))
    report(kept, "all pairs")
    report([p for p in kept if p["network_id"] == "solana"], "solana")
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('report', 'main'))

if __name__ == "__main__":
    raise SystemExit(main())

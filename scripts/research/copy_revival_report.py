#!/usr/bin/env python3
"""Read-only copy-revival counts for the currently armed wallet/chain pairs."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sniper.persistence.db_memecoin import _parse_real_chains
from sniper.services.memecoin_chains import canonical_chain_id


# Raw event statuses mapped to refusal/safety_failed by the funded runner's _trace_terminal.
REFUSALS = {
    "not_approved", "skipped_mcap", "stale", "skipped_chase", "disarmed",
    "dropped_base_asset", "ambiguous_network", "unresolved_network", "unroutable_chain",
    "network_mint_mismatch", "rejected", "pre_arm_backlog", "unknown_source",
    "source_initiation_unknown", "dropped_network", "evm_unarmed", "unsafe",
    "refused", "safety_failed",
}


def report(db, since):
    """Materialize one short read snapshot; formatting happens after closing it."""
    rows, decisions, refusals = [], [], Counter()
    with closing(sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro",
                                uri=True, timeout=0.25)) as conn:
        conn.row_factory = sqlite3.Row
        deadline = time.monotonic() + 5
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.create_function("canonical_chain_id", 1, canonical_chain_id, deterministic=True)
        conn.execute("BEGIN")
        wallets = conn.execute(
            "SELECT source_user_id, handle, real_chains FROM memecoin_wallets "
            "WHERE real_approved=1 AND paused=0 AND COALESCE(excluded,0)=0 "
            "ORDER BY source_user_id").fetchall()
        for wallet in wallets:
            uid = wallet["source_user_id"]
            for chain in sorted(_parse_real_chains(wallet["real_chains"])):
                groups = conn.execute(
                    "SELECT real_decision, real_decision_reason, COUNT(*) AS n "
                    "FROM memecoin_events WHERE source_user_id=? AND side='buy' "
                    "AND canonical_chain_id(network_id)=? AND julianday(created_at)>=julianday(?) "
                    "GROUP BY real_decision, real_decision_reason ORDER BY 1,2",
                    (uid, chain, since)).fetchall()
                for group in groups:
                    decision = group["real_decision"] or "unknown"
                    reason = group["real_decision_reason"] or "unknown"
                    decisions.append([uid, chain, decision, reason, group["n"]])
                    if decision in REFUSALS:
                        refusals[(decision, reason)] += group["n"]
                positions = conn.execute(
                    "SELECT is_open, closed_at, realized_pnl FROM memecoin_positions "
                    "WHERE source_user_id=? AND canonical_chain_id(network_id)=? "
                    "AND execution_mode='real' AND julianday(opened_at)>=julianday(?)",
                    (uid, chain, since)).fetchall()
                closed = [p for p in positions if p["is_open"] == 0 and p["closed_at"]]
                opened = sum(p["is_open"] == 1 for p in positions)
                unknown_pnl = sum(p["realized_pnl"] is None for p in closed)
                pnl = sum(p["realized_pnl"] for p in closed if p["realized_pnl"] is not None)
                rows.append([uid, wallet["handle"] or "-", chain,
                             sum(g["n"] for g in groups), len(positions), len(closed), opened,
                             len(positions) - len(closed) - opened, pnl, unknown_pnl])
    total = ["TOTAL", "-", "-"] + [sum(row[i] for row in rows) for i in range(3, 10)]
    lines = [
        f"Since {since} (inclusive); current armed roster, not historical arm membership.",
        "Signals: persisted buy events by feed created_at, not chain time; native decisions may be absent.",
        "Positions: execution_mode=real, opened_at >= since (entry cohort); closes require closed_at.",
        "PnL: closed ledger USD sum of known values; pnl_unknown counts closes with missing PnL.",
        "Missing/invalid clocks and unresolved/unarmed chains are excluded; missing decisions stay unknown.",
        "wallet\thandle\tchain\tsignals\tentries\tcloses\topen\tunresolved\tpnl_known_usd\tpnl_unknown",
    ]
    for row in rows + [total]:
        lines.append("\t".join(str(x) if i != 8 else f"{x:.2f}" for i, x in enumerate(row)))
    lines += ["", "Decisions (same persisted-buy denominator):", "wallet\tchain\tdecision\treason\tcount"]
    lines += ["\t".join(map(str, row)) for row in decisions]
    lines += ["", "Top 10 refusal reasons (known refusal/safety statuses only):", "decision\treason\tcount"]
    lines += [f"{decision}\t{reason}\t{count}" for (decision, reason), count in refusals.most_common(10)]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path,
                        default=Path(os.environ.get("SNIPER_STATE_ROOT", ROOT)) / "sniper_tracker.db")
    parser.add_argument("--since", default="2026-10-26T13:00:00Z")
    args = parser.parse_args()
    try:
        since = datetime.fromisoformat(args.since.replace("Z", "+00:00"))
        if since.tzinfo is None or since.utcoffset() is None:
            raise ValueError("--since requires an explicit UTC offset or Z")
        output = report(args.db, since.astimezone(timezone.utc).isoformat())
    except (ValueError, sqlite3.Error) as exc:
        parser.exit(2, f"copy revival report failed: {exc}\n")
    print(output)


if __name__ == "__main__":
    main()

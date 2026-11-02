#!/usr/bin/env python3
"""Read-only retrospective copy-roster screen; no funded authority."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import sqlite3
from statistics import median
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from sniper.persistence.db_core import get_readonly_connection
from sniper.persistence.db_memecoin import _alpha_b_base_sql, _parse_real_chains
from sniper.services.memecoin_chains import canonical_chain_id


PAUSE_START = "2026-10-23T06:43:00Z"
RESUMED = "2026-10-26T13:00:00Z"


def timestamp(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except ValueError:
        return None


def candidates(db, since):
    since = timestamp(since)
    if since is None:
        raise ValueError("invalid --since timestamp")
    with closing(get_readonly_connection(str(Path(db).resolve()))) as conn:
        conn.execute("PRAGMA busy_timeout=250")
        deadline = time.monotonic() + 5
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        conn.create_function("canonical_chain_id", 1, canonical_chain_id, deterministic=True)
        conn.execute("BEGIN")
        wallets = {r["source_user_id"]: dict(r) for r in conn.execute(
            "SELECT source_user_id, handle, real_approved, real_chains FROM memecoin_wallets "
            "WHERE paused=0 AND COALESCE(excluded,0)=0")}
        columns = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_positions)")}
        fill_columns = {r[1] for r in conn.execute("PRAGMA table_info(memecoin_fills)")}
        undated_real_closes = conn.execute(
            "SELECT COUNT(*) FROM memecoin_positions "
            "WHERE execution_mode='real' AND is_open=0 AND closed_at IS NULL").fetchone()[0]
        recorded = "opened_recorded_at" if "opened_recorded_at" in columns else "NULL"
        funded = defaultdict(list)
        for r in conn.execute(
                "SELECT source_user_id, canonical_chain_id(network_id) AS chain, "
                f"opened_at, {recorded} AS opened_recorded_at, closed_at, realized_pnl "
                "FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 "
                "AND julianday(closed_at)>=julianday(?)", (since.isoformat(),)):
            if r["source_user_id"] in wallets and r["chain"] not in (None, "evm-unknown"):
                funded[(r["source_user_id"], r["chain"])].append(dict(r))

        # Group full retained history before windowing, so a recent add-on is not a first buy.
        firsts = conn.execute(
            "SELECT source_user_id, canonical_chain_id(network_id) AS chain, mint, "
            "MIN(julianday(created_at)) AS first_jd, MAX(julianday(created_at)) AS last_jd, COUNT(*) AS raw_n "
            "FROM memecoin_events WHERE side='buy' AND mint IS NOT NULL AND trim(mint)<>'' "
            "GROUP BY source_user_id, chain, mint").fetchall()
        gap_start, gap_end = conn.execute("SELECT julianday(?),julianday(?)",
                                          (PAUSE_START, RESUMED)).fetchone()
        dropped_evm_unknown_buy_rows = sum(r["raw_n"] for r in firsts if r["chain"] == "evm-unknown")
        anchors = {}
        for r in firsts:
            if r["chain"] not in (None, "evm-unknown") and r["last_jd"] is not None:
                anchors[r["chain"]] = max(anchors.get(r["chain"], r["last_jd"]), r["last_jd"])
        windows = {}
        for chain, anchor in anchors.items():
            if gap_start <= anchor < gap_end:
                anchor = gap_start
            start = anchor - 7
            if anchor >= gap_end and start < gap_end:
                start = gap_start - (7 - (anchor - gap_end))
            windows[chain] = (start, anchor)
        activity = defaultdict(lambda: [0, 0])
        for r in firsts:
            key = (r["source_user_id"], r["chain"])
            window = windows.get(r["chain"])
            t = r["first_jd"]
            if key[0] in wallets and window and t is not None and window[0] <= t <= window[1]:
                if t < gap_start:
                    activity[key][0] += 1
                elif t >= gap_end:
                    activity[key][1] += 1

        needed = {"id", "strategy", "entry_fill_source", "exit_reason", "last_quote_price"}
        paper_available = needed <= columns and {"position_id", "kind", "qty", "fill_source"} <= fill_columns
        paper = {}
        if paper_available:
            for r in conn.execute(
                    "SELECT source_user_id, canonical_chain_id(network_id) AS chain, "
                    "COUNT(*) AS n, SUM(realized_pnl) AS pnl FROM memecoin_positions WHERE "
                    + _alpha_b_base_sql() + " AND julianday(closed_at)>=julianday(?) "
                    "GROUP BY source_user_id, chain", (since.isoformat(),)):
                paper[(r["source_user_id"], r["chain"])] = (r["n"], r["pnl"])

    rows = []
    oct_start, oct_end = timestamp("2026-10-01"), timestamp("2026-11-01")
    keys = set(funded) | set(activity) | {k for k in paper if k[0] in wallets}
    for uid, wallet in wallets.items():
        if wallet["real_approved"] == 1:
            keys.update((uid, chain) for chain in _parse_real_chains(wallet["real_chains"]))
    for uid, chain in keys:
        if uid not in wallets or chain in (None, "evm-unknown"):
            continue
        positions = funded.get((uid, chain), [])
        known = [p for p in positions if isinstance(p["realized_pnl"], (int, float))
                 and math.isfinite(p["realized_pnl"])]
        oct_rows = [p for p in positions if (t := timestamp(p["opened_at"])) is not None
                    and oct_start <= t < oct_end]
        oct_known = [p for p in known if p in oct_rows]
        holds = []
        for p in positions:
            opened = timestamp(p["opened_recorded_at"] or p["opened_at"])
            closed = timestamp(p["closed_at"])
            if opened is not None and closed is not None and closed >= opened:
                holds.append((closed - opened).total_seconds())
        pnl = sum(p["realized_pnl"] for p in known)
        oct_pnl = sum(p["realized_pnl"] for p in oct_known)
        pre, resumed = activity.get((uid, chain), (0, 0))
        paper_n, paper_pnl = paper.get((uid, chain), (0, 0)) if paper_available else (None, None)
        wallet = wallets[uid]
        rows.append(dict(uid=uid, handle=wallet["handle"] or "-", chain=chain,
                         armed=wallet["real_approved"] == 1 and chain in _parse_real_chains(wallet["real_chains"]),
                         n=len(positions), known_n=len(known), unknown_n=len(positions)-len(known),
                         pnl_known=pnl, wins=sum(p["realized_pnl"] > 0 for p in known),
                         oct_n=len(oct_rows), oct_known_n=len(oct_known), oct_pnl_known=oct_pnl,
                         hold_median_s=median(holds) if holds else None, hold_n=len(holds),
                         first_observed_buy_n=pre+resumed, pre_gap_n=pre, resumed_n=resumed,
                         activity_known=chain in windows, paper_n=paper_n, paper_pnl=paper_pnl,
                         screen=bool(known) and len(known) == len(positions) and pnl > 0
                         and bool(oct_rows) and len(oct_known) == len(oct_rows) and oct_pnl >= 0 and pre+resumed > 0,
                         low_n=len(known) < 10 or len(oct_known) < 5))
    rows.sort(key=lambda r: (r["chain"], not r["screen"],
                             r["known_n"] == 0 and r["first_observed_buy_n"] == 0,
                             -r["pnl_known"], r["uid"]))
    return {"rows": rows, "windows": windows, "since": since.isoformat(),
            "undated_real_closes": undated_real_closes,
            "dropped_evm_unknown_buy_rows": dropped_evm_unknown_buy_rows,
            "paper_status": "available" if paper_available else "unavailable"}


def render(result):
    lines = [
        "Retrospective selection on our funded ledger; no funded authority or forward quality claim.",
        f"Funded n/PnL: real closes with closed_at >= {result['since']}; win denominator is known PnL only.",
        "pnl_known is booked realized_pnl; pending operator backfills can understate totals.",
        f"Undated real closes (all ledger, excluded from dated cohort): {result['undated_real_closes']}",
        f"Dropped evm-unknown buy event rows (all retained history): {result['dropped_evm_unknown_buy_rows']}",
        "October columns: subset of those closes with opened_at in [2026-10-01,2026-11-01) UTC.",
        "Stored naive clocks interpreted as UTC; invalid clocks/unknown chains excluded; missing PnL counted separately.",
        "first_observed_buy: MIN buy created_at per uid/chain/mint over retained history, not the wallet's first buy.",
        f"Activity: trailing 7 days excluding declared gap [{PAUSE_START},{RESUMED}); other coverage unknown.",
        "Resumed zero can mean not watched for unarmed wallets; currently tracked feeds select the observations.",
        "Paper alpha_b: our measurable selection diagnostic, closed_at >= since; not wallet quality; " + result["paper_status"],
        "Screen: complete positive funded PnL, complete nonnegative October evidence, and observed activity; LOW_N: known closes<10 or October<5.",
    ]
    for chain, (start, end) in sorted(result["windows"].items()):
        dates = [datetime.fromtimestamp(round((jd-2440587.5)*86400, 3), timezone.utc).isoformat(timespec="seconds")
                 for jd in (start, end)]
        lines.append(f"Activity window {chain}: {dates[0]} .. {dates[1]} (feed clock, not chain time)")
    lines += ["", "Top 25 per chain (screen pass first, zero-known-close/no-activity last, funded PnL descending, uid tie-break):",
              "chain\tuid\thandle\tarmed\tscreen\tLOW_N\tclosed_since_n\tknown_n\tpnl_known_usd\tpnl_unknown_n\twins_known_n\twin_pct\toct_open_n\toct_pnl_known_usd\toct_unknown_n\thold_p50_s\thold_n\tfirst_observed_buy_n\tpre_gap_n\tresumed_n\tpaper_n\tpaper_pnl_usd"]
    counts = defaultdict(int)
    for r in result["rows"]:
        counts[r["chain"]] += 1
        if counts[r["chain"]] > 25:
            continue
        values = [r["chain"], r["uid"], r["handle"], int(r["armed"]), int(r["screen"]),
                  "LOW_N" if r["low_n"] else "-", r["n"], r["known_n"], f"{r['pnl_known']:.2f}",
                  r["unknown_n"], r["wins"], f"{100*r['wins']/r['known_n']:.1f}" if r["known_n"] else "unknown",
                  r["oct_n"], f"{r['oct_pnl_known']:.2f}" if r["oct_n"] else "unknown",
                  r["oct_n"]-r["oct_known_n"], r["hold_median_s"], r["hold_n"],
                  r["first_observed_buy_n"] if r["activity_known"] else None,
                  r["pre_gap_n"] if r["activity_known"] else None,
                  r["resumed_n"] if r["activity_known"] else None, r["paper_n"], r["paper_pnl"]]
        lines.append("\t".join("unknown" if v is None else " ".join(str(v).split()) for v in values))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path,
                        default=Path(os.environ.get("SNIPER_STATE_ROOT", ROOT)) / "sniper_tracker.db")
    parser.add_argument("--since", default="2026-08-15")
    args = parser.parse_args()
    try:
        print(render(candidates(args.db, args.since)))
    except (sqlite3.Error, ValueError) as exc:
        parser.exit(2, f"copy candidate report failed: {exc}\n")


if __name__ == "__main__":
    main()

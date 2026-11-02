#!/usr/bin/env python3
"""WO-2 — engine decision report. The outcome feedback loop, READ-ONLY.

The engine writes a row per open-position tick into `memecoin_exit_candidate_decisions`
(`data/memecoin_exit_state.db`). Nothing read it back. This grades those decisions against what the
tape did NEXT — did a CUT/DERISK/TAKE_PROFIT avoid a loss, or cut a runner?

Pre-registered read protocol: docs/research/2026-07-25-engine-decision-report-read-protocol.md.
Constants (horizons, min-n, bands) are frozen THERE, before any number is looked at. This script's
ONLY output is a report / proposal document — it NEVER edits a constant, flips a flag, or touches a
live path. That firewall is the difference between learning and curve-fitting.

Counterfactual = the held pool's continuing tape mark at >= decided_at + N minutes. The raw pool path
continues after a full exit, unlike open-position snapshots. Censored = the tape did not extend N
minutes past the decision; reported SEPARATELY, never folded into a rate.
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ── preregistered constants (mirror the read-protocol doc; do not edit to chase a result) ──
HORIZONS_MIN = (5, 15, 30)
MIN_CELL_N = 30
BAND_EDGES = ((1_000_000, "nano<1M"), (5_000_000, "small1-5M"), (30_000_000, "mid5-30M"))
BAND_TOP = "large>=30M"
ACTED = ("CUT", "TAKE_PROFIT", "DERISK")          # risk-reducing -> have a "should we have held?" counterfactual


def _epoch(iso):
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _band(entry_mcap):
    if entry_mcap is None:
        return "unknown"
    try:
        mc = float(entry_mcap)
    except (ValueError, TypeError):
        return "unknown"
    for edge, label in BAND_EDGES:
        if mc < edge:
            return label
    return BAND_TOP


def _connect_ro(path):
    """Read-only handle; returns None if the DB does not exist yet (engine never ran)."""
    if not os.path.exists(path):
        return None
    c = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _tape_timelines(decisions, ticks_db, sol_tape_db):
    """Continuing per-pool price paths, including ticks recorded after a position closed."""
    from sniper.analysis.exit_features import exec_price_path_per_block

    out = {}
    conns = {}
    try:
        for d in decisions:
            chain, pool = str(d.get("chain") or ""), d.get("pool")
            db_path = sol_tape_db if chain == "solana" else ticks_db
            if not db_path or not pool:
                continue
            if db_path not in conns:
                conns[db_path] = _connect_ro(db_path)
            conn = conns[db_path]
            if conn is None:
                continue
            meta = conn.execute(
                "SELECT quote_side FROM pool_meta WHERE chain_id=? AND pool=?",
                (chain, pool)).fetchone()
            if not meta or meta[0] is None:
                continue
            rows = []
            for r in conn.execute(
                    "SELECT p.kind,p.block_no,p.amount0,p.amount1,b.block_ts "
                    "FROM pool_ticks p JOIN block_times b ON b.chain_id=p.chain_id "
                    "AND b.block_no=p.block_no WHERE p.chain_id=? AND p.pool=? "
                    "AND p.kind IN ('v2_swap','v3_swap','sol_swap') "
                    "ORDER BY p.block_no,p.id", (chain, pool)):
                ts = _epoch(r["block_ts"])
                if ts is not None:
                    rows.append({**dict(r), "block_ts": ts})
            path = exec_price_path_per_block(rows, quote_side=int(meta[0]))
            if path:
                out[d["position_id"]] = [(float(ts), float(px)) for ts, px, *_ in path]
    finally:
        for conn in conns.values():
            if conn is not None:
                conn.close()
    return out


def load(exit_state_db, ledger_db, *, ticks_db=None, sol_tape_db=None):
    """Decisions joined to their decision-time snapshot + the position's band, plus per-position
    snapshot timelines for the counterfactual lookahead. All read-only."""
    xc = _connect_ro(exit_state_db)
    if xc is None:
        return [], {}, {}
    decisions = [dict(r) for r in xc.execute(
        "SELECT d.position_id, d.decided_at, d.candidate_action, d.outcome, d.executable_confirmed, "
        "d.censored AS row_censored, s.chain_id AS chain, s.pool, "
        "COALESCE(s.executable_price,s.nominal_price) AS price_now "
        "FROM memecoin_exit_candidate_decisions d "
        "LEFT JOIN memecoin_exit_market_state s ON s.id = d.state_id")]
    # per-position snapshot timeline (computed_at asc) for the lookahead
    timeline = defaultdict(list)
    for r in xc.execute("SELECT position_id, computed_at, executable_price FROM "
                        "memecoin_exit_market_state ORDER BY position_id, computed_at"):
        e = _epoch(r["computed_at"])
        if e is not None and r["executable_price"] is not None:
            timeline[r["position_id"]].append((e, float(r["executable_price"])))
    xc.close()

    tape = _tape_timelines(decisions, ticks_db, sol_tape_db)
    for pid, series in tape.items():
        timeline[pid] = series
    # A raw continuation path must use its own decision-time mark. Mixing an exact route quote with
    # an atomic pool ratio would create a unit error when token decimals differ.
    for d in decisions:
        series = tape.get(d["position_id"])
        t0 = _epoch(d["decided_at"])
        if series and t0 is not None:
            prior = [p for ts, p in series if ts <= t0]
            if prior:
                d["price_now"] = prior[-1]
            else:
                d["price_now"] = None

    bands = {}
    lc = _connect_ro(ledger_db)
    if lc is not None:
        try:
            for r in lc.execute("SELECT id, entry_mcap FROM memecoin_positions"):
                bands[r["id"]] = _band(r["entry_mcap"])
        except sqlite3.Error:
            pass
        lc.close()
    return decisions, dict(timeline), bands


def _price_at_or_after(series, t):
    """First recorded executable_price at time >= t (the counterfactual mark). None if the tape ends
    first (censored). ponytail: linear scan; series is tiny (~N=5 positions, 30s cadence)."""
    for e, p in series:
        if e >= t and p > 0:
            return p
    return None


def counterfactuals(decisions, timeline, bands):
    """Per (action, chain, band) cell: acted counts, censored, outcome dist, and the N-min lookahead
    return distribution per horizon."""
    cells = defaultdict(lambda: {
        "n_total": 0, "outcomes": defaultdict(int), "confirmed": 0,
        "cf": {n: {"returns": [], "censored": 0} for n in HORIZONS_MIN}})
    for d in decisions:
        action = d["candidate_action"]
        cell = cells[(action, d.get("chain") or "?", bands.get(d["position_id"], "unknown"))]
        cell["n_total"] += 1
        cell["outcomes"][d.get("outcome") or "unstamped"] += 1
        cell["confirmed"] += int(d.get("executable_confirmed") or 0)
        if action not in ACTED:
            continue
        t0 = _epoch(d["decided_at"])
        p0 = d.get("price_now")
        series = timeline.get(d["position_id"], [])
        if t0 is None or not p0 or p0 <= 0:
            for n in HORIZONS_MIN:
                cell["cf"][n]["censored"] += 1
            continue
        for n in HORIZONS_MIN:
            later = _price_at_or_after(series, t0 + n * 60)
            if later is None:
                cell["cf"][n]["censored"] += 1
            else:
                cell["cf"][n]["returns"].append((later - p0) / p0)
    return cells


def _median(xs):
    if not xs:
        return None
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def render(cells, min_n=MIN_CELL_N):
    lines = ["", "=" * 78, "  ENGINE DECISION REPORT (read-only; proposals only, never auto-tuned)",
             "=" * 78]
    if not cells:
        lines += ["  No decisions recorded yet (engine unarmed or exit-state DB empty).",
                  "  This is expected pre-arm; the report is ready for when decisions accrue.",
                  "=" * 78, ""]
        return "\n".join(lines), []
    proposals = []
    for (action, chain, band), c in sorted(cells.items()):
        lines.append(f"\n  {action}  chain={chain}  band={band}   n={c['n_total']}  "
                     f"confirmed={c['confirmed']}/{c['n_total']}")
        od = ", ".join(f"{k}:{v}" for k, v in sorted(c["outcomes"].items()))
        lines.append(f"    outcomes: {od}")
        if action not in ACTED:
            lines.append("    (non-acting; no counterfactual)")
            continue
        for n in HORIZONS_MIN:
            rets, cens = c["cf"][n]["returns"], c["cf"][n]["censored"]
            k = len(rets)
            total = k + cens
            gated = k >= min_n and (cens / total if total else 1.0) <= 0.5
            med = _median(rets)
            held_beat = (sum(1 for r in rets if r > 0) / k) if k else None
            verdict = ""
            if not gated:
                verdict = f"  [counts only: n={k}<{min_n}]" if k < min_n else "  [counts only: >50% censored]"
            lines.append(
                f"    +{n:2d}m: n={k:3d} censored={cens:3d}  "
                f"median_ret={med:+.3f}" if med is not None else
                f"    +{n:2d}m: n={k:3d} censored={cens:3d}  median_ret=  n/a")
            if med is not None:
                lines[-1] += (f"  held_would_beat={held_beat:.0%}" if held_beat is not None else "")
                lines[-1] += verdict
        # a proposal only if ALL horizons agree, decisively, above the gate
        gated_meds = [
            _median(c["cf"][n]["returns"]) for n in HORIZONS_MIN
            if len(c["cf"][n]["returns"]) >= min_n
            and (c["cf"][n]["censored"] / (len(c["cf"][n]["returns"]) + c["cf"][n]["censored"] or 1)) <= 0.5]
        if len(gated_meds) == len(HORIZONS_MIN) and gated_meds and all(m > 0.05 for m in gated_meds):
            p = (f"{action}/{chain}/{band}: holding beat the exit at all horizons "
                 f"(medians {[round(m,3) for m in gated_meds]}) — PROPOSE loosening this exit's "
                 f"trigger within its monotonic bound. Needs its own prereg to ever ship.")
            proposals.append(p)
        elif len(gated_meds) == len(HORIZONS_MIN) and gated_meds and all(m < -0.05 for m in gated_meds):
            p = (f"{action}/{chain}/{band}: the exit avoided loss at all horizons "
                 f"(medians {[round(m,3) for m in gated_meds]}) — action is earning its keep; no change.")
            proposals.append(p)
    if proposals:
        lines += ["", "  PROPOSALS (never auto-applied — each needs its own dated prereg):"]
        lines += [f"    - {p}" for p in proposals]
    lines += ["=" * 78, ""]
    return "\n".join(lines), proposals


def main():
    ap = argparse.ArgumentParser(description="Engine decision report (read-only).")
    ap.add_argument("--exit-state-db", default=None, help="override exit-state DB path")
    ap.add_argument("--ledger-db", default=None, help="override ledger DB path")
    ap.add_argument("--ticks-db", default=None, help="EVM continuing tick tape")
    ap.add_argument("--sol-tape-db", default=None, help="Solana continuing tick tape")
    ap.add_argument("--min-n", type=int, default=MIN_CELL_N)
    ap.add_argument("--write", metavar="PATH", help="write the proposal document to PATH")
    args = ap.parse_args()

    from sniper.persistence.db_exit_state import exit_state_db_path
    exit_db = args.exit_state_db or exit_state_db_path()
    ledger_db = args.ledger_db or os.getenv("DB_PATH", "sniper_tracker.db")

    if args.ticks_db is None or args.sol_tape_db is None:
        from sniper.persistence.db_ticks import sol_tape_db_path, ticks_db_path
    decisions, timeline, bands = load(
        exit_db, ledger_db,
        ticks_db=args.ticks_db or ticks_db_path(),
        sol_tape_db=args.sol_tape_db or sol_tape_db_path())
    cells = counterfactuals(decisions, timeline, bands)
    report, proposals = render(cells, min_n=args.min_n)
    print(report)
    if args.write:
        with open(args.write, "w", encoding="utf-8") as fh:
            fh.write(f"# Engine decision report — {len(decisions)} decisions\n\n")
            fh.write("Read-only. Proposals below need their own dated prereg to ever ship.\n\n```\n")
            fh.write(report)
            fh.write("\n```\n")
        print(f"[written] {args.write}")
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    sys.exit(main())

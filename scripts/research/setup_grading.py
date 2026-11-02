#!/usr/bin/env python3
"""Trading-setup grading, built to Gamma's pre-registration (read-only).

Rule: docs/audits/2026-09-30-setup-grading-preregistration.md on gamma/setup-grading-prereg
(9b9a2ddbb, stamped 2026-09-30T23:1xZ before any outcome was joined). Labels are the FIXED labels of
scripts/research/setup_inventory.py (same code, same thresholds). Descriptive only, never a gate: the
funded runner is copy-only and any filter is the operator's call.

Population choices (stated for Gamma's recompute):
- tested closes exclude manual (signal_source='manual'); written-offs count at their booked loss;
- axis 2 (feed setup): tested labels S1..S7; the complement is the chain's other S1..S7 closes;
  S0 and S8 are described only;
- axis 1 (lifecycle): every non-manual close, including those without a feed signal event;
- returns = realized_pnl / entry_notional; 4663 is gross of gas.

    python3 scripts/research/setup_grading.py --out <dir>
"""
from __future__ import annotations

import sys
import pathlib as _research_pathlib
sys.path.insert(0, str(_research_pathlib.Path(__file__).resolve().parents[2]))
from sniper.research.wallet_execution.publication import legacy_launch as _legacy_launch
_legacy_launch(__name__)

import argparse
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import setup_inventory as INV  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
SPLIT = "2026-09-23T00:00:00+00:00"
TESTED_CHAINS = ("solana", "4663", "56")
TESTED_SETUPS = ("S1_first_touch", "S2_reentry_after_dump", "S3_chase_vertical", "S4_breakout_new_high",
                 "S5_dip_buy", "S6_feed_burst", "S7_near_high_no_trigger")
LIFECYCLES = ("pre_graduation", "post_graduation", "unknown")
MIN_TRAIN, MIN_TEST = 20, 10
DRAWS, SEED, Q = 4000, 20260930, 0.10
MIN_DIFF = 0.05


def ts_iso(v):
    return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def labelled(c, data, *, paper=False):
    """[{id, chain, mint, uid, opened_at, ret, pnl, manual, setup, lifecycle}] with the fixed labels."""
    if paper:
        rows = c.execute(
            "SELECT p.id, p.network_id, p.mint, p.origin_event_id, p.attempt_id, "
            "json_extract(p.evm_route_json, '$.protocol'), e.created_at, e.their_price, e.side, "
            "p.source_user_id, p.opened_at, p.realized_pnl, p.entry_notional, p.signal_source "
            "FROM memecoin_positions p LEFT JOIN memecoin_events e ON e.fomo_event_id = p.origin_event_id "
            "WHERE p.execution_mode='paper' AND p.strategy='funded_clone' AND p.is_open=0 "
            "AND p.realized_pnl IS NOT NULL AND p.price_attribution IN ('route_quote','paper_quote','funded_quote_reuse') "
            "AND p.id NOT IN (SELECT position_id FROM memecoin_fills WHERE fill_source='fomo') "
            "AND NOT EXISTS (SELECT 1 FROM memecoin_fills f WHERE f.position_id = p.id AND f.kind != 'entry' "
            "AND COALESCE(f.fill_source,'') != 'route_quote')").fetchall()
    else:
        rows = c.execute(
            "SELECT p.id, p.network_id, p.mint, p.origin_event_id, p.attempt_id, "
            "json_extract(p.evm_route_json, '$.protocol'), e.created_at, e.their_price, e.side, "
            "p.source_user_id, p.opened_at, p.realized_pnl, p.entry_notional, p.signal_source "
            "FROM memecoin_positions p LEFT JOIN memecoin_events e ON e.fomo_event_id = p.origin_event_id "
            "WHERE p.execution_mode='real' AND p.is_open=0 AND p.realized_pnl IS NOT NULL").fetchall()
    ds = [{"id": r[0], "chain": str(r[1]), "mint": r[2], "origin": r[3], "attempt": r[4], "protocol": r[5],
           "t": INV.ts(r[6]) if r[6] else None, "p_src": r[7], "src_side": r[8], "uid": r[9],
           "opened_at": ts_iso(r[10]), "pnl": float(r[11]), "notional": r[12], "manual": r[13] == "manual"}
          for r in rows if r[12] and r[12] > 0]
    feed = INV.feed_by_coin(c, {(d["chain"], d["mint"]) for d in ds})
    venue = {} if paper else INV.sol_venue(Path(data) / "memecoin_sol_tape.db")
    for d in ds:
        d["ret"] = d["pnl"] / d["notional"]
        d["setup"] = "S0_no_signal_event" if d["t"] is None else INV.setup(INV.path_features(d, feed))
        d["lifecycle"] = INV.lifecycle(d, venue) if not paper or d["protocol"] else "unknown"
        d["split"] = "train" if d["opened_at"] < SPLIT else "test"
    return ds


def boot_diff(cell, comp, rng):
    """Mint-cluster bootstrap of mean(cell) - mean(comp); returns (diff, draws)."""
    diff = float(np.mean([x["ret"] for x in cell]) - np.mean([x["ret"] for x in comp]))
    clusters = defaultdict(lambda: ([], []))
    for x in cell:
        clusters[x["mint"]][0].append(x["ret"])
    for x in comp:
        clusters[x["mint"]][1].append(x["ret"])
    keys = list(clusters)
    a_sum = np.array([sum(clusters[k][0]) for k in keys]); a_n = np.array([len(clusters[k][0]) for k in keys])
    b_sum = np.array([sum(clusters[k][1]) for k in keys]); b_n = np.array([len(clusters[k][1]) for k in keys])
    idx = rng.integers(0, len(keys), size=(DRAWS, len(keys)))
    an, bn = a_n[idx].sum(1), b_n[idx].sum(1)
    ok = (an > 0) & (bn > 0)
    draws = a_sum[idx].sum(1)[ok] / an[ok] - b_sum[idx].sum(1)[ok] / bn[ok]
    return diff, draws


def two_sided(draws):
    return float(min(1.0, 2 * min((draws <= 0).mean(), (draws >= 0).mean()))) if len(draws) else None


def bh(pvals, q):
    """Benjamini-Hochberg: set of indices significant at q."""
    order = sorted(range(len(pvals)), key=lambda i: pvals[i])
    m, k = len(pvals), 0
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / m:
            k = rank
    return set(order[:k])


def cells(ds, chain):
    tested = [d for d in ds if d["chain"] == chain and not d["manual"]]
    out = []
    ax2 = [d for d in tested if d["setup"] in TESTED_SETUPS]
    for s in TESTED_SETUPS:
        out.append(("setup", s, [d for d in ax2 if d["setup"] == s], [d for d in ax2 if d["setup"] != s]))
    for lc in LIFECYCLES:
        out.append(("lifecycle", lc, [d for d in tested if d["lifecycle"] == lc],
                    [d for d in tested if d["lifecycle"] != lc]))
    return out


def within_wallet(cell, comp):
    by = defaultdict(lambda: ([], []))
    for x in cell:
        by[x["uid"]][0].append(x["ret"])
    for x in comp:
        by[x["uid"]][1].append(x["ret"])
    a, b = [], []
    for uid, (ins, outs) in by.items():
        if len(ins) >= 2 and len(outs) >= 2:
            a += ins; b += outs
    return (float(np.mean(a) - np.mean(b)), len(a), len(b)) if a and b else (None, 0, 0)


def traced(c, ids):
    """(untraced ids, written-off ids) against memecoin_fills: an entry fill, and an exit fill unless
    the close is a write-off booked at minus the entry cost."""
    bad, wo = [], []
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        q = ",".join("?" * len(chunk))
        fills = defaultdict(lambda: [0, 0])
        for pid, kind in c.execute(f"SELECT position_id, kind FROM memecoin_fills WHERE position_id IN ({q})", chunk):
            fills[pid][0 if kind == "entry" else 1] += 1
        for pid, reason, pnl, notional in c.execute(
                f"SELECT id, exit_reason, realized_pnl, entry_notional FROM memecoin_positions WHERE id IN ({q})", chunk):
            e, x = fills[pid]
            if reason == "written_off" and e and abs(float(pnl) + float(notional)) <= max(0.01, 0.005 * float(notional)):
                wo.append(pid)
            elif not (e and x):
                bad.append(pid)
    return bad, wo


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "sniper_tracker.db"))
    ap.add_argument("--data", default=str(ROOT / "data"))
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=30)
    real = labelled(c, a.data)
    paper = labelled(c, a.data, paper=True)
    tested_ids = [d["id"] for d in real if d["chain"] in TESTED_CHAINS and not d["manual"]]
    untraced, written_off = traced(c, tested_ids)

    rows = []
    for chain in TESTED_CHAINS:
        paper_cells = {(ax, lab): (cell, comp) for ax, lab, cell, comp in cells(paper, chain)}
        for ax, lab, cell, comp in cells(real, chain):
            tr_c = [x for x in cell if x["split"] == "train"]; tr_o = [x for x in comp if x["split"] == "train"]
            te_c = [x for x in cell if x["split"] == "test"]; te_o = [x for x in comp if x["split"] == "test"]
            row = {"chain": chain, "axis": ax, "cell": lab, "train_n": len(tr_c), "test_n": len(te_c),
                   "all_n": len(cell), "dollar_total": round(sum(x["pnl"] for x in cell), 2)}
            if lab in ("S0_no_signal_event", "S8_unpriced_path"):
                continue
            if len(tr_c) < MIN_TRAIN or len(te_c) < MIN_TEST or not tr_o or not te_o:
                row["result"] = "under_powered_not_tested"
                rows.append(row)
                continue
            rng = np.random.default_rng(SEED)
            d_tr, draws = boot_diff(tr_c, tr_o, rng)
            d_te, draws_te = boot_diff(te_c, te_o, np.random.default_rng(SEED))
            p_te = float((draws_te <= 0).mean() if d_tr > 0 else (draws_te >= 0).mean()) if len(draws_te) else None
            top = Counter(x["uid"] for x in tr_c).most_common(1)[0]
            ww, ww_a, ww_b = within_wallet(tr_c, tr_o)
            pc, po = paper_cells.get((ax, lab), ([], []))
            p_diff = p_p = None
            if len(pc) >= 5 and len(po) >= 5:
                p_diff, p_draws = boot_diff(pc, po, np.random.default_rng(SEED))
                p_p = two_sided(p_draws)
            row.update({"train_mean_pct": 100 * float(np.mean([x["ret"] for x in tr_c])),
                        "complement_mean_pct": 100 * float(np.mean([x["ret"] for x in tr_o])),
                        "train_diff_pts": 100 * d_tr, "train_p": two_sided(draws),
                        "test_diff_pts": 100 * d_te, "test_p_one_sided": p_te,
                        "top_wallet_share": top[1] / len(tr_c), "within_wallet_diff_pts":
                            None if ww is None else 100 * ww, "within_wallet_n": [ww_a, ww_b],
                        "paper_n": [len(pc), len(po)], "paper_diff_pts": None if p_diff is None else 100 * p_diff,
                        "paper_p": p_p})
            rows.append(row)
    tested = [r for r in rows if "train_p" in r]
    sig = bh([r["train_p"] for r in tested], Q) if tested else set()
    m = len(tested)
    order = sorted(range(m), key=lambda i: tested[i]["train_p"])
    for rank, i in enumerate(order, 1):
        tested[i]["bh_rank"] = rank
        tested[i]["bh_threshold"] = Q * rank / m
    for i, r in enumerate(tested):
        same_sign = r["test_diff_pts"] * r["train_diff_pts"] > 0
        if i not in sig:
            r["result"] = "not_shown:1_not_bh_significant_on_train"
        elif not (same_sign and r["test_p_one_sided"] is not None and r["test_p_one_sided"] <= 0.10):
            r["result"] = "not_shown:2_test_does_not_replicate"
        elif abs(r["train_diff_pts"]) < 100 * MIN_DIFF:
            r["result"] = "not_shown:3_difference_under_5_pts"
        elif r["top_wallet_share"] > 0.5 and not (r["within_wallet_diff_pts"] is not None
                                                  and r["within_wallet_diff_pts"] * r["train_diff_pts"] > 0):
            r["result"] = "not_shown:4_wallet_in_disguise"
        elif (r["paper_diff_pts"] is not None and r["paper_p"] is not None and r["paper_p"] <= 0.10
              and r["paper_diff_pts"] * r["train_diff_pts"] < 0):
            r["result"] = "not_shown:5_paper_opposite_sign"
        else:
            r["result"] = "FINDING"
    described = []
    for d_set, label in ((real, "funded"), (paper, "paper")):
        agg = defaultdict(lambda: [0, 0.0])
        for d in d_set:
            key = (d["chain"], d["setup"] if not d["manual"] else "manual", d["lifecycle"])
            agg[key][0] += 1; agg[key][1] += d["pnl"]
        described.append({"source": label, "cells": [{"chain": k[0], "setup_or_manual": k[1], "lifecycle": k[2],
                                                      "n": v[0], "dollar_total": round(v[1], 2)}
                                                     for k, v in sorted(agg.items())]})
    res = {"generated_at": datetime.now(timezone.utc).isoformat(),
           "rule": "gamma/setup-grading-prereg 9b9a2ddbb", "split": SPLIT,
           "eligible_cells": m, "stop_under_3_eligible": m < 3,
           "untraced_tested_closes": untraced, "written_off_counted": written_off,
           "real_closes": len(real), "paper_closes": len(paper), "rows": rows, "described": described}
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / "grading.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: res[k] for k in ("eligible_cells", "stop_under_3_eligible", "real_closes", "paper_closes")}))
    print("untraced:", untraced, "written_off:", written_off)
    return 0



# Operator protocol: legacy hypothetical conclusions cannot publish by default.
from sniper.research.wallet_execution.publication import quarantine_exports as _quarantine_exports
_quarantine_exports(globals(), ('main',))

if __name__ == "__main__":
    sys.exit(main())

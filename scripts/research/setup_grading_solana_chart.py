#!/usr/bin/env python3
"""Solana chart-setup grading, amendment A2.5 of docs/audits/2026-09-30-setup-grading-preregistration.md
(read-only). The outcome join reads ONLY the frozen feature file (A2.6) and refuses unless its sha256
matches the freeze record. Descriptive, never a gate.

Same outcome, holdout, eligibility, statistic and FINDING conditions as the feed-setup grading
(setup_grading.py, whose helpers this reuses unchanged):
- a cell is one chart category C1..C7 on solana; C0 (no data) is described only;
- tested closes: the frozen rows of C1..C7, joined to their funded close (non-manual, realized PnL
  known); the complement is the other C1..C7 closes;
- return = realized_pnl / entry_notional; split = the frozen row's own train/test (opened_at vs 09-23);
- condition 5 (paper replication) is "not evaluable: paper closes carry no chart features", never
  "passed"; a chart FINDING rests on conditions 1-4 and the report says so (Gamma, A2.5 ruling).
- ONE Benjamini-Hochberg family at q = 0.10 over the eligible chart cells plus the earlier eligible
  cells (--prior, the feed-setup grading.json); the earlier cells keep their reads and their q in the
  larger family is reported beside them.

    python3 scripts/research/setup_grading_solana_chart.py --prior <grading.json> --out <dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from setup_grading import (DRAWS, MIN_DIFF, MIN_TEST, MIN_TRAIN, Q, SEED, bh, boot_diff,  # noqa: E402
                           traced, two_sided, within_wallet)

ROOT = Path(__file__).resolve().parents[2]
FROZEN = ROOT / "docs" / "audits" / "frozen" / "2026-10-01-setup-solana-full-features.jsonl"
FROZEN_SHA256 = "f5ab009059e2a8ee005525db305165ca8ed68685210c01c29c25f9f1f6b0d3e6"
CHART = ("C1", "C2", "C3", "C4", "C5", "C6", "C7")


def frozen_rows(path=FROZEN, sha256=FROZEN_SHA256):
    """The frozen rows, or raise when the file is not the frozen one."""
    data = Path(path).read_bytes()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        raise SystemExit("REFUSE: %s sha256 %s is not the frozen %s" % (path, got, sha256))
    return [json.loads(line) for line in data.decode().splitlines() if line.strip()]


def joined(c, rows):
    """Frozen rows with their funded close: [{id, mint, uid, split, category, ret, pnl, manual}]."""
    ids = [int(r["position_id"]) for r in rows]
    closes = {}
    for i in range(0, len(ids), 900):
        chunk = ids[i:i + 900]
        for pid, mint, uid, pnl, notional, source, is_open, mode in c.execute(
                "SELECT id, mint, source_user_id, realized_pnl, entry_notional, signal_source, is_open, "
                "execution_mode FROM memecoin_positions WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk):
            closes[pid] = (mint, uid, pnl, notional, source, is_open, mode)
    out, dropped = [], Counter()
    for r in rows:
        got = closes.get(int(r["position_id"]))
        if got is None:
            dropped["no_position"] += 1
            continue
        mint, uid, pnl, notional, source, is_open, mode = got
        if mode != "real" or is_open or pnl is None or not notional or notional <= 0:
            dropped["not_a_funded_close"] += 1
            continue
        out.append({"id": int(r["position_id"]), "mint": mint, "uid": uid, "split": r["split"],
                    "category": r["category"], "c0_reason": r.get("c0_reason"), "pnl": float(pnl),
                    "ret": float(pnl) / float(notional), "manual": source == "manual"})
    return out, dict(dropped)


def grade(ds):
    tested = [d for d in ds if not d["manual"] and d["category"] in CHART]
    rows = []
    for cat in CHART:
        cell = [d for d in tested if d["category"] == cat]
        comp = [d for d in tested if d["category"] != cat]
        tr_c = [x for x in cell if x["split"] == "train"]; tr_o = [x for x in comp if x["split"] == "train"]
        te_c = [x for x in cell if x["split"] == "test"]; te_o = [x for x in comp if x["split"] == "test"]
        row = {"chain": "solana", "axis": "chart", "cell": cat, "train_n": len(tr_c), "test_n": len(te_c),
               "all_n": len(cell), "dollar_total": round(sum(x["pnl"] for x in cell), 2)}
        if len(tr_c) < MIN_TRAIN or len(te_c) < MIN_TEST or not tr_o or not te_o:
            row["result"] = "under_powered_not_tested"
            rows.append(row)
            continue
        d_tr, draws = boot_diff(tr_c, tr_o, np.random.default_rng(SEED))
        d_te, draws_te = boot_diff(te_c, te_o, np.random.default_rng(SEED))
        p_te = float((draws_te <= 0).mean() if d_tr > 0 else (draws_te >= 0).mean()) if len(draws_te) else None
        top = Counter(x["uid"] for x in tr_c).most_common(1)[0]
        ww, ww_a, ww_b = within_wallet(tr_c, tr_o)
        row.update({"train_mean_pct": 100 * float(np.mean([x["ret"] for x in tr_c])),
                    "complement_mean_pct": 100 * float(np.mean([x["ret"] for x in tr_o])),
                    "train_diff_pts": 100 * d_tr, "train_p": two_sided(draws),
                    "test_diff_pts": 100 * d_te, "test_p_one_sided": p_te,
                    "top_wallet_share": top[1] / len(tr_c),
                    "within_wallet_diff_pts": None if ww is None else 100 * ww,
                    "within_wallet_n": [ww_a, ww_b],
                    "condition_5": "not evaluable: paper closes carry no chart features"})
        rows.append(row)
    c0 = [d for d in ds if d["category"] == "C0" and not d["manual"]]
    by_reason = defaultdict(lambda: [0, 0.0])
    for d in c0:
        by_reason[d["c0_reason"] or "unstated"][0] += 1
        by_reason[d["c0_reason"] or "unstated"][1] += d["pnl"]
    described = {"C0": {"n": len(c0), "dollar_total": round(sum(d["pnl"] for d in c0), 2),
                        "by_reason": {k: {"n": v[0], "dollar_total": round(v[1], 2)}
                                      for k, v in sorted(by_reason.items())}},
                 "manual": {"n": sum(1 for d in ds if d["manual"]),
                            "dollar_total": round(sum(d["pnl"] for d in ds if d["manual"]), 2)}}
    return rows, described


def bh_q(pvals):
    """Benjamini-Hochberg adjusted q-values, in input order."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    q, running = [0.0] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvals[i] * m / rank)
        q[i] = running
    return q


def family(chart_rows, prior_rows):
    """One BH family over every eligible cell. Chart cells get their result; prior cells keep theirs."""
    tested = [r for r in chart_rows if "train_p" in r]
    prior = [r for r in prior_rows if "train_p" in r]
    allp = [r["train_p"] for r in tested] + [r["train_p"] for r in prior]
    sig, q = bh(allp, Q), bh_q(allp)
    for i, r in enumerate(tested):
        r["q_family"] = q[i]
        same = r["test_diff_pts"] * r["train_diff_pts"] > 0
        if i not in sig:
            r["result"] = "not_shown:1_not_bh_significant_on_train"
        elif not (same and r["test_p_one_sided"] is not None and r["test_p_one_sided"] <= 0.10):
            r["result"] = "not_shown:2_test_does_not_replicate"
        elif abs(r["train_diff_pts"]) < 100 * MIN_DIFF:
            r["result"] = "not_shown:3_difference_under_5_pts"
        elif r["top_wallet_share"] > 0.5 and not (r["within_wallet_diff_pts"] is not None
                                                  and r["within_wallet_diff_pts"] * r["train_diff_pts"] > 0):
            r["result"] = "not_shown:4_wallet_in_disguise"
        else:
            r["result"] = "FINDING (conditions 1-4; condition 5 not evaluable)"
    prior_out = [{"chain": r["chain"], "axis": r.get("axis"), "cell": r["cell"], "train_p": r["train_p"],
                  "earlier_bh_threshold": r.get("bh_threshold"), "q_family": q[len(tested) + j],
                  "bh_significant_in_family": (len(tested) + j) in sig, "earlier_result": r["result"]}
                 for j, r in enumerate(prior)]
    return len(allp), prior_out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "sniper_tracker.db"))
    ap.add_argument("--prior", required=True, help="the feed-setup grading.json (696f0aa40)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    rows = frozen_rows()
    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True, timeout=30)
    ds, dropped = joined(c, rows)
    untraced, written_off = traced(c, [d["id"] for d in ds if not d["manual"]])
    chart_rows, described = grade(ds)
    prior = json.loads(Path(a.prior).read_text())["rows"]
    m, prior_out = family(chart_rows, prior)
    res = {"generated_at": datetime.now(timezone.utc).isoformat(), "frozen": str(FROZEN.relative_to(ROOT)),
           "frozen_sha256": FROZEN_SHA256, "rule": "setup preregistration A2.5", "draws": DRAWS, "seed": SEED,
           "frozen_rows": len(rows), "joined": len(ds), "dropped": dropped,
           "untraced_tested_closes": untraced, "written_off_counted": written_off,
           "family_size": m, "stop_under_3_eligible": m < 3, "chart_rows": chart_rows,
           "prior_cells_in_family": prior_out, "described": described}
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    (out / "grading_solana_chart.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps({k: res[k] for k in ("frozen_rows", "joined", "dropped", "family_size",
                                          "untraced_tested_closes", "written_off_counted")}))
    for r in chart_rows:
        print(r["cell"], r["train_n"], r["test_n"], r.get("train_diff_pts"), r.get("train_p"),
              r.get("q_family"), r.get("test_diff_pts"), r["result"])
    for r in prior_out:
        print("prior", r["chain"], r["cell"], r["train_p"], round(r["q_family"], 3), r["earlier_result"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

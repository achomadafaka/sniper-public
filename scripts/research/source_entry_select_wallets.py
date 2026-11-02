#!/usr/bin/env python3
"""Regenerate the frozen source-entry wallet list (Amendment 3, 2026-10-26) and the figures quoted in it.
usage: python3 scripts/research/source_entry_select_wallets.py [--out PATH]   (default: print sha256 and the figures; with --out write the list)
Selection uses memecoin_events rows with network_id='solana' and created_at < '2026-09-16' ONLY, filter applied in SQL before any aggregate. Read-only (mode=ro).
Per wallet and coin: buys and sells are the stored rows (their_size_usd); flip = coin with >=1 buy AND >=1 sell; flip return = sum(sell usd)/sum(buy usd) - 1, winsorised above at +500%;
hold = first sell time - first buy time on that coin (a sell before any buy gives a negative or zero hold and is kept as stored: inventory is NOT netted, sells are not matched to lots)."""
import argparse, collections, hashlib, json, sqlite3, statistics
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
CUT = "2026-09-16"
START = "2026-07-05"
FROZEN_SHA = "e625a124484ddd9c924c0eb5b3c1a6d9bdbcbb8b94083ebb80a8ea2d77e9512b"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out"); ap.add_argument("--db", default=str(ROOT / "sniper_tracker.db")); a = ap.parse_args()
    db = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    ev = db.execute("select source_user_id, mint, side, their_size_usd, their_market_cap, created_at from memecoin_events where network_id='solana' and created_at < ?", (CUT,)).fetchall()
    by = collections.defaultdict(lambda: collections.defaultdict(lambda: {"b": [], "s": []}))
    mc = collections.defaultdict(list)
    for u, m, s, sz, cap, t in ev:
        by[u][m]["b" if s == "buy" else "s"].append((ts(t), sz or 0.0))
        if s == "buy" and cap and cap > 0:
            mc[u].append(cap)
    sel = []
    for u, coins in by.items():
        rets, holds = [], []
        for m, d in coins.items():
            if d["b"] and d["s"]:
                bu = sum(x[1] for x in d["b"]); su = sum(x[1] for x in d["s"])
                if bu > 0:
                    rets.append(min(su / bu - 1, 5.0)); holds.append(min(x[0] for x in d["s"]) - min(x[0] for x in d["b"]))
        if len(rets) < 8 or len(mc[u]) < 20:
            continue
        med = statistics.median(rets); hit = sum(r > 0 for r in rets) / len(rets); mean = sum(rets) / len(rets); hold = statistics.median(holds) / 60
        if med > 0 and hit >= 0.5 and mean > 0 and hold <= 30:
            c = np.array(mc[u])
            sel.append({"uid": u, "flips": len(rets), "median_flip_ret": round(med, 3), "hit": round(hit, 3), "mean_flip_ret_w500": round(mean, 3), "median_hold_min": round(hold, 1),
                        "priced_buys": len(c), "buys_le_40k": int((c <= 40000).sum()), "share_le_40k": round(float((c <= 40000).mean()), 3), "median_buy_mcap": round(float(np.median(c)))})
    sel.sort(key=lambda r: -r["median_flip_ret"])
    tot = sum(r["buys_le_40k"] for r in sel)
    doc = {"frozen_at": "2026-10-26", "selection_window": "2026-07-05..2026-09-15 (created_at < 2026-09-16), Solana, memecoin_events only",
           "rule": "flips >= 8 AND priced buys >= 20 AND median flip return > 0 AND hit rate >= 0.5 AND mean flip return (per-flip winsorized at +500%) > 0 AND median first-buy-to-first-sell hold <= 30 min; flip = coin with >=1 buy and >=1 sell, return = sum(sell usd)/sum(buy usd) - 1",
           "n_selected": len(sel), "in_range_buys_in_selection_window": tot, "wallets": sel}
    s = json.dumps(doc, indent=1, sort_keys=True)
    sha = hashlib.sha256(s.encode()).hexdigest()
    allc = np.array([x for u in mc for x in mc[u]])
    print("selected", len(sel), "sha256", sha, "MATCHES FROZEN" if sha == FROZEN_SHA else "DIFFERS from frozen %s" % FROZEN_SHA)
    print("all priced Solana buys (their_market_cap > 0) before %s: n %d, share <= 40k %.4f, median %.0f" % (CUT, len(allc), float((allc <= 40000).mean()), float(np.median(allc))))
    print("selected wallets: buys <= 40k = %d over 73 days (%s..%s) = %.2f/day" % (tot, START, CUT, tot / 73))
    if a.out:
        Path(a.out).write_text(s)


if __name__ == "__main__":
    main()

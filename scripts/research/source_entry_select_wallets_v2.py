#!/usr/bin/env python3
"""Inventory-valid wallet selection (Amendment 6, replaces the v1 cash-flow selector for the study; v1 stays as an exploratory list).
usage: python3 scripts/research/source_entry_select_wallets_v2.py [--out PATH] [--db PATH]
Only memecoin_events rows with network_id='solana' and created_at < '2026-09-16' are read (SQL filter before any aggregate; read-only).
Per (wallet, coin), events in (created_at, id) order. Quantity = their_size_usd / their_price. Average-cost inventory: a buy adds quantity and cost; a sell is matched against the
CURRENT inventory only (sold qty = min(inventory, sell qty)); a sell with no inventory is ignored and counted (orphan). realized proceeds = matched qty x sell price; realized cost = average cost of the
matched qty. Flip = a coin with realized cost > 0; return = proceeds/cost - 1 winsorised at +500%; hold = first MATCHED sell time - first buy time (> 0 by construction).
Rows with price <= 0, size <= 0 or NULL are dropped and counted."""
import argparse, collections, hashlib, json, sqlite3, statistics
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
CUT = "2026-09-16"


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out"); ap.add_argument("--db", default=str(ROOT / "sniper_tracker.db")); a = ap.parse_args()
    db = sqlite3.connect("file:%s?mode=ro" % a.db, uri=True)
    ev = db.execute("select id, source_user_id, mint, side, their_size_usd, their_price, their_market_cap, created_at from memecoin_events "
                    "where network_id='solana' and created_at < ? order by created_at, id", (CUT,)).fetchall()
    inv = {}
    stat = collections.Counter()
    flips = collections.defaultdict(list)       # uid -> [(ret, hold_s)]
    mc = collections.defaultdict(list)
    first_buy = {}
    acct = {}
    for i, u, m, side, sz, px, cap, t in ev:
        if not sz or sz <= 0 or not px or px <= 0:
            stat["dropped_bad_price_or_size"] += 1; continue
        k = (u, m); q = sz / px; tt = ts(t)
        s = acct.setdefault(k, {"qty": 0.0, "cost": 0.0, "rp": 0.0, "rc": 0.0, "first_buy": None, "first_match": None})
        if side == "buy":
            s["qty"] += q; s["cost"] += sz
            if s["first_buy"] is None: s["first_buy"] = tt
            if cap and cap > 0: mc[u].append(cap)
        else:
            if s["qty"] <= 1e-12:
                stat["orphan_sells_ignored"] += 1; continue
            sq = min(s["qty"], q); part = sq / s["qty"]
            cost_part = s["cost"] * part
            s["rp"] += sq * px; s["rc"] += cost_part; s["qty"] -= sq; s["cost"] -= cost_part
            if s["first_match"] is None: s["first_match"] = tt
    for (u, m), s in acct.items():
        if s["rc"] > 0 and s["first_match"] is not None:
            flips[u].append((min(s["rp"] / s["rc"] - 1, 5.0), s["first_match"] - s["first_buy"]))
    sel = []
    for u, fl in flips.items():
        if len(fl) < 8 or len(mc[u]) < 20:
            continue
        rets = [r for r, _ in fl]; holds = [h for _, h in fl]
        med = statistics.median(rets); hit = sum(r > 0 for r in rets) / len(rets); mean = sum(rets) / len(rets); hold = statistics.median(holds) / 60
        if med > 0 and hit >= 0.5 and mean > 0 and 10 / 60 <= hold <= 30:
            c = np.array(mc[u])
            sel.append({"uid": u, "flips": len(fl), "median_flip_ret": round(med, 3), "hit": round(hit, 3), "mean_flip_ret_w500": round(mean, 3), "median_hold_min": round(hold, 2),
                        "priced_buys": len(c), "buys_le_40k": int((c <= 40000).sum()), "share_le_40k": round(float((c <= 40000).mean()), 3), "median_buy_mcap": round(float(np.median(c)))})
    sel.sort(key=lambda r: (-r["median_flip_ret"], r["uid"]))
    doc = {"frozen_at": "2026-10-26", "version": "v2-inventory-valid", "selection_window": "2026-07-05..2026-09-15 (created_at < 2026-09-16), Solana, memecoin_events only",
           "rule": "average-cost inventory per (wallet, coin); flips >= 8 AND priced buys >= 20 AND median flip return > 0 AND hit rate >= 0.5 AND mean flip return (winsorized +500%) > 0 AND median hold (first buy to first matched sell) in [10 s, 30 min]",
           "n_selected": len(sel), "in_range_buys_in_selection_window": sum(r["buys_le_40k"] for r in sel), "dropped": dict(stat), "wallets": sel}
    s = json.dumps(doc, indent=1, sort_keys=True)
    print("selected", len(sel), "sha256", hashlib.sha256(s.encode()).hexdigest(), dict(stat))
    print("in-range (<=40k) buys by the selected:", doc["in_range_buys_in_selection_window"], "= %.2f per calendar day (73 d)" % (doc["in_range_buys_in_selection_window"] / 73))
    old = {w["uid"] for w in json.load(open(ROOT / "docs/audits/frozen/2026-10-26-source-entry-selected-wallets.jsonl"))["wallets"]}
    print("overlap with v1 list:", len(old & {w["uid"] for w in sel}), "of", len(old))
    for w in sel: print(w["uid"][:8], w["flips"], w["median_flip_ret"], w["hit"], w["median_hold_min"], "buys<=40k", w["buys_le_40k"], "of", w["priced_buys"], "medcap", w["median_buy_mcap"])
    if a.out: Path(a.out).write_text(s)


if __name__ == "__main__":
    main()

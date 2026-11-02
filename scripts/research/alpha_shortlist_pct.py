"""Shortlist amendment A2 (e453157a3): Tier A on percent per real close, 30 d + 7 d. Read-only (ledger only).

Population = Beta's Tier A rows (scripts/research/alpha_wallet_shortlist.py --out ~/.cache/beta/alpha-shortlist/rows.json).
    python3 scripts/research/alpha_shortlist_pct.py
"""
import json, os, random, sqlite3, statistics
from pathlib import Path
from collections import defaultdict
REPO = Path(__file__).resolve().parents[2]
LEDGER = "file:%s?mode=ro" % (REPO / "sniper_tracker.db")
END, S30, S7 = "2026-10-01T08:00:00", "2026-09-01T08:00:00", "2026-09-24T08:00:00"
pop = json.load(open(os.path.expanduser("~/.cache/beta/alpha-shortlist/rows.json")))["tier_a"]
c = sqlite3.connect(LEDGER, uri=True); c.row_factory = sqlite3.Row
armed = {}
for r in c.execute("SELECT source_user_id, real_approved, real_chains, excluded FROM memecoin_wallets"):
    armed[r["source_user_id"]] = (bool(r["real_approved"]) and not r["excluded"],
                                  {x.strip() for x in str(r["real_chains"] or "").split(",") if x.strip()})
def closes(uid, chain, lo):
    return [(r["mint"], r["realized_pnl"] / r["entry_notional"]) for r in c.execute(
        "SELECT mint, realized_pnl, entry_notional FROM memecoin_positions WHERE execution_mode='real' AND is_open=0 "
        "AND realized_pnl IS NOT NULL AND COALESCE(signal_source,'') != 'manual' AND entry_notional > 0 "
        "AND source_user_id=? AND network_id=? AND replace(closed_at,' ','T') > ? AND replace(closed_at,' ','T') <= ?",
        (uid, chain, lo, END))]
def stats(cl):
    if not cl:
        return None
    rs = [r for _m, r in cl]
    by = defaultdict(list)
    for m, r in cl:
        by[m].append(r)
    mints = list(by)
    rng = random.Random(20260930); means = []
    for _ in range(4000):
        draw = [r for m in (rng.choice(mints) for _ in mints) for r in by[m]]
        means.append(sum(draw) / len(draw))
    means.sort()
    sums = {m: sum(v) for m, v in by.items()}
    best = max(sums, key=sums.get)
    pos = sum(v for v in sums.values() if v > 0)
    rest = [r for m, r in cl if m != best]
    return {"n": len(rs), "coins": len(mints), "mean": sum(rs) / len(rs), "median": statistics.median(rs),
            "lo": means[int(0.05 * 4000)], "hi": means[int(0.95 * 4000) - 1],
            "best_share": (sums[best] / pos) if pos > 0 else None,
            "mean_wo_best": (sum(rest) / len(rest)) if rest else None}
rows = []
for p in pop:
    uid, ch = p["uid"], p["chain"]
    a30, a7 = stats(closes(uid, ch, S30)), stats(closes(uid, ch, S7))
    is_armed = armed.get(uid, (False, set()))
    armed_now = is_armed[0] and ch in is_armed[1]
    if a30 is None or a30["n"] < 5: res = "1_fewer_than_5_closes"
    elif a30["mean"] <= 0: res = "2_mean_not_positive"
    elif a30["mean_wo_best"] is None or a30["mean_wo_best"] <= 0: res = "3_not_positive_without_best_coin"
    elif a30["best_share"] is None or a30["best_share"] > 0.5: res = "4_best_coin_over_50pct"
    elif a7 is not None and a7["mean"] < 0: res = "5_7d_mean_negative"
    else: res = "recommend_re_arming_on_chain"
    rows.append(dict(handle=p["handle"], uid=uid, chain=ch, pools=",".join(p["pools"]), outside=(not p["pools"]),
                     armed_now=armed_now, res=res, a30=a30, a7=a7, dollar_result=p["result"], dollar_sum=p["sum"]))
from collections import Counter
print("population", len(rows), "armed now", sum(r["armed_now"] for r in rows), Counter(r["res"] for r in rows if not r["armed_now"]))
f = lambda x: "—" if x is None else "%+.1f" % (100 * x)
def line(r):
    a, b = r["a30"] or {}, r["a7"]
    return "| %s | %s | %s | %d | %d | %s | %s | [%s, %s] | %s | %s | %s | %s | %s |" % (
        r["handle"], r["chain"], r["pools"] or "(outside)", a.get("n", 0), a.get("coins", 0), f(a.get("mean")), f(a.get("median")),
        f(a.get("lo")), f(a.get("hi")), "—" if a.get("best_share") is None else "%.2f" % a["best_share"], f(a.get("mean_wo_best")),
        ("%s (n %d)" % (f(b["mean"]), b["n"])) if b else "no closes", r["res"], r["dollar_result"])
hdr = "| wallet | chain | pools | n | coins | mean % | median % | 90% CI % | best share | mean w/o best % | 7 d mean % | result (%) | result ($, 09-30) |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|"
keep = lambda r: (r["a30"] or {}).get("n", 0) >= 5 or r["handle"] in ("MemeCooksx", "notdecu")
print("\nNOT ARMED NOW (n>=5, plus the named wallets)\n" + hdr)
for r in sorted([r for r in rows if not r["armed_now"] and keep(r)], key=lambda r: -(r["a30"] or {}).get("mean", -9)):
    print(line(r))
print("\nARMED NOW (context, never recommended)\n" + hdr)
for r in sorted([r for r in rows if r["armed_now"] and keep(r)], key=lambda r: -(r["a30"] or {}).get("mean", -9)):
    print(line(r))

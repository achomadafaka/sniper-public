#!/usr/bin/env python3
"""Who landed a buy between the source's buy and ours, and which of them keep doing it.

Operator question 2026-09-27: "can we see how many people beat us? can we identify the same wallets
beating us?" Offline: reads `copier_crowd_and_fees.py fees --out` files (every transaction on the
mint from the source's slot to ours, with its signer) and the ledger read-only for each fill's lane
and source wallet. No network.

Only fills whose source transaction was read (status "ok") are counted; the rest are reported as
censored by status. A buy is "ahead" exactly as copier_crowd_and_fees defines it: a successful buy
strictly between the source's (slot, index) and ours. A signer is one fee payer, so a shared
executor (Fomo's AgmLJBMD signs for many users) counts as one signer while being a crowd.

    python3 scripts/research/copier_crowd_and_fees.py fees --since 2026-09-20T00:00:00 --out F.json
    python3 scripts/research/copiers_ahead_repeat.py F.json [more.json ...]
    python3 scripts/research/copiers_ahead_repeat.py F.json --census       # in-slot ordering census
    python3 scripts/research/copiers_ahead_repeat.py F.json --recipients   # tip-account discovery
    python3 scripts/research/copiers_ahead_repeat.py --selftest
"""
import argparse
import collections
import contextlib
import json
import sqlite3
from pathlib import Path

LEDGER = Path(__file__).resolve().parents[2] / "sniper_tracker.db"


def q(values, p):
    v = sorted(x for x in values if x is not None)
    return v[min(len(v) - 1, int(p * (len(v) - 1) + 0.5))] if v else None


def where(f, r):
    return ("source_slot" if r["slot"] == f["source_slot"] else
            "our_slot" if r["slot"] == f["our_slot"] else "between")


def summarize(fills, meta):
    """meta: {position_id: (lane, source_handle)}."""
    ok = [f for f in fills if f.get("status") == "ok"]
    lanes = {}
    for f in ok:
        lane = meta.get(f["position_id"], ("?", "?"))[0]
        d = lanes.setdefault(lane, {"n": 0, "slots": [], "ahead": [], "loc": collections.Counter(),
                                    "sol": collections.Counter()})
        d["n"] += 1
        d["slots"].append(f["slots_behind"])
        d["ahead"].append(f["ahead_n"])
        for r in f["rows"]:
            if r["ahead"]:
                d["loc"][where(f, r)] += 1
                d["sol"][where(f, r)] += r["sol"] or 0
    signer_fills = collections.defaultdict(set)
    signer_rows = collections.defaultdict(list)
    for f in ok:
        for r in f["rows"]:
            if r["ahead"]:
                signer_fills[r["signer"]].add(f["position_id"])
                signer_rows[r["signer"]].append((f, r))
    ranked = sorted(signer_fills, key=lambda s: (-len(signer_fills[s]), s))
    signers = []
    for s in ranked:
        rows = signer_rows[s]
        sources = collections.Counter(meta.get(pid, ("?", "?"))[1] for pid in signer_fills[s])
        signers.append({"signer": s, "fills": len(signer_fills[s]), "buys": len(rows),
                        "sol": round(sum(r["sol"] or 0 for _, r in rows), 2),
                        "where": dict(collections.Counter(where(f, r) for f, r in rows)),
                        "cu_price_p50": q([r.get("cu_price") or 0 for _, r in rows], 0.5),
                        "tip_txs": sum(1 for _, r in rows if r.get("tip_lamports")),
                        "programs": dict(collections.Counter(
                            p[:8] for _, r in rows for p in (r.get("programs") or [])).most_common(4)),
                        "sources": dict(sources.most_common(4))})
    top_share = {}
    for k in (5, 10, 20):
        top = set(ranked[:k])
        top_share[k] = sum(1 for f in ok if any(r["ahead"] and r["signer"] in top for r in f["rows"]))
    return {"fills": len(fills), "ok": len(ok),
            "status": dict(collections.Counter(f.get("status") for f in fills)),
            "lanes": {k: {"n": v["n"], "slots_behind_p50": q(v["slots"], .5), "slots_behind_p90": q(v["slots"], .9),
                          "ahead_p50": q(v["ahead"], .5), "ahead_p90": q(v["ahead"], .9),
                          "ahead_max": max(v["ahead"]), "zero_ahead": sum(1 for a in v["ahead"] if a == 0),
                          "by_location": {loc: [v["loc"][loc], round(v["sol"][loc], 1)]
                                          for loc in ("source_slot", "between", "our_slot")}}
                      for k, v in lanes.items()},
            "distinct_signers": len(ranked),
            "signers_ge": {k: sum(1 for s in ranked if len(signer_fills[s]) >= k) for k in (2, 3, 5)},
            "top_signers": signers[:25], "fills_hit_by_top": top_share}


# Relay tip accounts beyond Jito's, by vanity address prefix, matched case-insensitively (a live
# Astralane account is "AsTRAEoy..."): Astralane "astra", Nozomi "noz"/"TEMPaMeC", NextBlock
# "nextblock"; GMGN's relay was seen paying DXfkEGoo... and ByRRgnZe... (Alpha, 2026-09-28). A
# recipient with no vanity prefix stays unlabelled: `--recipients` lists the recurring ones. Rows
# from files written before 2026-09-28 carry no sol_out; only their Jito tip (tip_lamports) is known.
TIP_PREFIXES = {"astra": "astralane", "noz": "nozomi", "tempamec": "nozomi", "nextblock": "nextblock",
                "dxfkegoo": "gmgn", "byrrgnze": "gmgn"}
# GMGN's relay also tips accounts with no vanity prefix: on 66333 the twins GPLa6i/42oVfr paid these
# 18M and 20M lamports in the place 66364's twins paid DXfkEGoo/ByRRgnZe 17M. Unlisted ones stay
# uncounted, so a tip count here is a lower bound.
TIP_EXACT = {"dBhdrmwBkRa66XxBuAK4WZeZnsZ6bHeHCCLXa3a8bTJ": "gmgn",
             "HeZVpHj9jLwTVtMMbzQRf6mLtFPkWNSg11o68qrbUBa3": "gmgn"}


def tips(r):
    """{label: lamports} the transaction's signer paid to known tip accounts."""
    import copier_crowd_and_fees as ccf
    out = collections.Counter()
    if "sol_out" not in r:
        return {"jito": r["tip_lamports"]} if r.get("tip_lamports") else {}
    for to, lamports in r["sol_out"]:
        label = "jito" if to in ccf.JITO_TIP_ACCOUNTS else TIP_EXACT.get(to) or next(
            (v for k, v in TIP_PREFIXES.items() if to.lower().startswith(k)), None)
        if label:
            out[label] += lamports
    return dict(out)


def bucket(n):
    return "0" if n == 0 else "1-2" if n <= 2 else "3+"


def census(fills, outcomes):
    """Alpha's in-slot ordering census (2026-09-28): where we land against the source, who bought
    between the source and us, what they paid, and our drift and PnL by how many were ahead.

    `cu_price_to_lead` is the highest CU price paid by any buy ahead of us, plus one: what we would
    have had to bid to sit first after the source IF we had been in the leader's queue at the same
    moment. A buy in an earlier slot than ours was ahead because we were not in that block at all,
    and no CU price reaches it; `our_slot_cu_price_to_lead` is the same figure for the buys ahead
    inside the slot we actually landed in. Only fills whose source transaction was read (status ok)
    are counted; the rest are censored by status."""
    ok = [f for f in fills if f.get("status") == "ok"]
    per = []
    for f in ok:
        rows = f["rows"]
        ahead = [r for r in rows if r["ahead"]]
        ours = next((r for r in rows if r["role"] == "ours"), {})
        src = next((r for r in rows if r["role"] == "source"), {})
        in_our_slot = [r for r in ahead if r["slot"] == f["our_slot"]]
        o = outcomes.get(f["position_id"], {})
        top = max((r["cu_price"] or 0 for r in ahead), default=None)
        top_ours = max((r["cu_price"] or 0 for r in in_our_slot), default=None)
        per.append({
            "position_id": f["position_id"], "lane": f.get("lane"), "handle": o.get("handle"),
            "source_at": [src.get("slot"), src.get("index")], "ours_at": [ours.get("slot"), ours.get("index")],
            "slots_behind": f["slots_behind"], "ahead_n": len(ahead),
            "ahead_sol": round(sum(r["sol"] or 0 for r in ahead), 3),
            "ahead_tokens": round(sum(r.get("tokens") or 0 for r in ahead)),
            "ahead_in_our_slot_n": len(in_our_slot),
            "ahead_signers": sorted({r["signer"] for r in ahead}),
            "ahead_tipped_n": sum(1 for r in ahead if tips(r)),
            "cu_price_to_lead": top + 1 if top is not None else None,
            "our_slot_cu_price_to_lead": top_ours + 1 if top_ours is not None else None,
            "ours_cu_price": ours.get("cu_price"), "ours_cu_limit": ours.get("cu_limit"),
            "ours_fee": ours.get("fee"), "ours_tips": tips(ours) if ours else {},
            "drift_pct": o.get("drift_pct"), "pnl": o.get("pnl")})

    def slot_share(ps):
        c = collections.Counter("S" if p["slots_behind"] == 0 else "S+1" if p["slots_behind"] == 1
                                else "S+2..4" if p["slots_behind"] <= 4 else "S+5+" for p in ps)
        return {k: c[k] for k in ("S", "S+1", "S+2..4", "S+5+")}

    def ahead_stats(ps):
        return {"n": len(ps), "ahead_n_p50_p90": [q([p["ahead_n"] for p in ps], x) for x in (.5, .9)],
                "ahead_sol_p50_p90": [q([p["ahead_sol"] for p in ps], x) for x in (.5, .9)],
                "zero_ahead": sum(1 for p in ps if p["ahead_n"] == 0)}

    lanes = collections.defaultdict(list)
    for p in per:
        lanes[p["lane"]].append(p)
    buckets = {}
    for b in ("0", "1-2", "3+"):
        ps = [p for p in per if bucket(p["ahead_n"]) == b]
        pnl = [p["pnl"] for p in ps if p["pnl"] is not None]
        buckets[b] = {"n": len(ps), "closed": len(pnl),
                      "drift_p50_p90": [q([p["drift_pct"] for p in ps], x) for x in (.5, .9)],
                      "pnl_sum": round(sum(pnl), 2), "pnl_mean": round(sum(pnl) / len(pnl), 2) if pnl else None,
                      "wins": sum(1 for x in pnl if x > 0)}
    sig = collections.defaultdict(list)
    for f, p in zip(ok, per):
        for r in f["rows"]:
            if r["ahead"]:
                sig[r["signer"]].append((p, r))
    signers = []
    for s, prs in sorted(sig.items(), key=lambda kv: (-len({p["position_id"] for p, _ in kv[1]}), kv[0])):
        fills_hit = {p["position_id"] for p, _ in prs}
        if len(fills_hit) < 2:
            continue
        tipsum = collections.defaultdict(list)
        for _, r in prs:
            for k, v in tips(r).items():
                tipsum[k].append(v)
        signers.append({
            "signer": s, "fills": len(fills_hit), "buys": len(prs),
            "sol": round(sum(r["sol"] or 0 for _, r in prs), 2),
            "cu_price_p50_max": [q([r["cu_price"] or 0 for _, r in prs], .5),
                                 max(r["cu_price"] or 0 for _, r in prs)],
            "cu_limit_p50": q([r["cu_limit"] for _, r in prs], .5),
            "fee_p50": q([r["fee"] for _, r in prs], .5),
            "tips": {k: [len(v), q(v, .5)] for k, v in tipsum.items()},
            "same_slot_as_source": sum(1 for p, r in prs if r["slot"] == p["source_at"][0]),
            "sources": dict(collections.Counter(p["handle"] for p, _ in prs).most_common(6)),
            "programs": dict(collections.Counter(x[:8] for _, r in prs for x in r.get("programs") or [])
                             .most_common(3))})
    ours_cu = [p["ours_cu_price"] for p in per]
    lead = [p for p in per if p["cu_price_to_lead"] is not None]
    return {"fills": len(fills), "ok": len(ok),
            "status": dict(collections.Counter(f.get("status") for f in fills)),
            "1_slot_share": {"all": slot_share(per), **{k: slot_share(v) for k, v in lanes.items()}},
            "2_ahead": {"all": ahead_stats(per), **{k: ahead_stats(v) for k, v in lanes.items()}},
            "3_recurring_signers": signers[:25],
            "4_by_ahead_bucket": buckets,
            "5_cu_price_to_lead": {
                "ours_cu_price_p50": q(ours_cu, .5),
                "n_with_buys_ahead": len(lead),
                "to_lead_p50_p90_max": [q([p["cu_price_to_lead"] for p in lead], x) for x in (.5, .9)]
                + [max((p["cu_price_to_lead"] for p in lead), default=None)],
                "ours_already_at_or_above": sum(1 for p in lead if (p["ours_cu_price"] or 0)
                                                >= p["cu_price_to_lead"]),
                "our_slot_to_lead_p50_p90": [q([p["our_slot_cu_price_to_lead"] for p in per], x)
                                             for x in (.5, .9)]},
            "per_fill": per}


def transfer_recipients(fills, top=25):
    """Discovery table for TIP_PREFIXES: recipients of SOL the buyers ahead of us paid, by how many
    distinct signers and mints paid them. A tip account recurs across mints with small flat sums;
    a pool or curve account belongs to one mint."""
    by = collections.defaultdict(lambda: {"signers": set(), "mints": set(), "lamports": []})
    for f in fills:
        for r in f.get("rows", []):
            if r.get("ahead"):
                for to, lamports in r.get("sol_out") or []:
                    d = by[to]
                    d["signers"].add(r["signer"])
                    d["mints"].add(f["mint"])
                    d["lamports"].append(lamports)
    ranked = sorted(by.items(), key=lambda kv: (-len(kv[1]["mints"]), kv[0]))[:top]
    return [{"to": k, "signers": len(v["signers"]), "mints": len(v["mints"]), "n": len(v["lamports"]),
             "lamports_p50": q(v["lamports"], .5), "lamports_max": max(v["lamports"])} for k, v in ranked]


def ledger_outcomes():
    with contextlib.closing(sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=10)) as c:
        return {r[0]: {"handle": r[1], "drift_pct": r[2], "pnl": r[3]} for r in c.execute(
            "SELECT id, source_handle, total_copy_drift_pct, realized_pnl FROM memecoin_positions "
            "WHERE execution_mode='real' AND network_id='solana'")}


def ledger_meta():
    with contextlib.closing(sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=10)) as c:
        return {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT id, signal_source, source_handle FROM memecoin_positions "
            "WHERE execution_mode='real' AND network_id='solana'")}


def selftest():
    def row(sig, signer, slot, index, ahead, sol=1.0):
        return {"sig": sig, "signer": signer, "slot": slot, "index": index, "ahead": ahead, "sol": sol,
                "cu_price": 100, "tip_lamports": 0, "programs": ["pAMMBay6x"]}
    fills = [
        {"position_id": 1, "status": "ok", "source_slot": 10, "our_slot": 12, "slots_behind": 2, "ahead_n": 3,
         "rows": [row("a", "BOT", 10, 5, True), row("b", "BOT", 11, 1, True), row("c", "X", 12, 0, True),
                  row("d", "Y", 12, 9, False)]},
        {"position_id": 2, "status": "ok", "source_slot": 20, "our_slot": 27, "slots_behind": 7, "ahead_n": 1,
         "rows": [row("e", "BOT", 23, 4, True)]},
        {"position_id": 3, "status": "source_not_identified", "rows": [row("f", "BOT", 30, 1, True)]},
    ]
    out = summarize(fills, {1: ("slot_lane", "w1"), 2: ("follow_activity", "w2"), 3: ("slot_lane", "w1")})
    assert out["ok"] == 2 and out["status"] == {"ok": 2, "source_not_identified": 1}, out
    top = out["top_signers"][0]
    # BOT bought twice on fill 1 and once on fill 2: two distinct fills, three buys; fill 3 is censored.
    assert (top["signer"], top["fills"], top["buys"]) == ("BOT", 2, 3), top
    assert top["where"] == {"source_slot": 1, "between": 2}, top
    assert out["signers_ge"] == {2: 1, 3: 0, 5: 0}, out
    assert out["lanes"]["slot_lane"]["by_location"] == {"source_slot": [1, 1.0], "between": [1, 1.0],
                                                        "our_slot": [1, 1.0]}, out
    assert out["fills_hit_by_top"][5] == 2, out

    # The census. Fill 10 landed in the source's slot with nobody between; fill 11 at S+1 behind a
    # tipped source-slot buy (5M uL) and one in our slot (1M); fill 12 at S+3 behind three buys.
    import copier_crowd_and_fees as ccf
    jito = next(iter(ccf.JITO_TIP_ACCOUNTS))

    def cr(signer, slot, index, role=None, ahead=False, cu=None, sol_out=(), sol=1.0):
        return {"sig": f"{signer}{slot}{index}", "signer": signer, "slot": slot, "index": index,
                "role": role, "ahead": ahead, "sol": sol, "tokens": 10.0, "cu_price": cu,
                "cu_limit": 200_000, "fee": 5000, "tip_lamports": 0, "sol_out": [list(x) for x in sol_out],
                "programs": []}
    cf = [
        {"position_id": 10, "lane": "slot_lane_processed", "status": "ok", "source_slot": 100,
         "our_slot": 100, "slots_behind": 0, "mint": "M1",
         "rows": [cr("SRC", 100, 5, "source"), cr("US", 100, 9, "ours", cu=3_800_000)]},
        {"position_id": 11, "lane": "j7_wallets", "status": "ok", "source_slot": 200, "our_slot": 201,
         "slots_behind": 1, "mint": "M2",
         "rows": [cr("SRC", 200, 1, "source"), cr("BOT", 200, 2, ahead=True, cu=5_000_000,
                                                  sol_out=[(jito, 17_000_000)]),
                  cr("ASTR", 201, 0, ahead=True, cu=1_000_000, sol_out=[("AsTRAxYZ", 1_000_000)]),
                  cr("US", 201, 4, "ours", cu=3_800_000)]},
        {"position_id": 12, "lane": "j7_wallets", "status": "ok", "source_slot": 300, "our_slot": 303,
         "slots_behind": 3, "mint": "M3",
         "rows": [cr("SRC", 300, 1, "source"), cr("BOT", 300, 3, ahead=True, cu=None),
                  cr("X", 301, 0, ahead=True, cu=2), cr("Y", 302, 0, ahead=True, cu=7),
                  cr("Z", 303, 9, cu=9_999_999), cr("US", 303, 4, "ours", cu=3_800_000)]},
        {"position_id": 13, "status": "source_not_identified", "rows": []},
    ]
    c = census(cf, {10: {"handle": "a", "drift_pct": 1.0, "pnl": 5.0},
                    11: {"handle": "b", "drift_pct": 20.0, "pnl": -3.0},
                    12: {"handle": "b", "drift_pct": 30.0, "pnl": None}})
    assert c["ok"] == 3 and c["status"] == {"ok": 3, "source_not_identified": 1}, c["status"]
    assert c["1_slot_share"]["all"] == {"S": 1, "S+1": 1, "S+2..4": 1, "S+5+": 0}, c["1_slot_share"]
    per = {p["position_id"]: p for p in c["per_fill"]}
    assert [per[i]["ahead_n"] for i in (10, 11, 12)] == [0, 2, 3]
    # A buy AFTER us in our own slot (Z at index 9, a 9.99M bid) is never "ahead".
    assert per[12]["cu_price_to_lead"] == 8 and per[12]["our_slot_cu_price_to_lead"] is None
    assert per[11]["cu_price_to_lead"] == 5_000_001 and per[11]["our_slot_cu_price_to_lead"] == 1_000_001
    assert per[10]["cu_price_to_lead"] is None
    assert per[11]["ahead_tipped_n"] == 2 and tips(cf[1]["rows"][2]) == {"astralane": 1_000_000}
    b = c["4_by_ahead_bucket"]
    assert (b["0"]["n"], b["1-2"]["n"], b["3+"]["n"]) == (1, 1, 1)
    assert b["0"]["pnl_sum"] == 5.0 and b["1-2"]["wins"] == 0 and b["3+"]["closed"] == 0, b
    assert c["5_cu_price_to_lead"]["ours_already_at_or_above"] == 1, c["5_cu_price_to_lead"]
    sig = c["3_recurring_signers"]
    assert [s["signer"] for s in sig] == ["BOT"] and sig[0]["tips"] == {"jito": [1, 17_000_000]}, sig
    assert sig[0]["same_slot_as_source"] == 2 and sig[0]["sources"] == {"b": 2}, sig
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--census", action="store_true", help="the in-slot ordering census (Alpha 2026-09-28)")
    ap.add_argument("--recipients", action="store_true", help="SOL recipients of buys ahead (tip discovery)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    fills = [f for p in a.files for f in json.loads(Path(p).read_text())]
    if a.recipients:
        print(json.dumps(transfer_recipients(fills), indent=1))
    elif a.census:
        print(json.dumps(census(fills, ledger_outcomes()), indent=1))
    else:
        print(json.dumps(summarize(fills, ledger_meta()), indent=1))


if __name__ == "__main__":
    main()

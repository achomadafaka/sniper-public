#!/usr/bin/env python3
"""Mechanical screen over rising-wallet candidates.

This filters fresh wallets for analyzer review without claiming live edge from a tiny resolved
sample. It rejects maker-drip accumulators, high-churn wallets, straddlers, and off-season-only
books. Reads runs/discovery_rising_candidates.json; uses recent fills only. Read-only.
"""
import argparse, sys, os, json, statistics
from collections import defaultdict
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from sniper.markets import polymarket_api as P
from sniper.analysis import market_taxonomy as LG

TOP_N = 30
CAND_PATH = os.path.join(ROOT, "runs", "discovery_rising_candidates.json")


# slug -> coarse league via the shared taxonomy module.
_league_kw = LG.league_from_slug


# Outright/futures detector lives in the shared taxonomy module.
_is_outright = LG.is_outright


def screen_wallet(sess, wallet, active: set):
    try:
        trades = P.fetch_user_trades(sess, wallet, limit=1000)
    except Exception as e:  # noqa: BLE001
        return {"err": type(e).__name__}
    pos = defaultdict(lambda: {"stake": 0.0, "fills": 0, "lg": None, "outright": False})
    fills_all = []
    sold_keys = set()             # positions the wallet SOLD (on Polymarket a sell is pre-resolution
                                  # — post-resolution you REDEEM — so any sell = an early exit)
    for t in trades:
        side = (t.get("side") or "").upper()
        usd = float(t.get("usdcSize") or (float(t.get("size") or 0) * float(t.get("price") or 0)))
        if usd <= 0:
            continue
        try: oidx = int(t.get("outcomeIndex"))
        except (TypeError, ValueError): oidx = -1
        cid = str(t.get("conditionId") or "")
        if not cid:
            continue
        if side == "SELL":
            sold_keys.add((cid, oidx))
            continue
        if side != "BUY":
            continue
        blob = (t.get("eventSlug") or "") + " " + (t.get("title") or "")
        p = pos[(cid, oidx)]
        p["stake"] += usd; p["fills"] += 1; p["lg"] = _league_kw(blob)
        p["outright"] = p["outright"] or _is_outright(blob)
        fills_all.append(usd)
    if not pos:
        return {"err": "no_buys"}
    cid_oidx = defaultdict(set)
    for (cid, oidx) in pos:
        cid_oidx[cid].add(oidx)
    straddle_cids = sum(1 for c, ox in cid_oidx.items() if len(ox) > 1)
    stakes = [p["stake"] for p in pos.values()]
    fpp = [p["fills"] for p in pos.values()]
    soccer_pos = sum(1 for p in pos.values() if p["lg"] == "Soccer" and not p["outright"])
    n = len(pos)
    # FAIL-OPEN: an empty `active` set means season detection failed (Gamma fetch), not that the
    # wallet bets nothing in-season. Scoring active_share=0 there would silently reject EVERY
    # wallet on the active_share>=0.50 gate. Treat it as 1.0 (non-binding) so detection failure
    # doesn't fail closed. (review #LOW 2026-06-17)
    active_share = round(sum(1 for p in pos.values() if p["lg"] in active and not p["outright"]) / n, 2) if active else 1.0
    return {
        "n_positions": n, "n_fills": len(fills_all),
        "fills_per_pos": round(sum(fpp) / n, 1),
        "median_fill": round(statistics.median(fills_all), 0),
        "median_pos": round(statistics.median(stakes), 0),
        "straddle_rate": round(straddle_cids / len(cid_oidx), 2),
        "active_share": active_share,
        "soccer_share": round(soccer_pos / n, 2),
        # exit_rate = fraction of BOUGHT positions the wallet later SOLD (exited before resolution).
        # Position-level, NOT dollar-weighted sell/buy (review-2026-06-17 #4): a hold-to-resolution
        # picker rarely sells; an exit-driven trader (PatienceCapital) sells most positions early.
        # Per-position avoids one big capital rotation dominating a dollar ratio.
        "exit_rate": round(len(set(pos.keys()) & sold_keys) / n, 2),
    }


# Minimum share of a wallet's recent positions in a currently-active league. This is a SOFT
    # in-season floor, not proof of edge. 0.50 was too strict for a thin/transitional calendar
    # (57/59 discovery candidates failed only on it).
ACTIVE_SHARE_FLOOR = 0.20


def verdict(m):
    # CLEAN: directional (not maker-drip), modest fill rate, non-straddle, HOLD-to-resolution
    # (low exit_rate — not exit-driven like PatienceCapital),
    # SOME current in-season activity, tradeable bet size, at least a few distinct positions.
    if "err" in m:
        return "ERR"
    ok = (m["fills_per_pos"] <= 25 and m["median_fill"] >= 50 and m["straddle_rate"] <= 0.10
          and m["exit_rate"] <= 0.35 and m["active_share"] >= ACTIVE_SHARE_FLOOR
          and 1000 <= m["median_pos"] <= 300000 and m["n_positions"] >= 5)
    return "ENROLL" if ok else "skip"


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("top_n", nargs="?", type=int, default=TOP_N)
    args = parser.parse_args(argv)
    cands = json.load(open(CAND_PATH))["candidates"][:args.top_n]
    sess = P.build_session()
    active = LG.active_leagues()
    if active:
        print(f"active leagues: {sorted(active)}", file=sys.stderr)
    else:
        print("WARN: active-league detection returned NOTHING — Gamma fetch likely failed; "
              "season filter DISABLED (fail-open) so wallets aren't all rejected.", file=sys.stderr)
    print(f"{'wallet':44}{'name':16}{'npos':>5}{'f/pos':>6}{'medFill':>8}{'medPos':>9}{'strad':>6}{'exit':>6}{'actv':>6}{'socc':>6}  verdict")
    print("-" * 124)
    enroll = []
    for c in cands:
        m = screen_wallet(sess, c["wallet"], active)
        v = verdict(m)
        if "err" in m:
            print(f"{c['wallet']:44}{(c['name'] or '')[:15]:16}  {m['err']}")
            continue
        print(f"{c['wallet']:44}{(c['name'] or '')[:15]:16}{m['n_positions']:>5}{m['fills_per_pos']:>6}"
              f"{m['median_fill']:>8,.0f}{m['median_pos']:>9,.0f}{m['straddle_rate']:>6.2f}"
              f"{m['exit_rate']:>6.2f}{m['active_share']:>6.2f}{m['soccer_share']:>6.2f}  {v}")
        if v == "ENROLL":
            enroll.append({**c, **m})
    out = os.path.join(ROOT, "runs", "screen_enroll_candidates.json")
    with open(out, "w") as f:
        json.dump({"active_leagues": sorted(active), "enroll": enroll}, f, indent=2)
    print(f"\nENROLL candidates: {len(enroll)} -> {out}")
    for e in enroll:
        print(f"  {e['wallet']}  {e.get('name','')}  roi={e.get('roi',0)*100:.0f}% npos={e['n_positions']} "
              f"medPos=${e['median_pos']:,.0f} soccer={e['soccer_share']:.0%}")


if __name__ == "__main__":
    main()

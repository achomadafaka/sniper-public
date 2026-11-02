#!/usr/bin/env python3
"""Discover candidate winning wallets active in CURRENTLY-IN-SEASON soccer (World Cup 2026 is
live: ~$96M/24h on Polymarket; NBA/NHL/CBB/CFB are off-season). Scans open soccer markets,
collects wallets placing LARGE buys, ranks by conviction volume, enriches with official
leaderboard PnL. Read-only discovery — output feeds analyzer + dossiers. NOT live trading."""
import sys, os, json, time
from collections import defaultdict
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__))); sys.path.insert(0, ROOT)
from sniper.markets import polymarket_api as P
from sniper.analysis.market_taxonomy import slug_keywords, is_outright
import httpx

# SPORT-SPECIFIC soccer tokens only — generic "-vs-"/" vs " matched ufc/tennis/elections too
# (review #HIGH 2026-06-17). Single source: the shared league taxonomy (was a
# drifting copy here). Soccer game markets carry world-cup/fifa/league tokens.
SOCCER_KW = slug_keywords("Soccer")
MIN_TRADE_USD = 5000.0      # "large" bet floor for a candidate signal
MIN_MKT_VOL = 20000.0


def open_soccer_markets(client):
    out = []
    for off in range(0, 2000, 500):
        r = client.get("https://gamma-api.polymarket.com/markets",
                       params={"closed": "false", "active": "true", "limit": 500, "offset": off,
                               "order": "volume24hr", "ascending": "false"}, timeout=25)
        ms = r.json()
        if not ms:
            break
        for m in ms:
            blob = ((m.get("slug") or "") + " " + (m.get("question") or "")).lower()
            if not any(k in blob for k in SOCCER_KW):
                continue
            if is_outright(blob):           # drop tournament winner/top-scorer/etc. futures
                continue                    # consistency with the mechanics screen
            if float(m.get("volume24hr") or 0) < MIN_MKT_VOL:
                continue
            cid = m.get("conditionId")
            if cid:
                out.append({"cid": cid, "slug": m.get("slug") or "", "q": (m.get("question") or "")[:50],
                            "vol24": float(m.get("volume24hr") or 0)})
    return out


def main():
    client = httpx.Client(headers={"User-Agent": "sniper-discovery/1"})
    sess = P.build_session()
    mkts = open_soccer_markets(client)
    print(f"open soccer markets (vol>=${MIN_MKT_VOL:,.0f}): {len(mkts)}", file=sys.stderr)

    wallets = defaultdict(lambda: {"usd": 0.0, "n": 0, "mkts": set(), "name": ""})
    for i, m in enumerate(mkts):
        try:
            trades = P.fetch_market_trades(sess, m["cid"], side="BUY", limit=500)
        except Exception as e:  # noqa: BLE001
            print(f"  trade fetch err {m['slug'][:30]}: {type(e).__name__}", file=sys.stderr)
            continue
        for t in trades:
            usd = float(t.get("usdcSize") or (float(t.get("size") or 0) * float(t.get("price") or 0)))
            if usd < MIN_TRADE_USD:
                continue
            w = (t.get("proxyWallet") or t.get("proxy") or "").lower()
            if not w:
                continue
            rec = wallets[w]
            rec["usd"] += usd; rec["n"] += 1; rec["mkts"].add(m["cid"])
            rec["name"] = rec["name"] or (t.get("name") or t.get("pseudonym") or "")
        time.sleep(0.05)
        if (i + 1) % 10 == 0:
            print(f"  scanned {i+1}/{len(mkts)} markets, {len(wallets)} wallets so far", file=sys.stderr)

    ranked = sorted(wallets.items(), key=lambda kv: -kv[1]["usd"])
    top = ranked[:30]
    # enrich top with official leaderboard PnL where the wallet appears in the public LB
    lb = {}
    try:
        for off in (0, 50, 100, 150):
            for row in P.fetch_leaderboard_rankings(sess, category="OVERALL", time_period="ALL",
                                                    order_by="PNL", limit=50, offset=off):
                lb[(row.get("wallet") or "").lower()] = row
    except Exception as e:  # noqa: BLE001
        print(f"  leaderboard err: {type(e).__name__}", file=sys.stderr)

    print(f"\n{'wallet':44}{'soccer$':>12}{'nbig':>6}{'mkts':>6}  name / official_pnl")
    print("-" * 90)
    candidates = []
    for w, rec in top:
        lbr = lb.get(w)
        pnl = f"  LB_pnl=${float(lbr['official_pnl']):,.0f}" if lbr and lbr.get("official_pnl") is not None else ""
        nm = (rec["name"] or (lbr or {}).get("username") or "")[:24]
        print(f"{w:44}{rec['usd']:>12,.0f}{rec['n']:>6}{len(rec['mkts']):>6}  {nm}{pnl}")
        candidates.append({"wallet": w, "soccer_buy_usd": round(rec["usd"], 2), "n_big": rec["n"],
                           "n_markets": len(rec["mkts"]), "name": rec["name"],
                           "lb_pnl": (float(lbr["official_pnl"]) if lbr and lbr.get("official_pnl") is not None else None)})
    out_path = os.path.join(ROOT, "runs", "discovery_soccer_candidates.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"markets_scanned": len(mkts), "candidates": candidates}, f, indent=2)
    print(f"\nwrote {out_path} ({len(candidates)} candidates)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Daily candidacy watch for a chain we do NOT trade yet, from our own tape. Read-only, no network.

It answers one question: would a funded runner on this chain have had anything worth copying?
Every number is an UPPER BOUND on what would have been CONSIDERED, never on fills:

  * "armed" means the trader is armed on SOME chain -- nothing is armed on a chain we do not trade;
  * the market cap is the FEED's value at the trader's buy, not the live value the runner resolves
    and fails closed on;
  * the volume_h1 and turnover_h1 gates CANNOT be evaluated here. Their inputs come from the market
    -data provider, which returns nothing for a chain missing from `FOMO_TO_DEX`, so this script
    reports them as absent rather than passed.

Run it daily. It appends one snapshot per run and prints the drift against the previous one, so a
launch burst that decays is visible instead of averaging into a flattering total.
"""
import argparse, json, sqlite3, statistics, sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
# THE STATE ROOT, NOT THE CODE ROOT. A worktree and a pinned release both lack `.env`, and a bare
# settings import then returns plausible DEFAULTS: measured 2026-09-16, the mcap ceiling read 0
# (which this script would have taken as "no ceiling") and the source-buy floor read 0 instead of
# 200. Every gate value here must come from the env the RUNNER reads.
import os

STATE_ROOT = Path(os.getenv("SNIPER_STATE_ROOT") or "/home/bigpapi/Desktop/Sniper")


def _pct(values, p):
    if not values:
        return None
    if len(values) < 3:
        return max(values)
    return statistics.quantiles(values, n=100)[p - 1]


def snapshot(conn, chain, *, window_days, cfg):
    rows = [dict(r) for r in conn.execute(
        "SELECT e.created_at, e.mint, e.their_size_usd s, e.their_market_cap m, e.lag_s, "
        "e.source_user_id, w.handle, w.real_chains "
        "FROM memecoin_events e JOIN memecoin_wallets w ON w.source_user_id = e.source_user_id "
        "WHERE e.network_id = ? AND e.side = 'buy' AND w.real_approved = 1 "
        "AND e.created_at >= datetime('now', ?)",
        (chain, "-%d days" % window_days))]
    if not rows:
        return {"chain": chain, "armed_buys": 0}
    times = sorted(datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")) for r in rows)
    span_h = max(0.01, (times[-1] - times[0]).total_seconds() / 3600)
    sizes = [float(r["s"]) for r in rows if r["s"] is not None]
    ceiling = float(cfg.MEMECOIN_REAL_MAX_MCAP_USD or 0) or float("inf")
    floor_usd = float(getattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_USD", 0) or 0)
    ratio_floor = float(getattr(cfg, "MEMECOIN_REAL_MIN_SOURCE_BUY_MCAP_RATIO", 0) or 0)
    lag_cap = float(cfg.MEMECOIN_REAL_FRESH_FOLLOW_MAX_LAG_S or 0)

    def in_band(r):
        return r["m"] is not None and 0 < float(r["m"]) <= ceiling

    band = [r for r in rows if in_band(r)]
    fresh = [r for r in band if r["lag_s"] is not None and 0 <= float(r["lag_s"]) <= lag_cap]
    # the two gates our own tape CAN answer beyond the band: the absolute source-buy floor and the
    # source-buy / mcap ratio
    over_floor = [r for r in fresh if r["s"] is not None and float(r["s"]) >= floor_usd]
    over_ratio = [r for r in over_floor
                  if ratio_floor <= 0 or (r["m"] and float(r["s"]) / float(r["m"]) >= ratio_floor)]
    # traders armed on 4663 specifically, our closest analogue chain
    on_4663 = [r for r in over_ratio if "4663" in str(r["real_chains"] or "")]
    hours = Counter(r["created_at"][11:13] for r in rows)
    return {
        "chain": chain, "window_days": window_days, "span_h": round(span_h, 1),
        "armed_buys": len(rows), "armed_traders": len({r["source_user_id"] for r in rows}),
        "coins": len({r["mint"] for r in rows}),
        "buys_per_h": round(len(rows) / span_h, 2),
        "hours_covered": len(hours), "median_buys_per_h": statistics.median(hours.values()),
        "size_median": round(statistics.median(sizes), 2) if sizes else None,
        "size_p90": round(_pct(sizes, 90), 2) if sizes else None,
        "below_source_floor": sum(1 for v in sizes if v < floor_usd),
        "source_floor_usd": floor_usd,
        "in_band": len(band), "in_band_coins": len({r["mint"] for r in band}),
        "fresh": len(fresh),
        "over_source_floor": len(over_floor),
        "over_ratio_floor": len(over_ratio), "ratio_floor": ratio_floor,
        "considered_coins": len({r["mint"] for r in over_ratio}),
        "buys_from_traders_armed_on_4663": len(on_4663),
        "distinct_traders_armed_on_4663": len({r["source_user_id"] for r in on_4663}),
        "volume_h1_gate": "absent: provider has no chain entry",
        "turnover_h1_gate": "absent: provider has no chain entry",
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chain", default="5042")
    ap.add_argument("--window-days", type=int, default=7)
    ap.add_argument("--db", default=str(STATE_ROOT / "sniper_tracker.db"))
    ap.add_argument("--history", default=str(STATE_ROOT / "data" / "chain_candidacy_watch.jsonl"))
    ap.add_argument("--no-append", action="store_true", help="print only, store nothing")
    args = ap.parse_args()

    from sniper.util.envfiles import load_env_files
    load_env_files(str(STATE_ROOT))
    from sniper.settings import memecoin as cfg
    if not float(getattr(cfg, "MEMECOIN_REAL_MAX_MCAP_USD", 0) or 0):
        sys.exit("REFUSE: the mcap ceiling reads 0, so this process did not load the runner's env. "
                 "Point SNIPER_STATE_ROOT at the checkout that owns .env.")

    conn = sqlite3.connect("file:%s?mode=ro" % args.db, uri=True)
    conn.row_factory = sqlite3.Row
    now = snapshot(conn, args.chain, window_days=args.window_days, cfg=cfg)
    conn.close()

    history = Path(args.history)
    previous = None
    if history.exists():
        for line in history.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("chain") == args.chain:
                previous = row
    print(json.dumps(now, indent=1))
    if previous:
        print("\nDRIFT against %s:" % previous.get("at"))
        for key in ("armed_buys", "armed_traders", "coins", "buys_per_h", "in_band",
                    "in_band_coins", "over_ratio_floor", "considered_coins",
                    "from_traders_armed_on_4663"):
            was, is_ = previous.get(key), now.get(key)
            if isinstance(was, (int, float)) and isinstance(is_, (int, float)):
                print("  %-28s %8s -> %-8s %+.2f" % (key, was, is_, is_ - was))
    else:
        print("\nNo earlier snapshot for this chain: this run is the baseline.")
    if not args.no_append:
        history.parent.mkdir(parents=True, exist_ok=True)
        with history.open("a") as fh:
            fh.write(json.dumps(now) + "\n")


if __name__ == "__main__":
    main()

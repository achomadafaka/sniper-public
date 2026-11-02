#!/usr/bin/env python3
"""Forward paper-FOLLOW loop — the phase-2 price-band counterfactual, live.

Every cycle: snapshot each tracked (copyable) trader's positions, detect NEW directional entries,
and open a paper follow at the CURRENT mark (the honest price a lagging follower could get) plus
the taker fee. Mirror their exit (or a max-hold cap) and grade net-of-fee bps. The accumulating
follow PnL answers: 'does following a copyable HL pro, with our lag and 9bps, actually net
positive?' — the gate that killed every Polymarket 'copyable' wallet.

Public read-only data; paper only; no orders, no keys. Not copy advice, not a live trigger."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.markets import hyperliquid_api as HL  # noqa: E402
from sniper.hyperlive import follow_sim as FS  # noqa: E402
from sniper.hyperlive import intel_store as IS  # noqa: E402

HL_INFO = "https://api.hyperliquid.xyz/info"
DEFAULT_DB = "/home/bigpapi/Desktop/Sniper/data/hl_intel.sqlite"
MAX_HOLD_MS = 24 * 3600 * 1000        # cap a follow at 24h if the trader never closes


def _marks() -> dict:
    req = urllib.request.Request(HL_INFO, data=json.dumps({"type": "metaAndAssetCtxs"}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "curl/8"})
    meta, ctxs = json.load(urllib.request.urlopen(req, timeout=20))
    out = {}
    for u, c in zip(meta["universe"], ctxs):
        px = c.get("markPx") or c.get("midPx")
        if px:
            out[u["name"].upper()] = float(px)
    return out


def select_followable(tracked: list[dict]) -> list[dict]:
    """Follow the phase-1 archetype-pass cohort (watch ∪ proven) — NOT the display verdict.
    Keying off verdict=='copyable' would silently stop collection once we rename to 'watch'.
    Legacy rows (pre-migration, no archetype_pass) fall back to the old verdict."""
    return [t for t in tracked
            if t.get("archetype_pass") == 1 or t.get("verdict") == "copyable"]


def _cycle(conn, now_ms: int) -> tuple[int, int]:
    tracked = select_followable(IS.get_tracked(conn))
    if not tracked:
        return 0, 0
    marks = _marks()
    opened = closed = 0
    open_follows = IS.get_open_follows(conn)
    cur_states = {}
    # 1) detect new entries -> open follows at current mark
    for t in tracked:
        w = t["wallet"]
        try:
            cur = FS.positions_map(HL.fetch_clearinghouse_state(w))
        except Exception:
            continue
        cur_states[w] = cur
        # First time we see a trader: record baseline only. Following their pre-existing book
        # (opened in the past at prices we don't know) would be a lookahead confound — we only
        # follow entries opened WHILE we're watching.
        if not IS.seen_trader(conn, w):
            IS.save_trader_state(conn, w, cur, ts_ms=now_ms)
            continue
        prev = IS.get_trader_state(conn, w)
        for e in FS.detect_entries(prev, cur):
            mark = marks.get(e["coin"].upper())
            if not mark:
                continue
            # don't double-open the same (wallet, coin, side) while one is live
            if any(f["wallet"] == w and f["coin"] == e["coin"] and f["side"] == e["side"]
                   for f in open_follows):
                continue
            IS.open_follow(conn, {"wallet": w, "coin": e["coin"], "side": e["side"],
                                  "kind": e["kind"], "entry_mark": mark}, ts_ms=now_ms)
            opened += 1
        IS.save_trader_state(conn, w, cur, ts_ms=now_ms)
    # 2) manage open follows: mirror the trader's exit, or time-cap
    for f in IS.get_open_follows(conn):
        mark = marks.get(f["coin"].upper())
        if not mark:
            continue
        held = cur_states.get(f["wallet"], {}).get(f["coin"])
        trader_out = (held is None) or (held["side"] != f["side"])
        timed_out = now_ms - f["detect_ts_ms"] >= MAX_HOLD_MS
        if trader_out or timed_out:
            net = FS.follow_net_bps(f["side"], f["entry_mark"], mark)
            if net is None:
                continue
            IS.close_follow(conn, f["id"], exit_mark=mark, net_bps=net,
                            reason="mirror_exit" if trader_out else "time_cap", ts_ms=now_ms)
            closed += 1
    return opened, closed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HL forward paper-follow loop")
    ap.add_argument("--db-path", default=DEFAULT_DB)
    ap.add_argument("--interval-seconds", type=int, default=180)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args(argv)
    conn = IS.connect(args.db_path)
    IS.ensure_schema(conn)
    try:
        while True:
            try:
                o, c = _cycle(conn, int(time.time() * 1000))
                s = IS.follow_summary(conn)
                print(f"{time.strftime('%FT%TZ', time.gmtime())} follow: +{o} opened, {c} closed | "
                      f"open={s['open']} closed={s['closed']} wr={s['win_rate']:.0%} "
                      f"sum={s['sum_net_bps']:+.0f}bps avg={s['avg_net_bps']:+.1f}bps", file=sys.stderr)
            except Exception as e:
                print(f"follow cycle failed: {e}", file=sys.stderr)
            if args.once:
                break
            time.sleep(args.interval_seconds)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

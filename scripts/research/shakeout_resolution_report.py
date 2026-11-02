#!/usr/bin/env python3
"""WO-3 — the decisive episodic-memory read: does a coin's 2nd+ shakeout resolve upward differently
from its first?

That number decides whether a "shakeout-count" term ever enters v4+. If a later shakeout is no more
predictive than a first, the episodic memory is display-only forever. READ-ONLY over recorded pool
tapes; reuses the exit brain's own primitives so "shakeout" means exactly what the brain means (a
confirmed swing low broken then recovered — a flow-rejected fakeout).

Resolution of a shakeout = price N minutes after its recovery bar vs price AT the recovery bar.
Upward if higher. Censored (tape ends before N min) is reported separately, never folded in.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from sniper.analysis.exit_features import confirmed_pivots, exec_price_path_per_block  # noqa: E402

HORIZON_MIN = 15          # [inherited] engine target signal horizon (matches the decision-report prereg)
CONFIRM = 3               # [inherited] production pivot confirmation bars
MIN_POOLS = 20            # below this, report counts only — no verdict


def _epoch_ts(v):
    """block_times stores ISO-string timestamps ('2026-07-24T16:50:33+00:00') on EVM; some stores
    hold unix ints. Normalize to epoch seconds (numeric event time) or None."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        try:
            return int(v)
        except (ValueError, TypeError):
            return None


def _shakeout_recoveries(series, pivots):
    """[(recovery_ts, recovery_price, ordinal)] — each confirmed low that was broken then recovered,
    in the order they resolved. ordinal 1 = the coin's first shakeout."""
    lows = sorted((p.block_ts, p.price) for p in pivots if p.kind == "low")
    events = []
    for tL, pL in lows:
        after = [(t, px) for (t, px) in series if t > tL and px and px > 0]
        broke_i = next((i for i, (_t, px) in enumerate(after) if px < pL), None)
        if broke_i is None:
            continue
        rec = next(((t, px) for (t, px) in after[broke_i:] if px > pL), None)
        if rec is None:
            continue
        events.append((rec[0], rec[1]))
    events.sort()
    return [(t, px, i + 1) for i, (t, px) in enumerate(events)]


def _resolution(series, rec_ts, rec_px, horizon_s):
    """None if censored (no bar >= rec_ts+horizon), else True/False = price higher later."""
    later = next((px for (t, px) in series if t >= rec_ts + horizon_s and px and px > 0), None)
    if later is None:
        return None
    return later > rec_px


def _pool_series(conn, chain_id, pool, quote_side):
    from sniper.persistence import db_ticks as t
    rows = conn.execute(
        "SELECT kind, block_no, maker, amount0, amount1 FROM pool_ticks "
        "WHERE chain_id=? AND pool=? AND block_no IS NOT NULL ORDER BY block_no DESC LIMIT 20000",
        (str(chain_id), pool)).fetchall()
    swaps = [dict(r) for r in rows]
    blocks = {s["block_no"] for s in swaps if s.get("block_no") is not None}
    ts_map = t.block_ts_map(conn, chain_id, blocks)
    for s in swaps:
        s["block_ts"] = _epoch_ts(ts_map.get(s["block_no"]))
    swaps = [s for s in swaps if s["block_ts"] is not None]
    if not swaps:
        return []
    path = exec_price_path_per_block(swaps, quote_side=int(quote_side))
    return [(ts, px) for (ts, px, _n, _k) in path]


def run(db_path, horizon_min=HORIZON_MIN):
    from sniper.persistence import db_ticks as t
    if not os.path.exists(db_path):
        return None
    conn = t.connect_ticks_readonly(db_path)
    horizon_s = horizon_min * 60
    # buckets: ordinal-1 vs ordinal-2+  -> [upward, downward, censored]
    tally = {"1st": defaultdict(int), "2nd+": defaultdict(int)}
    pools_with_shakeouts = 0
    try:
        pools = conn.execute(
            "SELECT DISTINCT p.chain_id, p.pool, m.quote_side FROM pool_ticks p "
            "JOIN pool_meta m ON m.chain_id=p.chain_id AND m.pool=p.pool "
            "WHERE m.quote_side IS NOT NULL").fetchall()
        for chain_id, pool, quote_side in pools:
            series = _pool_series(conn, chain_id, pool, quote_side)
            if len(series) < 10:
                continue
            events = _shakeout_recoveries(series, confirmed_pivots(series, confirm=CONFIRM))
            if not events:
                continue
            pools_with_shakeouts += 1
            for rec_ts, rec_px, ordinal in events:
                res = _resolution(series, rec_ts, rec_px, horizon_s)
                bucket = "1st" if ordinal == 1 else "2nd+"
                key = "censored" if res is None else ("up" if res else "down")
                tally[bucket][key] += 1
    finally:
        conn.close()
    return {"pools": pools_with_shakeouts, "tally": {k: dict(v) for k, v in tally.items()},
            "horizon_min": horizon_min}


def _rate(d):
    up, down = d.get("up", 0), d.get("down", 0)
    n = up + down
    return (up / n, n) if n else (None, 0)


def render(result):
    if result is None:
        return "No tape DB found — nothing to read."
    lines = ["", "=" * 66, "  SHAKEOUT RESOLUTION — 1st vs 2nd+ (read-only)", "=" * 66,
             f"  horizon={result['horizon_min']}m  pools_with_shakeouts={result['pools']}"]
    for bucket in ("1st", "2nd+"):
        d = result["tally"][bucket]
        rate, n = _rate(d)
        cens = d.get("censored", 0)
        rate_s = f"{rate:.1%}" if rate is not None else "n/a"
        lines.append(f"  {bucket:5s}: upward={rate_s} (n={n})  censored={cens}")
    (r1, n1), (r2, n2) = _rate(result["tally"]["1st"]), _rate(result["tally"]["2nd+"])
    if result["pools"] < MIN_POOLS or n1 == 0 or n2 == 0:
        lines.append(f"  VERDICT: insufficient (pools<{MIN_POOLS} or an empty bucket) — no claim.")
    else:
        delta = r2 - r1
        lines.append(f"  VERDICT: 2nd+ vs 1st upward delta = {delta:+.1%}. "
                     + ("worth a prereg'd shakeout-count term" if abs(delta) >= 0.10
                        else "no material difference — keep episodic memory display-only"))
    lines += ["=" * 66, ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Shakeout resolution report (read-only).")
    ap.add_argument("--db", default=None, help="tape DB path (default: state-root ticks DB)")
    ap.add_argument("--horizon-min", type=int, default=HORIZON_MIN)
    args = ap.parse_args()
    from sniper.persistence.db_ticks import ticks_db_path
    print(render(run(args.db or ticks_db_path(), horizon_min=args.horizon_min)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

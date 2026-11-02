"""Edge decomposition for the copy book — where does the PnL actually come from? Pure functions.

Answers three questions the headline number hides:
  * by coin / major-vs-alt / side — is the edge broad or a couple of names?
  * consensus/crowding — when many tracked wallets pile into the same coin+side, is that a
    stronger signal (smart-money consensus) or a crowded exit (worse fills, reversal)?
The scorecard says IF there's edge; this says WHERE, so selection + sizing can target it."""
from __future__ import annotations

import statistics as st
from collections import defaultdict

from sniper.hyperlive.altshort_universe import ALT_SHORT_EXCLUDED, strategy_bucket

MAJORS = ALT_SHORT_EXCLUDED  # compatibility alias; this is the strategy exclusion set


def _summ(vals) -> dict:
    v = [float(x) for x in vals if x is not None]
    if not v:
        return {"n": 0}
    return {"n": len(v), "mean": round(st.mean(v), 2), "median": round(st.median(v), 2),
            "sum": round(sum(v), 1), "win_rate": round(sum(1 for x in v if x > 0) / len(v), 3)}


def by_key(rows, keyfn, *, value: str = "pnl_bps") -> dict:
    buckets = defaultdict(list)
    for r in rows:
        val = r.get(value)
        if val is not None:
            buckets[keyfn(r)].append(val)
    return {k: _summ(v) for k, v in buckets.items()}


def by_coin(rows) -> dict:
    return by_key(rows, lambda r: str(r.get("coin", "")).upper())


def by_side(rows) -> dict:
    return by_key(rows, lambda r: r.get("side"))


def major_vs_alt(rows) -> dict:
    def bucket(row):
        value = strategy_bucket(row.get("coin"))
        return "major" if value == "core" else value

    return by_key(rows, bucket)


def annotate_consensus(rows, *, now_ms: int) -> list[dict]:
    """For each row, consensus_n = how many mirrors (incl. itself) in the SAME coin+side had a
    live interval [detect, exit] covering this row's detect time. Open rows extend to now_ms."""
    groups = defaultdict(list)
    for r in rows:
        groups[(str(r.get("coin", "")).upper(), r.get("side"))].append(r)
    out = []
    for r in rows:
        t = r.get("detect_ts_ms")
        key = (str(r.get("coin", "")).upper(), r.get("side"))
        cnt = 1
        for o in groups[key]:
            if o is r:
                continue
            start = o.get("detect_ts_ms")
            end = o.get("exit_ts_ms") if o.get("exit_ts_ms") is not None else now_ms
            if start is None:
                continue
            if start <= t <= max(start, end):
                cnt += 1
        rr = dict(r)
        rr["consensus_n"] = cnt
        out.append(rr)
    return out


def consensus_buckets(annotated_closed) -> dict:
    """Bucket closed trades by concurrent consensus at entry: solo (1), small (2-3), crowd (4+).
    A rising mean across buckets => consensus helps; falling => crowding hurts."""
    def bucket(n):
        return "solo(1)" if n <= 1 else ("small(2-3)" if n <= 3 else "crowd(4+)")
    return by_key(annotated_closed, lambda r: bucket(r.get("consensus_n", 1)))


def top_contributors(rows, *, value: str = "pnl_bps", n: int = 8) -> list[dict]:
    """Coins ranked by total PnL contribution (sum), to expose concentration by name."""
    d = by_coin(rows)
    ranked = sorted(d.items(), key=lambda kv: kv[1].get("sum", 0), reverse=True)
    return [{"coin": k, **v} for k, v in ranked[:n]]

#!/usr/bin/env python3
"""Copier-crowd read: do a source's in-slot followers cost our funded Solana copies?

Alpha overnight order 2026-09-29, for the operator's "smarter ways to copy". Pre-registered as
brain episode ce226d0f before any outcome was read. Read-only: the funded ledger through a read-only
connection and the wallet graph DB (data/memecoin_wallet_graph.db, slot reads written by the hourly
graph cron). No network. wallet_crowding_table.py describes crowding per wallet; this measures
per-fill OUTCOME by crowd size, with a cluster bootstrap and a split-half hold-out.

UNIT: a closed funded Solana position opened since --since whose source buy has an ok slot read
(same source_user_id and mint, detected_at in [opened_at-600 s, opened_at], latest wins).
CROWD: companions in that read landing AFTER the source (transactionIndex); our own entry sigs were
excluded by the reader. Bins 0, 1-2, 3-5, 6+. Follower USD is censored (not cached).
OUTCOME: realized_pnl and return = realized_pnl / entry_notional; entry cost vs source =
fill_price / source_price - 1 from the position's attempt, by price_attribution.
HUBS: 'crowded' if the median follower count over all the wallet's ok reads is >= 3, 'quiet' if 0.
STATS: 95% CI from a 2,000-draw bootstrap over source wallets; headline 6+ minus 0 mean return; the
direction must hold in both halves (fills split at the median opened_at) to be called.

    python3 scripts/research/copier_crowd_read.py --since 2026-09-13T00:00:00+00:00 --out read.json
    python3 scripts/research/copier_crowd_read.py --selftest
"""
import argparse
import collections
import json
import random
import statistics
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

MATCH_S = 600.0
BINS = ((0, 0, "0"), (1, 2, "1-2"), (3, 5, "3-5"), (6, 10 ** 9, "6+"))
DRAWS = 2000


def ts(iso):
    return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()


def bin_of(n):
    return next(label for lo, hi, label in BINS if lo <= n <= hi)


def followers(read):
    if read.get("index") is None:
        return None
    return sum(1 for c in read.get("companions") or [] if c.get("index") is not None and c["index"] > read["index"])


def match(position, reads_by_key):
    """The latest ok read of the same (source, mint) detected within MATCH_S before the fill."""
    t = ts(position["opened_at"])
    best = None
    for r in reads_by_key.get((position["source_user_id"], position["mint"]), []):
        d = ts(r["detected_at"])
        if t - MATCH_S <= d <= t and (best is None or d > ts(best["detected_at"])):
            best = r
    return best


def wallet_class(reads):
    counts = [f for f in (followers(r) for r in reads) if f is not None]
    if not counts:
        return "unknown"
    med = statistics.median(counts)
    return "crowded" if med >= 3 else "quiet" if med == 0 else "middle"


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def cluster_ci(rows, stat, draws=DRAWS, seed=7):
    """95% CI of stat(rows) resampling source wallets with replacement."""
    by = collections.defaultdict(list)
    for r in rows:
        by[r["source_user_id"]].append(r)
    wallets = sorted(by)
    rng = random.Random(seed)
    vals = []
    for _ in range(draws):
        pick = [x for w in (rng.choice(wallets) for _ in wallets) for x in by[w]]
        v = stat(pick)
        if v is not None:
            vals.append(v)
    if len(vals) < draws // 2:
        return None
    vals.sort()
    return [round(vals[int(0.025 * (len(vals) - 1))], 4), round(vals[int(0.975 * (len(vals) - 1))], 4)]


def bin_mean(rows, label, key="ret"):
    return _mean([r[key] for r in rows if r["bin"] == label and r[key] is not None])


def diff_6_vs_0(rows):
    a, b = bin_mean(rows, "6+"), bin_mean(rows, "0")
    return None if a is None or b is None else a - b


def summarise(rows):
    out = {"n": len(rows), "wallets": len({r["source_user_id"] for r in rows}), "bins": {}}
    for _lo, _hi, label in BINS:
        sub = [r for r in rows if r["bin"] == label]
        out["bins"][label] = {
            "n": len(sub), "wallets": len({r["source_user_id"] for r in sub}),
            "pnl_usd": round(sum(r["pnl"] for r in sub), 2),
            "mean_return": None if not sub else round(bin_mean(rows, label), 4),
            "return_ci": cluster_ci(rows, lambda rs, lb=label: bin_mean(rs, lb)) if sub else None,
            "win_rate": round(sum(1 for r in sub if r["pnl"] > 0) / len(sub), 3) if sub else None,
            "mean_entry_cost": (None if not [r for r in sub if r["cost"] is not None]
                                else round(_mean([r["cost"] for r in sub if r["cost"] is not None]), 4)),
        }
    d = diff_6_vs_0(rows)
    out["diff_6plus_minus_0"] = None if d is None else round(d, 4)
    out["diff_ci"] = cluster_ci(rows, diff_6_vs_0)
    return out


def build(ledger, graph, since):
    positions = [dict(r) for r in ledger.execute(
        "SELECT id, source_user_id, mint, opened_at, closed_at, realized_pnl, entry_notional, signal_source "
        "FROM memecoin_positions WHERE execution_mode='real' AND network_id='solana' AND opened_at >= ? "
        "AND is_open=0 AND realized_pnl IS NOT NULL", (since,))]
    attempts = {}
    for pid, sp, fp, attr, lane in ledger.execute(
            "SELECT position_id, source_price, fill_price, price_attribution, delivery_lane "
            "FROM memecoin_entry_attempts WHERE position_id IS NOT NULL AND decision_started_at >= ?", (since,)):
        attempts[pid] = (sp, fp, attr, lane)
    reads = [dict(zip(("source_user_id", "mint", "detected_at", "index", "companions"), r[:5]))
             for r in graph.execute("SELECT source_user_id, mint, detected_at, idx, companions "
                                    "FROM memecoin_wallet_slot_reads WHERE status='ok'")]
    for r in reads:
        r["companions"] = json.loads(r["companions"] or "[]")
    by_key, by_wallet = collections.defaultdict(list), collections.defaultdict(list)
    for r in reads:
        by_key[(r["source_user_id"], r["mint"])].append(r)
        by_wallet[r["source_user_id"]].append(r)
    classes = {w: wallet_class(rs) for w, rs in by_wallet.items()}
    rows, unmatched = [], collections.Counter()
    for p in positions:
        read = match(p, by_key)
        if read is None:
            unmatched["no_read"] += 1
            continue
        f = followers(read)
        if f is None:
            unmatched["no_source_index"] += 1
            continue
        sp, fp, attr, lane = attempts.get(p["id"], (None, None, None, None))
        cost = (fp / sp - 1.0) if sp and fp and sp > 0 else None
        notional = p["entry_notional"] or 0
        rows.append({"position_id": p["id"], "source_user_id": p["source_user_id"], "opened_at": p["opened_at"],
                     "followers": f, "bin": bin_of(f), "pnl": float(p["realized_pnl"]),
                     "ret": float(p["realized_pnl"]) / notional if notional > 0 else None,
                     "cost": cost, "attribution": attr, "lane": lane,
                     "wallet_class": classes.get(p["source_user_id"], "unknown")})
    rows.sort(key=lambda r: r["opened_at"])
    half = len(rows) // 2
    by_class = {}
    for cls in ("quiet", "middle", "crowded", "unknown"):
        sub = [r for r in rows if r["wallet_class"] == cls]
        by_class[cls] = {"fills": len(sub), "wallets": len({r["source_user_id"] for r in sub}),
                         "pnl_usd": round(sum(r["pnl"] for r in sub), 2),
                         "mean_return": None if not sub else round(_mean([r["ret"] for r in sub if r["ret"] is not None]), 4),
                         "return_ci": cluster_ci(sub, lambda rs: _mean([r["ret"] for r in rs if r["ret"] is not None])) if sub else None}
    hubs = sorted(((w, statistics.median([f for f in (followers(r) for r in rs) if f is not None] or [0]), len(rs))
                   for w, rs in by_wallet.items()), key=lambda x: -x[1])[:8]
    return {
        "since": since, "positions": len(positions), "matched": len(rows), "unmatched": dict(unmatched),
        "reads": len(reads), "all": summarise(rows),
        "first_half": summarise(rows[:half]), "second_half": summarise(rows[half:]),
        "split_at": rows[half]["opened_at"] if rows else None,
        "by_wallet_class": by_class,
        "attribution_mix": dict(collections.Counter(r["attribution"] for r in rows)),
        "lane_mix": dict(collections.Counter(r["lane"] for r in rows)),
        "top_crowded_wallets": [{"source_user_id": w, "median_followers": m, "reads": n} for w, m, n in hubs],
    }


def selftest():
    assert [bin_of(n) for n in (0, 1, 2, 3, 5, 6, 40)] == ["0", "1-2", "1-2", "3-5", "3-5", "6+", "6+"]
    read = {"index": 5, "companions": [{"index": 3}, {"index": 6}, {"index": 9}, {"index": None}]}
    assert followers(read) == 2 and followers({"index": None, "companions": []}) is None
    reads = {("w", "m"): [{"detected_at": "2026-09-20T00:00:00+00:00", "index": 1, "companions": []},
                          {"detected_at": "2026-09-20T00:09:00+00:00", "index": 1, "companions": []},
                          {"detected_at": "2026-09-20T00:11:00+00:00", "index": 1, "companions": []}]}
    p = {"source_user_id": "w", "mint": "m", "opened_at": "2026-09-20T00:10:00+00:00"}
    assert match(p, reads)["detected_at"].endswith("00:09:00+00:00")       # latest before the fill
    assert match(dict(p, opened_at="2026-09-20T01:00:00+00:00"), reads) is None   # outside 600 s
    mk = lambda f: {"index": 0, "companions": [{"index": i + 1} for i in range(f)]}   # noqa: E731
    assert wallet_class([mk(3), mk(4), mk(0)]) == "crowded" and wallet_class([mk(0), mk(0), mk(2)]) == "quiet"
    assert wallet_class([mk(1), mk(2)]) == "middle" and wallet_class([mk(2), mk(2), mk(3)]) == "middle"
    rows = [{"source_user_id": "a%d" % i, "bin": "0", "ret": 0.1, "pnl": 1.0} for i in range(6)]
    rows += [{"source_user_id": "b%d" % i, "bin": "6+", "ret": -0.2, "pnl": -2.0} for i in range(6)]
    assert round(diff_6_vs_0(rows), 4) == -0.3
    ci = cluster_ci(rows, diff_6_vs_0, draws=400)
    assert ci is not None and ci[0] <= -0.3 <= ci[1]
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--since", default="2026-09-13T00:00:00+00:00")
    ap.add_argument("--graph")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    import sqlite3
    from sniper.persistence.db_core import get_readonly_connection
    from sniper.util.envfiles import load_env_files
    from sniper.util.state_paths import state_root
    state = state_root()
    load_env_files(str(state))
    ledger = get_readonly_connection(str(state / "sniper_tracker.db"))
    graph = sqlite3.connect("file:%s?mode=ro" % (args.graph or state / "data" / "memecoin_wallet_graph.db"), uri=True)
    try:
        result = build(ledger, graph, args.since)
    finally:
        ledger.close()
        graph.close()
    text = json.dumps(result, indent=1, default=str)
    if args.out:
        Path(args.out).write_text(text)
    print(text)


if __name__ == "__main__":
    sys.exit(main())

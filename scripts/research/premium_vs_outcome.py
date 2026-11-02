#!/usr/bin/env python3
"""Does the entry premium separate winners from losers? Funded Solana closes, ledger only.

Pre-registered 2026-09-27T16:54:24Z (Gamma, sent to Alpha before any outcome was read):
  claim   "the entry premium does not separate winners from losers" (HANDOFF 04:50Z, n=6)
  x       fill_gap_pct = (our fill / their_price - 1) x 100, frozen at open
  y       realized_pnl / entry_notional x 100; win = realized_pnl > 0
  sample  execution_mode='real', network_id='solana', closed, opened in [--since, --until), copy
          lanes only (follow_activity, onchain_watch, slot_lane); manual excluded
  primary AUC = P(a random loser paid a higher premium than a random winner), ties 0.5, 95%
          bootstrap CI (10,000 resamples, seed 20260927); SUPPORTED if the CI contains 0.5
  second  Spearman rho(x, y), same resamples
Chase-cap truncation: the sample holds only premiums the funded gate admitted.

    python3 scripts/research/premium_vs_outcome.py --since 2026-09-20T16:46:00+00:00 --until 2026-09-27T16:46:00+00:00
    python3 scripts/research/premium_vs_outcome.py --selftest
"""
import argparse
import contextlib
import sqlite3
import statistics as st
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.stats import rankdata, spearmanr

LEDGER = Path(__file__).resolve().parents[2] / "sniper_tracker.db"
COPY_LANES = ("follow_activity", "onchain_watch", "slot_lane")
BUCKETS = ((None, 5), (5, 10), (10, 20), (20, 35), (35, None))
SEED, RESAMPLES = 20260927, 10_000


def ts(s):
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def auc_loser_paid_more(x, win):
    """P(premium of a random loser > premium of a random winner), ties 0.5 (Mann-Whitney U / n1 n0)."""
    x, win = np.asarray(x, float), np.asarray(win, bool)
    n1, n0 = win.sum(), (~win).sum()
    if n1 == 0 or n0 == 0:
        return None
    r = rankdata(x)
    u_losers = r[~win].sum() - n0 * (n0 + 1) / 2
    return float(u_losers / (n0 * n1))


def boot(x, y, win, seed=SEED, n=RESAMPLES):
    rng = np.random.default_rng(seed)
    x, y, win = np.asarray(x, float), np.asarray(y, float), np.asarray(win, bool)
    aucs, rhos = [], []
    for _ in range(n):
        i = rng.integers(0, len(x), len(x))
        a = auc_loser_paid_more(x[i], win[i])
        if a is not None:
            aucs.append(a)
        rhos.append(spearmanr(x[i], y[i]).statistic)
    q = lambda v: [round(float(np.nanpercentile(v, 2.5)), 3), round(float(np.nanpercentile(v, 97.5)), 3)]
    return q(aucs), q(rhos)


def analyse(rows):
    """rows: (lane, fill_gap_pct, realized_pnl, entry_notional)."""
    x = [r[1] for r in rows]
    y = [r[2] / r[3] * 100 for r in rows]
    win = [r[2] > 0 for r in rows]
    auc = auc_loser_paid_more(x, win)
    auc_ci, rho_ci = boot(x, y, win)
    buckets = []
    for lo, hi in BUCKETS:
        sel = [(yi, r) for yi, r in zip(y, rows) if (lo is None or r[1] >= lo) and (hi is None or r[1] < hi)]
        buckets.append({"bucket": f"{'' if lo is None else lo}..{'' if hi is None else hi}", "n": len(sel),
                        "wins": sum(1 for _, r in sel if r[2] > 0),
                        "median_y_pct": round(st.median([v for v, _ in sel]), 1) if sel else None,
                        "net": round(sum(r[2] for _, r in sel), 2)})
    return {"n": len(rows), "wins": sum(win), "auc_loser_paid_more": round(auc, 3) if auc is not None else None,
            "auc_ci95": auc_ci, "rho": round(float(spearmanr(x, y).statistic), 3), "rho_ci95": rho_ci,
            "verdict": ("SUPPORTED (no separation)" if auc_ci[0] <= 0.5 <= auc_ci[1] else
                        "REFUTED: losers paid MORE" if auc_ci[0] > 0.5 else "REFUTED: losers paid LESS"),
            "buckets": buckets,
            "median_premium_winners": round(st.median([r[1] for r in rows if r[2] > 0]), 1),
            "median_premium_losers": round(st.median([r[1] for r in rows if r[2] <= 0]), 1)}


def selftest():
    # Premium perfectly separates: losers paid 20-29, winners 0-9 -> AUC 1.0, CI excludes 0.5.
    rows = [("follow_activity", float(i), 10.0, 100.0) for i in range(10)] + \
           [("follow_activity", float(20 + i), -10.0, 100.0) for i in range(10)]
    out = analyse(rows)
    assert out["auc_loser_paid_more"] == 1.0 and out["verdict"] == "REFUTED: losers paid MORE", out
    assert out["rho"] < -0.8, out
    # Same premiums for both groups -> AUC 0.5 and the claim is supported.
    rows = [("follow_activity", float(i % 5), 10.0 if i % 2 else -10.0, 100.0) for i in range(40)]
    out = analyse(rows)
    assert out["auc_loser_paid_more"] == 0.5 and out["verdict"].startswith("SUPPORTED"), out
    assert [b["n"] for b in out["buckets"]] == [40, 0, 0, 0, 0], out
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    since, until = ts(a.since), ts(a.until)
    with contextlib.closing(sqlite3.connect(f"file:{LEDGER}?mode=ro", uri=True, timeout=10)) as c:
        raw = c.execute(
            "SELECT signal_source, fill_gap_pct, realized_pnl, entry_notional, opened_at FROM memecoin_positions "
            "WHERE execution_mode='real' AND network_id='solana' AND is_open=0 AND opened_at >= ?",
            (since.date().isoformat(),)).fetchall()
    rows = [r[:4] for r in raw if since <= ts(r[4]) < until and r[0] in COPY_LANES
            and r[1] is not None and r[2] is not None and (r[3] or 0) > 0]
    print(f"window [{since.isoformat()}, {until.isoformat()}); copy-lane closes with a premium: {len(rows)}")
    out = analyse(rows)
    for k, v in out.items():
        if k != "buckets":
            print(f"  {k}: {v}")
    for b in out["buckets"]:
        print(f"  bucket {b['bucket']:>7}: n={b['n']:<4} wins={b['wins']:<4} median_y={b['median_y_pct']} net=${b['net']}")
    for lane in COPY_LANES:
        sub = [r for r in rows if r[0] == lane]
        if len(sub) >= 15:
            o = analyse(sub)
            print(f"  lane {lane}: n={o['n']} wins={o['wins']} auc={o['auc_loser_paid_more']} ci={o['auc_ci95']} "
                  f"rho={o['rho']} ci={o['rho_ci95']} premium p50 winners={o['median_premium_winners']} "
                  f"losers={o['median_premium_losers']}")
        else:
            print(f"  lane {lane}: n={len(sub)} (descriptive only) wins={sum(1 for r in sub if r[2] > 0)} "
                  f"premium p50={round(st.median([r[1] for r in sub]), 1) if sub else None}")


if __name__ == "__main__":
    main()

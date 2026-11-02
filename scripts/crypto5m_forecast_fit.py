#!/usr/bin/env python3
"""Fit the scalper's forecasting model on captured data: predict the BOOK's next-few-seconds move from the
PRIOR (Holt trend/velocity = memory) + the live signals (momentum, microprice, order-flow imbalance), and
report the DATA-DRIVEN weights (Ridge coefficients) + the OOS predictive power vs the current equal-weight
voting fusion.

Read-only on the capture. Ridge (closed-form, numpy) with a time-split OOS test (fit older half, score newer).
The output coefficients are the reanalyzed signal weights; the OOS correlation says whether the model (with the
Holt prior) actually predicts the next move better than the hand-set fusion. Survivors → wire into the engine.
"""
from __future__ import annotations
import bisect, json, math, os, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sniper.algos.polymarket_crypto5m.trend_filter import holt_trend

CAP = os.path.join(ROOT, "data", "crypto5m_capture.sqlite")
STEP = 5
TARGET_K = 8          # predict the up-mid move over the next K seconds (the scalper's ride horizon)
VOL_LB = 60
FEATURES = ["holt_vel_z", "mom15_z", "micro_z", "imbalance"]


def _vol(prices):
    if len(prices) < 5:
        return None
    r = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    if len(r) < 4:
        return None
    mu = sum(r) / len(r)
    return math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1)) or None


def _imb(bids, asks):
    b = sum(s for _, s in bids[:3]); a = sum(s for _, s in asks[:3])
    return (b - a) / (b + a) if b + a > 0 else None


def _micro(bids, asks):
    if not bids or not asks:
        return (None, None)
    bp, bs = bids[0]; ap, asz = asks[0]
    mid = (bp + ap) / 2
    return (((bp * asz + ap * bs) / (bs + asz)) if bs + asz > 0 else None, mid, ap - bp)


def load(c, cid, tok, ws):
    bz = c.execute("SELECT source_event_ts t,price v FROM crypto5m_binance_observations WHERE source_event_ts>=? "
                   "AND source_event_ts<? AND price IS NOT NULL ORDER BY t", ((ws - VOL_LB) * 1000, (ws + 300) * 1000)).fetchall()
    binance = {int(r["t"]) // 1000: float(r["v"]) for r in bz}
    rows = c.execute("SELECT exchange_ts t,bids,asks FROM crypto5m_book_snapshots WHERE condition_id=? AND token_id=? "
                     "AND exchange_ts>=? AND exchange_ts<? ORDER BY exchange_ts", (cid, tok, ws * 1000, (ws + 300) * 1000)).fetchall()
    bt, bd = [], []
    for r in rows:
        try:
            bids = json.loads(r["bids"]); asks = json.loads(r["asks"])
            mp, mid, spr = _micro(bids, asks)
            if mid is None:
                continue
            bt.append(int(r["t"]) // 1000)
            bd.append({"mid": mid, "imb": _imb(bids, asks), "micro_gap": (mp - mid) if mp is not None else None, "spread": spr})
        except Exception:
            continue
    return binance, bt, bd


def _at(ts, data, t):
    i = bisect.bisect_right(ts, t) - 1
    return data[i] if i >= 0 else None


def run():
    import sqlite3
    c = sqlite3.connect(f"file:{CAP}?mode=ro&immutable=1", uri=True); c.row_factory = sqlite3.Row
    wins = c.execute("SELECT s.condition_id cid,m.window_start_ts ws,m.up_token_id up FROM crypto5m_settlements s "
                     "JOIN crypto5m_markets m ON m.condition_id=s.condition_id WHERE s.winning_outcome IS NOT NULL "
                     "ORDER BY m.window_start_ts").fetchall()
    X, y, wid = [], [], []
    for wi, w in enumerate(wins):
        binance, bt, bd = load(c, w["cid"], str(w["up"]), w["ws"])
        if len(binance) < 40 or len(bt) < 40:
            continue
        for t in range(w["ws"] + 30, w["ws"] + 300 - TARGET_K - 2, STEP):
            if t not in binance or (t - 15) not in binance:
                continue
            recent = [binance[s] for s in range(t - VOL_LB, t + 1) if s in binance]
            vol = _vol(recent)
            if not vol:
                continue
            _, hv = holt_trend([binance[s] for s in range(t - 30, t + 1) if s in binance])
            if hv is None:
                continue
            holt_vel_z = (hv / binance[t]) / vol                      # per-s return / σ
            mom15_z = math.log(binance[t] / binance[t - 15]) / (vol * math.sqrt(15))
            bk = _at(bt, bd, t); bk2 = _at(bt, bd, t + TARGET_K)
            if not bk or not bk2 or bk["imb"] is None or bk["micro_gap"] is None or not bk["spread"]:
                continue
            micro_z = bk["micro_gap"] / bk["spread"]
            target = bk2["mid"] - bk["mid"]                           # the book's next-K move (signed)
            X.append([holt_vel_z, mom15_z, micro_z, bk["imb"]]); y.append(target); wid.append(wi)
    c.close()
    X = np.array(X); y = np.array(y); wid = np.array(wid)
    n = len(y)
    # standardize features
    mu, sd = X.mean(0), X.std(0) + 1e-12
    Xs = (X - mu) / sd
    # time-split OOS
    cut = np.median(wid)
    tr, te = wid < cut, wid >= cut

    def ridge(Xtr, ytr, lam=1.0):
        A = Xtr.T @ Xtr + lam * np.eye(Xtr.shape[1])
        return np.linalg.solve(A, Xtr.T @ ytr)

    def corr(a, b):
        if len(a) < 10 or a.std() == 0 or b.std() == 0:
            return float("nan")
        return float(np.corrcoef(a, b)[0, 1])

    w_full = ridge(Xs, y)
    w_tr = ridge(Xs[tr], y[tr])
    pred_te = Xs[te] @ w_tr
    fusion = Xs.sum(1)                                                # current equal-weight voting proxy
    print(f"=== forecast fit: predict book's next-{TARGET_K}s move from PRIOR + signals (n={n}) ===")
    print("\nDATA-DRIVEN WEIGHTS (Ridge, standardized — magnitude = predictive importance, sign = direction):")
    for f, wv in sorted(zip(FEATURES, w_full), key=lambda x: -abs(x[1])):
        print(f"   {f:14} {wv:+.4f}")
    print(f"\nOOS predictive power (corr of prediction vs realized next-move, newer half, n_te={te.sum()}):")
    print(f"   fitted model (prior+signals): {corr(pred_te, y[te]):+.3f}")
    print(f"   equal-weight fusion (current): {corr(fusion[te], y[te]):+.3f}")
    print(f"   Holt velocity alone:           {corr(Xs[te, 0], y[te]):+.3f}")
    print(f"   momentum(15s) alone:           {corr(Xs[te, 1], y[te]):+.3f}")
    print(f"   microprice alone:              {corr(Xs[te, 2], y[te]):+.3f}")
    print(f"   imbalance alone:               {corr(Xs[te, 3], y[te]):+.3f}")
    print("\nread: if the fitted model's OOS corr >> equal-weight fusion, the data-driven weights (and the Holt "
          "prior) help — wire them. The per-feature corr shows which signals actually predict the next move.")
    print(f"(standardize stats for wiring: mean={list(np.round(mu,6))} sd={list(np.round(sd,6))})")


if __name__ == "__main__":
    run()

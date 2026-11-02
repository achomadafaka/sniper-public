#!/usr/bin/env python3
"""Walk-forward viability test: can we TRAIN a model on the captured millisecond stream that predicts the
scalper's next-few-seconds move better than the current equal-weight fusion — AND does the predicted edge
clear the cost of crossing the spread?

Settles three questions honestly:
  1. PERFECT-FORESIGHT TAKER CEILING — if we always pick the side that actually rises over the next H seconds
     and trade it as a taker (buy at the ask now, sell at the bid in H s), what's the net? This is the absolute
     economic ceiling of the taker strategy GIVEN THE SPREAD, independent of any model. If it's ~0/negative,
     no model can rescue the taker → the answer is maker-or-nothing.
  2. MODEL LIFT — does a gradient-boosted model (HistGBR) over real microstructure features (tape-derived
     signed order flow + BBO/Binance momentum + realized vol) beat Ridge and the equal-weight z-fusion on
     OUT-OF-SAMPLE directional correlation, under window-grouped, embargoed walk-forward CV?
  3. SELECTIVE TAKER EV — taking only the top-confidence model decisions, is the realized taker round-trip
     (bid_{t+H} − ask_t on the chosen side) positive after the spread?

Leakage controls: every feature at decision time t uses only data with ts ≤ t. CV groups by WINDOW (events
inside a window are autocorrelated → the unit of independence is the window, ~183 of them, not the millions of
events) with a 1-window embargo between train and test. Read-only on a snapshot of the live capture.
"""
from __future__ import annotations
import bisect, json, math, os, sys, statistics
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

CAP = sys.argv[1] if len(sys.argv) > 1 else "/tmp/cap_snap.sqlite"
GRID_S = 2                 # decision cadence (matches the live 2s poll)
HORIZON_S = 8              # the scalper's ride horizon — predict the move over the next H seconds
WARMUP_S = 30             # skip the first 30s (book settling) and last H+2s of each window
VOL_LB = 60               # realized-vol lookback (s)
EMBARGO_WINDOWS = 1       # drop the window adjacent to each train/test boundary

FEATURES = [
    "mom5_z", "mom15_z", "mom30_z",          # Binance spot momentum (z vs realized vol)
    "bookmom5", "bookmom15",                 # Up-mid momentum (cents)
    "ofi10", "ofi30",                        # signed tape order-flow imbalance (buy-sell)/total
    "tvol10_z", "tintensity10",              # signed tape volume (z), trade intensity (trades/s)
    "rvol30", "spread", "secs_left",         # realized micro-vol, current spread, time left
]


def _f(x, d=None):
    try:
        return float(x)
    except Exception:
        return d


def load_window(c, cid, up_tok, ws):
    """Reconstruct the Up-token BBO path, the signed trade tape, and Binance spot for one 5-min window."""
    # BBO path (ms) for the Up token
    bbo_t, bid, ask = [], [], []
    for r in c.execute("SELECT exchange_ts, raw_payload FROM crypto5m_book_events WHERE condition_id=? "
                       "AND event_type='best_bid_ask' AND side='Up' ORDER BY exchange_ts", (cid,)):
        try:
            p = json.loads(r["raw_payload"]); b = _f(p.get("best_bid")); a = _f(p.get("best_ask"))
            if b is None or a is None or a <= b:
                continue
            bbo_t.append(int(r["exchange_ts"]) / 1000.0); bid.append(b); ask.append(a)
        except Exception:
            continue
    # signed trade tape (ms) for the Up token: +size on BUY (lifts ask), -size on SELL (hits bid)
    tr_t, tr_sgn = [], []
    for r in c.execute("SELECT exchange_ts, raw_payload FROM crypto5m_book_events WHERE condition_id=? "
                       "AND event_type='last_trade_price' AND side='Up' ORDER BY exchange_ts", (cid,)):
        try:
            p = json.loads(r["raw_payload"]); sz = _f(p.get("size"), 0.0)
            sgn = sz if str(p.get("side")).upper() == "BUY" else -sz
            tr_t.append(int(r["exchange_ts"]) / 1000.0); tr_sgn.append(sgn)
        except Exception:
            continue
    # Binance spot (s)
    binance = {}
    for r in c.execute("SELECT source_event_ts t, price v FROM crypto5m_binance_observations WHERE "
                       "source_event_ts>=? AND source_event_ts<? AND price IS NOT NULL ORDER BY t",
                       ((ws - VOL_LB) * 1000, (ws + 300) * 1000)):
        binance[int(r["t"]) // 1000] = float(r["v"])
    return (bbo_t, bid, ask, tr_t, tr_sgn, binance)


def _mid_at(bbo_t, bid, ask, t):
    i = bisect.bisect_right(bbo_t, t) - 1
    if i < 0:
        return (None, None, None)
    return ((bid[i] + ask[i]) / 2.0, bid[i], ask[i])


def _rvol(binance, t):
    px = [binance[s] for s in range(t - VOL_LB, t + 1) if s in binance]
    if len(px) < 6:
        return None
    r = [math.log(b / a) for a, b in zip(px, px[1:]) if a > 0 and b > 0]
    if len(r) < 4:
        return None
    mu = sum(r) / len(r)
    return math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1)) or None


def build():
    import sqlite3
    c = sqlite3.connect(CAP); c.row_factory = sqlite3.Row
    wins = c.execute("SELECT condition_id cid, window_start_ts ws, up_token_id up FROM crypto5m_markets "
                     "WHERE up_token_id IS NOT NULL ORDER BY window_start_ts").fetchall()
    X, y, spread_at, taker_rt, wid, true_dir = [], [], [], [], [], []
    used = 0
    for wi, w in enumerate(wins):
        cid, ws, up = w["cid"], int(w["ws"]), str(w["up"])
        bbo_t, bid, ask, tr_t, tr_sgn, binance = load_window(c, cid, up, ws)
        if len(bbo_t) < 200 or len(binance) < 40:
            continue
        used += 1
        for t in range(ws + WARMUP_S, ws + 300 - HORIZON_S - 2, GRID_S):
            if t not in binance or (t - 30) not in binance:
                continue
            vol = _rvol(binance, t)
            if not vol:
                continue
            mid_t, bid_t, ask_t = _mid_at(bbo_t, bid, ask, t)
            mid_h, bid_h, ask_h = _mid_at(bbo_t, bid, ask, t + HORIZON_S)
            mid_5, _, _ = _mid_at(bbo_t, bid, ask, t - 5)
            mid_15, _, _ = _mid_at(bbo_t, bid, ask, t - 15)
            if None in (mid_t, mid_h, mid_5, mid_15, bid_t, ask_t, bid_h):
                continue
            # spot momentum z
            def mom_z(h):
                if (t - h) not in binance:
                    return 0.0
                return math.log(binance[t] / binance[t - h]) / (vol * math.sqrt(h))
            # tape order flow over the last L seconds (ts ≤ t only)
            def flow(L):
                lo = bisect.bisect_left(tr_t, t - L); hi = bisect.bisect_right(tr_t, t)
                seg = tr_sgn[lo:hi]
                if not seg:
                    return (0.0, 0.0, 0)
                signed = sum(seg); gross = sum(abs(s) for s in seg)
                return (signed / gross if gross > 0 else 0.0, signed, hi - lo)
            ofi10, tvol10, ntr10 = flow(10)
            ofi30, _, _ = flow(30)
            tvol10_z = tvol10 / 500.0      # rough scale; standardized again below
            # realized micro-vol of the book mid over last 30s
            mids = [_mid_at(bbo_t, bid, ask, s)[0] for s in range(t - 30, t + 1, 2)]
            mids = [m for m in mids if m is not None]
            rvol30 = statistics.pstdev(mids) if len(mids) >= 5 else 0.0
            feat = [
                mom_z(5), mom_z(15), mom_z(30),
                (mid_t - mid_5), (mid_t - mid_15),
                ofi10, ofi30,
                tvol10_z, ntr10 / 10.0,
                rvol30, (ask_t - bid_t), (ws + 300 - t) / 300.0,
            ]
            if any((f is None or not math.isfinite(f)) for f in feat):
                continue
            move = mid_h - mid_t                     # signed Up-mid move over next H (the prediction target)
            X.append(feat); y.append(move); spread_at.append(ask_t - bid_t); wid.append(wi)
            true_dir.append(1 if move > 0 else (-1 if move < 0 else 0))
            # perfect-foresight & model taker round-trip is computed later from bid/ask at t and t+H:
            taker_rt.append((bid_t, ask_t, bid_h, ask_h))      # store the touch prices for the economic sim
    c.close()
    return (np.array(X), np.array(y), np.array(spread_at), taker_rt, np.array(wid), np.array(true_dir), used, len(wins))


def main():
    from sklearn.ensemble import HistGradientBoostingRegressor
    X, y, spr, touch, wid, tdir, used, total = build()
    n = len(y)
    print(f"=== crypto5m walk-forward viability (H={HORIZON_S}s, grid={GRID_S}s) ===")
    print(f"windows with usable book: {used}/{total} | decision points: {n} | median spread: {np.median(spr)*100:.2f}c")
    if n < 300:
        print("Too few decision points — capture more before trusting this."); return

    uw = np.unique(wid)
    # ---- 1. PERFECT-FORESIGHT TAKER CEILING (no model; pick the side that actually rises) ----
    # long Up if move>0 (enter ask_up_t, exit bid_up_{t+H}); else long Down. Down touch ≈ 1 - Up touch (binary).
    pf = []
    for (bid_t, ask_t, bid_h, ask_h), m in zip(touch, y):
        if m > 0:                                   # buy Up at ask, sell at bid in H
            pf.append(bid_h - ask_t)
        elif m < 0:                                 # buy Down: Down_ask ≈ 1-Up_bid, Down_bid ≈ 1-Up_ask
            pf.append((1 - ask_h) - (1 - bid_t))
        else:
            pf.append(0.0)
    pf = np.array(pf)
    print(f"\n[1] PERFECT-FORESIGHT TAKER CEILING (knows the next-{HORIZON_S}s direction, crosses the spread):")
    print(f"    mean net = {pf.mean()*100:+.3f}c/trade   median = {np.median(pf)*100:+.3f}c   %positive = {(pf>0).mean()*100:.0f}%")
    print(f"    -> if this is ≤0, NO model rescues the taker (spread eats the move). If >0, model quality is the lever.")

    # ---- 2. MODEL LIFT under window-grouped, embargoed walk-forward CV ----
    mu, sd = X.mean(0), X.std(0) + 1e-12
    Xs = (X - mu) / sd
    folds, k = [], max(3, len(uw) // 6)
    order = uw  # windows already time-ordered by wid index
    # expanding-window folds over the ordered window ids
    step = max(1, len(order) // 5)
    for start in range(step, len(order), step):
        tr_w = set(order[:start - EMBARGO_WINDOWS]) if start - EMBARGO_WINDOWS > 0 else set(order[:start])
        te_w = set(order[start:start + step])
        if not tr_w or not te_w:
            continue
        folds.append((np.isin(wid, list(tr_w)), np.isin(wid, list(te_w))))

    def ridge(Xtr, ytr, lam=1.0):
        A = Xtr.T @ Xtr + lam * np.eye(Xtr.shape[1])
        return np.linalg.solve(A, Xtr.T @ ytr)

    preds = {"fusion": [], "ridge": [], "gbr": []}
    reals = []
    for tr, te in folds:
        if tr.sum() < 200 or te.sum() < 50:
            continue
        reals.append(y[te])
        preds["fusion"].append(Xs[te][:, :3].sum(1) + Xs[te][:, 3:7].sum(1))  # z-sum proxy of the engine's vote
        wr = ridge(Xs[tr], y[tr]); preds["ridge"].append(Xs[te] @ wr)
        g = HistGradientBoostingRegressor(max_depth=3, max_iter=200, learning_rate=0.05,
                                          min_samples_leaf=40, l2_regularization=1.0)
        g.fit(Xs[tr], y[tr]); preds["gbr"].append(g.predict(Xs[te]))
    if not reals:
        print("no valid folds"); return
    R = np.concatenate(reals)

    def corr(a, b):
        a = np.asarray(a); b = np.asarray(b)
        if a.std() == 0 or b.std() == 0:
            return float("nan")
        return float(np.corrcoef(a, b)[0, 1])

    print(f"\n[2] MODEL LIFT — OOS directional corr (walk-forward, {len(reals)} folds, window-grouped + embargo):")
    model_oos = {}
    for name in ("fusion", "ridge", "gbr"):
        P = np.concatenate(preds[name]); model_oos[name] = P
        print(f"    {name:8} corr(pred, realized next-move) = {corr(P, R):+.3f}")
    print("    -> GBR >> fusion means the data-driven nonlinear model genuinely helps. ~tie means fusion is enough.")

    # ---- 3. SELECTIVE TAKER EV — take only the model's top-confidence decisions ----
    # rebuild touch aligned to the concatenated test order
    te_touch, te_pf = [], []
    for tr, te in folds:
        if tr.sum() < 200 or te.sum() < 50:
            continue
        idx = np.where(te)[0]
        te_touch.extend([touch[i] for i in idx]); te_pf.extend([pf[i] for i in idx])
    te_pf = np.array(te_pf)
    gbr = model_oos["gbr"]
    print(f"\n[3] SELECTIVE TAKER EV — act only on the model's most confident calls (GBR):")
    for q in (0.5, 0.8, 0.9, 0.95):
        thr = np.quantile(np.abs(gbr), q)
        sel = np.abs(gbr) >= thr
        if sel.sum() < 20:
            continue
        # taker round-trip following the MODEL's predicted side
        rt = []
        for take, g, (bid_t, ask_t, bid_h, ask_h) in zip(sel, gbr, te_touch):
            if not take:
                continue
            if g > 0:
                rt.append(bid_h - ask_t)
            else:
                rt.append((1 - ask_h) - (1 - bid_t))
        rt = np.array(rt)
        print(f"    top {int((1-q)*100):2d}% conf (n={sel.sum():4d}): taker net = {rt.mean()*100:+.3f}c/trade  "
              f"%pos={(rt>0).mean()*100:.0f}%  | dir-hit={( (gbr[sel]>0)==(R[sel]>0) ).mean()*100:.0f}%")
    print("\nread: [1] is the ceiling. [2] says whether a model beats the current fusion. [3] says whether the "
          "BEST decisions clear the spread as a TAKER. If [1]≤0 or [3]≤0 everywhere, the edge is maker-side, not model-side.")


if __name__ == "__main__":
    main()

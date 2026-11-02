#!/usr/bin/env python3
"""Conditional-edge scanner for the Polymarket BTC 5m binary.

The question (NOT average calibration — that's the book's, and it wins): conditional on a signal firing, does
buying the favored side at the EXECUTABLE price settle +EV? Tests 5 theses (mean-reversion-near-the-money,
odds-overshoot, settlement-lead, vol-conditioned-distance, microprice-lead), each over a small param sweep,
plus coherent AND-combinations. Reports the full landscape with n, WR, net edge (fee0 / fee-documented),
window-clustered bootstrap 95% CI, and an OOS-hold tag (older-half vs newer-half sign agreement).

Read-only on the capture. Survivors go to FORWARD PAPER, not live. n is small (~100 windows) — most rows are
noise; trust the CI + OOS tag, not the top row. See docs/superpowers/specs/2026-06-21-crypto5m-conditional-
edge-scanner-design.md.
"""
from __future__ import annotations
import bisect, json, math, os, sqlite3, statistics as st, sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAP = os.path.join(ROOT, "data", "crypto5m_capture.sqlite")
STEP = 5            # decision grid (seconds)
MIN_LEFT = 25       # need >= this seconds left at a decision
VOL_LB = 120        # vol lookback (s)
HORIZON_K = 30      # "horizon" exit: sell K seconds after entry (vs settle)
FEE_DOC = 0.07
SEED = 12345


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _fee(p, rate):
    return rate * p * (1 - p)


def _vol(prices):
    if len(prices) < 5:
        return None
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a > 0 and b > 0]
    if len(rets) < 4:
        return None
    mu = sum(rets) / len(rets)
    return math.sqrt(sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) or None


def _imb_micro(bids, asks):
    """top-of-book imbalance (top 3) + microprice (top 1), from [[price,size]] arrays (best first)."""
    b3 = sum(s for _, s in bids[:3]); a3 = sum(s for _, s in asks[:3])
    imb = (b3 - a3) / (b3 + a3) if b3 + a3 > 0 else None
    micro = None
    if bids and asks and (bids[0][1] + asks[0][1]) > 0:
        bp, bs = bids[0]; ap, asz = asks[0]
        micro = (bp * asz + ap * bs) / (bs + asz)
    return imb, micro


def load_window(c, cid, ws, up_tok, down_tok):
    bz = c.execute("SELECT source_event_ts t, price v FROM crypto5m_binance_observations "
                   "WHERE source_event_ts>=? AND source_event_ts<? AND price IS NOT NULL ORDER BY t",
                   ((ws - VOL_LB) * 1000, (ws + 300) * 1000)).fetchall()
    binance = {int(r["t"]) // 1000: float(r["v"]) for r in bz}
    if len(binance) < 40:
        return None

    def book(tok):
        rows = c.execute("SELECT exchange_ts t, best_bid, best_ask, bids, asks FROM crypto5m_book_snapshots "
                         "WHERE condition_id=? AND token_id=? AND exchange_ts>=? AND exchange_ts<? ORDER BY exchange_ts",
                         (cid, tok, ws * 1000, (ws + 300) * 1000)).fetchall()
        times, data = [], []
        for r in rows:
            try:
                bb = float(r["best_bid"]); ba = float(r["best_ask"])
                if not (0 < bb < 1 and 0 < ba < 1):
                    continue
                imb, micro = _imb_micro(json.loads(r["bids"]), json.loads(r["asks"]))
                times.append(int(r["t"]) // 1000)
                data.append({"bid": bb, "ask": ba, "mid": (bb + ba) / 2, "imb": imb, "micro": micro})
            except Exception:
                continue
        return times, data

    ut, ud = book(up_tok); dt, dd = book(down_tok)
    if not ut or not dt:
        return None
    start = binance[min(binance)]
    return {"binance": binance, "ut": ut, "ud": ud, "dt": dt, "dd": dd, "ws": ws, "start": start}


def _at(times, data, t):
    i = bisect.bisect_right(times, t) - 1
    return data[i] if i >= 0 else None


def features_at(win, t, up_mid_hist):
    ws, start = win["ws"], win["start"]
    binance = win["binance"]
    if t not in binance:
        return None
    left = 300 - (t - ws)
    if left < MIN_LEFT or (t - ws) < 30:
        return None
    btc = binance[t]
    vol = _vol([binance[s] for s in range(t - VOL_LB, t + 1) if s in binance])
    if not vol:
        return None
    log_ret = math.log(btc / start) if start > 0 else 0.0
    diff_z = log_ret / (vol * math.sqrt(left))
    up = _at(win["ut"], win["ud"], t); dn = _at(win["dt"], win["dd"], t)
    if not up or not dn:
        return None
    odds = up["mid"]                                       # market P(Up)
    ret = {}
    for h in (15, 30, 60):
        p0 = binance.get(t - h)
        ret[h] = (math.log(btc / p0) if (p0 and p0 > 0) else 0.0)
    srz = ret[30] / (vol * math.sqrt(30)) if vol else 0.0  # 30s move in sigma (overextension)
    dch = (odds - up_mid_hist.get(t - 30)) if up_mid_hist.get(t - 30) is not None else None  # odds move 30s
    return {"t": t, "left": left, "odds": odds, "diff_p": _phi(diff_z), "srz": srz,
            "ret30": ret[30], "ret60": ret[60], "vol": vol, "odds_change30": dch,
            "up_ask": up["ask"], "down_ask": dn["ask"], "up_bid": up["bid"], "down_bid": dn["bid"],
            "imb": up["imb"], "micro": up["micro"]}


# ---- signal library: each returns ('Up'|'Down') to BUY, or None -------------------------------
def sig_meanrev(f, z, band):
    if abs(f["odds"] - 0.5) >= band or abs(f["srz"]) < z:
        return None
    return "Down" if f["ret30"] > 0 else "Up"            # fade the recent move (reversion)


def sig_overshoot(f, m):
    d = f["odds_change30"]
    if d is None or abs(d) < m:
        return None
    return "Down" if d > 0 else "Up"                      # fade the odds move


def sig_settlead(f, L, r):
    if f["left"] > L or abs(f["ret30"]) < r:
        return None
    return "Up" if f["ret30"] > 0 else "Down"            # ride the late Binance move


def sig_volcond(f, e):
    edge = f["diff_p"] - f["odds"]
    if abs(edge) < e:
        return None
    return "Up" if edge > 0 else "Down"                   # our diffusion vs the odds


def sig_microprice(f, d):
    if f["micro"] is None:
        return None
    diff = f["micro"] - f["odds"]
    if abs(diff) < d:
        return None
    return "Up" if diff > 0 else "Down"                   # book microprice pressure


SIGNALS = {
    "meanrev_nearmoney": (sig_meanrev, [{"z": z, "band": b} for z in (1.0, 1.5) for b in (0.12, 0.20)]),
    "odds_overshoot":    (sig_overshoot, [{"m": m} for m in (0.04, 0.08)]),
    "settlement_lead":   (sig_settlead, [{"L": L, "r": r} for L in (60, 90) for r in (0.0005, 0.001)]),
    "vol_cond_distance": (sig_volcond, [{"e": e} for e in (0.06, 0.10)]),
    "microprice_lead":   (sig_microprice, [{"d": d} for d in (0.01, 0.02)]),
}
COMBOS = [("meanrev_nearmoney", {"z": 1.0, "band": 0.20}, "odds_overshoot", {"m": 0.04}),
          ("settlement_lead", {"L": 90, "r": 0.0005}, "microprice_lead", {"d": 0.01})]


def _won(side, up_won):
    return 1 if ((side == "Up") == up_won) else 0


def _exec_and_settle(win, t, side, up_won):
    f_up_ask = None
    up = _at(win["ut"], win["ud"], t); dn = _at(win["dt"], win["dd"], t)
    ask = (up["ask"] if side == "Up" else dn["ask"]) if (up and dn) else None
    if ask is None or not (0.03 < ask < 0.97):
        return None
    y = _won(side, up_won)
    # horizon exit: sell into our side's bid K seconds later (or settle if past window)
    hb = _at(win["ut"], win["ud"], t + HORIZON_K) if side == "Up" else _at(win["dt"], win["dd"], t + HORIZON_K)
    exit_px = (hb["bid"] if (hb and (t + HORIZON_K) < win["ws"] + 300) else float(y))
    return {"ask": ask, "settle_y": y, "horizon_exit": exit_px}


def collect(windows):
    """Walk every window's decision grid; for each (signal,params) and combo, collect fired trades."""
    cells = defaultdict(list)   # key -> list of trade dicts (with window index for clustering)
    for wi, win in enumerate(windows):
        up_won = win["up_won"]
        up_mid_hist = {}
        # precompute up_mid at every second for odds-change lookups
        for t in range(win["ws"], win["ws"] + 300):
            u = _at(win["ut"], win["ud"], t)
            if u:
                up_mid_hist[t] = u["mid"]
        for t in range(win["ws"] + 30, win["ws"] + 300 - MIN_LEFT, STEP):
            f = features_at(win, t, up_mid_hist)
            if not f:
                continue
            fired = {}
            for name, (fn, grid) in SIGNALS.items():
                for params in grid:
                    side = fn(f, **params)
                    if side:
                        fired[(name, tuple(sorted(params.items())))] = side
            # individual signals
            for key, side in fired.items():
                res = _exec_and_settle(win, t, side, up_won)
                if res:
                    cells[key].append({**res, "w": wi})
            # combinations (both fire AND agree on side)
            for n1, p1, n2, p2 in COMBOS:
                k1 = (n1, tuple(sorted(p1.items()))); k2 = (n2, tuple(sorted(p2.items())))
                if k1 in fired and k2 in fired and fired[k1] == fired[k2]:
                    res = _exec_and_settle(win, t, fired[k1], up_won)
                    if res:
                        cells[(f"COMBO:{n1}+{n2}", ())].append({**res, "w": wi})
    return cells


def _edge(trades, exit_kind, fee_rate):
    """mean per-trade pnl for an exit ('settle'|'horizon')."""
    out = []
    for tr in trades:
        if exit_kind == "settle":
            out.append(tr["settle_y"] - tr["ask"] - _fee(tr["ask"], fee_rate))
        else:
            out.append(tr["horizon_exit"] - tr["ask"] - _fee(tr["ask"], fee_rate))
    return st.mean(out) if out else None


def _boot_ci(trades, exit_kind, rng):
    """window-clustered bootstrap 95% CI of net edge (fee0)."""
    by_w = defaultdict(list)
    for tr in trades:
        by_w[tr["w"]].append(tr)
    ws = list(by_w)
    if len(ws) < 3:
        return (None, None)
    means = []
    for _ in range(800):
        samp = []
        for _ in range(len(ws)):
            samp += by_w[ws[rng.randrange(len(ws))]]
        m = _edge(samp, exit_kind, 0.0)
        if m is not None:
            means.append(m)
    means.sort()
    return (means[int(0.025 * len(means))], means[int(0.975 * len(means))])


def _oos(trades, exit_kind, median_w):
    older = [t for t in trades if t["w"] < median_w]
    newer = [t for t in trades if t["w"] >= median_w]
    eo, en = _edge(older, exit_kind, 0.0), _edge(newer, exit_kind, 0.0)
    if eo is None or en is None or len(older) < 8 or len(newer) < 8:
        return "—"
    return "hold" if (eo > 0) == (en > 0) else "flip"


def run():
    import random
    rng = random.Random(SEED)
    c = sqlite3.connect(f"file:{CAP}?mode=ro&immutable=1", uri=True); c.row_factory = sqlite3.Row
    wins = c.execute("SELECT s.condition_id cid, m.window_start_ts ws, m.up_token_id up, m.down_token_id dn, "
                     "s.winning_outcome wout FROM crypto5m_settlements s "
                     "JOIN crypto5m_markets m ON m.condition_id=s.condition_id "
                     "WHERE s.winning_outcome IS NOT NULL ORDER BY m.window_start_ts").fetchall()
    windows = []
    for w in wins:
        win = load_window(c, w["cid"], w["ws"], str(w["up"]), str(w["dn"]))
        if win:
            win["up_won"] = (w["wout"] or "").strip().lower().startswith("up")
            windows.append(win)
    c.close()
    median_w = len(windows) // 2
    cells = collect(windows)

    rows = []
    for key, trades in cells.items():
        name, params = key
        n = len(trades)
        if n < 8:
            continue
        wr = st.mean(tr["settle_y"] for tr in trades)
        for exit_kind in ("settle", "horizon"):
            e0 = _edge(trades, exit_kind, 0.0)
            ed = _edge(trades, exit_kind, FEE_DOC)
            lo, hi = _boot_ci(trades, exit_kind, rng)
            oos = _oos(trades, exit_kind, median_w)
            pstr = " ".join(f"{k}={v}" for k, v in params) if params else ""
            rows.append((e0, {"sig": name, "params": pstr, "exit": exit_kind, "n": n, "wr": wr,
                              "e0": e0, "ed": ed, "ci": (lo, hi), "oos": oos}))
    rows.sort(key=lambda x: (x[0] if x[0] is not None else -9), reverse=True)

    print(f"=== conditional-edge scan over {len(windows)} windows "
          f"(EV per $1 share; +ve = signal beats the price; CI=window-bootstrap fee0) ===")
    print(f"{'signal':22}{'params':20}{'exit':8}{'n':>5}{'WR':>6}{'edge¢ fee0':>11}{'edge¢ feeDoc':>13}"
          f"{'  95% CI ¢':>16}{'  OOS':>6}")
    for _, r in rows:
        lo, hi = r["ci"]
        ci = f"[{100*lo:+.1f},{100*hi:+.1f}]" if lo is not None else "—"
        star = " *" if (lo is not None and lo > 0) else ""   # CI excludes zero (fee0)
        print(f"{r['sig'][:21]:22}{r['params'][:19]:20}{r['exit']:8}{r['n']:>5}{r['wr']:>6.0%}"
              f"{100*r['e0']:>11.2f}{100*r['ed']:>13.2f}{ci:>16}{r['oos']:>6}{star}")
    print("\n¢ = cents per $1 share traded. '*' = bootstrap 95% CI (fee0) excludes zero. OOS hold = older/newer "
          "half agree on sign. Small n — treat as candidates for FORWARD PAPER, not live.")


if __name__ == "__main__":
    run()

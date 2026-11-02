#!/usr/bin/env python3
"""Compare ENTRY-memory and EXIT rules for the crypto5m scalper over captured windows — gap-faithfully.

Two questions, both answered with numbers instead of vibes:
  EXIT  — the tight price-stop bails into the 5-12c/poll gap (95% of stops recover, median +26c). Replace it?
  ENTRY — the read is memoryless; it commits on micro-wiggles. How should it tell a real move from noise?

Entry modes (how a direction is CONFIRMED before committing):
  INSTANT   single-tick: |mom15_z|>=1 AND tape order flow agrees                 [today's engine, ~memoryless]
  PERSIST   memory: mom15 sign STABLE for K grids AND short+long horizon agree AND order flow agrees
  SMOOTH    Holt velocity (a level+velocity state w/ memory) sustained K grids AND order flow agrees
Exit rules:
  CURRENT   take-profit/trailing + TIGHT price-stop (gain <= -max(1.5c, 3*spread))               [today]
  PATIENCE  take-profit/trailing + stop only if adverse PERSISTS N sec (gap-immune) + persistent flip + cap

Fidelity: 2s grid (live poll); bid/ask from the ms best_bid_ask stream at each grid point, so inter-poll GAPS
are present (what makes the stop comparison honest). Fills deferred one grid (poll latency). Held positions
settle at the window outcome. Touch-optimistic intra-tick — RANKS rules, doesn't prove absolute PnL. Read-only.
"""
from __future__ import annotations
import bisect, json, math, os, statistics, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sniper.algos.polymarket_crypto5m.trend_filter import holt_trend

CAP = sys.argv[1] if len(sys.argv) > 1 else "/tmp/cap_snap.sqlite"
GRID_S = 2
WARMUP_S = 30
STAKE = 10.0
VOL_LB = 60
MIN_PX, MAX_PX = 0.08, 0.92
# entry
ENTER_MOM_Z = 1.0
ENTER_OFI = 0.15
HOLT_BAND = 1.0             # SMOOTH: |holt velocity z| to call a move
PERSIST_K = 3              # PERSIST/SMOOTH: this many consecutive grids (×2s) the sign must hold (memory)
# common exit
TP_LOCK = 0.90
TRAIL = 0.04
# CURRENT exit
TIGHT_STOP_FLOOR, TIGHT_STOP_SPREAD = 0.015, 3.0
# PATIENCE exit
CATASTROPHE = 0.25
PATIENCE_STOP = 0.10
PATIENCE_S = 12
FLIP_PERSIST_S = 6


def _vol(px):
    if len(px) < 6:
        return None
    r = [math.log(b / a) for a, b in zip(px, px[1:]) if a > 0 and b > 0]
    if len(r) < 4:
        return None
    mu = sum(r) / len(r)
    return math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1)) or None


def _bbo(conn, cid, side):
    ts, bid, ask = [], [], []
    for r in conn.execute("SELECT exchange_ts, raw_payload FROM crypto5m_book_events WHERE condition_id=? "
                          "AND event_type='best_bid_ask' AND side=? ORDER BY exchange_ts", (cid, side)):
        try:
            p = json.loads(r["raw_payload"]); b = float(p.get("best_bid")); a = float(p.get("best_ask"))
            if a > b:
                ts.append(int(r["exchange_ts"]) / 1000.0); bid.append(b); ask.append(a)
        except Exception:
            continue
    return ts, bid, ask


def _tape(conn, cid):
    ts, sgn = [], []
    for r in conn.execute("SELECT exchange_ts, raw_payload FROM crypto5m_book_events WHERE condition_id=? "
                          "AND event_type='last_trade_price' AND side='Up' ORDER BY exchange_ts", (cid,)):
        try:
            p = json.loads(r["raw_payload"]); s = float(p.get("size") or 0)
            ts.append(int(r["exchange_ts"]) / 1000.0); sgn.append(s if str(p.get("side")).upper() == "BUY" else -s)
        except Exception:
            continue
    return ts, sgn


def _at(ts, arr, t):
    i = bisect.bisect_right(ts, t) - 1
    return arr[i] if i >= 0 else None


def _ofi(tt, ts, t, L=10):
    lo = bisect.bisect_left(tt, t - L); hi = bisect.bisect_right(tt, t)
    seg = ts[lo:hi]; g = sum(abs(x) for x in seg)
    return (sum(seg) / g) if g > 0 else 0.0


def grid_features(binance, tt, tsgn, ws):
    """Per-grid reads: t -> {mom8,mom15,mom30 (z), holt (vel z), ofi, vol} or None."""
    feats = {}
    for t in range(ws + WARMUP_S, ws + 300, GRID_S):
        if t not in binance or (t - 30) not in binance:
            feats[t] = None; continue
        px = [binance[s] for s in range(t - VOL_LB, t + 1) if s in binance]
        vol = _vol(px)
        if not vol:
            feats[t] = None; continue

        def momz(h):
            return math.log(binance[t] / binance[t - h]) / (vol * math.sqrt(h)) if (t - h) in binance else 0.0
        hp = [binance[s] for s in range(t - 30, t + 1) if s in binance]
        _, hv = holt_trend(hp)
        feats[t] = {"mom8": momz(8), "mom15": momz(15), "mom30": momz(30),
                    "holt": ((hv / binance[t]) / vol) if hv is not None else 0.0,
                    "ofi": _ofi(tt, tsgn, t), "vol": vol}
    return feats


def _stable(feats, grids, t, key, sg):
    hist = [feats[g][key] for g in grids if t - PERSIST_K * GRID_S < g <= t and feats.get(g)]
    return len(hist) >= PERSIST_K and all((h > 0) == (sg > 0) for h in hist)


def entry_side(feats, grids, t, mode):
    f = feats.get(t)
    if not f:
        return None
    ofi = f["ofi"]
    if mode == "INSTANT":
        if abs(f["mom15"]) >= ENTER_MOM_Z and abs(ofi) >= ENTER_OFI and (f["mom15"] > 0) == (ofi > 0):
            return "Up" if f["mom15"] > 0 else "Down"
        return None
    if mode == "PERSIST":
        sg = 1 if f["mom15"] > 0 else -1
        if abs(f["mom15"]) < ENTER_MOM_Z or abs(ofi) < ENTER_OFI or (ofi > 0) != (sg > 0):
            return None
        if (f["mom8"] > 0) != (sg > 0) or (f["mom30"] > 0) != (sg > 0):     # multi-horizon trend
            return None
        if not _stable(feats, grids, t, "mom15", sg):                       # memory
            return None
        return "Up" if sg > 0 else "Down"
    if mode == "SMOOTH":
        sg = 1 if f["holt"] > 0 else -1
        if abs(f["holt"]) < HOLT_BAND or abs(ofi) < ENTER_OFI or (ofi > 0) != (sg > 0):
            return None
        if not _stable(feats, grids, t, "holt", sg):
            return None
        return "Up" if sg > 0 else "Down"
    return None


def simulate(window, feats, grids, entry_mode, exit_rule):
    binance, paths, tt, tsgn, ws, outcome = window
    trades, pos, pending = [], None, None
    flip_since, adverse_since = None, None
    for t in grids:
        f = feats.get(t)
        side_now = (("Up" if f["mom15"] > 0 else "Down") if f else None)
        conv = (min(1.0, abs(f["mom15"]) / 3.0) if f else 0.0)
        if pending and pos is None:
            tsd, bd, ad = paths[pending]
            ask = _at(tsd, ad, t)
            if ask is not None and MIN_PX < ask < MAX_PX:
                pos = {"side": pending, "entry": ask, "peak": None}
            pending = None; flip_since = adverse_since = None
        if pos is not None:
            tsd, bd, ad = paths[pos["side"]]
            bid = _at(tsd, bd, t); ask = _at(tsd, ad, t)
            if bid is not None and 0 < bid < 1:
                pos["peak"] = bid if pos["peak"] is None else max(pos["peak"], bid)
                gain = bid - pos["entry"]; peak_gain = pos["peak"] - pos["entry"]
                spread = (ask - bid) if (ask is not None and ask > bid) else 0.01
                opp = side_now is not None and side_now != pos["side"]
                flip_since = (flip_since or t) if opp else None
                adverse_since = (adverse_since or t) if gain <= -PATIENCE_STOP else None
                exit_px = None
                if bid >= TP_LOCK:
                    exit_px = bid
                elif peak_gain > 0 and gain <= peak_gain - TRAIL:
                    exit_px = bid
                elif exit_rule == "CURRENT":
                    if gain <= -max(TIGHT_STOP_FLOOR, TIGHT_STOP_SPREAD * spread):
                        exit_px = bid
                elif exit_rule == "PATIENCE":
                    if adverse_since is not None and (t - adverse_since) >= PATIENCE_S:
                        exit_px = bid
                    elif flip_since is not None and (t - flip_since) >= FLIP_PERSIST_S and conv >= 0.3:
                        exit_px = bid
                    elif gain <= -CATASTROPHE:
                        exit_px = bid
                if exit_px is not None:
                    trades.append(round(STAKE / pos["entry"] * exit_px - STAKE, 4))
                    pos = None; flip_since = adverse_since = None
                    continue
        if pos is None and pending is None:
            side = entry_side(feats, grids, t, entry_mode)
            if side is not None:
                pending = side
    if pos is not None:
        win = outcome == pos["side"]
        trades.append(round(STAKE / pos["entry"] * (1.0 if win else 0.0) - STAKE, 4))
    return trades


def load(conn):
    wins = conn.execute("SELECT m.condition_id cid, m.window_start_ts ws, s.winning_outcome o "
                        "FROM crypto5m_markets m JOIN crypto5m_settlements s ON s.condition_id=m.condition_id "
                        "WHERE s.winning_outcome IS NOT NULL ORDER BY m.window_start_ts").fetchall()
    out = []
    for w in wins:
        cid, ws, o = w["cid"], int(w["ws"]), w["o"]
        up = _bbo(conn, cid, "Up"); dn = _bbo(conn, cid, "Down")
        if len(up[0]) < 100 or len(dn[0]) < 100:
            continue
        binance = {}
        for r in conn.execute("SELECT source_event_ts t, price v FROM crypto5m_binance_observations WHERE "
                              "source_event_ts>=? AND source_event_ts<? AND price IS NOT NULL",
                              ((ws - VOL_LB) * 1000, (ws + 300) * 1000)):
            binance[int(r["t"]) // 1000] = float(r["v"])
        if len(binance) < 40:
            continue
        tt, tsgn = _tape(conn, cid)
        out.append((binance, {"Up": up, "Down": dn}, tt, tsgn, ws, o))
    return out


def report(name, allt, worst_win):
    if not allt:
        print(f"{name:24} no trades"); return
    net = sum(allt); wins = sum(1 for x in allt if x > 0)
    print(f"{name:24} n={len(allt):4}  net={net:+8.2f}  mean={net/len(allt):+.3f}  win%={100*wins/len(allt):3.0f}  "
          f"worst_trade={min(allt):+6.2f}  worst_window={worst_win:+7.2f}")


def main():
    import sqlite3
    conn = sqlite3.connect(CAP); conn.row_factory = sqlite3.Row
    windows = load(conn); conn.close()
    feats_cache, grids_cache = [], []
    for w in windows:
        binance, paths, tt, tsgn, ws, o = w
        grids = list(range(ws + WARMUP_S, ws + 300, GRID_S))
        feats_cache.append(grid_features(binance, tt, tsgn, ws)); grids_cache.append(grids)
    print(f"=== entry×exit replay (gap-faithful, 2s grid, {len(windows)} settled windows) ===\n")
    combos = [("INSTANT", "CURRENT", "today's engine (baseline)"),
              ("INSTANT", "PATIENCE", "exit fix only"),
              ("PERSIST", "PATIENCE", "exit fix + entry memory (persist+multi-horizon)"),
              ("SMOOTH", "PATIENCE", "exit fix + Holt smoothed-state entry")]
    for em, xr, label in combos:
        allt = []; worst = 0.0
        for w, feats, grids in zip(windows, feats_cache, grids_cache):
            tr = simulate(w, feats, grids, em, xr)
            allt += tr; worst = min(worst, sum(tr))
        report(f"{em}+{xr}", allt, worst)
        print(f"{'':24} -> {label}")
    print("\nread: compare net + win% + worst_window across rows. The marginal value of each layer = the row "
          "delta. Entry caveat: PERSIST/SMOOTH trade FEWER but should be HIGHER quality (mean/win%). "
          "Touch-optimistic intra-tick; ranks rules, not absolute PnL — the live forward run is the judge.")


if __name__ == "__main__":
    main()

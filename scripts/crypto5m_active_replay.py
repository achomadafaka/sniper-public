#!/usr/bin/env python3
"""Backtest the active TAKER engine over the captured history — run the REAL decide() logic tick-by-tick on
each resolved window, with the CONVICTION gate ON vs OFF (the A/B that answers 'does the gate help'), then
inspect the resulting wins/losses for DECISION ERRORS (premature stops, gate violations, sub-cost entries).

Read-only on the capture. Reuses the exact engine functions (active_trader.decide, live_desk fair value /
adaptive momentum / book imbalance) so the backtest is the engine, not a reimplementation. Not a profit
claim — paper, cost-bound taker, n~100 windows.
"""
from __future__ import annotations
import argparse, bisect, json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sniper.algos.polymarket_crypto5m import active_trader as AT
from sniper.algos.polymarket_crypto5m.active_trader import Position, decide, settle_pnl, exit_pnl
from sniper.algos.polymarket_crypto5m import live_desk as LD

CAP = os.path.join(ROOT, "data", "crypto5m_capture.sqlite")
STEP = 3
STAKE = 10.0
VOL_LB = LD.VOL_LOOKBACK_S
REENTRY_COOLDOWN_S = 25     # mirror the runner's anti-churn cooldown between re-entries in a window


def _imb(bids, asks, levels=3):
    bs = sum(s for _, s in bids[:levels]); asz = sum(s for _, s in asks[:levels])
    return round((bs - asz) / (bs + asz), 3) if bs + asz > 0 else None


def _load_token_book(c, cid, token, ws):
    rows = c.execute("SELECT exchange_ts t, best_bid, best_ask, bids, asks FROM crypto5m_book_snapshots "
                     "WHERE condition_id=? AND token_id=? AND exchange_ts>=? AND exchange_ts<? ORDER BY exchange_ts",
                     (cid, token, ws * 1000, (ws + 300) * 1000)).fetchall()
    times, data = [], []
    for r in rows:
        t = int(r["t"]) // 1000
        try:
            bid = float(r["best_bid"]) if r["best_bid"] is not None else None
            ask = float(r["best_ask"]) if r["best_ask"] is not None else None
            imb = _imb(json.loads(r["bids"]), json.loads(r["asks"])) if r["bids"] and r["asks"] else None
        except Exception:
            bid = ask = imb = None
        times.append(t); data.append((bid, ask, imb))
    return times, data


def _at(times, data, t):
    i = bisect.bisect_right(times, t) - 1
    return data[i] if i >= 0 else (None, None, None)


def replay_window(binance, up, down, up_won, *, gate):
    """Replay decide() across one window. Returns list of trade dicts."""
    ut, ud = up; dt, dd = down
    secs = sorted(binance)
    if len(secs) < 30:
        return []
    ws = secs[0]; start = binance[ws]
    pos, entry_ctx, prob_prev, cooldown = None, None, None, 0
    trades = []

    def close(kind, price, t):
        nonlocal pos, entry_ctx
        won = (pos.side == "Up") == up_won
        if kind == "settlement":
            pnl = settle_pnl(pos, "Up" if up_won else "Down", STAKE)["pnl"]
        else:
            pnl = exit_pnl(pos, price, STAKE)["pnl"]
        trades.append({**entry_ctx, "exit_kind": kind, "exit_price": price, "pnl": pnl,
                       "won_at_settle": won})
        pos, entry_ctx = None, None

    for t in range(ws + 12, ws + 296, STEP):
        if t not in binance or (t - 30) not in binance:
            continue
        left = 300 - (t - ws)
        recent = [binance[s] for s in range(t - VOL_LB, t + 1) if s in binance]
        rv = LD.realized_vol_per_s(recent)
        vol = max(rv, LD.VOL_FLOOR_PER_S) if rv else None
        fair = LD.fair_value_up(binance[t], start, vol, left)
        if fair is None:
            continue
        mom, _, agree = LD.adaptive_momentum({s: binance[s] for s in binance if s <= t}, t, left)
        ub, ua, uimb = _at(ut, ud, t)
        db, da, _ = _at(dt, dd, t)
        book = {"up_ask": ua, "up_bid": ub, "down_ask": da, "down_bid": db}
        a = decide(prob_up=fair, book=book, position=pos, seconds_left=left, now_ts=t,
                   prob_up_prev=prob_prev,
                   momentum_bps=(mom if gate else None),
                   momentum_agreement=(agree if gate else None),
                   book_imbalance=(uimb if gate else None))
        if a["action"] == "enter" and pos is None and t >= cooldown:
            pos = Position(a["side"], a["price"], t, a["shares"], entry_prob=a.get("entry_prob"))
            entry_ctx = {"side": a["side"], "entry_price": a["price"], "entry_ts": t, "reason": a["reason"],
                         "momentum_bps": mom, "imbalance": uimb, "fair_at_entry": round(fair, 4)}
        elif a["action"] == "exit" and pos is not None:
            close(a["kind"], a["price"], t)
            cooldown = t + REENTRY_COOLDOWN_S
        prob_prev = fair
    if pos is not None:
        close("settlement", 1.0 if (pos.side == "Up") == up_won else 0.0, ws + 300)
    return trades


def _agg(trades):
    n = len(trades)
    if not n:
        return (0, 0, 0.0)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    return (n, wins, sum(t["pnl"] for t in trades))


def conviction_class(reason):
    r = (reason or "").lower()
    if "both favor" in r: return "lean_in"
    if "bar raised" in r: return "vs_penalty"
    if "favors" in r: return "one_favors"
    if "neutral trend" in r: return "neutral"
    return "untagged"


def load_windows(c, limit):
    wins = c.execute("SELECT s.condition_id cid, m.window_start_ts ws, m.up_token_id up, m.down_token_id dn, "
                     "s.winning_outcome wout FROM crypto5m_settlements s "
                     "JOIN crypto5m_markets m ON m.condition_id=s.condition_id "
                     "WHERE s.winning_outcome IS NOT NULL ORDER BY m.window_start_ts").fetchall()
    if limit:
        wins = wins[-limit:]
    data = []
    for w in wins:
        binance = {int(r["t"]) // 1000: float(r["v"]) for r in c.execute(
            "SELECT source_event_ts t, price v FROM crypto5m_binance_observations "
            "WHERE source_event_ts>=? AND source_event_ts<? AND price IS NOT NULL ORDER BY t",
            ((w["ws"] - VOL_LB) * 1000, (w["ws"] + 300) * 1000))}
        if len(binance) < 30:
            continue
        up = _load_token_book(c, w["cid"], str(w["up"]), w["ws"])
        down = _load_token_book(c, w["cid"], str(w["dn"]), w["ws"])
        if not up[0] or not down[0]:
            continue
        data.append((binance, up, down, (w["wout"] or "").strip().lower().startswith("up")))
    return data


def replay_all(data, gate):
    out = []
    for (binance, up, down, up_won) in data:
        out.extend(replay_window(binance, up, down, up_won, gate=gate))
    return out


def _metrics(trades):
    n, wn, pnl = _agg(trades)
    stops = [t for t in trades if t["exit_kind"] == "stop"]
    prem = [t for t in stops if t["won_at_settle"]]
    return {"n": n, "wr": wn / max(n, 1), "pnl": pnl, "avg": pnl / max(n, 1),
            "stops": len(stops), "prem": len(prem), "prem_pct": len(prem) / max(len(stops), 1)}


def sweep(data):
    """Try candidate engine-constant tweaks against the SAME windows (monkeypatch AT; restore after each)."""
    # levers of the v2 disciplined engine (conviction-required, late entry, hold-to-settlement)
    configs = [
        ("baseline (current)", {}),
        ("enter <60s left", {"ENTER_WHEN_SECONDS_LEFT_BELOW": 60}),
        ("enter <120s left", {"ENTER_WHEN_SECONDS_LEFT_BELOW": 120}),
        ("enter <180s left", {"ENTER_WHEN_SECONDS_LEFT_BELOW": 180}),
        ("momentum sign only (0bps)", {"CONVICTION_MOM_BPS": 0.0}),
        ("stronger momentum (8bps)", {"CONVICTION_MOM_BPS": 8.0}),
        ("stronger imbalance (0.35)", {"IMBALANCE_THRESHOLD": 0.35}),
    ]
    lines = [f"=== TUNING SWEEP over {len(data)} windows (engine replay; same windows each row) ===",
             f"\n{'config':32}{'trades':>7}{'WR':>6}{'PnL':>9}{'avg':>8}{'stops':>7}{'prem%':>7}"]
    for name, ov in configs:
        saved = {k: getattr(AT, k) for k in ov}
        for k, v in ov.items():
            setattr(AT, k, v)
        m = _metrics(replay_all(data, gate=True))
        for k, v in saved.items():
            setattr(AT, k, v)
        lines.append(f"{name:32}{m['n']:>7}{m['wr']:>6.0%}{m['pnl']:>+9.2f}{m['avg']:>+8.3f}"
                     f"{m['stops']:>7}{m['prem_pct']:>7.0%}")
    lines.append("\nread: pick the config with the best PnL/avg AND lower premature-stop% — that's the engine tweak.")
    lines.append("FIDELITY WARNING: book snapshots are ~1/2.5s, so this REPLAY SMOOTHS intra-tick noise and is "
                 "OPTIMISTIC for STOP-sensitive tweaks (tight stops whipsaw in live but look fine here). Trust "
                 "the LIVE forward review (crypto5m_active_review) over this for stop/exit $ tuning.")
    return "\n".join(lines)


def run(args):
    import sqlite3
    c = sqlite3.connect(f"file:{CAP}?mode=ro&immutable=1", uri=True); c.row_factory = sqlite3.Row
    data = load_windows(c, args.windows)
    c.close()
    if args.sweep:
        return sweep(data)
    nwin = len(data)
    res = {True: replay_all(data, gate=True), False: replay_all(data, gate=False)}

    lines = [f"=== active-engine REPLAY backtest over {nwin} captured windows (step {STEP}s) ==="]
    lines.append(f"\n{'config':22}{'trades':>7}{'WR':>6}{'PnL':>10}{'avg':>9}")
    for gate, label in ((True, "CONVICTION gate ON"), (False, "gate OFF (mispricing only)")):
        n, wn, pnl = _agg(res[gate])
        if n:
            lines.append(f"{label:22}{n:>7}{wn/n:>6.0%}{pnl:>+10.2f}{pnl/n:>+9.3f}")
    on = res[True]
    # exit-kind breakdown (gate ON)
    lines.append("\nexit kinds (gate ON):")
    for k in sorted({t["exit_kind"] for t in on}):
        n, wn, pnl = _agg([t for t in on if t["exit_kind"] == k])
        lines.append(f"  {k:14} n={n:>4} WR {wn/max(n,1):>4.0%} PnL {pnl:>+8.2f} avg {pnl/max(n,1):>+6.3f}")
    # conviction breakdown (gate ON)
    lines.append("\nconviction class (gate ON):")
    for cl in ("lean_in", "one_favors", "neutral", "vs_penalty"):
        grp = [t for t in on if conviction_class(t["reason"]) == cl]
        if grp:
            n, wn, pnl = _agg(grp)
            lines.append(f"  {cl:12} n={n:>4} WR {wn/n:>4.0%} PnL {pnl:>+8.2f} avg {pnl/n:>+6.3f}")
    # ---- DECISION ERRORS ----
    lines.append("\nDECISION ERRORS (gate ON):")
    stops = [t for t in on if t["exit_kind"] == "stop"]
    premature = [t for t in stops if t["won_at_settle"]]      # we stopped a position that WOULD have won
    lines.append(f"  premature stops (cut a winner): {len(premature)}/{len(stops)} stops "
                 f"= {len(premature)/max(len(stops),1):.0%}  "
                 f"(${sum(STAKE/t['entry_price']*1.0 - STAKE for t in premature):+.2f} left on the table)")
    against_both = [t for t in on if "both against" in (t["reason"] or "").lower()]
    lines.append(f"  gate violations (entered vs BOTH signals): {len(against_both)}  (should be 0)")
    tp = [t for t in on if t["exit_kind"] == "take_profit"]
    tp_left = [t for t in tp if t["won_at_settle"] and t["exit_price"] < 0.98]   # took profit, side still won big
    lines.append(f"  take-profits that left upside (won at settle, sold <0.98): {len(tp_left)}/{len(tp)}")
    settle_losses = [t for t in on if t["exit_kind"] == "settlement" and t["pnl"] < 0]
    lines.append(f"  held losers to settlement: {len(settle_losses)} (couldn't exit — no bid)")
    # suggestion
    lines.append("\nread: if premature-stop% is high, the stop is too tight (STOP_EDGE/FAIR_REVERSAL_STOP); if "
                 "gate ON >> gate OFF, the conviction gate helps; gate violations must be 0.")
    return "\n".join(lines)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=0, help="limit to the most recent N resolved windows (0=all)")
    ap.add_argument("--sweep", action="store_true", help="sweep candidate stop/gate tweaks against the same windows")
    ap.add_argument("--quiet", action="store_true")
    rep = run(ap.parse_args())
    if "--quiet" not in sys.argv:
        print(rep)
    log = os.path.join(ROOT, "logs", "crypto5m_active_review.log")
    import datetime
    with open(log, "a") as f:
        f.write(f"\n[replay {datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}]\n{rep}\n")

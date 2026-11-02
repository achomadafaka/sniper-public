#!/usr/bin/env python3
"""Maker-fill paper simulator over the captured Polymarket BTC 5m tape.

Question it answers: can we monetize the verified ~1s Binance->book lead as a MAKER
(post resting quotes, earn the spread, dodge adverse selection by repricing off Binance
faster than the book) — when we CAN'T as a taker (1.3c edge < 2.25c taker cost)?

Honest mechanics (no synthetic fills):
  - We quote the UP token only: a resting BID at fair_up - margin and ASK at fair_up + margin.
  - fair_up = driftless diffusion off the BINANCE path (Phi(ln(now/start)/(vol*sqrt(t_left)))),
    computed from the Binance price as of (t - latency) -> models our reaction speed.
  - A REAL fill happens when a real last_trade_price print crosses our resting quote:
      a SELL print at price <= our bid  -> we BUY  size (capped by remaining capacity),
      a BUY  print at price >= our ask  -> we SELL size.
  - Quotes are clamped to be valid maker orders (bid < best_ask, ask > best_bid).
  - Fee = observed realized rate (0 on the tape); maker rebate is a parameter.
  - Settle held inventory at resolution: UP share -> $1 if up_won else $0.

The decisive comparison: BINANCE-REACTIVE maker vs STATIC maker (quotes fixed at the
opening fair value, never repriced) across reaction latency. If reactive >> static, the
Binance lead is the source of edge. Read-only on the capture DB.
"""
from __future__ import annotations
import argparse, json, math, sqlite3, statistics as st

CAP_DB = "data/crypto5m_capture.sqlite"
WINDOW_S = 300
TICK = 0.01


# ---------- pure compute -----------------------------------------------------------------------
def fair_value_up(btc_now, btc_start, vol_per_s, seconds_left):
    if not btc_now or not btc_start or btc_start <= 0 or not vol_per_s or vol_per_s <= 0 or seconds_left <= 0:
        return None
    z = math.log(btc_now / btc_start) / (vol_per_s * math.sqrt(seconds_left))
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def realized_vol_per_s(closes):
    pts = [float(x) for x in closes if x]
    if len(pts) < 5:
        return None
    rets = [math.log(b / a) for a, b in zip(pts, pts[1:]) if a > 0 and b > 0]
    if len(rets) < 4:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) or None


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------- per-window replay ------------------------------------------------------------------
def load_window(c, cid, ws, up_token):
    """Return per-second binance price, per-second UP best bid/ask, and the ordered trade prints."""
    bz = c.execute(
        "SELECT source_event_ts t, price v FROM crypto5m_binance_observations "
        "WHERE source_event_ts>=? AND source_event_ts<? AND price IS NOT NULL ORDER BY t",
        (ws * 1000, (ws + WINDOW_S) * 1000)).fetchall()
    binance = {int(r["t"]) // 1000: float(r["v"]) for r in bz}

    bba = c.execute(
        "SELECT exchange_ts t, raw_payload p FROM crypto5m_book_events "
        "WHERE condition_id=? AND token_id=? AND event_type='best_bid_ask' "
        "AND exchange_ts>=? AND exchange_ts<? ORDER BY exchange_ts",
        (cid, up_token, ws * 1000, (ws + WINDOW_S) * 1000)).fetchall()
    book = {}  # sec -> (best_bid, best_ask)
    for r in bba:
        try:
            d = json.loads(r["p"]); bb = float(d["best_bid"]); ba = float(d["best_ask"])
            if 0 < bb < 1 and 0 < ba < 1 and ba >= bb:
                book[int(r["t"]) // 1000] = (bb, ba)
        except Exception:
            pass

    trades = c.execute(
        "SELECT exchange_ts t, raw_payload p FROM crypto5m_book_events "
        "WHERE condition_id=? AND token_id=? AND event_type='last_trade_price' "
        "AND exchange_ts>=? AND exchange_ts<? ORDER BY exchange_ts",
        (cid, up_token, ws * 1000, (ws + WINDOW_S) * 1000)).fetchall()
    prints = []
    for r in trades:
        try:
            d = json.loads(r["p"])
            prints.append((int(r["t"]) // 1000, float(d["price"]), d["side"], float(d["size"])))
        except Exception:
            pass
    return binance, book, prints


def _last_at(d, t):
    """value in dict d at the greatest key <= t (forward-fill)."""
    best = None
    for k in d:
        if k <= t and (best is None or k > best):
            best = k
    return d.get(best) if best is not None else None


def simulate_window(binance, book, prints, up_won, *, latency, reactive,
                    margin, clip, max_shares, rebate, fee_rate,
                    top_of_book=True, fill_frac=1.0, center_source="binance"):
    """Run one window. Returns dict with pnl + fill stats. reactive=False => static opening quote.

    center_source (when reactive): "binance" -> quote center = diffusion fair off Binance (our alpha hypothesis);
                                   "bookmid" -> quote center = forward-filled book mid (control: just reprice on book).
    Realism knobs:
      top_of_book=True  -> only fill when our quote is at/improving the best price (no deep-book phantom fills)
      fill_frac<1.0     -> we capture only this share of each crossing print (queue competition)
    """
    if not binance or not book or not prints:
        return None
    ws_keys = sorted(binance)
    start = binance[ws_keys[0]]
    # opening (static) fair value: use first ~30s of path for vol, value at t=0
    vol0 = realized_vol_per_s([binance[k] for k in ws_keys[: min(30, len(ws_keys))]])
    if not vol0:
        return None
    fair0 = fair_value_up(start, start, vol0, WINDOW_S) or 0.5

    inv = 0.0            # UP shares held (long if >0)
    cash = 0.0           # realized cash from fills (+ when we sell, - when we buy)
    fills = 0
    fee_paid = 0.0
    reb = 0.0
    last_fair = fair0

    # full path vol (stable) for reactive repricing
    full_vol = realized_vol_per_s([binance[k] for k in ws_keys]) or vol0

    # walk prints in time order; quotes recomputed from Binance at (t - latency)
    for (t, px, side, size) in prints:
        left = max(1, WINDOW_S - (t - ws_keys[0]))
        if reactive and center_source == "bookmid":
            # control: re-center on the book mid as of (t - latency), no Binance
            bm = _last_at(book, t - latency)
            fair = ((bm[0] + bm[1]) / 2) if bm else last_fair
            if fair is not None:
                last_fair = fair
        elif reactive:
            bt = _last_at(binance, t - latency)
            fair = fair_value_up(bt, start, full_vol, left) if bt else last_fair
            if fair is not None:
                last_fair = fair
        else:
            fair = fair0
        if fair is None:
            continue
        bb_ba = _last_at(book, t)
        if not bb_ba:
            continue
        best_bid, best_ask = bb_ba
        # our resting quotes, clamped to valid maker prices
        my_bid = clamp(round(fair - margin, 2), TICK, best_ask - TICK)
        my_ask = clamp(round(fair + margin, 2), best_bid + TICK, 1 - TICK)
        take = min(size, clip) * fill_frac
        # to fill we must be competitive: at/improving the top of book (else flow hits a better resting order first)
        bid_competitive = (not top_of_book) or (my_bid >= best_bid - 1e-9)
        ask_competitive = (not top_of_book) or (my_ask <= best_ask + 1e-9)
        # SELL print hits our BID -> we BUY (if we have room to go longer)
        if side == "SELL" and bid_competitive and px <= my_bid + 1e-9 and inv < max_shares:
            q = min(take, max_shares - inv)
            cash -= q * my_bid; inv += q; fills += 1
            fee_paid += q * fee_rate * my_bid * (1 - my_bid); reb += q * rebate
        # BUY print hits our ASK -> we SELL (allow going short down to -max_shares)
        elif side == "BUY" and ask_competitive and px >= my_ask - 1e-9 and inv > -max_shares:
            q = min(take, max_shares + inv)
            cash += q * my_ask; inv -= q; fills += 1
            fee_paid += q * fee_rate * my_ask * (1 - my_ask); reb += q * rebate

    payoff = 1.0 if up_won else 0.0
    settle = inv * payoff
    pnl = cash + settle - fee_paid + reb
    return {"pnl": pnl, "fills": fills, "end_inv": inv, "rebate": reb, "fee": fee_paid}


def run(args):
    c = sqlite3.connect(f"file:{args.db}?mode=ro&immutable=1", uri=True); c.row_factory = sqlite3.Row
    wins = []
    for r in c.execute(
        "SELECT s.condition_id cid, m.window_start_ts ws, m.up_token_id up, s.winning_outcome wout "
        "FROM crypto5m_settlements s JOIN crypto5m_markets m ON m.condition_id=s.condition_id "
        "WHERE s.winning_outcome IS NOT NULL"):
        wins.append((r["cid"], r["ws"], str(r["up"]), (r["wout"] or "").strip().lower()))
    print(f"resolved windows: {len(wins)}   margin={args.margin}  clip={args.clip}  "
          f"max_shares={args.max_shares}  rebate={args.rebate}  fee_rate={args.fee_rate}")

    # (name, reactive, latency, center_source)
    configs = [("static", False, 0, "binance")]
    for L in args.latencies:
        configs.append((f"binance L={L}s", True, L, "binance"))
    for L in args.latencies:
        configs.append((f"bookmid L={L}s", True, L, "bookmid"))

    results = {name: [] for name, *_ in configs}
    for (cid, ws, up, wout) in wins:
        binance, book, prints = load_window(c, cid, ws, up)
        up_won = wout.startswith("up")
        for name, reactive, L, csrc in configs:
            res = simulate_window(binance, book, prints, up_won, latency=L, reactive=reactive,
                                  margin=args.margin, clip=args.clip, max_shares=args.max_shares,
                                  rebate=args.rebate, fee_rate=args.fee_rate,
                                  top_of_book=not args.no_top_of_book, fill_frac=args.fill_frac,
                                  center_source=csrc)
            if res:
                results[name].append(res)
    c.close()

    print(f"\n{'config':14} {'n':>4} {'totPnL':>9} {'mean/win':>9} {'win%':>6} {'avgFills':>9} {'medPnL':>8}")
    for name, *_ in configs:
        rs = results[name]
        if not rs:
            print(f"{name:14}  no data"); continue
        pnls = [r["pnl"] for r in rs]
        wr = sum(1 for p in pnls if p > 0) / len(pnls)
        print(f"{name:14} {len(rs):>4} {sum(pnls):>9.2f} {st.mean(pnls):>9.3f} {wr:>6.1%} "
              f"{st.mean([r['fills'] for r in rs]):>9.1f} {st.median(pnls):>8.3f}")
    print("\nread: reactive >> static  => the Binance lead is the edge.  reactive falls off as L grows "
          "=> latency-gated (need fast feed).  all <=0 => no maker edge at these params.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=CAP_DB)
    ap.add_argument("--margin", type=float, default=0.02, help="quote half-width around fair value")
    ap.add_argument("--clip", type=float, default=50.0, help="max shares taken per crossing print")
    ap.add_argument("--max-shares", type=float, default=200.0, help="inventory cap per side per window")
    ap.add_argument("--rebate", type=float, default=0.0, help="maker rebate per share")
    ap.add_argument("--fee-rate", type=float, default=0.0, help="taker fee coeff (observed 0; 0.07=config)")
    ap.add_argument("--latencies", type=int, nargs="+", default=[0, 1, 3])
    ap.add_argument("--fill-frac", type=float, default=1.0, help="share of crossing print we capture (queue competition)")
    ap.add_argument("--no-top-of-book", action="store_true", help="allow deep-book fills (unrealistic; original behavior)")
    run(ap.parse_args())

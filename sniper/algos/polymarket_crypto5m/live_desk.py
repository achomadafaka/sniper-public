"""Live 'trader's desk' for the in-flight Polymarket BTC Up/Down 5-minute window.

This is the HONEST version of "act like a trader": it does NOT predict where BTC is going (that horizon is
~efficient — see research-5min-btc-binary-edge-signals). It computes where BTC ALREADY IS vs where the
Polymarket book is PRICED, and flags a DYNAMIC entry the moment the book underprices a side (a good price
appears) — instead of a fixed 240s clock tick. Everything is fetched live on demand.

Pure compute (fair value / edge / entry) is unit-testable; the live fetch is best-effort and degrades to
a 'data unavailable' desk rather than ever raising.
"""
from __future__ import annotations

import concurrent.futures as _cf
import json
import math
import os
import time
import urllib.request

WINDOW_S = 300
GAMMA = "https://gamma-api.polymarket.com/events?slug="
SLUG_PREFIX = "btc-updown-5m-"
CLOB_BOOK = "https://clob.polymarket.com/book?token_id="
BINANCE_TICKER = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
BINANCE_KLINES = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s"
BINANCE_TIME = "https://api.binance.com/api/v3/time"
UA = {"User-Agent": "sniper-research/0.1"}
DEFAULT_FEE_RATE = 0.07
MIN_EDGE = 0.02          # don't call ENTER unless fair value beats the ask + entry fee by this margin
VOL_FLOOR_PER_S = 1e-4   # floor on the 1s realized-vol estimate — a near-zero vol would collapse fair value
                         # to 0/1 on noise. BTC 1s log-return stdev is ~1e-4, so this catches degenerate
                         # dead-tape estimates without turning routine bp moves into 70%+ probabilities.
VOL_LOOKBACK_S = 180     # estimate vol over a longer, STABLE lookback (vol is a property of BTC's process,
                         # not the 5m window) so the fair value doesn't jitter tick-to-tick and churn entries
MOMENTUM_LOOKBACK_S = 45  # short-horizon BTC trend window — used as an AGREEMENT signal on entries (don't
                          # fight a strong move). NOT a forecast: the 5m binary settles on the END price.
CAPTURE_DB = os.environ.get("CRYPTO5M_CAPTURE_DB", "data/crypto5m_capture.sqlite")  # the WS-fed tape source


# ---------- pure compute (testable, no network) -------------------------------------------------

def diffusion_z(btc_now, btc_start, vol_per_s, seconds_left):
    """Diffusion moneyness z = ln(now/start)/(vol*sqrt(t_left)). None on degenerate inputs."""
    if not btc_now or not btc_start or btc_start <= 0 or not vol_per_s or vol_per_s <= 0 or seconds_left <= 0:
        return None
    return math.log(btc_now / btc_start) / (vol_per_s * math.sqrt(seconds_left))


def fair_value_up(btc_now, btc_start, vol_per_s, seconds_left) -> float | None:
    """P(BTC_end >= BTC_start) under a driftless diffusion: Phi(z). 'Where BTC already is' projected to close."""
    z = diffusion_z(btc_now, btc_start, vol_per_s, seconds_left)
    if z is None:
        return None
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2)))


def side_edges(fair_up, up_ask, down_ask, fee_rate=DEFAULT_FEE_RATE) -> dict:
    """Net edge per side = fair value − (ask + entry taker fee). +ve = the book UNDERPRICES that side."""
    out = {"up": None, "down": None}
    if fair_up is not None and up_ask and 0 < up_ask < 1:
        out["up"] = round(fair_up - up_ask - fee_rate * up_ask * (1 - up_ask), 4)
    if fair_up is not None and down_ask and 0 < down_ask < 1:
        out["down"] = round((1.0 - fair_up) - down_ask - fee_rate * down_ask * (1 - down_ask), 4)
    return out


def entry_signal(edges, min_edge=MIN_EDGE) -> dict:
    """Dynamic entry: ENTER the best side whose edge clears the price+fee by min_edge; else WAIT.
    The point: enter when a good price appears, not on a fixed clock."""
    cands = [(s, e) for s, e in edges.items() if e is not None and e > min_edge]
    if not cands:
        best = max(((s, e) for s, e in edges.items() if e is not None), key=lambda x: x[1], default=None)
        return {"action": "WAIT", "reason": "book hasn't underpriced a side yet (no edge beats price + fee)",
                "best_side": (best[0].capitalize() if best else None), "best_edge": (best[1] if best else None)}
    s, e = max(cands, key=lambda x: x[1])
    return {"action": "ENTER", "side": s.capitalize(), "edge": e,
            "reason": f"book underprices {s.capitalize()} by {e:+.1%} vs where BTC already is"}


def btc_momentum_bps(closes_by_sec, now, lookback=MOMENTUM_LOOKBACK_S) -> float | None:
    """Signed BTC return over the last `lookback` seconds, in bps (+ve = up-trending). The 'are we fighting
    the move?' signal. Driftless fair value ignores this; we use it only to gate entries, not to forecast."""
    if not closes_by_sec:
        return None
    cur = [s for s in closes_by_sec if s <= now]
    past = [s for s in closes_by_sec if s <= now - lookback]
    if not cur or not past:
        return None
    p_now, p_past = closes_by_sec[max(cur)], closes_by_sec[max(past)]
    if p_past <= 0:
        return None
    return round(1e4 * (p_now / p_past - 1.0), 2)


def adaptive_momentum(closes_by_sec, now, seconds_left):
    """Trend over a horizon SCALED to time-left, confirmed across scales — the 'not-hardcoded, computed
    when needed' trend. Returns (momentum_bps, horizon_s, agreement):
      - horizon = look back about as far as we're betting forward (clamped 15..120s)
      - we also measure a shorter and a longer scale; agreement = fraction sharing the matched-scale's sign
    A real trend shows at several scales (agreement high); a blip shows at one (agreement low → don't trust it).
    """
    if not closes_by_sec or not seconds_left or seconds_left <= 0:
        return (None, None, None)
    base = max(15, min(120, int(seconds_left)))               # match the lookback to the remaining horizon
    scales = sorted({max(8, base // 2), base, min(120, base * 2)})
    moms = [(h, btc_momentum_bps(closes_by_sec, now, h)) for h in scales]
    moms = [(h, m) for h, m in moms if m is not None]
    if not moms:
        return (None, None, None)
    matched_h, matched_m = min(moms, key=lambda x: abs(x[0] - base))
    sgn = (matched_m > 0) - (matched_m < 0)
    agree = (sum(1 for _, m in moms if ((m > 0) - (m < 0)) == sgn) / len(moms)) if sgn != 0 else 0.0
    return (matched_m, matched_h, round(agree, 2))


def realized_vol_per_s(closes) -> float | None:
    """Stdev of consecutive 1s log returns over the recent path. None if too few points."""
    pts = [float(x) for x in closes if x]
    if len(pts) < 5:
        return None
    rets = [math.log(b / a) for a, b in zip(pts, pts[1:]) if a > 0 and b > 0]
    if len(rets) < 4:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) or None


# ---------- live fetch (best-effort) ------------------------------------------------------------

def _get(url, timeout=6):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read())


def _safe(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001
        return None


def server_now():
    """Binance server time (real unix seconds). The local clock is skewed (env is +7h), and Polymarket
    windows are keyed by REAL settlement time — using the local clock fetches a future, untraded window."""
    return int(_get(BINANCE_TIME)["serverTime"]) // 1000


def book_imbalance(book, levels=3):
    """Signed top-of-book size imbalance for a token: (bid_size - ask_size) / (bid_size + ask_size) over the
    top `levels`. +ve = buy pressure on this token (price likely to rise) — the order-flow direction signal
    (R2). None if the book is empty/one-sided-empty."""
    bids = sorted(([float(b["price"]), float(b["size"])] for b in (book or {}).get("bids", []) if float(b["size"]) > 0),
                  key=lambda x: -x[0])[:levels]
    asks = sorted(([float(a["price"]), float(a["size"])] for a in (book or {}).get("asks", []) if float(a["size"]) > 0),
                  key=lambda x: x[0])[:levels]
    bs, asz = sum(s for _, s in bids), sum(s for _, s in asks)
    if bs + asz <= 0:
        return None
    return round((bs - asz) / (bs + asz), 3)


def microprice(book):
    """Size-weighted top-of-book price (leads the mid): (bid·ask_size + ask·bid_size)/(bid_size+ask_size).
    Returns (microprice, mid) or (None, None). microprice − mid = book pressure (the scanner's best signal)."""
    bids = [[float(b["price"]), float(b["size"])] for b in (book or {}).get("bids", []) if float(b["size"]) > 0]
    asks = [[float(a["price"]), float(a["size"])] for a in (book or {}).get("asks", []) if float(a["size"]) > 0]
    if not bids or not asks:
        return (None, None)
    bp, bs = max(bids, key=lambda x: x[0]); ap, asz = min(asks, key=lambda x: x[0])
    mid = (bp + ap) / 2
    if bs + asz <= 0:
        return (None, mid)
    return ((bp * asz + ap * bs) / (bs + asz), mid)


def tape_order_flow(condition_id, now_ts, capture_db=None, windows=(10, 30)):
    """Signed trade-tape order-flow imbalance for the Up token of the active window, read LIVE from the
    capture DB (the WS-fed `last_trade_price` tape that's already being recorded, ~0.3s fresh). For each
    horizon L: ofi_L = (buy_vol − sell_vol) / total_vol over the last L seconds, in [−1, +1] — +ve = aggressive
    buying of Up (price likely to rise). The walk-forward study (finding-crypto5m-walkforward-tape-model) found
    this the strongest predictor the engine wasn't using. Best-effort: returns Nones if the capture DB is
    missing/locked/empty — never raises (the engine must survive without it)."""
    out = {f"ofi_{w}s": None for w in windows}
    out["tape_n_10s"] = None
    if not condition_id or not now_ts:
        return out
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{capture_db or CAPTURE_DB}?mode=ro", uri=True, timeout=2)
        c.row_factory = sqlite3.Row
        lo = (now_ts - max(windows)) * 1000
        rows = c.execute(
            "SELECT exchange_ts, raw_payload FROM crypto5m_book_events WHERE condition_id=? "
            "AND event_type='last_trade_price' AND side='Up' AND exchange_ts>=? AND exchange_ts<=?",
            (condition_id, lo, now_ts * 1000)).fetchall()
        c.close()
        trades = []
        for r in rows:
            try:
                p = json.loads(r["raw_payload"]); sz = float(p.get("size") or 0)
                trades.append((int(r["exchange_ts"]) / 1000.0, sz if str(p.get("side")).upper() == "BUY" else -sz))
            except Exception:  # noqa: BLE001
                continue
        for w in windows:
            seg = [s for t, s in trades if t >= now_ts - w]
            gross = sum(abs(s) for s in seg)
            out[f"ofi_{w}s"] = round(sum(seg) / gross, 3) if gross > 0 else None
        out["tape_n_10s"] = sum(1 for t, _ in trades if t >= now_ts - 10)
    except Exception:  # noqa: BLE001 — capture DB optional; degrade silently
        return out
    return out


def tape_prints(condition_id, since_ts, now_ts, token="up", capture_db=None):
    """Trades printed on the tape in (since_ts, now_ts], in the given token's price space:
    [(price, size), ...]. Up and Down have independent CLOB tape rows; never synthesize Down prints from
    Up prices. Returns None when the tape is unreachable (capture DB missing/locked). Empty list = tape
    alive, no trades."""
    if not condition_id or since_ts is None or now_ts is None:
        return None
    side = "Down" if str(token).lower() == "down" else "Up"
    try:
        import sqlite3
        c = sqlite3.connect(f"file:{capture_db or CAPTURE_DB}?mode=ro", uri=True, timeout=2)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT raw_payload FROM crypto5m_book_events WHERE condition_id=? "
            "AND event_type='last_trade_price' AND side=? AND exchange_ts>? AND exchange_ts<=?",
            (condition_id, side, int(since_ts) * 1000, int(now_ts) * 1000)).fetchall()
        c.close()
        out = []
        for r in rows:
            try:
                p = json.loads(r["raw_payload"])
                px, sz = float(p.get("price")), float(p.get("size") or 0)
                if sz > 0 and 0.0 < px < 1.0:
                    out.append((px, sz))
            except (TypeError, ValueError):
                continue
        return out
    except Exception:  # noqa: BLE001 — capture DB optional; print-only fill models should no-fill
        return None


def kline_signals(kl, now):
    """Short-horizon Binance directional signals from 1s klines (each row: ..,close=4,volume=5,..,taker_buy=9):
    multi-horizon log-returns, EMA(10s)-vs-EMA(30s) slope, and taker-buy share − 0.5 (aggressor direction)."""
    closes = {int(k[0]) // 1000: float(k[4]) for k in kl}

    def ret(h):
        a, b = closes.get(now - h), closes.get(now)
        return (math.log(b / a) if (a and b and a > 0 and b > 0) else None)

    secs = sorted(s for s in closes if s <= now)
    series = [closes[s] for s in secs[-60:]]
    ema_slope = None
    if len(series) >= 30:
        def _ema(vals, span):
            k = 2.0 / (span + 1); e = vals[0]
            for v in vals[1:]:
                e = v * k + e * (1 - k)
            return e
        ef, es = _ema(series, 10), _ema(series, 30)
        ema_slope = (ef - es) / es if es else None
    vol_dir = None
    recent = [k for k in kl if now - 30 <= int(k[0]) // 1000 <= now]
    tot = sum(float(k[5]) for k in recent); buy = sum(float(k[9]) for k in recent)
    if tot > 0:
        vol_dir = buy / tot - 0.5
    return {"ret_5s": ret(5), "ret_15s": ret(15), "ret_30s": ret(30),
            "ema_slope": ema_slope, "volume_dir": vol_dir}


def _ema_series(vals, span):
    """Running EMA of a series — one value per input point — so the moving average can be PLOTTED over time,
    not just its final value."""
    k = 2.0 / (span + 1)
    e = vals[0]; out = [e]
    for v in vals[1:]:
        e = v * k + e * (1 - k); out.append(e)
    return out


def signal_series(kl, btc_start, vol_per_s, now, seconds_left, lookback=75):
    """The engine's OWN predictive constructs as time-series (for the live chart), evolving second-by-second:
    BTC price, our EMA(10)/EMA(30) moving averages (running values), and our driftless-diffusion P(Up)
    projection. Pure compute; None if too few klines."""
    closes = sorted(((int(k[0]) // 1000, float(k[4])) for k in kl if int(k[0]) // 1000 <= now), key=lambda x: x[0])
    if len(closes) < 30:
        return None
    secs = [s for s, _ in closes]; price = [p for _, p in closes]
    ef, es = _ema_series(price, 10), _ema_series(price, 30)
    n = min(lookback, len(secs))
    out = {"ts": secs[-n:], "price": [round(p, 2) for p in price[-n:]],
           "ema_fast": [round(x, 2) for x in ef[-n:]], "ema_slow": [round(x, 2) for x in es[-n:]]}
    if btc_start and btc_start > 0 and vol_per_s and vol_per_s > 0 and seconds_left and seconds_left > 0:
        prob = []
        for s, p in zip(out["ts"], out["price"]):
            sl = seconds_left + (now - s)                  # time left at that historical second
            if p > 0 and sl > 0:
                z = math.log(p / btc_start) / (vol_per_s * math.sqrt(sl))
                prob.append(round(0.5 * (1.0 + math.erf(z / math.sqrt(2))), 4))
            else:
                prob.append(None)
        out["prob_up"] = prob
    return out


def _best_ask(book):
    asks = [[float(a["price"]), float(a["size"])] for a in (book or {}).get("asks", []) if float(a["size"]) > 0]
    if not asks:
        return None
    return min(asks, key=lambda x: x[0])[0]


def _best_bid(book):
    bids = [[float(b["price"]), float(b["size"])] for b in (book or {}).get("bids", []) if float(b["size"]) > 0]
    if not bids:
        return None
    return max(bids, key=lambda x: x[0])[0]


def build_live_desk(now_ts=None) -> dict:
    """Live state for the current in-flight 5m window: BTC start/now, time, book asks, fair value, per-side
    edge, and the dynamic-entry signal. Never raises — returns available=False fields on fetch failure."""
    now = int(now_ts) if now_ts is not None else _safe(server_now)
    if now is None:
        return {"window_start_ts": None, "now_ts": None, "seconds_elapsed": None, "seconds_left": None,
                "slug": None, "available": False, "message": "server time unavailable"}
    ws = now - (now % WINDOW_S)
    elapsed, left = now - ws, WINDOW_S - (now - ws)
    desk = {"window_start_ts": ws, "now_ts": now, "seconds_elapsed": elapsed, "seconds_left": left,
            "slug": f"{SLUG_PREFIX}{ws}", "available": False}

    # fetch ticker + klines + gamma CONCURRENTLY (independent) — cuts the per-poll latency so the scalper can
    # react closer to the start of a move (moves peak ~7s; sequential fetch was the bottleneck).
    with _cf.ThreadPoolExecutor(max_workers=3) as _ex:
        _f_btc = _ex.submit(_safe, lambda: float(_get(BINANCE_TICKER)["price"]))
        _f_kl = _ex.submit(_safe, lambda: _get(f"{BINANCE_KLINES}&startTime={(ws - 90) * 1000}&endTime={now * 1000}&limit=400"))
        _f_ev = _ex.submit(_safe, lambda: _get(f"{GAMMA}{SLUG_PREFIX}{ws}"))
        btc_now = _f_btc.result()
        kl = _f_kl.result()
        _ev_pre = _f_ev.result()
    btc_start, vol, momentum_bps, momentum_horizon, momentum_agreement = None, None, None, None, None
    sig_series = None
    if kl:
        at_start = [k for k in kl if int(k[0]) // 1000 <= ws]
        btc_start = float(at_start[-1][4]) if at_start else (float(kl[0][4]) if kl else None)
        # vol over a STABLE lookback (last VOL_LOOKBACK_S of klines, not just the in-window slice) + floor,
        # so the fair value reflects BTC's volatility process rather than jittering on a short noisy sample
        recent = [k[4] for k in kl if int(k[0]) // 1000 >= now - VOL_LOOKBACK_S] or [k[4] for k in kl]
        raw_vol = realized_vol_per_s(recent)
        vol = max(raw_vol, VOL_FLOOR_PER_S) if raw_vol is not None else None
        closes_by_sec = {int(k[0]) // 1000: float(k[4]) for k in kl}
        momentum_bps, momentum_horizon, momentum_agreement = adaptive_momentum(closes_by_sec, now, left)
        ksig = kline_signals(kl, now)
        sig_series = signal_series(kl, btc_start, vol, now, left)   # our EMAs + P(Up) as plottable series
    else:
        ksig = {}
    desk.update({"btc_now": btc_now, "btc_start": btc_start, "vol_per_s": vol, "momentum_bps": momentum_bps,
                 "momentum_horizon_s": momentum_horizon, "momentum_agreement": momentum_agreement,
                 "ret_5s": ksig.get("ret_5s"), "ret_15s": ksig.get("ret_15s"), "ret_30s": ksig.get("ret_30s"),
                 "ema_slope": ksig.get("ema_slope"), "volume_dir": ksig.get("volume_dir"),
                 "signal_series": sig_series,
                 "btc_move_pct": (round(100 * (btc_now / btc_start - 1), 4) if (btc_now and btc_start) else None)})

    ev = _ev_pre                                            # fetched above, concurrently
    up_ask = up_bid = down_ask = down_bid = None
    imbalance_up = None
    micro_gap_up = None
    condition_id = None
    if ev:
        mk = (ev[0].get("markets") or [{}])[0]
        condition_id = mk.get("conditionId")
        toks = _safe(lambda: json.loads(mk.get("clobTokenIds") or "[]")) or []
        if len(toks) == 2:
            with _cf.ThreadPoolExecutor(max_workers=2) as _ex:    # both books concurrently
                _fu = _ex.submit(_safe, lambda: _get(f"{CLOB_BOOK}{toks[0]}"))   # toks[0] = Up token
                _fd = _ex.submit(_safe, lambda: _get(f"{CLOB_BOOK}{toks[1]}"))
                ub = _fu.result()
                db = _fd.result()
            up_ask, up_bid = _best_ask(ub), _best_bid(ub)
            down_ask, down_bid = _best_ask(db), _best_bid(db)
            imbalance_up = book_imbalance(ub)                   # +ve = order-flow leans Up
            mp, mid = microprice(ub)
            micro_gap_up = (round(mp - mid, 4) if (mp is not None and mid is not None) else None)
    flow = tape_order_flow(condition_id, now)              # signed trade-tape order flow (the new alpha)
    up_mid = (round((up_ask + up_bid) / 2, 4) if (up_ask is not None and up_bid is not None) else None)
    down_mid = (round((down_ask + down_bid) / 2, 4) if (down_ask is not None and down_bid is not None) else None)
    desk.update({"condition_id": condition_id, "up_ask": up_ask, "up_bid": up_bid,
                 "down_ask": down_ask, "down_bid": down_bid, "book_imbalance": imbalance_up,
                 "up_mid": up_mid, "down_mid": down_mid,
                 "microprice_gap": micro_gap_up, "tape_ofi_10s": flow.get("ofi_10s"),
                 "tape_ofi_30s": flow.get("ofi_30s"), "tape_n_10s": flow.get("tape_n_10s")})

    fair_up = fair_value_up(btc_now, btc_start, vol, left)
    edges = side_edges(fair_up, up_ask, down_ask)
    desk.update({"fair_value_up": (round(fair_up, 4) if fair_up is not None else None),
                 "z": diffusion_z(btc_now, btc_start, vol, left),
                 "market_up": (round(1 - down_ask, 4) if down_ask else (round(up_ask, 4) if up_ask else None)),
                 "edges": edges, "signal": entry_signal(edges)})
    # --- brain v2 (SHADOW): stabilized, market-regularized probability next to the current z. Never feeds a
    # decision in v1. Pure functions of the per-poll klines + the book mid -> no buffer, fail-closed to None.
    # Function-local imports avoid a circular import (stabilized_z imports diffusion_z from this module).
    from sniper.algos.polymarket_crypto5m.regime_vol import regime_vol as _regime_vol
    from sniper.algos.polymarket_crypto5m.stabilized_z import stabilized_z as _stabilized_z
    rv = _regime_vol(closes_by_sec, now) if kl else None
    _spread_up = (up_ask - up_bid) if (up_ask is not None and up_bid is not None and up_ask > up_bid) else None
    sz = _stabilized_z(btc_now, btc_start, left, rv, up_mid, _spread_up) if rv else None
    desk.update({
        "vol_slow": (rv or {}).get("vol_slow"), "vol_fast": (rv or {}).get("vol_fast"),
        "jump_flag": (rv or {}).get("jump_flag"), "divergence": (rv or {}).get("divergence"),
        "z_ours": (sz or {}).get("z_ours"), "z_market": (sz or {}).get("z_market"),
        "z_v2": (sz or {}).get("z_v2"), "z_weight": (sz or {}).get("weight")})

    desk["available"] = btc_now is not None and (up_ask is not None or down_ask is not None)
    return desk

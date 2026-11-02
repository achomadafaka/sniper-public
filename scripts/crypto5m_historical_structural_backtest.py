#!/usr/bin/env python3
"""Historical structural backtest for the Polymarket BTC 5m binary (research tooling, read-only).

Answers "can we test the structural model without waiting for forward data?" — YES, partially:
  * gamma serves PAST resolved windows (label = Up/Down + priceToBeat = Chainlink start), back >=7d;
  * Binance serves historical 1s klines (full BTC path per window).
So the STRUCTURAL track (P(end >= line) from a BTC price path) is backtestable now against REAL outcomes.
What is NOT historically available: the Polymarket order book / midpoint / executable prices mid-window —
so the residual-vs-market and execution/fee tracks still need forward collection.

Two structural proxies are scored (the model needs a price proxy for Chainlink mid-window):
  (a) line = priceToBeat (Chainlink start), price = Binance at checkpoint   -> cross-asset basis enters
  (b) line = Binance at window start,        price = Binance at checkpoint   -> basis cancels within Binance
Both predict the REAL Chainlink outcome; comparing them measures the basis cost.

Metrics: Brier vs the 0.5 baseline, calibration deciles, and a shuffled-label NEGATIVE CONTROL (must
collapse to ~0.25). No DB, no orders. Binance klines + gamma REST only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.algos.polymarket_crypto5m import market_resolver as MR  # noqa: E402
from sniper.algos.polymarket_crypto5m import settlement as S        # noqa: E402

UA = {"User-Agent": "sniper-research/0.1"}
GAMMA = "https://gamma-api.polymarket.com/events?slug=btc-updown-5m-"
KLINES = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1s"
CHECKPOINT = 240
WINDOW = 300


def _get(url, timeout=15):
    return json.loads(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read())


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _closes(ws):
    """1s close prices indexed by second-offset 0..WINDOW for the window starting at ws (epoch s)."""
    try:
        kl = _get(f"{KLINES}&startTime={ws*1000}&endTime={(ws+WINDOW)*1000}&limit=1000")
    except Exception:  # noqa: BLE001
        return None
    out = {}
    for bar in kl:
        off = bar[0] // 1000 - ws
        if 0 <= off <= WINDOW:
            out[off] = float(bar[4])
    return out


def _price_at(closes, off):
    for o in range(off, -1, -1):
        if o in closes:
            return closes[o]
    return None


def _vol_per_s(closes, upto):
    seq = [closes[o] for o in range(0, upto + 1) if o in closes]
    if len(seq) < 5:
        return None
    rets = [math.log(seq[i] / seq[i - 1]) for i in range(1, len(seq)) if seq[i - 1] > 0]
    if len(rets) < 4:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def _q_diffusion(price, line, vol_per_s, secs_remaining):
    if None in (price, line, vol_per_s) or price <= 0 or line <= 0 or vol_per_s <= 0 or secs_remaining <= 0:
        return None
    z = math.log(price / line) / (vol_per_s * math.sqrt(secs_remaining))
    return _phi(z)


def fetch_window(ws):
    try:
        ev = _get(f"{GAMMA}{ws}")
    except Exception:  # noqa: BLE001
        return None
    if not ev:
        return None
    spec, verdict = MR.parse_event(ev[0])
    if spec is None or not verdict.rule_supported:
        return None
    st = S.from_gamma(spec)
    if st.winning_outcome not in ("Up", "Down"):
        return None
    closes = _closes(ws)
    if not closes:
        return None
    price = _price_at(closes, CHECKPOINT - 1)   # strictly pre-decision (the bar opening at CHECKPOINT closes 1s later)
    start = _price_at(closes, 0)
    vol = _vol_per_s(closes, CHECKPOINT)
    line_p2b = getattr(spec, "price_to_beat", None)
    secs_rem = WINDOW - CHECKPOINT
    return {
        "ws": ws,
        "up_won": 1 if st.winning_outcome == "Up" else 0,
        "q_a": _q_diffusion(price, line_p2b, vol, secs_rem) if line_p2b else None,  # line = Chainlink P2B
        "q_b": _q_diffusion(price, start, vol, secs_rem),                            # line = Binance start
        "price_to_beat": line_p2b, "binance_start": start, "binance_at_cp": price, "vol_per_s": vol,
    }


def brier(pairs):
    return sum((q - y) ** 2 for q, y in pairs) / len(pairs) if pairs else None


def calibration(pairs, bins=5):
    out = []
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sub = [(q, y) for q, y in pairs if (lo <= q < hi or (b == bins - 1 and q == 1.0))]
        if sub:
            out.append((round(lo, 2), round(hi, 2), len(sub),
                        round(sum(q for q, _ in sub) / len(sub), 3),
                        round(sum(y for _, y in sub) / len(sub), 3)))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=1.0)
    ap.add_argument("--now", type=int, default=None, help="override 'now' epoch (else Binance server time)")
    a = ap.parse_args(argv)
    now = a.now or (int(_get("https://api.binance.com/api/v3/time")["serverTime"]) // 1000)
    latest = (now - 2 * WINDOW) // WINDOW * WINDOW           # skip the still-settling tail
    n_windows = int(a.days * 86400 / WINDOW)
    starts = [latest - i * WINDOW for i in range(n_windows)]
    print(f"fetching {n_windows} windows back from {latest} ({a.days}d)...")

    from concurrent.futures import ThreadPoolExecutor
    rows = []
    done = 0
    with ThreadPoolExecutor(max_workers=12) as ex:
        for r in ex.map(fetch_window, starts):
            done += 1
            if r:
                rows.append(r)
            if done % 100 == 0:
                print(f"  {done}/{n_windows} fetched, {len(rows)} usable")
    rows.sort(key=lambda r: r["ws"])
    print(f"\nUsable windows: {len(rows)}  (up_won rate = {round(sum(r['up_won'] for r in rows)/len(rows),3) if rows else None})")

    for key, label in (("q_a", "(a) line=Chainlink P2B, price=Binance@cp"),
                       ("q_b", "(b) line=Binance start,   price=Binance@cp")):
        pairs = [(r[key], r["up_won"]) for r in rows if r[key] is not None]
        if not pairs:
            print(f"\n{label}: no usable predictions"); continue
        base = [(0.5, y) for _, y in pairs]
        # negative control: keep predictions, shuffle labels deterministically (reverse order)
        ys = [y for _, y in pairs]
        shuf = list(zip([q for q, _ in pairs], ys[::-1]))
        bm, bb, bn = brier(pairs), brier(base), brier(shuf)
        # directional accuracy: predict Up when q>0.5
        acc = sum(1 for q, y in pairs if (q >= 0.5) == (y == 1)) / len(pairs)
        print(f"\n=== {label}  (n={len(pairs)}) ===")
        print(f"  Brier(model)={bm:.4f}   Brier(0.5 baseline)={bb:.4f}   "
              f"Brier(shuffled-label control)={bn:.4f}")
        print(f"  skill vs baseline = {(bb-bm):+.4f}   directional accuracy = {acc:.3f}")
        print(f"  calibration [lo,hi,n,mean_q,realized]:")
        for c in calibration(pairs):
            print(f"    {c}")


if __name__ == "__main__":
    raise SystemExit(main())

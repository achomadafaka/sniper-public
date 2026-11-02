"""Market-structure context for a Pump curve coin, from the local U tape only (pure: no I/O, no clock, no network).

Operator order 2026-10-22 (option a): the JUDGE stays off until it can read the chart. One function serves the JUDGE (at its as-of
claim) and the RISK watcher (every cycle): structure(swaps, as_of=, cut_slot=, ...) -> a closed dict of numbers and enums.

WINDOW RULE: a window of W seconds is [as_of - W, as_of) on received_at (lower edge included, as_of excluded), AND every row must
have slot < cut_slot. Bars are fixed 5 s buckets over the same half-open ranges; the chart covers [as_of - 120, as_of).
Rules:
  * strict as-of: only swaps with received_at < as_of AND slot < cut_slot are read; a later swap can never change the output;
  * our own wallet and our fill transactions are dropped first (ours / ours_sigs), so our entry never reads as demand;
  * unknown is not zero: a window with a tape gap, or with too few priced swaps for a figure, reports None with a reason; a healthy
    window with no trades reports volume 0 (known) and no price figures (None);
  * no text of any kind: numbers, booleans, short enums; nothing a coin's author wrote can reach a prompt;
  * the suggested rails are ADVISORY (the caller's operator bounds clamp them); they never widen what the engine allows.
Prices are USD per token from the curve's virtual reserves after each swap (swap['price_usd']); volumes are USD.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

VERSION = "structure-v1"
VERSION_V2 = "structure-v2"          # the facts sheet (Params.sheet == "v2"); v1 output stays byte-identical
VERSIONS = (VERSION, VERSION_V2)
# The Mayhem agent trades fee-free and is most of the flow on mayhem coins (73% of the swaps on the 19 mayhem coins of 2026-10-22). Its trades
# stay in the PRICE path (they move the curve) but never count as FLOW (volume, buyers, sellers, control, breakout volume, chart volume):
# the sheet reports them apart as agent_buy_usd / agent_sell_usd. Pinned equal to research/state.MAYHEM_AGENT_IDS by a test.
AGENTS = frozenset(("BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s",))
WINDOWS = (15, 60, 300)
# the closed vocabulary of every structure() output (the JUDGE sheet validator accepts nothing else)
ALLOWED_KEYS = frozenset("""version tape reason as_of cut_slot price_usd last_swap_age_s windows 15s 60s 300s launch state n_swaps
    buy_usd sell_usd net_delta_usd buyers sellers buy_share_trades_pct top3_trade_share_pct high low range_pct pos_in_range_pct
    return_pct trend slope_pct_per_min strength higher_highs higher_lows label delta_trend volume_accel swings resistance_above_pct
    support_below_pct breakout broke seconds_since holding volume_vs_avg volatility median_5s_swing_pct bars median_5s_true_range_pct true_range_bars chart rails sl_pct
    tp_pct trail_pct basis sl tp trail advisory meta tape_cut read_margin_s sol_usd launch_known swaps_read gaps_in_range n_agent_swaps agent_buy_usd agent_sell_usd launch_truncated
    chart_coverage swaps_lost flow_is_lower_bound""".split()) | frozenset("""sheet crossed seconds_since_cross trades_above_after_cross regular_buy_usd_after
    sell_usd_after_cross recovered_after_sell cross_by recent_volume_concentration_15_of_60 recent_volume_concentration_reason history_s
    abs_slope_pct_per_min covered_s partial own_pool_losses unattributed_loss_receipts""".split()) | frozenset("""qualify buyer_growth_60 creator_sold_share buy_share_usd_60
    crowd_60 crowd_300_max reentry kind since_exit_s entry_fill_price_usd exit_price_usd realized_pnl now_vs_exit_pct
    post_exit_high_pct post_exit_low_pct high_vs_now_pct pullback_range5 entry_state pos300 breakout_age_s base_s
    breakout_vol""".split())     # sheet = structure-v2 facts sheet (the first frozenset after the base); v23 facts (memecoin_desk_v23_facts)
ALLOWED_STRINGS = frozenset("""structure-v1 ok unknown up down flat rising falling tape_gap no_swaps no_reference support
    resistance volatility range_extension tape_stale heartbeat_unreadable sol_usd_unknown no_block_before_as_of tape_unreadable
    structure_error structure_invalid not_applicable beyond_lookback ours_unknown swaps_lost pool_unread win soft_loss structure-v2 facts-v2
    insufficient_history no_volume_60 regular agent mixed""".split())
LOST_BLIND = 3      # a window with this many lost swaps is blind: more than one can hide a non-monotone path (Gamma)
ALLOWED_PREFIXES = ("tape_unreadable:",)


@dataclass(frozen=True)
class Params:
    bar_s: int = 5
    chart_s: int = 120
    recent_s: float = 15.0               # the breakout reference high excludes this recent stretch
    flat_pct_per_min: float = 2.0        # |slope| under this is 'flat'
    min_prices: int = 2
    sl_vol_k: float = 4.0                # stop = k x median 5 s swing, when there is no support
    sl_support_buffer: float = 1.1       # stop = distance to support x buffer
    trail_vol_k: float = 3.0
    tp_extension: float = 0.5            # take profit = this x the 300 s range when there is no resistance
    sheet: str = "v1"                    # "v2" = the facts sheet (breakout facts, concentration + history_s, abs_slope, covered_s/partial); default v1 unchanged


def _num(x):
    return type(x) in (int, float) and math.isfinite(x)


def _pct(a, b):
    return (a / b - 1) * 100 if _num(a) and _num(b) and b > 0 else None


def _r(x, nd=4):
    return None if x is None else round(x, nd)


def _usable(swaps, as_of, cut_slot, ours, ours_sigs):
    rows = [s for s in swaps if s["received_at"] < as_of and s["slot"] < cut_slot
            and s["actor"] not in ours and s["signature"] not in ours_sigs]
    rows.sort(key=lambda s: (s["received_at"], s["slot"], s["ordinal"]))
    return rows


def _flow(s, agents):
    """A swap that counts as FLOW: not the Mayhem agent and not a price-only print (a lost PumpSwap swap's reserve state)."""
    return not s.get("price_only") and s["actor"] not in agents


def _bars(rows, lo, hi, bar_s, agents=frozenset()):
    """Fixed bars [lo + i*bar_s, ...) up to hi. A bar with no priced swap has no OHLC (None), volume 0 (known)."""
    import bisect
    times = [s["received_at"] for s in rows]              # rows are sorted by received_at
    n = int(math.ceil((hi - lo) / bar_s))
    out = []
    for i in range(n):
        a, b = lo + i * bar_s, lo + (i + 1) * bar_s
        inside = rows[bisect.bisect_left(times, a):bisect.bisect_left(times, b)]
        prices = [s["price_usd"] for s in inside if _num(s.get("price_usd"))]
        out.append({"t": a, "o": prices[0] if prices else None, "h": max(prices) if prices else None,
                    "l": min(prices) if prices else None, "c": prices[-1] if prices else None,
                    "buy_usd": sum(s["usd"] for s in inside if s["is_buy"] and _flow(s, agents)),
                    "sell_usd": sum(s["usd"] for s in inside if not s["is_buy"] and _flow(s, agents)),
                    "makers": len({s["actor"] for s in inside if _flow(s, agents)})})
    return out


def _slope_pct_per_min(points):
    """Least-squares slope of price over time as % of the mean price per minute; None under 2 distinct times."""
    if len(points) < 2:
        return None
    t0 = points[0][0]
    xs, ys = [p[0] - t0 for p in points], [p[1] for p in points]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0 or my <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den / my * 100 * 60


def gap_hit(gaps, lo, hi):
    """`gaps` holds floats (a point gap at that time) and (start, end) tuples (a capture stop span, closed ends). A window [lo, hi] is blind to a point inside it AND to any
    span that overlaps it: a stop covering the whole window leaves no point inside it (RC#2 item 3)."""
    return any((g[0] <= hi and g[1] >= lo) if isinstance(g, tuple) else lo <= g <= hi for g in gaps)


def _window(rows, as_of, w, gaps, params, launched_at=None, agents=frozenset(), lost=(), first_obs=None):
    lo = as_of - w if launched_at is None else launched_at
    if gap_hit(gaps, lo, as_of):
        return {"state": "unknown", "reason": "tape_gap"}
    n_lost = sum(1 for g in lost if lo <= g <= as_of)
    if n_lost >= LOST_BLIND:
        return {"state": "unknown", "reason": "swaps_lost"}
    everything = [s for s in rows if lo <= s["received_at"] < as_of]
    priced = [(s["received_at"], s["price_usd"]) for s in everything if _num(s.get("price_usd")) and s["price_usd"] > 0]   # PRICE: all
    inside = [s for s in everything if _flow(s, agents)]                                                                  # FLOW: not the agent
    agent = [s for s in everything if not s.get("price_only") and s["actor"] in agents]
    buys = [s for s in inside if s["is_buy"]]
    sells = [s for s in inside if not s["is_buy"]]
    buy_usd, sell_usd = sum(s["usd"] for s in buys), sum(s["usd"] for s in sells)
    top3 = sorted((s["usd"] for s in inside), reverse=True)[:3]
    total = buy_usd + sell_usd
    out = {"state": "ok", "reason": None, "n_swaps": len(inside), "buy_usd": _r(buy_usd, 2), "sell_usd": _r(sell_usd, 2),
           "net_delta_usd": _r(buy_usd - sell_usd, 2), "buyers": len({s["actor"] for s in buys}),
           "sellers": len({s["actor"] for s in sells}),
           "buy_share_trades_pct": _r(len(buys) / len(inside) * 100) if inside else None,
           "top3_trade_share_pct": _r(sum(top3) / total * 100) if total > 0 else None,
           "n_agent_swaps": len(agent), "agent_buy_usd": _r(sum(s["usd"] for s in agent if s["is_buy"]), 2),
           "agent_sell_usd": _r(sum(s["usd"] for s in agent if not s["is_buy"]), 2)}
    if params.sheet == "v2":
        # observed seconds of THIS window: never more than the tape we hold (first_obs = our first usable swap); a zero in a partial window is an
        # observed zero, not a proven full-window absence
        nominal = as_of - lo
        raw = None if first_obs is None else max(0.0, min(nominal, as_of - first_obs))
        out["covered_s"] = None if raw is None else _r(raw, 2)                 # shown rounded, decided on the raw seconds
        out["partial"] = None if raw is None else raw < nominal
    if lost:                                       # only a reader that can lose swaps (PumpSwap) passes `lost`
        out.update(swaps_lost=n_lost, flow_is_lower_bound=n_lost > 0)
    if len(priced) < params.min_prices:
        out.update(high=None, low=None, range_pct=None, pos_in_range_pct=None, return_pct=None, trend=None, delta_trend=None)
        return out
    hi, lo_p = max(p for _, p in priced), min(p for _, p in priced)
    last, first = priced[-1][1], priced[0][1]
    out.update(high=hi, low=lo_p, range_pct=_r(_pct(hi, lo_p)), pos_in_range_pct=_r((last - lo_p) / (hi - lo_p) * 100) if hi > lo_p else None,
               return_pct=_r(_pct(last, first)))
    bars = [b for b in _bars(rows, lo, as_of, params.bar_s, agents) if b["h"] is not None]
    hh = sum(1 for a, b in zip(bars, bars[1:]) if b["h"] > a["h"])
    hl = sum(1 for a, b in zip(bars, bars[1:]) if b["l"] > a["l"])
    slope = _slope_pct_per_min(priced)
    label = None if slope is None else ("up" if slope > params.flat_pct_per_min else "down" if slope < -params.flat_pct_per_min else "flat")
    if params.sheet == "v2":
        out["trend"] = {"slope_pct_per_min": _r(slope), "abs_slope_pct_per_min": None if slope is None else _r(abs(slope)),
                        "higher_highs": hh, "higher_lows": hl, "label": label}
    else:
        out["trend"] = {"slope_pct_per_min": _r(slope), "strength": None if slope is None else _r(abs(slope)),
                        "higher_highs": hh, "higher_lows": hl, "label": label}
    half = lo + (as_of - lo) / 2
    d1 = sum(s["usd"] if s["is_buy"] else -s["usd"] for s in inside if s["received_at"] < half)
    d2 = sum(s["usd"] if s["is_buy"] else -s["usd"] for s in inside if s["received_at"] >= half)
    out["delta_trend"] = "rising" if d2 > d1 else "falling" if d2 < d1 else "flat"
    return out


def _true_ranges(bars):
    """Per priced 5 s bar after the first: ln-free percent true range max(h, prev_close) - min(l, prev_close), prev_close = close of the
    previous PRICED bar (a quiet bar carries nothing). Needs >= 2 priced bars; a single-swap bar still shows its move from the last print."""
    out, prev = [], None
    for b in bars:
        if b["h"] is None or not (b["l"] and b["l"] > 0):
            continue
        if prev is not None:
            r = _pct(max(b["h"], prev), min(b["l"], prev))
            if r is not None:
                out.append(r)
        prev = b["c"]
    return out


def _swings(bars):
    """Pivot highs / lows on the bar series: a bar strictly above (below) both neighbours."""
    ok = [b for b in bars if b["h"] is not None]
    highs = [b["h"] for a, b, c in zip(ok, ok[1:], ok[2:]) if b["h"] > a["h"] and b["h"] > c["h"]]
    lows = [b["l"] for a, b, c in zip(ok, ok[1:], ok[2:]) if b["l"] < a["l"] and b["l"] < c["l"]]
    return highs, lows


def _breakout(rows, as_of, w, price, params, bar_s, agents=frozenset()):
    ref = [s["price_usd"] for s in rows if as_of - w <= s["received_at"] < as_of - params.recent_s and _num(s.get("price_usd"))]
    if not ref or price is None:
        return {"state": "unknown", "reason": "no_reference"}
    prior = max(ref)
    window = [s for s in rows if as_of - w <= s["received_at"] < as_of and _num(s.get("price_usd"))]
    cross = next((s for s in window if s["received_at"] >= as_of - params.recent_s and s["price_usd"] > prior), None)
    if cross is None:
        return {"state": "ok", "reason": None, "broke": False, "seconds_since": None, "holding": None, "volume_vs_avg": None}
    since = [s["price_usd"] for s in window if s["received_at"] >= cross["received_at"]]
    bars = _bars(rows, as_of - w, as_of, bar_s, agents)
    vols = [b["buy_usd"] + b["sell_usd"] for b in bars]
    idx = min(len(bars) - 1, int((cross["received_at"] - (as_of - w)) // bar_s))
    avg = sum(vols) / len(vols) if vols else 0
    return {"state": "ok", "reason": None, "broke": True, "seconds_since": _r(as_of - cross["received_at"], 2),
            "holding": min(since) > prior, "volume_vs_avg": _r(vols[idx] / avg) if avg > 0 else None}


def _breakout_v2(rows, as_of, w, price, params, agents=frozenset(), lost=()):
    """Facts, not a verdict. Cross = the first priced swap (sorted order) in the last recent_s above the window's earlier high (strictly above). After = strictly
    later in the (received_at, slot, ordinal) order up to as_of. Flow = not agent, not price_only; a price-only print or an agent trade moves the PRICE path
    (trades_above_after_cross) but is never regular flow and never a recovery trigger."""
    if any(as_of - w <= g <= as_of for g in lost):                 # ANY known-lost swap in the window can change the reference high, the cross, a sell or an agent: no
        return {"state": "unknown", "reason": "swaps_lost"}       # field of this block has a provable bound, so the whole block is unknown (smallest honest rule, Zeta)
    ref = [s["price_usd"] for s in rows if as_of - w <= s["received_at"] < as_of - params.recent_s and _num(s.get("price_usd"))]
    if not ref or price is None:
        return {"state": "unknown", "reason": "no_reference"}
    prior = max(ref)
    inwin = [s for s in rows if as_of - w <= s["received_at"] < as_of]
    cross = next((s for s in inwin if s["received_at"] >= as_of - params.recent_s and _num(s.get("price_usd")) and s["price_usd"] > prior), None)
    if cross is None:
        return {"state": "ok", "reason": None, "crossed": False, "seconds_since_cross": None, "trades_above_after_cross": None,
                "regular_buy_usd_after": None, "sell_usd_after_cross": None, "recovered_after_sell": None, "cross_by": None}
    i = next(k for k, s in enumerate(inwin) if s is cross)
    before, after = inwin[:i], inwin[i + 1:]
    above = sum(1 for s in after if _num(s.get("price_usd")) and s["price_usd"] > prior)
    buy = sum(s["usd"] for s in after if s["is_buy"] and _flow(s, agents))
    sell = sum(s["usd"] for s in after if not s["is_buy"] and _flow(s, agents))
    first_sell = next((k for k, s in enumerate(after) if not s["is_buy"] and _flow(s, agents)), None)
    recovered = None if first_sell is None else any(_num(s.get("price_usd")) and s["price_usd"] > prior for s in after[first_sell + 1:])
    if cross.get("price_only") or not cross.get("actor"):
        by = "unknown"
    elif cross["actor"] in agents:
        by = "agent"
    elif any(s["actor"] in agents and cross["received_at"] - params.bar_s <= s["received_at"] for s in before if not s.get("price_only")):
        by = "mixed"
    else:
        by = "regular"
    return {"state": "ok", "reason": None, "crossed": True, "seconds_since_cross": _r(as_of - cross["received_at"], 2),
            "trades_above_after_cross": above, "regular_buy_usd_after": _r(buy, 2), "sell_usd_after_cross": _r(sell, 2),
            "recovered_after_sell": recovered, "cross_by": by}


def structure(swaps, *, as_of, cut_slot, ours=frozenset(), ours_sigs=frozenset(), launched_at=None, gaps=(), blind=None,
              bounds=None, params=Params(), launch_unknown=None, agents=AGENTS, lost=()):
    """The structure dict at `as_of`. `blind` = a reason string when the caller already knows the tape is unusable (stale
    heartbeat, unreadable file): then NO figure is produced, only {'tape': 'unknown', 'reason': blind}. `gaps` = tape gap
    times (epoch) inside the longest look-back; a window containing one is unknown. `bounds` = the operator rail bounds
    {'sl_pct': (lo, hi), 'tp_pct': ..., 'trail_pct': ...} the advisory rails are clamped to."""
    if blind:
        return {"version": VERSION, "tape": "unknown", "reason": str(blind)}
    p = params
    rows = _usable(swaps, as_of, cut_slot, ours, ours_sigs)
    priced = [s for s in rows if _num(s.get("price_usd")) and s["price_usd"] > 0]
    price = priced[-1]["price_usd"] if priced else None
    v2 = p.sheet == "v2"
    if p.sheet not in ("v1", "v2"):
        raise ValueError("unsupported sheet %r" % (p.sheet,))
    first_obs = rows[0]["received_at"] if rows else None          # coverage starts at our first usable swap; a launch time never extends it
    out = {"version": VERSION_V2 if v2 else VERSION, "tape": "ok", "reason": None, "as_of": as_of, "cut_slot": cut_slot,
           "price_usd": price, "last_swap_age_s": _r(as_of - priced[-1]["received_at"], 3) if priced else None, "windows": {}}
    if v2:
        out["sheet"] = "facts-v2"
    for w in WINDOWS:
        out["windows"]["%ds" % w] = _window(rows, as_of, w, gaps, p, agents=agents, lost=lost, first_obs=first_obs)
    first = rows[0]["received_at"] if rows else None
    start = launched_at if launched_at is not None else first
    out["windows"]["launch"] = ({"state": "unknown", "reason": str(launch_unknown)} if launch_unknown
                                else _window(rows, as_of, as_of - start, gaps, p, launched_at=start, agents=agents, lost=lost, first_obs=first_obs)
                                if start is not None
                                else {"state": "unknown", "reason": "no_swaps"})
    v15, v60 = out["windows"]["15s"], out["windows"]["60s"]
    vol = lambda x: None if x.get("state") != "ok" else x["buy_usd"] + x["sell_usd"]
    a15, a60 = vol(v15), vol(v60)
    if v2:
        raw_hist = None if first_obs is None else as_of - first_obs
        hist = None if raw_hist is None else _r(raw_hist, 2)
        out["history_s"] = hist
        reason = (v15 if v15.get("state") != "ok" else v60).get("reason") if "ok" != v15.get("state") or "ok" != v60.get("state") else None
        if reason is None and (raw_hist is None or raw_hist < 60):
            reason = "insufficient_history"
        if reason is None and not a60:
            reason = "no_volume_60"
        out["recent_volume_concentration_15_of_60"] = None if reason else _r(a15 / (a60 / 4))      # [0, 4]: the 15 s interval sits inside the 60 s one
        out["recent_volume_concentration_reason"] = reason
    else:
        out["volume_accel"] = _r(a15 / (a60 / 4)) if a15 is not None and a60 else None      # 15 s volume vs the 60 s average
    # bars and swings over the last 300 s
    long_bars = _bars(rows, as_of - 300, as_of, p.bar_s, agents) if not gap_hit(gaps, as_of - 300, as_of) else None
    highs, lows = _swings(long_bars) if long_bars else ([], [])
    above = [h for h in highs if price is not None and h > price]
    below = [l for l in lows if price is not None and l < price]
    res, sup = (min(above) if above else None), (max(below) if below else None)
    out["swings"] = {"state": "ok" if long_bars else "unknown", "reason": None if long_bars else "tape_gap",
                     "resistance_above_pct": _r(_pct(res, price)), "support_below_pct": _r(-_pct(sup, price)) if sup else None}
    bk = (lambda w: _breakout_v2(rows, as_of, w, price, p, agents, lost)) if v2 else (lambda w: _breakout(rows, as_of, w, price, p, p.bar_s, agents))
    out["breakout"] = {"60s": bk(60), "300s": bk(300)} \
        if not gap_hit(gaps, as_of - 300, as_of) else {"60s": {"state": "unknown", "reason": "tape_gap"},
                                                                  "300s": {"state": "unknown", "reason": "tape_gap"}}
    swing = [_pct(b["h"], b["l"]) for b in (long_bars or []) if b["h"] is not None and b["l"] and b["l"] > 0]
    med = statistics.median(swing) if len(swing) >= 3 else None
    tr = _true_ranges(long_bars or [])
    tr_med = statistics.median(tr) if tr else None
    # swing/bars/rails stay the raw within-bar median (legacy cohorts byte-identical; a single-swap bar reads 0.0 there). The true range
    # (carries the move from the previous print; 0 = unknown) is the NEW noise figure; the judge's structure_fn drops it for cohorts
    # whose frozen policy does not read it.
    out["volatility"] = {"median_5s_swing_pct": _r(med), "bars": len(swing),
                         "median_5s_true_range_pct": _r(tr_med) or None, "true_range_bars": len(tr)}
    chart_bars = _bars(rows, as_of - p.chart_s, as_of, p.bar_s, agents)
    bp = lambda x: None if x is None or price is None else int(round((x / price - 1) * 10000))
    out["chart"] = [[int(b["t"] - as_of), bp(b["o"]), bp(b["h"]), bp(b["l"]), bp(b["c"]), int(round(b["buy_usd"])),
                     int(round(b["sell_usd"])), b["makers"]] for b in chart_bars] if not gap_hit(gaps, as_of - p.chart_s, as_of) else None
    out["rails"] = _rails(price, sup, res, med, out["windows"]["300s"], bounds, p)
    return out


def _clamp(x, rng):
    return x if rng is None or x is None else max(rng[0], min(rng[1], x))


def _rails(price, support, resistance, vol, w300, bounds, p):
    """Advisory rails, percent, clamped to the operator bounds; None when the structure cannot support a figure."""
    b = bounds or {}
    sl = tp = trail = None
    basis = {}
    if price and support:
        sl, basis["sl"] = (price - support) / price * 100 * p.sl_support_buffer, "support"
    elif vol is not None:
        sl, basis["sl"] = vol * p.sl_vol_k, "volatility"
    if price and resistance:
        tp, basis["tp"] = (resistance - price) / price * 100, "resistance"
    elif w300.get("state") == "ok" and w300.get("range_pct") is not None:
        tp, basis["tp"] = w300["range_pct"] * p.tp_extension, "range_extension"
    if vol is not None:
        trail, basis["trail"] = vol * p.trail_vol_k, "volatility"
    return {"sl_pct": _r(_clamp(sl, b.get("sl_pct"))), "tp_pct": _r(_clamp(tp, b.get("tp_pct"))),
            "trail_pct": _r(_clamp(trail, b.get("trail_pct"))), "basis": basis, "advisory": True}

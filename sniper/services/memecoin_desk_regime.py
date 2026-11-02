"""REGIME of a coin right now, from its own curve tape, in its own noise units (observe-only context block for the JUDGE and RISK sheets).

classify(buckets, open_idx, n5, ...) is pure: closed 5 s buckets in, one compact block out. regime_for(mint, desk_dir, as_of) reads the curve
swaps tape STRICTLY before as_of (closed buckets only) and calls it. Nothing here trades, sizes, gates or changes a rail.

Regimes (precedence in this order): unknown, quiet, dying, impulse, trend, consolidation, range_chop, mixed.
  quiet          the coin's median 5 s range over the last 300 s is ZERO: flat/dead (Gamma's first cut: 91% of in-band grid time). A real
                 observation, not a missing one; it is the regime to ABSTAIN on.
  impulse        fast directional move: |displacement over 30 s| >= IMPULSE_SIGMA * sqrt(6) noise units and path efficiency >= 0.6.
  trend          sustained: |displacement over 60 s| >= TREND_SIGMA * sqrt(12) noise units and path efficiency >= 0.35.
  consolidation  tight: the 30 s range <= sqrt(6) noise units (feed C's definition) while the coin is still trading (activity >= 0.5).
  range_chop     a two-sided range: small net move, low efficiency, a band >= 2 noise units wide with >= 2 touches on each edge and
                 two-sided flow. The block carries the band (lo, hi, touches, buy share).
  dying          the flow collapsed against the coin's OWN history (activity <= 0.25), not merely tight.
  mixed          none of the above (stated, never forced into one).
  unknown        noise unknown, thin window, a capture gap, a graduated coin (the curve tape ends there: NOT dying), unreadable tape.

All thresholds are multiples of the coin's own noise or random-walk sqrt(n) scalings, are PROVISIONAL (no fit yet) and are echoed in every
block so a later study can refit them. Absent / zero / unknown: a quiet bucket is 0 in the noise and in activity; an unknown input gives
regime 'unknown' with a reason, never a default regime.
"""
from __future__ import annotations

import math
import sqlite3
import statistics
from pathlib import Path

from sniper.services import memecoin_desk_tape_shape as TS

VERSION = "regime-v0-provisional"
BUCKET_S = 5
WIN_LONG = 12                  # 60 s: trend / range window
WIN_SHORT = 6                  # 30 s: impulse / consolidation window
MIN_PRESENT = 8                # of the 12 long-window slots must have trades, else the window is thin
IMPULSE_SIGMA, IMPULSE_EFF = 2.0, 0.6      # PROVISIONAL
TREND_SIGMA, TREND_EFF = 2.0, 0.35         # PROVISIONAL
CHOP_MIN_WIDTH_N = 2.0                     # band at least two noise units wide, else it is a consolidation
CHOP_TOUCHES = 2
CHOP_FLOW = (0.35, 0.65)                   # PROVISIONAL buy-share band that counts as two-sided
DYING_ACTIVITY = 0.25                      # PROVISIONAL
CONS_ACTIVITY = 0.5                        # PROVISIONAL
NOISE_WINDOW_S = 330.0                     # tape read: the 300 s noise window plus one 30 s block of margin
THRESHOLDS = {"impulse": [IMPULSE_SIGMA, IMPULSE_EFF], "trend": [TREND_SIGMA, TREND_EFF], "chop_flow": list(CHOP_FLOW),
              "dying_activity": DYING_ACTIVITY, "cons_activity": CONS_ACTIVITY, "provisional": True}


def _unknown(reason, n5=None):
    return {"version": VERSION, "regime": "unknown", "reason": reason, "n5": None if n5 is None else round(n5, 5)}


def _closes(by, lo, hi):
    """Closes for slots lo..hi, a quiet slot carrying the previous close forward, SEEDED from the newest known close before lo (a coin quiet at
    the start of the window still has a known price); None only before the coin's first trade."""
    out, last = [], None
    earlier = [i for i in by if i < lo]
    if earlier:
        last = by[max(earlier)][4]
    for i in range(lo, hi + 1):
        b = by.get(i)
        if b is not None:
            last = b[4]
        out.append(last)
    return out


def _efficiency(closes):
    pts = [c for c in closes if c is not None and c > 0]
    if len(pts) < 2:
        return None
    path = sum(abs(math.log(b / a)) for a, b in zip(pts, pts[1:]))
    return abs(math.log(pts[-1] / pts[0])) / path if path > 0 else 0.0     # closes never moved: no directional efficiency (0), not unknown


def _activity(buckets, last, first_idx):
    """Trades in the last 30 s over the coin's own median 30 s block count across the trailing 300 s (quiet blocks 0); None = unknown."""
    by = {b[0]: b for b in buckets}
    blocks = []
    for k in range(10):
        hi = last - k * WIN_SHORT
        lo = hi - WIN_SHORT + 1
        if lo < first_idx:
            break
        blocks.append(sum((by[i][7] + by[i][8]) for i in range(lo, hi + 1) if i in by))
    if len(blocks) < 3:
        return None
    med = statistics.median(blocks)
    return None if med <= 0 else blocks[0] / med


def classify(buckets, open_idx, n5, first_idx, *, gap_in_window=False, graduated=False, noise_why="ok"):
    """The compact block. `buckets` = the coin's tracker-style buckets [idx, o, h, l, c, buy, sell, n_buy, n_sell, ...] (any superset of the
    closed ones; open and future buckets are ignored), `first_idx` = the first clean bucket, n5 = TS.range5(...) or None."""
    if graduated:
        return _unknown("graduated")
    if gap_in_window:
        return _unknown("capture_gap", n5)
    if n5 is None:
        if noise_why == "zero":
            # A zero MEDIAN range is an observation about 5 s bars, not proof that the price did not move: quiet only when the closes of every
            # present bucket in the window are identical (no inter-bucket motion either); otherwise the noise unit is unknown.
            last_ = open_idx - 1
            win = [b for b in buckets if first_idx <= b[0] <= last_ and b[0] > last_ - TS.NOISE_B]
            closes = [b[4] for b in win]
            # FULL OHLC: every present bucket must have high == low (no wick) AND all closes identical (no inter-bucket motion)
            if closes and max(closes) == min(closes) and all(b[2] == b[3] for b in win):
                return {"version": VERSION, "regime": "quiet", "dir": None, "n5": None, "window_slots": min(TS.NOISE_B, last_ - first_idx + 1),
                        "asof_idx": last_, "thresholds": THRESHOLDS}
            return _unknown("noise_zero_with_motion")
        return _unknown("noise_unknown")
    last = open_idx - 1
    by = {b[0]: b for b in buckets if b[0] <= last}
    long_lo = last - WIN_LONG + 1
    present = [i for i in range(long_lo, last + 1) if i in by]
    if long_lo - 1 < first_idx or len(present) < MIN_PRESENT:
        return _unknown("thin_window", n5)
    cl = _closes(by, long_lo - 1, last)               # 13 closes: the close BEFORE the window starts it, so 12 intervals = 60 s, 6 = 30 s
    if cl[-1] is None or cl[0] is None:
        return _unknown("thin_window", n5)
    c12, c6 = cl, cl[WIN_LONG - WIN_SHORT:]
    d12 = math.log(c12[-1] / c12[0]) / n5
    d6 = math.log(c6[-1] / c6[0]) / n5
    e12, e6 = _efficiency(c12), _efficiency(c6)
    win12 = [by[i] for i in present]
    win6 = [by[i] for i in range(last - WIN_SHORT + 1, last + 1) if i in by]
    hi12, lo12 = max(b[2] for b in win12), min(b[3] for b in win12)
    r12 = math.log(hi12 / lo12) / n5
    r6 = math.log(max(b[2] for b in win6) / min(b[3] for b in win6)) / n5 if win6 else None
    buy, sell = sum(b[5] for b in win12), sum(b[6] for b in win12)
    buy_share = buy / (buy + sell) if buy + sell > 0 else None
    act = _activity(buckets, last, first_idx)
    out = {"version": VERSION, "regime": "mixed", "dir": None, "n5": round(n5, 5), "d6_n": round(d6, 2), "d12_n": round(d12, 2),
           "eff6": None if e6 is None else round(e6, 2), "eff12": None if e12 is None else round(e12, 2), "range12_n": round(r12, 2),
           "range6_n": None if r6 is None else round(r6, 2), "activity": None if act is None else round(act, 2),
           "buy_share": None if buy_share is None else round(buy_share, 2), "present_12": len(present), "asof_idx": last,
           "thresholds": THRESHOLDS}
    if act is not None and act <= DYING_ACTIVITY:
        out["regime"] = "dying"
    elif e6 is not None and abs(d6) >= IMPULSE_SIGMA * math.sqrt(WIN_SHORT) and e6 >= IMPULSE_EFF:
        out["regime"], out["dir"] = "impulse", "up" if d6 > 0 else "down"
    elif e12 is not None and abs(d12) >= TREND_SIGMA * math.sqrt(WIN_LONG) and e12 >= TREND_EFF:
        out["regime"], out["dir"] = "trend", "up" if d12 > 0 else "down"
    elif r6 is not None and r6 <= math.sqrt(WIN_SHORT) and act is not None and act >= CONS_ACTIVITY:
        out["regime"] = "consolidation"
    elif (e12 is not None and e12 < TREND_EFF and abs(d12) <= math.sqrt(WIN_LONG) and r12 >= CHOP_MIN_WIDTH_N
          and buy_share is not None and CHOP_FLOW[0] <= buy_share <= CHOP_FLOW[1]):
        top_touch = sum(1 for b in win12 if math.log(hi12 / b[2]) <= n5)
        bot_touch = sum(1 for b in win12 if math.log(b[3] / lo12) <= n5)
        if top_touch >= CHOP_TOUCHES and bot_touch >= CHOP_TOUCHES:
            out["regime"] = "range_chop"
            out["band"] = {"lo": lo12, "hi": hi12, "touches_lo": bot_touch, "touches_hi": top_touch, "buy_share": round(buy_share, 2)}
    return out


# ---------------------------------------------------------------------------------------------------------------- the tape read

def buckets_from_swaps(rows):
    """Curve swap rows (received_at, is_buy, quote_atomic, actor, virtual_quote, virtual_token) -> tracker-style 5 s buckets, in time order."""
    out = {}
    for r in sorted(rows, key=lambda x: x["received_at"]):
        if not r.get("virtual_quote") or not r.get("virtual_token"):
            continue
        px = (r["virtual_quote"] / 1e9) / (r["virtual_token"] / 1e6)
        sol = (r.get("quote_atomic") or 0) / 1e9
        idx = int(r["received_at"] // BUCKET_S)
        b = out.get(idx)
        if b is None:
            b = out[idx] = [idx, px, px, px, px, 0.0, 0.0, 0, 0, 0.0, 0.0]
        b[2], b[3], b[4] = max(b[2], px), min(b[3], px), px
        if r["is_buy"]:
            b[5] += sol
            b[7] += 1
        else:
            b[6] += sol
            b[8] += 1
    return [out[i] for i in sorted(out)]


GRAD_REAL_QUOTE = 84_000_000_000       # lamports of real SOL at curve completion (memecoin_desk_ps_tape.GRADUATION_REAL_SOL)
TAPE_ALIVE_S = 30.0                    # an affirmative 'quiet' needs the swaps tape alive within this long of the cutoff (any coin)
LOOKBACK_S = 86400.0                   # a coin seen on the curve within a day is a coin that existed (graduation and prior observation)


def regime_for(mint, *, desk_dir, as_of, check_gaps=True):
    """The block for `mint` as of tape time `as_of` (seconds, the swaps tape's host clock), using only swaps received strictly before the
    open bucket's start. Any unreadable or unproven input = unknown with a reason; an affirmative 'quiet' needs a readable tape ALIVE up to the
    cutoff with no capture gap in the window and proof that the coin existed (a swap of it in the last 24 h)."""
    from sniper.services import memecoin_desk_structure_tape as T
    open_idx = int(as_of // BUCKET_S)
    hi = open_idx * float(BUCKET_S)
    lo = hi - NOISE_WINDOW_S
    try:
        rows, seen_tape, newest_any, last_known = [], False, None, None
        for day in T.days_between(lo, hi):
            conn = T.ro(Path(desk_dir) / "swaps" / ("%s.db" % day))
            if conn is None:
                continue
            seen_tape = True
            try:
                rows += [dict(zip(("received_at", "is_buy", "quote_atomic", "actor", "virtual_quote", "virtual_token"), r)) for r in conn.execute(
                    "SELECT received_at, is_buy, quote_atomic, actor, virtual_quote, virtual_token FROM swaps WHERE mint=? AND received_at>=? "
                    "AND received_at<? ORDER BY received_at", (mint, lo, hi))]
                top = conn.execute("SELECT received_at FROM swaps WHERE received_at<? ORDER BY slot DESC LIMIT 1", (hi,)).fetchone()
                if top and top[0] is not None:
                    newest_any = max(newest_any or 0.0, float(top[0]))
            finally:
                conn.close()
        for day in reversed(T.days_between(hi - LOOKBACK_S, hi)):                  # the coin's newest known curve state before the cutoff, window or not
            conn = T.ro(Path(desk_dir) / "swaps" / ("%s.db" % day))
            if conn is None:
                continue
            try:
                try:
                    r = conn.execute("SELECT received_at, real_quote FROM swaps WHERE mint=? AND received_at<? ORDER BY received_at DESC LIMIT 1",
                                     (mint, hi)).fetchone()
                except sqlite3.Error as exc:
                    if "no such column" in str(exc):
                        r = None                                                      # a day file from before real_quote: no proof either way
                    else:
                        raise
            finally:
                conn.close()
            if r is not None:
                last_known = r
                break
        gaps = T.gaps_between(desk_dir, lo, hi) if check_gaps else []
    except (T.TapeUnreadable, OSError, ValueError) as exc:
        return _unknown("tape_unreadable:%s" % type(exc).__name__)
    except Exception as exc:                                  # a malformed row or a SQL error: unknown, never a default regime
        return _unknown("tape_error:%s" % type(exc).__name__)
    if last_known is not None and type(last_known[1]) is int and last_known[1] >= GRAD_REAL_QUOTE:
        return _unknown("graduated")                          # the NEWEST known curve state decides, whatever the feature window holds
    if not rows:
        if not seen_tape:
            return _unknown("no_tape")
        if gaps:
            return _unknown("capture_gap")                    # rows may have been lost in the gap: silence proves nothing
        if newest_any is None or hi - newest_any > TAPE_ALIVE_S:
            return _unknown("tape_stale")                     # the capture is not proven alive up to the cutoff
        if last_known is None:
            return _unknown("no_prior_observation")           # a coin never seen on this tape is not 'quiet'
        return {"version": VERSION, "regime": "quiet", "dir": None, "n5": None, "reason": "no_swaps_300s", "asof_idx": open_idx - 1,
                "thresholds": THRESHOLDS}
    if newest_any is None or hi - newest_any > TAPE_ALIVE_S:
        return _unknown("tape_stale")                         # the capture is not proven alive up to the cutoff: missing slots are not zero-filled into a regime
    buckets = buckets_from_swaps(rows)
    first = max(buckets[0][0], int(max(gaps) // BUCKET_S) + 1 if gaps else 0)
    in_window = bool(gaps) and max(gaps) >= hi - WIN_LONG * BUCKET_S
    n5, why = TS.range5_detail(buckets, open_idx, first)
    return classify(buckets, open_idx, n5, first, gap_in_window=in_window, noise_why=why)

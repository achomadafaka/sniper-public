"""v23 JUDGE sheet facts (operator 2026-10-23): `qualify` (positive coin qualification facts, never a gate) and `reentry`
(the post-exit read of a re-entry ticket). Pure functions over the same as-of curve rows structure() reads: strict as-of,
our own trades and the Mayhem agent excluded from every count. Absent is None, never 0; a fact that cannot be read is None.

crowd_* counts curve wallets, not humans (Gamma's caveat): w60(s) = distinct wallets with any swap in (s - 60, s];
crowd_300_max = max of w60(s) over s = t - 10 j, j = 0..30 (docs/audits/2026-10-23-crowd-burst-abstain-preregistration.md)."""
from __future__ import annotations

import math

VERSION = "v23-facts-v1"
KEYS = frozenset("""qualify buyer_growth_60 creator_sold_share buy_share_usd_60 crowd_60 crowd_300_max reentry kind
    since_exit_s entry_fill_price_usd exit_price_usd realized_pnl now_vs_exit_pct post_exit_high_pct post_exit_low_pct
    high_vs_now_pct pullback_range5 win soft_loss entry_state pos300 breakout_age_s base_s breakout_vol""".split())


def _num(x):
    return type(x) in (int, float) and math.isfinite(x)


def _r(x, nd=4):
    return None if x is None else round(x, nd)


from sniper.services.memecoin_desk_structure import gap_hit as _gap      # point gaps and (start, end) stop spans; a span overlapping the window blinds it


def _w60(rows, s):
    return len({r["actor"] for r in rows if s - 60 < r["received_at"] <= s})


def qualify(rows, *, as_of, gaps=(), covered_from=None, creator=None, creator_whole_life=False):
    """rows: structure() rows (received_at, actor, is_buy, usd, tokens), ALREADY cut to < as_of with ours and the agent removed.
    covered_from = the earliest time the rows are complete from (None = unknown): a window starting earlier is None.
    creator / creator_whole_life: the launch's creator and whether rows hold the coin's whole life (else creator_sold_share
    is None: a partial origin cannot tell 'never sold' from 'sold before the read')."""
    def window_ok(lo):
        return covered_from is not None and lo >= covered_from and not _gap(gaps, lo, as_of)
    out = {"buyer_growth_60": None, "creator_sold_share": None, "buy_share_usd_60": None, "crowd_60": None,
           "crowd_300_max": None}
    if window_ok(as_of - 60):
        last = [r for r in rows if as_of - 60 <= r["received_at"] < as_of]
        buy = sum(r["usd"] for r in last if r["is_buy"])
        total = buy + sum(r["usd"] for r in last if not r["is_buy"])
        out["buy_share_usd_60"] = _r(buy / total * 100) if total > 0 else None
        out["crowd_60"] = _w60(rows, as_of)
    if window_ok(as_of - 120):
        now_b = {r["actor"] for r in rows if as_of - 60 <= r["received_at"] < as_of and r["is_buy"]}
        prior_b = {r["actor"] for r in rows if as_of - 120 <= r["received_at"] < as_of - 60 and r["is_buy"]}
        out["buyer_growth_60"] = _r(len(now_b) / len(prior_b)) if prior_b else None     # no prior buyers: never infinite
    if window_ok(as_of - 360):
        out["crowd_300_max"] = max(_w60(rows, as_of - 10 * j) for j in range(31))
    if creator is not None and creator_whole_life and not _gap(gaps, covered_from or as_of, as_of):
        bought = sum(r["tokens"] for r in rows if r["actor"] == creator and r["is_buy"] and _num(r.get("tokens")))
        sold = sum(r["tokens"] for r in rows if r["actor"] == creator and not r["is_buy"] and _num(r.get("tokens")))
        if any(r["actor"] == creator and not _num(r.get("tokens")) for r in rows):
            pass                                              # an unreadable creator swap: unknown, not a number
        elif bought > 0:
            out["creator_sold_share"] = _r(min(1.0, sold / bought))
    return out


def reentry(rows, close, *, as_of, gaps=(), range5_pct=None, covered_from=None):
    """close: the prior position's desk close output (realized_pnl, exit_price_usd, entry_fill_price_usd, closed_at epoch
    under 'closed_at_s'). rows as in qualify (priced: price_usd). Figures over the post-exit stretch (closed_at, as_of);
    a tape gap inside it makes the price path None. high_vs_now_pct is the floor a re-entry px_up must clear."""
    pnl, exit_px, entry_px, closed = (close.get(k) for k in ("realized_pnl", "exit_price_usd", "entry_fill_price_usd",
                                                              "closed_at_s"))
    out = {"kind": None if not _num(pnl) else "win" if pnl > 0 else "soft_loss",
           "since_exit_s": _r(as_of - closed, 1) if _num(closed) else None,
           "entry_fill_price_usd": entry_px if _num(entry_px) else None, "exit_price_usd": exit_px if _num(exit_px) else None,
           "realized_pnl": _r(pnl, 2) if _num(pnl) else None, "now_vs_exit_pct": None, "post_exit_high_pct": None,
           "post_exit_low_pct": None, "high_vs_now_pct": None, "pullback_range5": None}
    if (not _num(closed) or not _num(exit_px) or exit_px <= 0 or _gap(gaps, closed, as_of)
            or covered_from is None or closed < covered_from):
        return out                      # the whole post-exit stretch must be on this tape, in one price identity (eCat)
    path = [r["price_usd"] for r in rows if closed < r["received_at"] < as_of and _num(r.get("price_usd")) and r["price_usd"] > 0]
    if not path:
        return out
    hi, lo, now = max(path), min(path), path[-1]
    pct = lambda a, b: (a / b - 1) * 100
    out.update(now_vs_exit_pct=_r(pct(now, exit_px)), post_exit_high_pct=_r(pct(hi, exit_px)),
               post_exit_low_pct=_r(pct(lo, exit_px)), high_vs_now_pct=_r(pct(hi, now)))
    if _num(range5_pct) and range5_pct > 0:
        out["pullback_range5"] = _r(pct(hi, now) / range5_pct, 3)
    return out


def entry_state(rows, *, as_of, gaps=(), covered_from=None):
    """Gamma's ENTER-now vs WAIT state inputs (docs/audits/2026-10-23-enter-now-vs-wait-table-preregistration.md;
    scripts/research/desk_enter_wait_units.state, ported): facts for JUDGE, never a gate. rows: priced rows (all traders,
    ours removed: the agent moves the price), < as_of. pos300 = (price - low300) / (high300 - low300); breakout_age_s =
    seconds since the 300 s high; base_s = the longest span before that high with ln(high/low) <= 0.15 (>= 10 swaps), in 60 s
    steps, cap 1800; breakout_vol = 60 s volume over the base's mean 60 s volume (base_s >= 120). Unknown is None."""
    out = {"pos300": None, "breakout_age_s": None, "base_s": None, "breakout_vol": None}
    if covered_from is None or as_of - 300 < covered_from or _gap(gaps, as_of - 300, as_of):
        return out
    w300 = [r for r in rows if as_of - 300 <= r["received_at"] < as_of and _num(r.get("price_usd")) and r["price_usd"] > 0]
    if len(w300) < 2:
        return out
    top = max(w300, key=lambda r: r["price_usd"])
    hi, lo, now, t_hi = top["price_usd"], min(r["price_usd"] for r in w300), w300[-1]["price_usd"], top["received_at"]
    out["pos300"] = _r((now - lo) / (hi - lo)) if hi > lo else None
    out["breakout_age_s"] = _r(as_of - t_hi, 1)
    priced = [r for r in rows if _num(r.get("price_usd")) and r["price_usd"] > 0]
    base = 0.0
    for x in range(60, 1801, 60):
        if t_hi - x < covered_from or _gap(gaps, t_hi - x, t_hi):
            base = None                          # cannot see the whole span: unknown, never a short base
            break
        w = [r["price_usd"] for r in priced if t_hi - x < r["received_at"] < t_hi]
        if len(w) < 10:
            continue
        if math.log(max(w) / min(w)) <= 0.15:
            base = float(x)
        else:
            break
    out["base_s"] = base
    if base is not None and base >= 120:
        vol = lambda a, b: sum(r["usd"] for r in rows if a <= r["received_at"] < b and _num(r.get("usd")))
        per60 = vol(t_hi - base, t_hi) / (base / 60.0)
        out["breakout_vol"] = _r(vol(as_of - 60, as_of) / per60, 3) if per60 > 0 else None
    return out

#!/usr/bin/env python3
"""Solana chase-cap cost study (Lambda 2026-10-07). READ-ONLY, local data only, no DexScreener.

Pre-registration: docs/superpowers/specs/2026-10-07-memecoin-solana-chase-cap-cost-study-prereg.md

  --cohort  identity and coverage only. Prints no price, premium or return; safe before review.
  --read    THE one pre-registered read (only after Gamma's PASS on the pre-registration).

Run from the repo root.
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext

START, END = "2026-10-04T00:00:00", "2026-10-07T12:38:00"
HORIZONS = {60: 15.0, 300: 30.0}             # horizon s -> nearest-observation tolerance s
THRESHOLDS = (5.0, 10.0, 20.0)               # pp added to the cap
HARD_CAP_PCT = 35.0                          # MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT in the window
MIN_POOLED, MIN_WALLET, MIN_BAND = 8, 5, 3   # decision-rule sample floors
SLOT = ("slot_lane_processed", "slot_lane_confirmed_receipt")
FAST = {*SLOT, "onchain_fallback_after_slot_lane", "onchain_watch", None}
CAP_FAILS = {"chain_error:BuySlippageBelowMinTokensOut", "chain_error:BuySlippageBelowMinBaseAmountOut",
             "chain_error:slippage tolerance exceeded (Jupiter 0x1771)",
             "chain_error:exceeds desired slippage limit", "chain_error:ExceededSlippage"}
# Same exclusion as ~/.cache/lambda/solana_gap_classes.py, which produced the 33 in task fe513bdd.
POLICY = re.compile(r"re-entry cooldown|one position per coin|over ceiling|under mcap floor|under absolute "
                    r"floor|kill_switch|coin_too_young|already ran|post_peak|launchpad|safety|volume|"
                    r"mint authority|cooldown")


def sig_of(origin):
    origin = str(origin or "")
    for p in ("onchain:", "slotlane:", "swap:raybot:"):
        if origin.startswith(p):
            return origin[len(p):].split(":", 1)[0] or None
    return None


def ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def _ceil_tokens(cost_usdc_atomic, price_usd, cap_pct, decimals):
    """pump_curve_buy.source_price_floor_atomic, restated so the study pins the live formula."""
    with localcontext() as ctx:
        ctx.prec = 50
        cost, price, cap = (Decimal(str(v)) for v in (cost_usdc_atomic, price_usd, cap_pct))
        tokens = cost / 10 ** 6 / (price * (1 + cap / 100)) * 10 ** int(decimals)
        return int(tokens.to_integral_value(rounding=ROUND_CEILING))


def min_out(spl, chase_pct, hard_pct):
    """The signed minimum at these caps: max(output haircut, chase floor, hard floor). The haircut and
    the chase floor take the same cap (pump_curve_buy.py:1170, pumpswap_buy.py:833); the hard floor
    applies only when the row carries the source average (it was optional before SOL-1)."""
    output = int(int(spl["expected_tokens_atomic"]) * (1.0 - float(chase_pct) / 100.0))
    price = _ceil_tokens(spl["cost_usdc_atomic"], spl["source_price_usd"], chase_pct, spl["base_decimals"])
    if spl.get("source_average_price_usd") is not None:
        price = max(price, _ceil_tokens(spl["cost_usdc_atomic"], spl["source_average_price_usd"],
                                        hard_pct, spl["base_decimals"]))
    return max(output, price)


def caps_at(base_pct, t, variant):
    """(chase, hard) after adding t pp. A: chase only, clamped to the hard cap as
    settings.solana_wallet_chase_cap_pct does. B: both caps."""
    if variant == "A":
        return min(base_pct + t, HARD_CAP_PCT), HARD_CAP_PCT
    return base_pct + t, HARD_CAP_PCT + t


def needed_pp(spl, available, variant="B", hi=400.0):
    """Smallest t on the 0.01 pp grid at which `available` meets the signed minimum; None if not by
    `hi`. Searched on integer hundredths, so the t returned passes and t - 0.01 fails (review SF05,
    2026-10-08): rounding a bisection bound to 0.01 could land on a failing cap, and buy_at then
    counted a fill the exact floor refuses (true threshold +5.004 reported as 5.0)."""
    cap = float(spl["cap_pct"])

    def passes(k):
        return min_out(spl, *caps_at(cap, k / 100.0, variant)) <= available
    top = int(round(float(hi) * 100))
    if not passes(top):
        return None
    lo, hi_k = -1, top                    # passes(hi_k); lo fails, or sits below the grid
    while hi_k - lo > 1:
        mid = (lo + hi_k) // 2
        if passes(mid):
            hi_k = mid
        else:
            lo = mid
    return hi_k / 100.0


def nearest(points, target, tol):
    """(t, value) nearest `target` within `tol` seconds, else None."""
    best = min(points, key=lambda p: abs((p[0] - target).total_seconds()), default=None)
    return best if best is not None and abs((best[0] - target).total_seconds()) <= tol else None


def substage(trace_json, name):
    for e in (json.loads(trace_json or "{}").get("substage_events") or ()):
        if isinstance(e, dict) and e.get("name") == name:
            return e
    return None


def cohort(led, ft):
    skips = {r[0]: str(r[1]) for r in ft.execute("SELECT signature, reason FROM memecoin_slot_lane_skips")}
    rows = led.execute("""SELECT attempt_id, decision_started_at, delivery_lane, source_handle, origin_event_id,
        terminal_status, terminal_reason, trace_json, mint, completed_at, source_user_id
        FROM memecoin_entry_attempts WHERE network_id='solana' AND decision_started_at>=? AND decision_started_at<?
        AND terminal_reason!='not_approved'""", (START, END)).fetchall()
    buys = collections.defaultdict(list)
    for r in rows:
        if r[2] in FAST and sig_of(r[4]):
            buys[sig_of(r[4])].append(r)
    out, excluded = [], collections.Counter()
    for s, atts in buys.items():
        atts.sort(key=lambda a: a[1])
        slot = [a for a in atts if a[2] in SLOT]
        if not slot or any(a[5] == "filled" for a in slot):
            continue
        if any(POLICY.search(a[6] or "") for a in atts) or POLICY.search(skips.get(s, "")):
            continue
        last = slot[-1]
        if last[5] != "failed":
            continue
        if (last[6] or "") not in CAP_FAILS:
            excluded[last[6]] += 1
            continue
        fill = next((a for a in atts if a[2] not in SLOT and a[5] == "filled"), None)
        out.append(dict(sig=s, attempt=last, fallback=fill))
    return sorted(out, key=lambda d: d["attempt"][1]), excluded


def capture_sells(cap, mint, sig):
    """(time, sell unit price, capture bag atomic) for every ok exact-route Jupiter sell quote captured on
    this source buy (origin slotlane:<sig> or onchain:<sig>, any gate). Time = refused_at + age_s."""
    rows = cap.execute("""SELECT refused_at, age_s, quote_price, token_qty_atomic FROM memecoin_refusal_captures
        WHERE network_id='solana' AND mint=? AND origin_event_id IN (?, ?) AND provider='jupiter'
        AND quote_price_basis='exact_route' AND quote_side='sell' AND quality='ok'""",
                       (mint, "slotlane:" + sig, "onchain:" + sig))
    return [(ts(r) + timedelta(seconds=float(a)), float(p), int(q) if q else None) for r, a, p, q in rows if p]


QUOTE_REFUSAL = re.compile(r"^exact quote is [+-]?\d+% above source price \(cap \d+%\)$")


def fallback_legs(led, sig):
    """The buy's pre-sign quote refusals on a live lane, time-ordered: (completed_at, exact gap pct, cap
    pct, quote price USD/token, attempt id) from the last chase_quote_confirmation substage."""
    out = []
    for aid, done, lane, reason, tj in led.execute("""SELECT attempt_id, completed_at, delivery_lane, terminal_reason,
        trace_json FROM memecoin_entry_attempts WHERE network_id='solana' AND origin_event_id IN (?, ?)
        ORDER BY completed_at""", ("onchain:" + sig, "slotlane:" + sig)):
        if lane == "j7_shadow" or not QUOTE_REFUSAL.match(reason or ""):
            continue
        conf = [e for e in (json.loads(tj or "{}").get("substage_events") or ())
                if isinstance(e, dict) and e.get("name") == "chase_quote_confirmation"]
        if conf:
            c = conf[-1]
            out.append((done, float(c["gap_pct"]), float(c["cap_pct"]), float(c["quote_price"]), aid))
    return out


def row_result(d, cap):
    a = d["attempt"]
    spl, sps = substage(a[7], "slot_price_limit"), substage(a[7], "slot_pump_slippage")
    r = dict(sig=d["sig"][:12], at=a[1][:19], wallet=a[3], reason=a[6].replace("chain_error:", ""),
             fate="late_fallback" if d["fallback"] else "missed", censor=None)
    if spl is None:
        r["censor"] = "no_price_limit"
        return r
    # The program's Left/Right pair (solana_slot_executor.pump_slippage_failure; its no_tokens_bought
    # is always True on a failed buy). Right must be the minimum we signed, or the pair is not ours.
    if sps is None or not isinstance(sps.get("available_tokens_atomic"), int):
        r["censor"] = "landing_unknown"
        return r
    if sps.get("min_tokens_atomic") != int(spl["min_tokens_atomic"]):
        r["censor"] = "pair_mismatch"
        return r
    if min_out(spl, float(spl["cap_pct"]), HARD_CAP_PCT) != int(spl["min_tokens_atomic"]):
        r["censor"] = "floor_unreproduced"
        return r
    avail, dec = int(sps["available_tokens_atomic"]), int(spl["base_decimals"])
    cost = int(spl["cost_usdc_atomic"]) / 1e6
    landing = cost / (avail / 10 ** dec)
    r.update(cap_pct=float(spl["cap_pct"]), need_pp_A=needed_pp(spl, avail, "A"), need_pp_B=needed_pp(spl, avail, "B"),
             prem_post_pct=100 * (landing / float(spl["source_price_usd"]) - 1),
             prem_avg_pct=(100 * (landing / float(spl["source_average_price_usd"]) - 1)
                           if spl.get("source_average_price_usd") is not None else None))
    sol_usd = cost / (int(spl["spend_lamports"]) / 1e9)
    fee_usd = 2 * int(sps.get("fee_lamports") or 0) / 1e9 * sol_usd
    sells, done = capture_sells(cap, a[8], d["sig"]), ts(a[9])
    r["fee_pct"] = 100 * fee_usd / cost
    for h, tol in HORIZONS.items():
        p = nearest(sells, done + timedelta(seconds=h), tol)
        r["ret_%d" % h] = None if p is None else 100 * ((avail / 10 ** dec * p[1] - fee_usd) / cost - 1)
        if h == 300 and p is not None and p[2]:
            r["bag_ratio"] = avail / p[2]            # reported only (Gamma SMALL 2); flag outside [0.8, 1.25]
            r["bag_flag"] = not 0.8 <= r["bag_ratio"] <= 1.25
    r["landing_usd"] = landing
    return r


def fallback_value(cap, mint, sig, leg, fee_pct):
    """Hold-to-h return of a fill at the refused exact quote, anchored at that refusal."""
    done, _gap, _cap, quote, _aid = leg
    sells = capture_sells(cap, mint, sig)
    out = {}
    for h, tol in HORIZONS.items():
        p = nearest(sells, ts(done) + timedelta(seconds=h), tol)
        out["ret_%d" % h] = None if p is None or fee_pct is None else 100 * (p[1] / quote - 1) - fee_pct
    return out


def lifted_cap(cap_pct, t, variant):
    return min(cap_pct + t, HARD_CAP_PCT) if variant == "A" else cap_pct + t


def buy_at(r, legs, cap, mint, sig, t, variant, fee_pct):
    """The buy's outcome at +t: the slot send fills when its landing meets the looser floor; else the
    earliest pre-sign quote refusal whose exact gap fits the looser cap fills there; else missed."""
    need = r.get("need_pp_" + variant)
    if need is not None and need <= t:
        return dict(leg="slot", ret_60=r.get("ret_60"), ret_300=r.get("ret_300"))
    for leg in legs:
        if leg[1] <= lifted_cap(leg[2], t, variant):
            return dict(leg="fallback", **fallback_value(cap, mint, sig, leg, fee_pct))
    return None


def fallback_fate(led, d, r):
    pos = led.execute("SELECT entry_price, entry_notional, realized_pnl, closed_at FROM memecoin_positions "
                      "WHERE attempt_id=? AND execution_mode='real'", (d["fallback"][0],)).fetchone()
    if pos is None:
        return dict(sig=r["sig"], fallback="no position row")
    paid = (100 * (pos[0] / r["landing_usd"] - 1)) if r.get("landing_usd") and pos[0] else None
    ret = 100 * pos[2] / pos[1] if pos[2] is not None and pos[1] else None
    return dict(sig=r["sig"], wallet=r["wallet"], lane=d["fallback"][2], paid_over_slot_landing_pct=paid,
                realized_pct=ret, closed=bool(pos[3]))


def comparison(led):
    """Cap-passing slot fills in the window: book result (policy exits) and hold-to-60 s on the route
    series where it covers. Not like-for-like with the hold-to-h cohort; shown, never decisive."""
    out = []
    for pid, op, notional, price, pnl, closed, handle in led.execute("""SELECT p.id, p.opened_at, p.entry_notional,
        p.entry_price, p.realized_pnl, p.closed_at, a.source_handle FROM memecoin_entry_attempts a
        JOIN memecoin_positions p ON p.attempt_id=a.attempt_id WHERE a.network_id='solana'
        AND a.decision_started_at>=? AND a.decision_started_at<? AND a.terminal_status='filled'
        AND a.delivery_lane IN (?,?) AND p.execution_mode='real'""", (START, END, *SLOT)):
        pts = [(ts(o), float(u)) for o, u in led.execute(
            "SELECT observed_at, unit_price FROM memecoin_route_series WHERE position_id=? AND unit_price>0", (pid,))]
        p60 = nearest(pts, ts(op) + timedelta(seconds=60), HORIZONS[60])
        hold60 = 100 * (p60[1] / price - 1) if p60 and price else None     # unit_price vs entry_price, USD/token
        out.append(dict(wallet=handle, book_pct=(100 * pnl / notional) if closed and pnl is not None and notional else None,
                        hold60_pct=hold60))
    return out


def stats(vals):
    v = sorted(x for x in vals if x is not None)
    if not v:
        return dict(n=0, mean=None, median=None, sum=None, mean_wo_best=None)
    mid = len(v) // 2
    return dict(n=len(v), mean=round(sum(v) / len(v), 2), sum=round(sum(v), 2),
                median=round(v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2, 2),
                mean_wo_best=round(sum(v[:-1]) / (len(v) - 1), 2) if len(v) > 1 else None)


def independent_refusals(led, c1sigs):
    """Pre-sign quote / coin-ran refusals on buys NOT in the slot cohort, never filled, on a live lane,
    with their points over the cap (from the reason string). The frozen window holds 8, all over +20."""
    pat = re.compile(r"^(exact quote is|coin already ran) \+?(-?\d+)% (above source price|past their entry) \(cap (\d+)%\)$")
    by = collections.defaultdict(list)
    for r in led.execute("""SELECT origin_event_id, delivery_lane, terminal_status, terminal_reason, source_handle
        FROM memecoin_entry_attempts WHERE network_id='solana' AND decision_started_at>=? AND decision_started_at<?""",
                         (START, END)):
        if sig_of(r[0]):
            by[sig_of(r[0])].append(r)
    out = []
    for s, atts in by.items():
        if s in c1sigs or any(a[2] == "filled" for a in atts):
            continue
        ref = [a for a in atts if a[1] != "j7_shadow" and pat.match(a[3] or "")]
        if ref:
            m = pat.match(ref[-1][3])
            out.append(dict(sig=s[:12], wallet=ref[-1][4], lane=ref[-1][1], need_pp=int(m.group(2)) - int(m.group(4))))
    return out


def first_gain(r, legs, variant):
    """Smallest t at which the buy fills by either leg (the marginal-band key)."""
    cands = [r.get("need_pp_" + variant)]
    for _done, gap, cap_pct, _q, _aid in legs:
        cands.append(gap - cap_pct if variant == "B" else (gap - cap_pct if gap <= HARD_CAP_PCT else None))
    cands = [c for c in cands if c is not None]
    return max(0.0, min(cands)) if cands else None


def verdict(buys, floor, variant="B"):
    """Largest t whose gained missed buys pass every check, else None (keep the caps). Checks: n >= floor
    with both horizons determined; mean ret_300 >= 0, mean ret_60 >= 0, median ret_300 >= 0, mean ret_300
    without the single best buy >= 0 (Gamma SMALL 1); the marginal band's mean ret_300 >= 0 when it has
    MIN_BAND or more buys."""
    best, prev = None, -1.0
    for t in THRESHOLDS:
        gained = [b for b in buys if b["fate"] == "missed" and b["out"][(variant, t)]
                  and b["out"][(variant, t)].get("ret_300") is not None and b["out"][(variant, t)].get("ret_60") is not None]
        band = [b for b in gained if b["first_" + variant] is not None and b["first_" + variant] > prev]
        s300 = stats(b["out"][(variant, t)]["ret_300"] for b in gained)
        s60 = stats(b["out"][(variant, t)]["ret_60"] for b in gained)
        b300 = stats(b["out"][(variant, t)]["ret_300"] for b in band)
        if (s300["n"] >= floor and s300["mean"] >= 0 and s60["mean"] >= 0 and s300["median"] >= 0
                and s300["mean_wo_best"] is not None and s300["mean_wo_best"] >= 0
                and (b300["n"] < MIN_BAND or b300["mean"] >= 0)):
            best = t
        prev = t
    return best


def table(buys, variant, t, leg=None):
    picked = [b["out"][(variant, t)] for b in buys if b["fate"] == "missed" and b["out"][(variant, t)]
              and (leg is None or b["out"][(variant, t)]["leg"] == leg)]
    return dict(gained=len(picked), ret_60=stats(o.get("ret_60") for o in picked),
                ret_300=stats(o.get("ret_300") for o in picked))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", action="store_true")
    ap.add_argument("--read", action="store_true")
    args = ap.parse_args()
    led = sqlite3.connect("file:sniper_tracker.db?mode=ro", uri=True, timeout=5)
    ft = sqlite3.connect("file:data/memecoin_fill_timing.db?mode=ro", uri=True, timeout=5)
    cap = sqlite3.connect("file:data/memecoin_refusal_captures.db?mode=ro", uri=True, timeout=5)
    rows, excluded = cohort(led, ft)
    legs = {d["sig"]: fallback_legs(led, d["sig"]) for d in rows}
    indep = independent_refusals(led, {d["sig"] for d in rows})
    if args.cohort:
        for d in rows:
            a = d["attempt"]
            subs = {n for n in ("slot_price_limit", "slot_pump_slippage") if substage(a[7], n)}
            print(json.dumps(dict(sig=d["sig"][:12], at=a[1][:19], wallet=a[3], reason=a[6].replace("chain_error:", ""),
                                  fate="late_fallback" if d["fallback"] else "missed", substages=sorted(subs),
                                  quote_refusals=len(legs[d["sig"]]),
                                  capture_sells=len(capture_sells(cap, a[8], d["sig"])))))
        print("n", len(rows), "excluded non-cap failures", dict(excluded))
        print("independent pre-sign refusals", json.dumps(indep))
        return
    if not args.read:
        ap.error("--cohort or --read")
    res = [row_result(d, cap) for d in rows]
    fees = sorted(r["fee_pct"] for r in res if r.get("fee_pct") is not None)
    fee_pct = fees[len(fees) // 2] if fees else None          # median slot round-trip fee, for fallback legs
    buys = []
    for d, r in zip(rows, res):
        b = dict(sig=r["sig"], wallet=r["wallet"], fate=r["fate"], out={},
                 first_A=first_gain(r, legs[d["sig"]], "A"), first_B=first_gain(r, legs[d["sig"]], "B"))
        for variant in ("A", "B"):
            for t in THRESHOLDS:
                b["out"][(variant, t)] = buy_at(r, legs[d["sig"]], cap, d["attempt"][8], d["sig"], t, variant, fee_pct)
        buys.append(b)
    print("## rows (slot leg)")
    for r in res:
        print(json.dumps({k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items() if k != "landing_usd"}))
    print("## fallback legs (pre-sign quote refusals on cohort buys): gap - cap, points")
    for d in rows:
        if legs[d["sig"]]:
            print(d["sig"][:12], [round(g - c, 2) for _t, g, c, _q, _a in legs[d["sig"]]])
    print("## median round-trip fee pct (fallback legs):", fee_pct)
    print("## thresholds: missed buys gained, by leg and in union (hold-to-h, after fees)")
    for variant in ("A", "B"):
        for t in THRESHOLDS:
            print(variant, "+%g" % t, "slot", table(buys, variant, t, "slot"), "fallback", table(buys, variant, t, "fallback"),
                  "union", table(buys, variant, t))
    print("## independent pre-sign refusals (not in the cohort):", json.dumps(indep))
    print("## per wallet (variant B, union)")
    for w in sorted({b["wallet"] for b in buys}):
        mine = [b for b in buys if b["wallet"] == w]
        print(w, "n", len(mine), {"+%g" % t: table(mine, "B", t) for t in THRESHOLDS},
              "verdict:", verdict(mine, MIN_WALLET) or "NOT SHOWN, keep the caps")
    print("## late fallbacks")
    for d, r in zip(rows, res):
        if d["fallback"]:
            print(json.dumps(fallback_fate(led, d, r)))
    comp = comparison(led)
    print("## cap-passing slot fills (book result; not like-for-like)",
          "book", stats(c["book_pct"] for c in comp), "hold60", stats(c["hold60_pct"] for c in comp))
    print("## censored (slot leg)", dict(collections.Counter(r["censor"] for r in res if r["censor"])))
    print("## verdict (pooled, variant B, union): raise both caps by",
          verdict(buys, MIN_POOLED) or "NOT SHOWN; keep the caps")


if __name__ == "__main__":
    sys.exit(main())

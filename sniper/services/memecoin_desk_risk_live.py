"""Desk RISK seat, LIVE (design: Lambda 2026-10-22, accepted by Alpha; engine side: Beta).

One call per cycle covers EVERY open desk position. The model answers HOLD / TIGHTEN(rails) / EXIT per
position; it can never widen a rail, switch one off or add size. Everything here is pure: the watcher
(scripts/memecoin_desk_risk_watcher.py) owns the reads, the call and its own DB, and the engine
(Beta, exit sweep) owns the apply. Deterministic rails run first in the engine and always win; a
failed, late or malformed answer is a HOLD with the cause recorded.

Facts are code-built numbers and enums only: no ticket text, no ticker, no wallet (nothing a coin's
author can write reaches the prompt). SOL/token candles use the actual USD/token fill and a validated
local SOL/USD rate; the position's own PnL is the exact whole-bag exit quote, or null.
"""
from __future__ import annotations

import os
import hashlib
import json
import math
import re

from sniper.services import memecoin_desk_risk as R

RISK_LIVE_VERSION = "desk-risk-live-v12"          # v12: optional evidence gates and decision-time recheck; v11: observe-only regime
# v10: confirmed noise-sized 5 s closes; v9: explicit precedence; v8: capped trail-only LOOSEN_TRAIL
# v7: actual-fill candle baseline and fresh tightening validation
# v6: bars continue on the PumpSwap pool after graduation
# v5: engine attention signals wake the cycle + attn in the prompt
# v4: exact whole-bag PnL + ~5 s cadence + warm spare (v3 PnL was a bug)
MODEL = R.MODEL
DEADLINE_S = 12.0              # Alpha 2026-10-22: a late answer beats a HOLD (thinking tail: 6-8 s seen); rails are the fast path
CYCLE_S = float(os.getenv("MEMECOIN_DESK_RISK_CYCLE_S", "5"))   # from the END of the previous cycle: calls never overlap (v3: 15)
EVENT_MIN_GAP_S = 5.0          # a structure break may wake the cycle early, never faster than this
ORDER_TTL_S = 10.0             # an order older than this at apply time is refused by the engine
MAX_POSITIONS = 12             # per call; more open positions = another call in the same cycle
RECENT_S = 60                  # last minute at 1 s, earlier since entry at 5 s
OLD_BAR_S = 5
BLIND_CYCLES_PAGE = 6          # consecutive failed cycles with positions open = page the operator

HOLD_DEFAULT_S = 300           # the desk's default hold; longer only while the run continues
COLS = ["t", "o", "h", "l", "c", "buy", "sell", "mk"]
REASON_CODES = {
    # HOLD
    1: "run intact: structure and buy flow holding",
    2: "too early: under 60 s held, not enough tape to judge",
    3: "no tape (graduated or gap): rails only",
    4: "pnl unknown (stale mark): the tape says hold",
    5: "dip recovering: structure intact",
    # TIGHTEN
    6: "trail tightened: run alive, lock more of the gain",
    7: "stop tightened: weakness showing, run not yet over",
    8: "take profit lowered: target near or flow fading",
    9: "trail added: position had none",
    # EXIT
    10: "structure break: confirmed closes under the prior low (sb = 1)",
    11: "flow flipped: sells dominate buys",
    12: "failed higher high: no new high and fading",
    13: "held past 5 min and the run has ended",
    14: "collapse: fast drop through the structure",
    15: "tape dying: buyers and makers gone",
    16: "other exit (use rarely)",
    # LOOSEN_TRAIL (v8, action "L")
    17: "trail loosened: trend intact, the dip sits inside the structure",
    18: "trail loosened: breakout holding, give the run room",
    19: "trail loosened: buy flow strong and the pullback is shallow",
}
SYSTEM_PROMPT = """You are the RISK seat of an automated memecoin desk. You watch every open position at once.
OBJECTIVE: total PnL. Runners are the money. A false EXIT cuts a runner and costs its whole upside; a late
EXIT costs the drawdown. While a run is alive, TIGHTEN (lock gains) beats EXIT.
HOLDS are short by default, about 300 s (to_5min_s shows where each position is). Hold longer ONLY while
the run continues: still making higher highs, structure intact, buy flow holding.
EXIT when the run has ended, not on fear: a structure break (closes under the prior low), flow flipped to
sells, a failed higher high (no new high, fading), or past 300 s with the run over.
Rails already run on every tick and always win. You can make a rail stricter, never switch one off, never add
size, and the ONLY rail you may widen is the trailing stop (action "L", below). Rail meaning (bounds in the input): sl_pct = max loss from entry in percent (smaller =
tighter); trail_pct = trailing stop distance from the peak in percent (smaller = tighter, 0/none = no trail, you
may add one); tp_pct = take profit in percent gain (smaller = nearer). A TIGHTEN that would fire at once is not
allowed: say EXIT.
LOOSEN_TRAIL ("L"): widen ONLY trail_pct, only when the trend is intact and the pullback sits inside the structure
(a shakeout, not a break). r = {"trail_pct": n} with n larger than the current trail and at most __TRAIL_CAP__ (percent);
f = 1-4 tokens naming the structure facts you rely on, from: trend_up, higher_lows, support_below, breakout_holding,
delta_rising, volume_accel, flow_buys, shallow_dd. Use codes 17-19. Code refuses a loosen when a rail would fire, after
an engine attention tighten, within 60 s of another loosen or of a tighten, and when the trail is off. Do not loosen
on fear of being stopped: a stop that fires is the plan working. When unsure, HOLD.
INPUT (compact JSON, numbers only): {"cols": [...], "bounds": {...}, "pos": [one object per position]}. Per
position: k key; held_s; to_5min_s (300 - held_s); rails (current sl/trail/tp/catastrophe in percent);
pnl_pct_exact (null = unknown, rely on the tape); mark_age_s; tape ("ok"|"none"|"unknown", unknown = no valid
fill-price conversion); high_bp/last_bp (SOL tape repriced at snapshot FX against the actual USD fill,
10000 = the fill; these are not historical USD high-water or executable returns; mixed fill-second and
pre-fill bars cannot set the post-entry extrema); dd_bp (drop from the high, bp); since_high_s; flow_10s/flow_30s = [buy_usd,
sell_usd]; sb = 1 when the structure-break rule (risk-break-v2) fired: two consecutive 5 s bar CLOSES, or one close with sellers
dominating its bar, under the lowest low of the 60 s before it (pre-fill history included) by more than a multiple of THIS coin's own median
5 s range; a wick that closes back above, a missing support or an unknown noise is never sb = 1; code 10 EXITs need sb = 1 and say so; attn = engine attention signals that FIRED in the last
60 s, [[signal, seconds_ago], ...] (creator_sell, sell_burst, buyer_exodus, volume_spike, holder_dump,
quote_collapse); empty = none fired, which is not proof of calm. A creator_sell with a sell_burst is the
strongest exit evidence you get; code may already have tightened the stop on it; after a coin GRADUATES its bars
continue on its PumpSwap pool; rows = [t, o, h, l, c, buy, sell, mk] per bar: t seconds
relative to now (0 = now, negative = past), o/h/l/c in bp of the fill, buy/sell in USD, mk = makers. Last 60 s
are 1 s bars, earlier bars are 5 s.
rg = the coin's REGIME right now from its own tape in its own 5 s noise units (provisional thresholds, no fit yet): r = impulse | trend |
consolidation | range_chop | dying | quiet (no trades, or no price movement at all, in its tape window) | mixed | unknown (unknown = no evidence, never calm); dir up|down for impulse and trend; d6/d12 = net
move over 30/60 s in noise units; e12 = path efficiency (1 = a straight line, near 0 = back and forth); rng = the 60 s range in noise
units; act = trades in the last 30 s against this coin's own median (about 1 = as usual, 0.25 or less = flow collapsed); n5 = the noise
unit (ln high/low of a 5 s bar); band = [lo, hi, touches_lo, touches_hi] in bp of YOUR fill, only in range_chop. It describes the kind of
move, it is not an order: an oscillation inside a band is not a break (a close under the band low is), a consolidation is not weakness,
an impulse or trend against you is evidence, dying with fading flow supports code 15.
st = market structure (tape "unknown" with a reason = blind, NOT calm): windows 15s/60s/300s/launch each with
buy_usd, sell_usd, net_delta_usd, buyers, sellers, range_pct, pos_in_range_pct, return_pct, trend {label up|down|
flat, slope_pct_per_min, higher_highs, higher_lows}, delta_trend rising|falling|flat; swings
{support_below_pct, resistance_above_pct}; breakout 60s/300s {broke, seconds_since, holding, volume_vs_avg};
volume_accel (15 s volume vs the 60 s average); volatility {median_5s_swing_pct}; chart = 2 min of 5 s bars
[t, o, h, l, c, buy, sell, mk] in bp of the CURRENT price; rails = advisory sl/tp/trail from structure.
STRUCTURE RULES: EXIT when structure breaks, on a break you can SEE in the data: support lost (sb = 1, or the bars show closes
under the last swing low; support_below_pct is None on a healthy run with no pivot under price AND after a pivot has been
crossed, so a missing value says nothing: UNKNOWN, not a break), control flips to sellers (60s net_delta_usd < 0 with
sellers > buyers and delta_trend falling), or a failed breakout NOW (broke true and holding false AND the broken level
and the failure are both observable in the supplied bars and trend, price back under that level; the sheet publishes no
breakout reference price and crossed pivots disappear, so an absent observable reference is UNKNOWN: never invent a
level; holding is path-dependent and also false on a touch or equality, one earlier undercut keeps it false after a
reclaim, so false alone is not a failure now).
TIGHTEN while the trend holds (60s/300s label up, higher lows intact): lock gains with the trail, not an EXIT. A window
with state "unknown" is no evidence either way.
PRECEDENCE: the engine's rails come first, then the valid position facts and evidence, then your action. A missing or stale
quote or mark is not a loss; sparse tape, a gap, a missing USD conversion or a graduation step is not a collapse (a real, valid price or FX move still is evidence); no attention
signal is not calm; mk counts addresses, not people, and agent volume does not prove independent organic demand. Three bases
stay apart: rows are the price as a ratio of your FILL in bp (10000 = your fill price), the st chart is the CHANGE versus the
CURRENT price in bp (0 = the current price): two different encodings, not one baseline; pnl_pct_exact is the executable
whole-bag PnL.
Cite a fact (f tokens) only when it is positively present in gap-free
data; never infer one. HOLD leaves the rails running; never loosen a stop or trail to rescue a rail that has triggered.
OUTPUT: raw JSON only (no markdown fence, no prose): one JSON array, one object per position you act on or confirm (a missing position is a HOLD):
[{"k": <key>, "a": "H"|"T"|"E"|"L", "r": null | {"sl_pct"?: n, "trail_pct"?: n, "tp_pct"?: n}, "c": <code>, "f"?: [tokens, only with "L"]}]
"c" is the reason code as an INTEGER (6, not "6"), exactly one of:
""" + "\n".join("%d %s" % (k, v) for k, v in REASON_CODES.items()) + """
"""
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("__TRAIL_CAP__", "%g" % R.loosen_cap())   # the knob at import; a change needs a restart


def _ratio(x, base):
    return round(x / base, 6) if isinstance(x, (int, float)) and base else None


def compress(bars):
    """Bars (ascending t, 1 s) -> the last RECENT_S seconds as given, everything earlier merged into
    OLD_BAR_S buckets (o first, h max, l min, c last, flows summed, makers max). Gaps stay gaps."""
    if not bars:
        return []
    cut = bars[-1]["t"] - RECENT_S
    old = [b for b in bars if b["t"] <= cut]
    recent = [b for b in bars if b["t"] > cut]
    out = []
    bucket = None
    for b in old:
        k = b["t"] // OLD_BAR_S
        if bucket is None or bucket["k"] != k:
            if bucket is not None:
                out.append(bucket)
            bucket = {"k": k, "t": k * OLD_BAR_S, "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"],
                      "buy_usd": b.get("buy_usd") or 0.0, "sell_usd": b.get("sell_usd") or 0.0,
                      "n_makers": b.get("n_makers") or 0}
        else:
            bucket["h"], bucket["l"] = max(bucket["h"], b["h"]), min(bucket["l"], b["l"])
            bucket["c"] = b["c"]
            bucket["buy_usd"] += b.get("buy_usd") or 0.0
            bucket["sell_usd"] += b.get("sell_usd") or 0.0
            bucket["n_makers"] = max(bucket["n_makers"], b.get("n_makers") or 0)
    if bucket is not None:
        out.append(bucket)
    return [{kk: vv for kk, vv in b.items() if kk != "k"} for b in out] + recent


# The structure block (structure-v1, Lambda's module): numbers, bools, None and these enums only. Absolute
# USD prices and clocks are dropped (the block is relative to now and to the current price).
_ST_ENUMS = frozenset({"ok", "unknown", "up", "down", "flat", "rising", "falling", "tape_gap", "no_swaps",
                       "no_reference", "no_tape", "graduated", "unreadable", "adapter_missing", "not_provided",
                       "structure-v1"})
_ST_DROP = frozenset({"high", "low", "price_usd", "as_of", "cut_slot", "meta"})
# A 'reason' is the adapter's own machine code (stale_heartbeat, sol_usd_unknown, tape_unreadable:<kind>, ...),
# never coin text: allowed by shape, not by list.
_ST_REASON = re.compile(r"[a-z][a-z0-9_]{0,40}(:[a-z][a-z0-9_]{0,24})?\Z")


def structure_block(st):
    """Sanitize a structure dict for the prompt: a value that is not a finite number, bool, None, a known
    enum, or a list/dict of those is dropped (no free text from any coin ever reaches the model)."""
    def clean(v, key=None):
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            try:
                return v if math.isfinite(v) else None
            except OverflowError:
                return None
        if isinstance(v, str):
            return v if v in _ST_ENUMS or (key == "reason" and _ST_REASON.match(v)) else None
        if isinstance(v, list):
            return [clean(x) for x in v]
        if isinstance(v, dict):
            return {k: clean(x, k) for k, x in v.items() if isinstance(k, str) and k not in _ST_DROP}
        return None
    if not isinstance(st, dict):
        return {"tape": "unknown", "reason": "not_provided"}
    return clean(st)


def _bp(x, base):
    return int(round(x / base * 10000)) if isinstance(x, (int, float)) and base else None


_RG_ENUMS = frozenset({"impulse", "trend", "consolidation", "range_chop", "dying", "quiet", "mixed", "unknown"})
_RG_REASON = re.compile(r"[a-z][a-z0-9_]{0,40}(:[a-z][a-z0-9_]{0,24})?\Z")


def regime_block(rg, base):
    """The compact, sanitized prompt form of a memecoin_desk_regime block: enums, finite numbers, None. {'r': 'unknown', 'why': ...} for a
    block that is not a dict or carries an unknown enum (never a default regime)."""
    def num(v, nd=2):
        return round(v, nd) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
    if not isinstance(rg, dict) or rg.get("regime") not in _RG_ENUMS:
        return {"r": "unknown", "why": "not_provided"}
    out = {"r": rg["regime"]}
    if rg["regime"] == "quiet":
        return out
    if rg["regime"] == "unknown":
        why = rg.get("reason")
        out["why"] = why if isinstance(why, str) and _RG_REASON.match(why) else "unknown"
        return out
    out.update({"dir": rg.get("dir") if rg.get("dir") in ("up", "down") else None, "d6": num(rg.get("d6_n")), "d12": num(rg.get("d12_n")),
                "e12": num(rg.get("eff12")), "rng": num(rg.get("range12_n")), "act": num(rg.get("activity")), "n5": num(rg.get("n5"), 4)})
    band = rg.get("band")
    if isinstance(band, dict) and base:
        out["band"] = [_bp(band.get("lo"), base), _bp(band.get("hi"), base), band.get("touches_lo"), band.get("touches_hi")]
    return out


def position_facts(pos, policy, *, mark, bars, now, structure=None, attn=(), sol_usd=None, host_now=None, sb=None, regime=None):
    """`now` is corrected UTC for ledger/candles; attention ages retain their producer's host clock."""
    base_facts = R.facts(pos, policy, mark=mark, bars=[], boundary="cycle", now=now)
    base = R.fill_baseline(pos, sol_usd)
    rows = compress(bars) if base else []
    table = [[int(round(r["t"] - now)), _bp(r["o"], base), _bp(r["h"], base), _bp(r["l"], base), _bp(r["c"], base),
              int(round(r.get("buy_usd") or 0)), int(round(r.get("sell_usd") or 0)), int(r.get("n_makers") or 0)]
             for r in rows]
    last_t = bars[-1]["t"] if bars else None
    # The fill-second candle contains trades from both sides of our fill; its high is not ours.
    opened = R._ts(pos["opened_at"])
    post_fill = [b for b in bars if b["t"] > opened]

    def flow(sec):
        recent = [b for b in bars if last_t is not None and b["t"] > last_t - sec]
        return [int(round(sum(b.get("buy_usd") or 0 for b in recent))), int(round(sum(b.get("sell_usd") or 0 for b in recent)))]
    hi = max(post_fill, key=lambda b: b["h"]) if post_fill and base else None
    high = _bp(hi["h"], base) if hi else None
    last = _bp(post_fill[-1]["c"], base) if post_fill and base else None
    held = base_facts["held_s"]
    return {"k": base_facts["k"], "held_s": held, "to_5min_s": round(HOLD_DEFAULT_S - held, 1),
            "rails": base_facts["rails"], "pnl_pct_exact": base_facts["pnl_pct_exact"],
            "mark_age_s": base_facts["mark_age_s"], "tape": "ok" if table else ("unknown" if bars else "none"),
            "high_bp": high, "last_bp": last, "dd_bp": (high - last if high is not None and last is not None else None),
            "since_high_s": (int(last_t - hi["t"]) if hi else None),
            "flow_10s": flow(10) if table else None, "flow_30s": flow(30) if table else None,
            "sb": int(bool(sb)) if sb is not None else (int(R.structure_break(bars) is not None) if bars else 0), "rows": table,
            "attn": [[sig, int(round((now if host_now is None else host_now) - at))] for sig, at in attn],
            "st": structure_block(structure), **({"rg": regime_block(regime, base)} if regime is not None else {})}


def build_user(blocks, bounds=None, *, gate_params=None):
    """The whole prompt body: compact JSON, header once. `bounds` = the cohort's rail bounds."""
    out = {"cols": COLS, "bounds": {k: list(v) for k, v in (bounds or {}).items()}, "pos": blocks}
    if gate_params is not None:
        out["gate"] = {"thresholds": gate_params, "provisional": True,
                       "exit_codes": list(range(10, 16)), "tighten_code_rails": {6: "trail_pct", 7: "sl_pct", 8: "tp_pct", 9: "trail_pct"}}
    return json.dumps(out, sort_keys=True, separators=(",", ":"))


def inputs_hash(blocks, bounds=None, *, gate_params=None):
    return hashlib.sha256(build_user(blocks, bounds, gate_params=gate_params).encode()).hexdigest()


def block_hash(block):
    return hashlib.sha256(json.dumps(block, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _unfence(text):
    """The answer with one surrounding markdown code fence removed (the first smoke call wrapped its JSON in
    ```json ... ```): the body is still parsed strictly."""
    t = (text or "").strip()
    if t.startswith("```") and t.endswith("```") and len(t) >= 6:
        t = t[3:-3]
        t = t.split("\n", 1)[1] if t.split("\n", 1)[0].strip().isalpha() and "\n" in t else t
    return t.strip()


def _triggers(rails, pnl):
    """A TIGHTEN whose new rail already fires is an EXIT in disguise: the model must say E."""
    if pnl is None:
        return None
    if "sl_pct" in rails and pnl <= -rails["sl_pct"]:
        return "tighten_triggers:sl_pct"
    if "tp_pct" in rails and pnl >= rails["tp_pct"]:
        return "tighten_triggers:tp_pct"
    return None


def validate(text, *, blocks, policies, bounds, gate_params=None):
    """{key: (action, rails | None, reason, code)} for EVERY key in `blocks`. Anything not a valid HOLD /
    tightening TIGHTEN / EXIT is HOLD with the reason. A key the model omits is HOLD 'omitted'; a key it
    repeats is HOLD 'dup_key'; a key it invents is ignored. Non-None validated `gate_params` additionally
    require code-built evidence for EXIT/TIGHTEN; None preserves legacy callers. LOOSEN is independent."""
    keys = [b["k"] for b in blocks]
    out = {k: ("HOLD", None, "omitted", None) for k in keys}
    try:
        raw = json.loads(_unfence(text))
    except ValueError:
        return {k: ("HOLD", None, "malformed_json", None) for k in keys}
    if not isinstance(raw, list):
        return {k: ("HOLD", None, "not_an_array", None) for k in keys}
    seen, dup = set(), set()
    by_key = {b["k"]: b for b in blocks}
    for item in raw:
        k = item.get("k") if isinstance(item, dict) else None
        if k not in out:
            continue                                                  # invented or unreadable key
        if k in seen:
            dup.add(k)
            continue
        seen.add(k)
        action, rails, reason, code = R.validate(json.dumps(item), key=k, policy=policies[k], bounds=bounds)
        if action == "EXIT" and code == 10 and by_key[k].get("sb") != 1:
            action, rails, reason = "HOLD", None, "unbacked_reason:10"
        if action == "TIGHTEN":
            trig = _triggers(rails, by_key[k].get("pnl_pct_exact"))
            if trig:
                action, rails, reason = "HOLD", None, trig
        if gate_params is not None and action in ("EXIT", "TIGHTEN"):
            from sniper.services import memecoin_desk_risk_gate as G

            backed, failures = G.backing(by_key[k], action, rails, code, params=gate_params)
            if not backed:
                action, rails, reason = "HOLD", None, "unbacked_reason:%s" % code
                if failures:
                    reason += ":" + ",".join(failures)
        out[k] = (action, rails, reason, code)
    for k in dup:
        out[k] = ("HOLD", None, "dup_key", None)
    return out

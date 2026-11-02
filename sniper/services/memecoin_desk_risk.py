"""Desk RISK seat, Phase 0 SHADOW (docs/superpowers/specs/2026-10-21-desk-risk-seat-design.md §9).

The seat re-reads an open desk position at boundaries and answers HOLD, TIGHTEN(rails) or EXIT. In
Phase 0 every answer is RECORDED and NEVER APPLIED: nothing here or in the worker writes an override,
an exit intent or the funded ledger. Pure code; scripts/memecoin_desk_risk_shadow.py owns the reads,
the call and its own DB.

Facts are code-built numbers and enums only (no ticket free text), from two sources that are never
mixed in one ratio: the position's own funded numbers (entry notional, the exact-quote exit value of
a non-DexScreener mark) and the U tape's SOL/token bars, converted against the actual USD/token fill.
"""
from __future__ import annotations

from sniper.services import memecoin_desk_model_echo as model_echo
import hashlib
import json
import math
import statistics
from datetime import datetime

RISK_VERSION = "desk-risk-shadow-v3"           # v3: actual USD fill baseline with validated SOL/USD conversion
# risk-break-v1 (docs/audits/2026-10-21-desk-risk-shadow-preregistration.md): N consecutive 1 s closes
# under the low of the 60 s before them; a missing second resets. N = 3 (2 and 5 are offline sensitivity).
STRUCTURE_RULE_VERSION = "risk-break-v1"
STRUCTURE_N = 3
STRUCTURE_PRIOR_S = 60
MODEL = "claude-haiku-5-5"
# risk-break-v2 (operator 2026-10-23 after the MEND wick, pos 74155): a break is CLOSES, not wicks, measured against the coin's own noise.
# v1 read three 1 s closes under the low of the bars SINCE THE FILL: for a position seconds old that "prior 60 s low" is a few seconds of
# bars, so a one-bar wick that recovered inside 15 s read as a break and RISK exited at -13.8% right before +29%. v2 uses the 60 s before
# the run INCLUDING the pre-fill history, 5 s bar closes, and a size test in units of the coin's own median 5 s range.
STRUCTURE_RULE_VERSION_V2 = "risk-break-v2"
SB_BUCKET_S = 5
SB_PRIOR_BUCKETS = 12          # the support = the lowest LOW of the 60 s before the run (the v1 window)
SB_MIN_PRIOR_BUCKETS = 6       # fewest data buckets the support needs
SB_NOISE_BUCKETS = 60          # 300 s of 5 s bars for the coin's own noise
SB_MIN_NOISE_BUCKETS = 12      # fewest 5 s bars a noise estimate needs (Gamma's >= 12 returns rule)
SB_K = 2.0                     # provisional, unvalidated multiple of median 5 s high/low range; no death-study calibration


def echo_ok(echo):
    """This seat's use of the shared rule (memecoin_desk_model_echo): MODEL or a dated snapshot of it.
    A missing echo is allowed, as in every seat."""
    return model_echo.echo_ok(echo, MODEL)
DEADLINE_S = 5.0
TTL_S = 10.0               # a decision older than this at apply time would be refused (recorded)
COOLDOWN_S = 30.0          # per position between calls; a boundary inside it is recorded, not asked
TIMECAP_LEAD_S = 60.0      # the timecap - N boundary
MARK_MAX_AGE_S = 30.0
BOUNDARIES = ("fill", "rail_hit", "structure_break", "timecap")
SYSTEM_PROMPT = """You are the RISK seat of an automated memecoin desk. One open position, its rails and its
last 60 one-second bars (relative to the actual fill; absent conversion means unknown). Rails already run on every tick. You may
answer HOLD, TIGHTEN (make one or more rails stricter: a smaller stop, a smaller trail, a smaller take
profit, or add a trail or take profit) or EXIT (close now). You can never widen a rail, switch one off
or add size. Answer one JSON object only:
{"k": <position key>, "a": "H"|"T"|"E", "r": null | {"sl_pct"?: n, "trail_pct"?: n, "tp_pct"?: n}, "c": 1-16}
"""


def _ts(iso):
    return datetime.fromisoformat(iso).timestamp()


def structure_break(bars, n=STRUCTURE_N):
    """The start second of the current risk-break-v1 streak, or None. The last n bars must be
    consecutive seconds, each closing under the low of the STRUCTURE_PRIOR_S seconds before the run;
    the key is where that below-low streak began, so one streak is one boundary."""
    if len(bars) < n:
        return None
    run = bars[-n:]
    if any(b["t"] - a["t"] != 1 for a, b in zip(run, run[1:])):
        return None                                                   # a gap resets
    start = run[0]["t"]
    prior = [b["l"] for b in bars if start - STRUCTURE_PRIOR_S <= b["t"] < start]
    if not prior:
        return None
    low = min(prior)
    if not all(b["c"] < low for b in run):
        return None
    i = len(bars) - n
    while i > 0 and bars[i]["t"] - bars[i - 1]["t"] == 1 and bars[i - 1]["c"] < low:
        i -= 1
    return bars[i]["t"]


def _buckets(bars, size=SB_BUCKET_S):
    """1 s bars -> {bucket start: {o,h,l,c,buy,sell,last_t}} (a bucket's close is its LAST second's close)."""
    out = {}
    for b in sorted(bars, key=lambda x: x["t"]):
        if not all(_finite_number(b.get(k)) and b[k] > 0 for k in ("o", "h", "l", "c")):
            continue
        s = int(b["t"] // size) * size
        x = out.setdefault(s, {"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "buy": 0.0, "sell": 0.0, "last_t": b["t"]})
        x["h"], x["l"], x["c"], x["last_t"] = max(x["h"], b["h"]), min(x["l"], b["l"]), b["c"], b["t"]
        x["buy"] += b.get("buy_usd") or 0.0
        x["sell"] += b.get("sell_usd") or 0.0
    return out


SB_LATE_S = 2.0            # candle write lag: with no later bar, a bucket is closed only once the clock is this far past its last second


def sb_closes(bars, *, k=SB_K, after=None, as_of=None):
    """risk-break-v2: the start second of the current break streak, or None. `bars` = 1 s bars in time order INCLUDING the pre-fill
    history. These are all-market marginal-price candles, including our own impact, not position executable value V. Only COMPLETE
    5 s buckets count (every second up to the bucket's end is covered by the newest bar). Support = the lowest
    LOW of the SB_PRIOR_BUCKETS buckets before the run (>= SB_MIN_PRIOR_BUCKETS with data). A close is a break only when it is under
    support by MORE than k x the coin's own median 5 s range (median over the trailing 300 s, >= SB_MIN_NOISE_BUCKETS bars). Fires on
    (A) two CONSECUTIVE bucket closes under that line, or (B) one bucket close under it with sellers dominating the bucket
    (sell USD > buy USD). Unknown noise, unknown support or a gap is NOT a break (None)."""
    buckets = _buckets(bars)
    if not buckets:
        return None
    newest = max(b["t"] for b in bars if _finite_number(b.get("t")))
    # A bucket is CLOSED only when its last second has ELAPSED at `as_of` (chain UTC) by the candle write lag: completion is a fact about the
    # clock, never inferred from a trade in the bucket's last second (the newest 1 s candle is still forming) nor from its absence (a sparse
    # bucket closes like any other). as_of=None means the caller asserts the bars are complete through the newest one (no lag margin).
    eff = (newest + 1.0) if as_of is None else (as_of - SB_LATE_S)

    def closed(s):
        return s + SB_BUCKET_S <= eff
    keys = [s for s in sorted(buckets) if closed(s)]
    if len(keys) < 2:
        return None
    if after is not None and keys[-1] < (int(after // SB_BUCKET_S) + 1) * SB_BUCKET_S:
        return None                                        # the newest closed bucket is not after the fill: any run is pre-fill history
    # NOISE = median over the trailing 300 s of ln(high/low) per 5 s bucket (Gamma's range5 pin, 2026-10-23): a bucket with no swap counts 0,
    # buckets count from the coin's first bar; the still-open newest bucket is left out, never counted as quiet.
    first = max(min(buckets), keys[-1] - (SB_NOISE_BUCKETS - 1) * SB_BUCKET_S)
    ranges = sorted(math.log(buckets[s]["h"] / buckets[s]["l"]) if s in buckets else 0.0
                    for s in range(first, keys[-1] + SB_BUCKET_S, SB_BUCKET_S) if s in keys or s not in buckets)
    if len(ranges) < SB_MIN_NOISE_BUCKETS:
        return None
    noise = ranges[len(ranges) // 2] if len(ranges) % 2 else (ranges[len(ranges) // 2 - 1] + ranges[len(ranges) // 2]) / 2
    if not noise > 0:
        return None                                                    # a coin that has not moved in 300 s has no noise estimate: unknown
    margin = k * noise

    def support(s):
        lows = [buckets[x]["l"] for x in range(s - SB_PRIOR_BUCKETS * SB_BUCKET_S, s, SB_BUCKET_S) if x in buckets]
        return min(lows) if len(lows) >= SB_MIN_PRIOR_BUCKETS else None

    def under(s, sup):
        return sup is not None and buckets[s]["c"] < sup * math.exp(-margin)       # margin = k x LOG noise: compare in log distance
    last = keys[-1]
    chain = [last]                                                     # the contiguous complete buckets ending at the newest
    while len(chain) < SB_PRIOR_BUCKETS and (chain[-1] - SB_BUCKET_S) in buckets and (chain[-1] - SB_BUCKET_S) in keys:
        chain.append(chain[-1] - SB_BUCKET_S)
    for start in sorted(chain):                                        # the EARLIEST start whose whole run sits under ITS OWN pre-run support
        sup = support(start)
        run = [x for x in chain if x >= start]
        if sup is not None and all(under(x, sup) for x in run):
            if len(run) >= 2 or buckets[last]["sell"] > buckets[last]["buy"]:
                return start
            return None
    return None


def boundaries(pos, policy, *, now, seen, rail_fill_ids, bars, structure_n=STRUCTURE_N):
    """[(kind, key)] boundaries due for this position that this worker has not recorded yet.
    `seen` = recorded (kind, key) pairs; `rail_fill_ids` = the position's non-entry fill ids."""
    due = []
    if ("fill", "fill") not in seen:
        due.append(("fill", "fill"))
    for fid in sorted(rail_fill_ids):
        if ("rail_hit", str(fid)) not in seen:
            due.append(("rail_hit", str(fid)))
    hold = float(policy.get("max_hold_s") or 0)
    if hold > 0 and now >= _ts(pos["opened_at"]) + hold - TIMECAP_LEAD_S and ("timecap", "timecap") not in seen:
        due.append(("timecap", "timecap"))
    start = structure_break(bars, structure_n) if structure_n else None
    if start is not None and ("structure_break", str(start)) not in seen:
        due.append(("structure_break", str(start)))
    return due


def _finite_number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except OverflowError:
        return False


def pnl_basis(pos, mark, *, now):
    """Why pnl_pct_exact is what it is: 'exact' when it is a number, else the reason it is null. Never a stand-in
    0 or -100 (Gamma 2026-10-23)."""
    notional = pos.get("entry_notional")
    if not _finite_number(notional) or notional <= 0:
        return "no_entry_notional"
    if not mark:
        return "no_exact_quote"
    value = mark.get("executable_exit_value")
    if not _finite_number(value):
        return "no_exact_quote"
    if value <= 0:
        return "zero_exact_quote"           # a zero-out route is a failed quote, not a known -100%
    if not _finite_number((value / notional - 1) * 100):
        return "no_exact_quote"
    age = _mark_age(mark, now)
    if age is None:
        return "no_exact_quote"
    if age < 0 or age > MARK_MAX_AGE_S:
        return "stale_exact_quote"
    return "exact"


def _mark_age(mark, now):
    try:
        stamp = datetime.fromisoformat(mark["sweep_ts"])
        if stamp.tzinfo is None or not _finite_number(now):
            return None
        return now - stamp.timestamp()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def fill_baseline(pos, sol_usd):
    """Actual USD/token fill expressed in the SOL/token denomination of the curve candles."""
    entry = pos.get("entry_price")
    if any(not _finite_number(v) or v <= 0 for v in (entry, sol_usd)):
        return None
    base = entry / sol_usd
    return base if math.isfinite(base) and base > 0 else None


def facts(pos, policy, *, mark, bars, boundary, now, sol_usd=None):
    """`now` is corrected UTC like ledger timestamps and candle t; missing values stay null."""
    notional = pos.get("entry_notional")
    value = (mark or {}).get("executable_exit_value")
    mark_age = _mark_age(mark, now) if mark else None
    pnl = round((value / notional - 1) * 100, 3) if pnl_basis(pos, mark, now=now) == "exact" else None
    base = fill_baseline(pos, sol_usd)
    norm = [{"t": b["t"], "o": _ratio(b["o"], base), "h": _ratio(b["h"], base), "l": _ratio(b["l"], base),
             "c": _ratio(b["c"], base), "buy_usd": b.get("buy_usd"), "sell_usd": b.get("sell_usd"),
             "makers": b.get("n_makers")} for b in bars[-60:]] if base else []
    hold = float(policy.get("max_hold_s") or 0)
    return {"k": "pos:%d" % pos["id"], "boundary": boundary,
            "rails": {"sl_pct": policy.get("stop_pct"), "trail_pct": policy.get("trail_pct"),
                      "tp_pct": policy.get("tp_pct"), "catastrophe_pct": policy.get("catastrophe_pct")},
            "held_s": round(now - _ts(pos["opened_at"]), 1),
            "to_timecap_s": round(_ts(pos["opened_at"]) + hold - now, 1) if hold > 0 else None,
            "pnl_pct_exact": pnl, "mark_age_s": round(mark_age, 1) if mark_age is not None else None,
            "bars_vs_fill": norm}


def _ratio(x, base):
    return round(x / base, 6) if isinstance(x, (int, float)) and base else None


def inputs_hash(f):
    return hashlib.sha256(json.dumps(f, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# LOOSEN_TRAIL (v8): the tokens a loosen must cite (code-built enums, never free text) and the reason codes it may use
CITE_TOKENS = frozenset({"trend_up", "higher_lows", "support_below", "breakout_holding", "delta_rising", "volume_accel",
                         "flow_buys", "shallow_dd"})
LOOSEN_CODES = (17, 18, 19)
MAX_CODE = 19


def loosen_cap(bounds=None):
    """The most a loosen may set trail_pct to: MEMECOIN_DESK_RISK_TRAIL_MAX_PCT, never above the cohort's trail upper bound."""
    import sniper.settings.memecoin as cfg
    cap = float(getattr(cfg, "MEMECOIN_DESK_RISK_TRAIL_MAX_PCT", 25.0))
    hi = ((bounds or {}).get("trail_pct") or (None, None))[1]
    return min(cap, float(hi)) if hi is not None else cap


def validate(text, *, key, policy, bounds):
    """(action, rails | None, reason, reason_code). Anything not a valid HOLD / tightening TIGHTEN /
    EXIT is HOLD with the reason; a TIGHTEN that does not tighten every rail it names is refused.
    v8: action "L" = LOOSEN_TRAIL, rails {"trail_pct": n} ONLY, plus "f" = 1-4 cited CITE_TOKENS; the reason of a
    loosen is "loosen:<tok>,<tok>" (code-built). Anything else about it is a HOLD with the cause."""
    try:
        raw = json.loads((text or "").strip())
    except ValueError:
        return "HOLD", None, "malformed_json", None
    keys = set(raw) if isinstance(raw, dict) else set()
    if not isinstance(raw, dict) or keys not in ({"k", "a", "r", "c"}, {"k", "a", "r", "c", "f"}) \
            or ("f" in keys and raw.get("a") != "L"):
        return "HOLD", None, "verdict_keys", None
    code = raw["c"] if type(raw["c"]) is int and 1 <= raw["c"] <= MAX_CODE else None
    if raw["k"] != key:
        return "HOLD", None, "key_mismatch", code
    if raw["a"] == "H":
        return "HOLD", None, "hold", code
    if raw["a"] == "E":
        return "EXIT", None, "exit", code
    if raw["a"] == "L":
        import sniper.settings.memecoin as cfg
        if not getattr(cfg, "MEMECOIN_DESK_RISK_TRAIL_LOOSEN", True):
            return "HOLD", None, "loosen_disabled", code
        if "f" not in keys:
            return "HOLD", None, "loosen_no_cite", code
        cited = raw["f"]
        if (not isinstance(cited, list) or not 1 <= len(cited) <= 4 or len(set(map(str, cited))) != len(cited)
                or any(type(c) is not str or c not in CITE_TOKENS for c in cited)):
            return "HOLD", None, "loosen_bad_cite", code
        if code not in LOOSEN_CODES:
            return "HOLD", None, "loosen_bad_code", code
        r = raw["r"]
        if not isinstance(r, dict) or set(r) != {"trail_pct"}:
            return "HOLD", None, "loosen_rails", code
        value = r["trail_pct"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            return "HOLD", None, "loosen_rails", code
        cur = float(policy.get("trail_pct") or 0)
        if not cur > 0:
            return "HOLD", None, "loosen_no_trail", code                 # an OFF trail is never created by a loosen
        if not value > cur:
            return "HOLD", None, "loosen_not_wider", code
        if value > loosen_cap(bounds):
            return "HOLD", None, "loosen_above_cap", code
        return "LOOSEN_TRAIL", {"trail_pct": float(value)}, "loosen:" + ",".join(sorted(cited)), code
    if raw["a"] != "T" or not isinstance(raw["r"], dict) or not raw["r"]:
        return "HOLD", None, "verdict_enum", code
    current = {"sl_pct": policy.get("stop_pct"), "trail_pct": policy.get("trail_pct"), "tp_pct": policy.get("tp_pct")}
    rails = {}
    for rail, value in raw["r"].items():
        if rail not in current or isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value) or value <= 0:
            return "HOLD", None, "tighten_loosens:%s" % rail, code      # off, zero, unknown: never
        lo, hi = (bounds or {}).get(rail, (None, None))
        if lo is None or not lo <= value <= hi:
            return "HOLD", None, "out_of_bounds:%s" % rail, code
        cur = float(current[rail] or 0)
        if rail == "sl_pct" and not value < abs(cur):
            return "HOLD", None, "tighten_loosens:sl_pct", code
        if rail in ("trail_pct", "tp_pct") and cur > 0 and not value < cur:
            return "HOLD", None, "tighten_loosens:%s" % rail, code
        rails[rail] = float(value)
    return "TIGHTEN", rails, "tighten", code


# ---------------------------------------------------------------------------------------------------------- measured evidence

EVIDENCE_VERSION = "risk-evidence-v2"


def _num(v):
    return _finite_number(v)


def risk_evidence(swaps, *, as_of, opened_ts, entry_price, sb, coverage=None, bars=None, ours_sigs=frozenset(), ours_complete=False):
    """MEASURED facts about one position's coin for an evidence-gated validator: no thresholds, no free text, no I/O, no pnl, no policy.

    Prices are marginal reserve prices, not fee/quantity-bound executable V; fee-tier changes need separate exact quotes. n5 is median
    5 s high/low log range, not the death study's close-to-close noise_5. sb is supplied from all-market candles and includes our impact;
    own-fill exclusion applies to the facts computed here, not to that separate structure-break population.

    Inputs, all in ONE clock (the swaps tape's host clock; the caller converts):
      swaps     already loaded [{received_at, is_buy, quote_atomic, actor, signature}]: CURVE prices use virtual_quote/virtual_token; normalized
                PumpSwap rows explicitly supply price_sol (SOL/token). A present but invalid price_sol is unknown, never a reserve fallback.
      coverage  {"from": t0, "through": t1}: the interval the capture is PROVEN alive and complete for this coin's venue (the caller derives it from the
                committed watermark and the gap/stop tables). None = no proof: every window fact is None. NO end watermark is ever manufactured here.
      ours_sigs / ours_complete  signatures of OUR verified fills; flows, sellers and prices exist only when ours_complete is True (the caller
                asserts the list is complete) AND every row of the window carries a signature, so our own fills are provably excluded.
    end = min(floor(as_of - SB_LATE_S), floor(coverage.through)): only ELAPSED seconds that the capture covers. A window [a, b) is PROVEN iff
    coverage.from <= a and b <= end; anything else is None and its name is listed in `unknown` (never a zero, never inactivity).
    Row integrity: malformed amounts/reserves poison their windows; an invalid time cannot be located and poisons all windows. NULL quotes or unknown
    directions poison flows; missing/zero reserves poison prices. Swap COUNTS still count well-timed unpriceable/unknown-direction events. Aggregate
    maker bars cannot exclude our actors, so their fields stay unknown."""
    unknown = []

    def known(name, value):
        if value is None:
            unknown.append(name)
        return value
    cov_from = coverage.get("from") if isinstance(coverage, dict) else None
    cov_to = coverage.get("through") if isinstance(coverage, dict) else None
    have_cov = (_num(cov_from) and _num(cov_to) and _num(as_of)
                and 0 <= cov_from <= cov_to and as_of >= SB_LATE_S)
    end = min(math.floor(as_of - SB_LATE_S), math.floor(cov_to)) if have_cov else None
    if end is not None and end < cov_from:
        end = None

    def identity(value):
        return isinstance(value, str) and bool(value.strip())

    nonours_ok = (ours_complete is True and isinstance(ours_sigs, (set, frozenset, tuple, list))
                  and all(identity(sig) for sig in ours_sigs))
    own_sigs = frozenset(ours_sigs) if nonours_ok else frozenset()
    rows, rejected, timed = [], [], []
    unlocated = 0
    for r in swaps:
        if not isinstance(r, dict) or not _num(r.get("received_at")) or r["received_at"] < 0:
            unlocated += 1
        else:
            timed.append(r)
    for r in sorted(timed, key=lambda x: x["received_at"]):
        t = r["received_at"]
        if end is not None and t >= end:
            continue
        q = r.get("quote_atomic")
        vq, vt = r.get("virtual_quote"), r.get("virtual_token")
        bad_amount = q is not None and not (_num(q) and q >= 0)
        priceable = _num(vq) and _num(vt) and vq > 0 and vt > 0
        direct_price = "price_sol" in r
        bad_reserves = (not direct_price and ((vq is not None and not _num(vq)) or (vt is not None and not _num(vt))
                                             or (_num(vq) and vq < 0) or (_num(vt) and vt < 0)))
        if bad_amount or bad_reserves:
            rejected.append(t)                              # malformed: the row cannot be trusted for any figure
            continue
        sig = r.get("signature") if identity(r.get("signature")) else None
        actor = r.get("actor") if identity(r.get("actor")) else None
        direction = r.get("is_buy")
        buy = bool(direction) if type(direction) in (bool, int) and direction in (0, 1) else None
        # SOL/token, without underflowing a separately scaled denominator; PS price units/provenance are caller-attested.
        px = r["price_sol"] if direct_price else ((vq / vt) / 1e3 if priceable else None)
        rows.append({"t": t, "buy": buy, "sol": None if q is None else q / 1e9, "actor": actor, "sig": sig,
                     "px": px if _num(px) and px > 0 else None, "ours": sig in own_sigs})

    def proven(a, b):
        return end is not None and cov_from <= a <= b <= end

    def win(a, b):
        return [x for x in rows if a <= x["t"] < b]

    def has_bad(a, b):
        return bool(unlocated) or any(a <= t < b for t in rejected)

    def nonown_rows(a, b):
        if not proven(a, b) or has_bad(a, b) or not nonours_ok:
            return None
        w = win(a, b)
        if any(x["sig"] is None for x in w):
            return None
        return [x for x in w if not x["ours"]]

    def px_rows(a, b):
        """Priced rows of a proven window, or None when the window is unproven / holds a malformed or unpriceable row."""
        w = nonown_rows(a, b)
        if w is None or any(x["px"] is None for x in w):
            return None
        return w

    def flow_rows(a, b):
        """Rows of a proven window with provable exclusion of our fills and every quote known, else None."""
        w = nonown_rows(a, b)
        if w is None or any(x["sol"] is None or x["buy"] is None for x in w):
            return None
        return w

    out = {"version": EVIDENCE_VERSION, "as_of": as_of, "known_through": end, "last_closed_at": end,
           "sb": int(sb) if type(sb) in (bool, int) and sb in (0, 1) else 0, "entry_price": entry_price,
           "coverage_from": cov_from if have_cov else None, "coverage_through": cov_to if have_cov else None,
           "stale_s": None if end is None else as_of - end, "rejected_rows": len(rejected) + unlocated}
    if end is None:
        unknown.append("coverage")
    opened_ok = end is not None and _num(opened_ts) and 0 <= opened_ts <= end
    out["held_s"] = known("held_s", end - opened_ts if opened_ok else None)
    last = next((x for x in reversed(rows) if not x["ours"]), None)
    last_ok = last is not None and end is not None and nonown_rows(last["t"], end) is not None
    out["last_swap_at"] = last["t"] if last_ok else None
    out["last_swap_age_s"] = known("last_swap_age_s", end - last["t"] if last_ok else None)
    out["coverage_start"] = known("coverage_start", rows[0]["t"] if rows else None)
    out["last_price"] = known("last_price", last["px"] if last_ok else None)
    # price windows: ours excluded only when provable (our fills move the price; without the complete list the extremes are unknown too)
    def extreme(name, a, fn):
        w = px_rows(end - a, end) if end is not None else None
        if w is None:
            return known(name, None)
        return known(name, fn(x["px"] for x in w) if w else None)
    out["low_30s"], out["high_30s"] = extreme("low_30s", 30, min), extreme("high_30s", 30, max)
    out["max_price_30s"] = out["high_30s"]
    out["low_60s"], out["high_60s"] = extreme("low_60s", 60, min), extreme("high_60s", 60, max)
    out["low_120s"], out["high_120s"] = extreme("low_120s", 120, min), extreme("high_120s", 120, max)
    # since the fill: needs capture proven from the fill itself, an anchor price at/before the fill inside the coverage, and a clean priced history
    post_ok = opened_ok and proven(opened_ts, end) and nonours_ok
    anchor_rows = [x for x in rows if cov_from <= x["t"] <= opened_ts and not x["ours"]] if post_ok else []
    anchor_row = anchor_rows[-1] if anchor_rows else None
    history = px_rows(anchor_row["t"], end) if anchor_row is not None else None
    post_ok = post_ok and history is not None
    since = [x for x in history if x["t"] > opened_ts] if post_ok else []
    anchor = anchor_row["px"] if post_ok else None
    high = anchor if post_ok and anchor is not None else None
    t_new_high, low = None, anchor if (post_ok and anchor is not None) else None
    if post_ok and anchor is not None:
        for x in since:
            if x["px"] > high:
                high, t_new_high = x["px"], x["t"]
            low = min(low, x["px"])
    out["anchor_price"] = known("anchor_price", anchor if post_ok else None)
    out["high_since_fill"] = known("high_since_fill", high if post_ok and anchor is not None else None)
    out["low_since_fill"] = known("low_since_fill", low if post_ok and anchor is not None else None)
    # A proven run with no new high has stagnated since the fill; only unproven history is unknown.
    out["secs_since_new_high"] = known("secs_since_new_high", None if not (post_ok and anchor is not None)
                                       else end - (opened_ts if t_new_high is None else t_new_high))
    out["new_high_60s"] = known("new_high_60s", None if not (post_ok and anchor is not None) or end - opened_ts < 60 else bool(t_new_high is not None and end - t_new_high <= 60))
    out["new_high_120s"] = known("new_high_120s", None if not (post_ok and anchor is not None) or end - opened_ts < 120 else bool(t_new_high is not None and end - t_new_high <= 120))

    def post_ext(name, a, fn):
        ok = post_ok and anchor is not None and end - a >= opened_ts
        w = px_rows(end - a, end) if ok else None
        return known(name, fn(x["px"] for x in w) if w else None)
    out["high_30s_post_fill"], out["low_30s_post_fill"] = post_ext("high_30s_post_fill", 30, max), post_ext("low_30s_post_fill", 30, min)
    out["high_120s_post_fill"] = post_ext("high_120s_post_fill", 120, max)
    w30, wp30 = flow_rows(end - 30, end) if end is not None else None, flow_rows(end - 60, end - 30) if end is not None else None
    out["buy_sol_30s"] = known("buy_sol_30s", None if w30 is None else sum(x["sol"] for x in w30 if x["buy"]))
    out["sell_sol_30s"] = known("sell_sol_30s", None if w30 is None else sum(x["sol"] for x in w30 if not x["buy"]))
    out["distinct_sellers_30s"] = known("distinct_sellers_30s", None if w30 is None or any(x["actor"] is None for x in w30 if not x["buy"])
                                        else len({x["actor"] for x in w30 if not x["buy"]}))
    out["buy_sol_prev_30s"] = known("buy_sol_prev_30s", None if wp30 is None else sum(x["sol"] for x in wp30 if x["buy"]))
    b30, s30 = out["buy_sol_30s"], out["sell_sol_30s"]
    out["sell_share_30s"] = known("sell_share_30s", None if b30 is None or s30 is None or b30 + s30 <= 0 else s30 / (b30 + s30))
    # activity counts EVERY well-timed event (an unpriceable swap is activity); our own fills excluded only when provable
    def count(a, b):
        w = nonown_rows(a, b)
        return len(w) if w is not None else None
    out["swaps_60s"] = known("swaps_60s", count(end - 60, end) if end is not None else None)
    blocks = [count(end - 360 + 60 * i, end - 300 + 60 * i) for i in range(5)] if end is not None else [None]
    med = statistics.median(blocks) if all(b is not None for b in blocks) else None
    out["swaps_median_60s_prior_300s"] = known("swaps_median_60s_prior_300s", med)
    out["swaps_60s_vs_median"] = known("swaps_60s_vs_median", out["swaps_60s"] / med if out["swaps_60s"] is not None and med and med > 0 else None)
    # n5 may use pre-fill history, but only from proven, clean, priced slots
    n5 = None
    noise_end = int(end // SB_BUCKET_S) * SB_BUCKET_S if end is not None else None
    priced = px_rows(noise_end - 300, noise_end) if noise_end is not None else None
    if priced:
        buckets = []
        for x in priced:
            idx = int(x["t"] // SB_BUCKET_S)
            if not buckets or buckets[-1][0] != idx:
                buckets.append([idx, x["px"], x["px"], x["px"], x["px"], 0.0, 0.0, 0, 0])
            b = buckets[-1]
            b[2], b[3], b[4] = max(b[2], x["px"]), min(b[3], x["px"]), x["px"]
        from sniper.services import memecoin_desk_tape_shape as TS
        n5 = TS.range5(buckets, int(noise_end // SB_BUCKET_S), int((noise_end - 300) // SB_BUCKET_S))
        if not _num(n5):
            n5 = None
    out["n5"] = known("n5", n5)
    # Aggregate bars have no actor/signature membership from which to remove our own makers.
    out["max_makers_per_1s_30s"] = known("max_makers_per_1s_30s", None)
    out["max_makers_per_1s_prev_30s"] = known("max_makers_per_1s_prev_30s", None)
    out["unknown"] = sorted(set(unknown))
    return out

"""LESSONS: computed facts about what each desk trade (and each trade we did NOT take) could have been, from the curve tape.

REPORT ONLY (operator order 2026-10-23 via Alpha: "we need something to learn from the trades, what we did, what could've done
better"). Pure functions over tape rows, no I/O except the explicit loaders; nothing here feeds a JUDGE/RISK/entry decision, writes a ledger
or config row (rule b9930440). Rows are IMMUTABLE: one per (position/ticket key, feature_version), INSERT OR IGNORE; a new feature version
adds rows and never edits old ones.

Model: the exact bonding-curve buy/sell on the tape's post-swap reserves, fees included, validated within 0.4% on our own exit swaps
(docs/audits/2026-10-23-desk-scaleout-counterfactual.md). After a graduation the exact PumpSwap AMM sell on the pool tape. Everything is in
SOL terms; R = value / cost - 1 where cost = the SOL actually spent (realized) or the clip at FX0 (counterfactual kinds). A fact whose inputs
are missing is NULL with a reason, never zero.

KINDS: 'realized' (a closed desk position), 'counterfactual' (a JUDGE ENTER that was refused or never attempted: entered at the verdict time
+ the measured fill latency), 'skip_counterfactual' (a hash-sampled JUDGE SKIP / never-vetted hard-filter PASS, same entry model). Only
'realized' rows carry USD into the sums; the others say what selection and admission cost.

Alternatives replayed on the same tape (all exits at an exact sell with a 1 s lag; a path with no exit by 900 s is marked at its exact sell):
rails as JUDGE set them, fixed exits at 10/30/60/120 s, hold 300 s with a 50% catastrophe stop, Gamma's death rule (ANY of six indicators,
one noise multiple k; k is PROVISIONAL until her discovery table exists, stored in every row), and sell on the curve at 90% fill (the
migration step). A position still held at curve completion sells on the pool: with no pool swap in the tape the value is NULL (censored).
"""
from __future__ import annotations

import bisect
import hashlib
import json
import math
import sqlite3
import statistics
from datetime import datetime, timezone
from pathlib import Path

FEATURE_VERSION = "lessons-v3"
FX0 = 104.329875
CLIP_USD = 25.0
LAG_S = 1.0
HOLD_S = 900
COST = 0.025                                   # round-trip cost shown as the net column (Gamma's pre-registered 2.5 pp)
DEATH_K = 4                                    # PROVISIONAL until Gamma's discovery table (stored in every row)
GRAD_REAL_QUOTE = 84_000_000_000               # lamports of real SOL at curve completion (memecoin_desk_ps_tape.GRADUATION_REAL_SOL)
SELL_BEFORE_FILL = 0.90                        # the migration-step exit tested in the exits replay
WINDOW_S = 120                                 # entry-regret window either side of the fill
FIXED_EXITS = (10, 30, 60, 120)
LEARNING_TRAILS = (10, 15, 20)
LEARNING_STAGE_NAMES = ("4-10k", "10-20k", "20-30k", "30k-completion (curve)")
ENTRY_LAG_S = 1.5
RETURN_HORIZON_S = 300
POOL_FRESH_S = 60                              # a pool state older than this at the asked second is not the value then: unknown
MIGRATION_MAX_S = 60                           # the first pool state must exist within this long after completion, else unknown
GIVE_UP_S = 2 * 86400                          # a unit whose tape is still missing this long after its time is stored as censored for good
KINDS = ("realized", "counterfactual", "skip_counterfactual")
TAGS = ("entered_top", "late_curve", "shaken_out", "held_through_death", "good_exit", "migration_step", "entered_farm")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lessons(
  key TEXT NOT NULL, feature_version TEXT NOT NULL, kind TEXT NOT NULL, mint TEXT NOT NULL, position_id INTEGER, ticket_id TEXT,
  computed_at REAL NOT NULL, t0 REAL, facts_json TEXT NOT NULL, tags_json TEXT NOT NULL, PRIMARY KEY(key, feature_version));
CREATE INDEX IF NOT EXISTS lessons_kind ON lessons(kind, t0);
"""


def version_num(v):
    """'lessons-v10' -> 10 (the order of versions is numeric, never lexical: v10 is newer than v2); 0 when unparseable."""
    try:
        return int(str(v).rsplit("-v", 1)[1])
    except (IndexError, ValueError):
        return 0


def connect(path):
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _fin(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _r(x, nd=4):
    return round(x, nd) if _fin(x) else None


# ------------------------------------------------------------------------------------------------------------- the tape


def _curve_fee_parts(r):
    """Native integer fee components, or None for missing/malformed proof; unknown never becomes zero."""
    try:
        raw = (r.get("fee_bps"), r.get("creator_fee_bps"))
        parts = tuple(int(x) for x in raw)
        if any(isinstance(x, bool) or float(x) != n or not 0 <= n < 10000 for x, n in zip(raw, parts)):
            return None
        return parts
    except (TypeError, ValueError, OverflowError):
        return None


def curve_sell_net_sol(vq, vt, qty, parts, real_quote=None):
    """Native sell arithmetic on the post-swap reserves: the constant-product gross (floor) less each fee rounded UP, in SOL. The curve can
    never pay out more SOL than it really holds: a gross above the REAL quote reserve is not executable (None), and an unknown real reserve is
    unproven (None), never assumed."""
    if not vq or not vt or qty <= 0 or parts is None or real_quote is None:
        return None
    gross = qty * vq // (vt + qty)
    if gross > real_quote:
        return None
    return (gross - sum(-(-gross * bps // 10_000) for bps in parts)) / 1e9


def ps_sell_net_sol(qty, st):
    """Exact AMM sell on a native pool state (pumpswap_sell.expected_quote_out): gross = (real quote + virtual quote) * in // (base + in),
    capped at the REAL quote reserve, less each fee component rounded UP independently. None when the state or qty is unusable."""
    base, rq, vq = int(st["base"]), int(st["rq"]), int(st["vq"])
    qty = int(qty)
    if qty <= 0 or base <= 0 or rq <= 0:
        return None
    gross = min((rq + vq) * qty // (base + qty), rq)
    return max(0, gross - sum(-(-gross * int(b) // 10_000) for b in st["bps"])) / 1e9


def _native_state(st):
    """Pool states come native (t, base, rq, vq, bps tuple, known_at). The legacy fixture shape {t, q, b, fee} is adapted: q = the real quote
    reserve, no virtual quote, the whole fee as one component."""
    if "base" in st:
        return st
    return {"t": st["t"], "base": st["b"], "rq": st["q"], "vq": 0, "bps": (int(round(st["fee"] * 10_000)),), "known_at": st["t"]}


def curve_spend_for_net(net_lamports, parts):
    """The spend that puts `net` lamports into the curve (pump_curve_buy.curve_buy_net inverted): net + each fee rounded up."""
    return net_lamports + sum(-(-net_lamports * bps // 10_000) for bps in parts)


class Tape:
    """One coin's curve swaps in time order, plus (after a graduation) its pool states. SOL terms, native integer arithmetic.
    coverage_until = the last second the capture is PROVEN alive for this coin (worker: min(the day tape's global high-water, the
    first gap after the entry); default: the newest swap/pool state of this tape). Nothing past it is a value, a mark or a zero."""

    def __init__(self, rows, pool_states=(), coverage_until=None, *, _ordered=False, pool_invalid_states=()):
        rows = list(rows) if _ordered else sorted(rows, key=lambda r: (r["received_at"], r["slot"], r["ordinal"]))
        self.rows = rows
        self.t = [r["received_at"] for r in rows]
        self.vq = [r["virtual_quote"] for r in rows]
        self.vt = [r["virtual_token"] for r in rows]
        self.parts = [_curve_fee_parts(r) for r in rows]
        self.real_token = [r.get("real_token") for r in rows]
        self.real_quote = [r.get("real_quote") for r in rows]
        self.buy = [bool(r["is_buy"]) for r in rows]
        self.sol = [(r["quote_atomic"] or 0) / 1e9 for r in rows]
        self.tok = [(r["token_atomic"] or 0) for r in rows]
        self.actor = [r["actor"] for r in rows]
        self.price = [(q / 1e9) / (t / 1e6) if q and t else None for q, t in zip(self.vq, self.vt)]
        self.complete = [r.get("real_quote") is not None and r["real_quote"] >= GRAD_REAL_QUOTE for r in rows]
        self.fill = [(r["real_quote"] / GRAD_REAL_QUOTE) if r.get("real_quote") is not None else None for r in rows]
        self.pool = sorted((_native_state(x) for x in pool_states), key=lambda s: s["t"])
        self.pool_t = [s["t"] for s in self.pool]
        self.pool_invalid = sorted(pool_invalid_states, key=lambda s: s["t"])
        self.pool_invalid_t = [s["t"] for s in self.pool_invalid]
        self.done = next((i for i, c in enumerate(self.complete) if c), None)
        self.done_t = self.t[self.done] if self.done is not None else None
        last = max([self.t[-1] if self.t else 0.0] + ([self.pool_t[-1]] if self.pool_t else []))
        self.coverage_until = last if coverage_until is None else coverage_until

    def idx(self, s):
        return bisect.bisect_right(self.t, s) - 1

    def buy_tokens(self, i, cost_sol):
        """Native buy: pump_curve_buy.expected_tokens_out on the reserves after swap i (net = the largest whose fees fit, one lamport
        priced less, capped by the real token reserves). 0 when the fee or a reserve is unknown."""
        parts = self.parts[i]
        if parts is None or not self.vq[i] or not self.vt[i] or self.real_token[i] is None:
            return 0                                         # a fee, a reserve or the real token reserve unproven: no modelled buy
        from sniper.execution.pump_curve_buy import expected_tokens_out
        real = self.real_token[i]
        return expected_tokens_out(int(cost_sol * 1e9), {"fee_basis_points": parts[0], "creator_fee_basis_points": parts[1],
                                                         "cashback_fee_basis_points": 0, "virtual_sol_reserves": self.vq[i],
                                                         "virtual_token_reserves": self.vt[i], "real_token_reserves": real})

    def v(self, i, qty):
        if i < 0:
            return None
        return curve_sell_net_sol(self.vq[i], self.vt[i], qty, self.parts[i], self.real_quote[i])

    def pool_value_at(self, s, qty):
        """Exact AMM sell (SOL) at the LATEST pool state at or before s, provided it is not older than POOL_FRESH_S and not before the
        curve completed; None otherwise (never the first print AFTER s)."""
        j = bisect.bisect_right(self.pool_t, s) - 1
        if j < 0 or s - self.pool_t[j] > POOL_FRESH_S or (self.done_t is not None and self.pool_t[j] < self.done_t):
            return None
        if self.pool[j].get("known_at", self.pool_t[j]) > s:
            return None              # a sell state confirmed only by a later row was not known at s (Delta, fa033462f)
        return ps_sell_net_sol(qty, self.pool[j])

    def migration_value(self, qty):
        """The first executable pool value at/after completion + the exit lag, within MIGRATION_MAX_S; None = no pool tape there."""
        if self.done_t is None:
            return None
        j = bisect.bisect_left(self.pool_t, self.done_t + LAG_S)
        if j >= len(self.pool) or self.pool_t[j] - self.done_t > MIGRATION_MAX_S:
            return None
        return Tape.pool_value_at(self, self.pool_t[j], qty)    # the known_at guard applies here too

    def value_at(self, s, qty):
        """Executable value of qty at second s on the venue that holds the coin then; None = unknown (beyond the proven coverage,
        no fresh pool state, a NULL fee)."""
        if s > self.coverage_until:
            return None
        i = self.idx(s)
        if i < 0:
            return None
        if self.done is not None and s >= self.done_t:
            return self.pool_value_at(s, qty)
        return self.v(i, qty)


def noise_5(tape, s, upto_i):
    closes = []
    for k in range(61):
        i = tape.idx(s - 300 + 5 * k)
        closes.append(tape.price[i] if 0 <= i <= upto_i else None)
    first = next((j for j, x in enumerate(closes) if x is not None), None)
    if first is None:
        return None
    seq, last = [], None
    for x in closes[first:]:
        last = x if x is not None else last
        seq.append(last)
    rets = [abs(math.log(b / a)) for a, b in zip(seq, seq[1:]) if a and b]
    return statistics.median(rets) if len(rets) >= 12 else None


def _win_sum(tape, s, w, want_buy):
    i0, i1 = bisect.bisect_left(tape.t, s - w), bisect.bisect_right(tape.t, s)
    return sum(tape.sol[i] for i in range(i0, i1) if tape.buy[i] == want_buy)


def death_flag(tape, s, i, qty, vseries, noise, first_swapper, k=DEATH_K):
    """Gamma's six death indicators (death-vs-pullback pre-registration section 3) at one noise multiple k. Returns the list of
    firing indicator names (empty = none; unknown inputs fire nothing). Curve swaps only: after graduation nothing is computed."""
    fired = []
    bins = [_win_sum(tape, s - 10 * j, 10, False) for j in range(1, 31)]
    nonempty = [b for b in bins if b > 0]
    cur = _win_sum(tape, s, 10, False)
    if len(nonempty) >= 6 and cur > 0 and cur >= k * statistics.median(nonempty):
        fired.append("SELL_BURST")

    def buyers(end, w):
        i0, i1 = bisect.bisect_left(tape.t, end - w), bisect.bisect_right(tape.t, end)
        return len({tape.actor[j] for j in range(i0, i1) if tape.buy[j]})
    nb = [b for b in (buyers(s - 30 * j, 30) for j in range(1, 11)) if b > 0]
    if len(nb) >= 3 and buyers(s, 30) <= statistics.median(nb) / k and _win_sum(tape, s, 30, False) > _win_sum(tape, s, 30, True):
        fired.append("EXODUS")
    v_now = vseries(i)
    if noise is not None and v_now:
        i0, i1 = bisect.bisect_left(tape.t, s - 300), bisect.bisect_right(tape.t, s - 5)
        lows = [vseries(j) for j in range(i0, i1) if vseries(j)]
        if lows and _win_sum(tape, s, 15, False) > _win_sum(tape, s, 15, True) and min(lows) > v_now \
                and math.log(min(lows) / v_now) >= k * noise:
            fired.append("STRUCT_BREAK")
        j0, j1 = bisect.bisect_left(tape.t, s - 10), bisect.bisect_right(tape.t, s)
        drops = [math.log(vseries(j - 1) / vseries(j)) for j in range(max(j0, 1), j1)
                 if vseries(j) and vseries(j - 1) and vseries(j) < vseries(j - 1)]
        if drops and max(drops) >= k * noise:
            fired.append("ONE_SWAP_DROP")
    net = {}
    for j in range(0, bisect.bisect_left(tape.t, s - 30)):
        net[tape.actor[j]] = net.get(tape.actor[j], 0) + (tape.tok[j] if tape.buy[j] else -tape.tok[j])
    if net:
        top = max(net, key=net.get)
        if net[top] > 0:
            j0, j1 = bisect.bisect_left(tape.t, s - 30), bisect.bisect_right(tape.t, s)
            if sum(tape.tok[j] for j in range(j0, j1) if tape.actor[j] == top and not tape.buy[j]) >= net[top] / k:
                fired.append("TOP_DUMP")
    if first_swapper is not None:
        j0, j1 = bisect.bisect_left(tape.t, s - 30), bisect.bisect_right(tape.t, s)
        if any(tape.actor[j] == first_swapper and not tape.buy[j] for j in range(j0, j1)):
            fired.append("CREATOR_SELL")
    return fired


# ------------------------------------------------------------------------------------------------------- one unit's facts


def verdict_rank(tape, t_verdict):
    """entry_rank_prior_pct: where the executable price of the fixed research clip AT THE VERDICT second sits among the executable prices of the WINDOW_S strictly
    before it. A function of the verdict time and the tape up to it ONLY: it is computed before any later fill state can make an outcome fact unavailable."""
    if t_verdict is None:
        return None
    budget = CLIP_USD / FX0
    iv = tape.idx(t_verdict)
    qv = tape.buy_tokens(iv, budget) if iv >= 0 else 0
    if qv <= 0:
        return None
    prior = []
    for j in range(bisect.bisect_left(tape.t, t_verdict - WINDOW_S), bisect.bisect_right(tape.t, t_verdict - 1e-6)):
        tk = tape.buy_tokens(j, budget)
        if tk > 0:
            prior.append(budget / (tk / 1e6))
    if not prior:
        return None
    price_verdict = budget / (qv / 1e6)
    return _r(sum(1 for x in prior if x <= price_verdict) / len(prior) * 100, 1)


def facts_for(unit, tape, *, launch_at, fill_latency, exit_info=None, _learning_grid=False):
    """The facts for one unit. unit = {kind, t_ref, cost_sol (None -> clip at FX0), qty (None -> the native modelled buy), t_fill (None ->
    t_ref + latency), rails {sl_pct,tp_pct,trail_pct}|None, emitted_at}. exit_info (realized only) = {exit_ts, exit_sig, realized_pnl_usd,
    exit_reason, entry_notional_usd}. Returns (facts dict, tags list). Every time is on the TAPE clock."""
    cost = unit.get("cost_sol") or CLIP_USD / FX0
    t0 = unit.get("t_fill") or unit["t_ref"] + fill_latency
    i0 = tape.idx(t0)
    f = {"feature_version": FEATURE_VERSION, "kind": unit["kind"], "death_k": DEATH_K, "death_k_provisional": True, "fx0": FX0,
         "t0": t0, "cost_sol": _r(cost, 6), "censored": []}
    f["entry_rank_prior_pct"] = verdict_rank(tape, unit.get("t_verdict") if unit["kind"] == "realized" else unit.get("t_ref"))     # BEFORE any early return
    if i0 < 0:
        f["censored"].append("no_state_at_entry")
        return f, []
    qty = unit.get("qty") or tape.buy_tokens(i0, cost)
    if qty <= 0:
        f["censored"].append("no_buy")
        return f, []
    f["qty"] = qty
    price_paid = cost / (qty / 1e6)                       # SOL per whole token, incl. fees and our impact
    f["curve_fill_at_entry"] = _r(tape.fill[i0], 4)
    f["mcap_usd_at_entry"] = _r(tape.price[i0] * 1e9 * FX0, 0) if tape.price[i0] else None
    f["secs_band_cross_to_fill"] = _r(t0 - unit["emitted_at"], 1) if unit.get("emitted_at") else None

    def execs_in(a_, b_, budget=None):
        budget = cost if budget is None else budget
        out = []
        for j in range(bisect.bisect_left(tape.t, a_), bisect.bisect_right(tape.t, b_)):
            tk = tape.buy_tokens(j, budget)
            if tk > 0:
                out.append(budget / (tk / 1e6))
        return out
    execs = execs_in(t0 - WINDOW_S, t0 + WINDOW_S)
    if execs:
        best = min(execs)
        f["entry_best_achievable_price"] = _r(best, 12)
        f["entry_regret_pct"] = _r((price_paid / best - 1) * 100, 3)
        f["entry_rank_pct"] = _r(sum(1 for x in execs if x <= price_paid) / len(execs) * 100, 1)
        before, after_ = execs_in(t0 - WINDOW_S, t0 - 1e-6), execs_in(t0 + 1e-6, t0 + WINDOW_S)
        f["entry_vs_best_before_pct"] = _r((price_paid / min(before) - 1) * 100, 3) if before else None
        f["entry_vs_best_after_pct"] = _r((price_paid / min(after_) - 1) * 100, 3) if after_ else None
    else:
        f["censored"].append("no_entry_window")
    if not any(t0 < t <= t0 + HOLD_S for t in tape.t):                       # no swap in the window: quiet or capture gone, we cannot tell
        f["censored"].append("no_swaps_after_entry")
        return f, []
    # ---- per-second path from the fill: the bag's exact value on whichever venue holds the coin (curve, then the pool), as far as the
    # capture is PROVEN alive (coverage_until). A second with no executable value (migration gap, stale pool) is a None value.
    # the creator proxy = the coin's FIRST swapper, only when the tape holds the launch itself (its first swap within 5 s of the launch)
    first = tape.rows[0]["actor"] if (launch_at is not None and tape.rows and tape.rows[0]["received_at"] <= launch_at + 5) else None
    vcache = {}

    def vseries(i):
        if i not in vcache:
            vcache[i] = tape.v(i, qty)
        return vcache[i]
    seconds, peak = [], cost
    for k in range(1, HOLD_S + 1):
        s = t0 + k
        if s > tape.coverage_until:
            break
        i = tape.idx(s)
        pool_era = tape.done is not None and s >= tape.done_t
        v = tape.pool_value_at(s, qty) if pool_era else vseries(i)
        if v is not None:
            peak = max(peak, v)
        seconds.append({"s": s, "v": v, "peak": peak, "fill": tape.fill[min(i, len(tape.fill) - 1)], "i": i, "pool": pool_era,
                        "noise": None, "flag": None})
    if not seconds:
        f["censored"].append("no_coverage_after_entry")
        return f, []
    f["horizon_covered_s"] = int(round(seconds[-1]["s"] - t0))
    f["tape_end_after_entry_s"] = _r(tape.coverage_until - t0, 1)
    known = [x["v"] for x in seconds if x["v"] is not None]
    gap_inside = any(x["v"] is None for x in seconds)
    entry_complete = len(seconds) >= HOLD_S and not gap_inside
    # A POINT outcome (the max over the 900 s) exists only when every second of the window has an executable value; a partial path proves a
    # crossing (lower bound) but never a miss, so it is stored separately and never feeds a share, a band or a label.
    f["entry_window"] = {"after_s": HOLD_S, "known_s": len(known), "complete": entry_complete}
    f["best_R_observed_lb"] = _r(max(known) / cost - 1, 4) if known else None
    f["best_R_hindsight"] = f["best_R_observed_lb"] if entry_complete else None
    f["graduated_in_window"] = tape.done_t is not None and tape.done_t <= t0 + HOLD_S
    if f["graduated_in_window"]:
        vp = tape.migration_value(qty)
        last_curve = next((x["v"] for x in reversed(seconds) if not x["pool"] and x["v"] is not None), None)
        f["migration"] = {"last_curve_R": _r(last_curve / cost - 1, 4) if last_curve else None,
                          "pool_R": None if vp is None else _r(vp / cost - 1, 4)}
        f["migration"]["step_R"] = (None if vp is None or not last_curve else _r(vp / cost - last_curve / cost, 4))
        if vp is None:
            f["censored"].append("no_pool_tape")

    def exit_at(k, reason):
        j = k + int(LAG_S)
        if j >= len(seconds):
            return (None, "coverage", None)
        x = seconds[j]
        if x["v"] is None:
            return (None, "no_venue_value", None)
        return (x["v"] / cost - 1, reason, x["s"] - t0)

    def run(trigger, needs_indicators=False):
        """Path-dependent exit: the FIRST trigger over the known seconds. An unknown second before the trigger could hide an earlier
        peak or breach, so a trigger that fires after any unknown second is censored, never rescheduled."""
        gap = False
        for k, x in enumerate(seconds):
            if needs_indicators and x["pool"]:
                return (None, "no_indicators_after_migration", None)
            if x["v"] is None:
                gap = True
                continue
            why = trigger(k, x)
            if why:
                if gap:
                    return (None, "unknown_interval_before_trigger", None)
                return exit_at(k, why)
        if len(seconds) >= HOLD_S and seconds[-1]["v"] is not None and not gap:
            return (seconds[-1]["v"] / cost - 1, "mark_900", seconds[-1]["s"] - t0)
        return (None, "coverage" if not gap else "unknown_interval_before_trigger", None)

    def run_fixed(T):
        """A fixed-time exit: scheduled at t0 + T, executed LAG_S later. No value at the execution second = censored (never slid to the next price)."""
        k = T - 1                                                  # seconds[k] is t0 + T
        j = k + int(LAG_S)
        if j >= len(seconds):
            return (None, "coverage", None)
        x = seconds[j]
        if x["v"] is None:
            return (None, "no_venue_value", None)
        return (x["v"] / cost - 1, "time", x["s"] - t0)
    alts = {}
    rails = unit.get("rails")
    if rails:
        tp, sl, tr = (rails.get("tp_pct") or 0) / 100.0, (rails.get("sl_pct") or 0) / 100.0, (rails.get("trail_pct") or 0) / 100.0

        def r0(k, x):
            if sl and x["v"] <= cost * (1 - sl):
                return "sl"
            if tp and x["v"] >= cost * (1 + tp):
                return "tp"
            if tr and x["v"] <= x["peak"] * (1 - tr):
                return "trail"
        alts["rails"] = run(r0)
    for T in FIXED_EXITS:
        alts["fixed_%ds" % T] = run_fixed(T)
    cat = lambda x: x["v"] <= cost * 0.5
    alts["hold300_cat"] = run(lambda k, x: "cat" if cat(x) else ("time" if x["s"] - t0 >= 300 else None))
    if _learning_grid:
        alts["live_rails"] = alts.pop("rails", (None, "booked_rails_unknown", None))
        for trail in LEARNING_TRAILS:
            def trail_exit(k, x, tr=trail / 100.0):
                if rails and rails.get("sl_pct") and x["v"] <= cost * (1 - rails["sl_pct"] / 100.0):
                    return "sl"
                if rails and rails.get("tp_pct") and x["v"] >= cost * (1 + rails["tp_pct"] / 100.0):
                    return "tp"
                return "trail" if x["v"] <= x["peak"] * (1 - tr) else None
            alts["trail_%dpct" % trail] = run(trail_exit) if rails else (None, "booked_rails_unknown", None)
        horizon = unit.get("plan_horizon_s")
        alts["hold_plan"] = run_fixed(int(horizon)) if _fin(horizon) and horizon > 0 and int(horizon) == horizon else (None, "plan_horizon_unknown", None)
        alts["no_exit_mark900"] = ((seconds[-1]["v"] / cost - 1, "mark_900", HOLD_S)
                                   if len(seconds) == HOLD_S and seconds[-1]["v"] is not None else (None, "coverage_or_venue_unknown", None))
    else:
        for k, x in enumerate(seconds):                                       # death indicators once, curve era only
            if not x["pool"] and x["v"] is not None:
                x["noise"] = noise_5(tape, x["s"], x["i"])
                x["flag"] = death_flag(tape, x["s"], x["i"], qty, vseries, x["noise"], first)
        alts["death_any"] = run(lambda k, x: "cat" if cat(x) else ("death:" + ",".join(x["flag"]) if x["flag"] else None), needs_indicators=True)
    alts["sell_before_migration"] = run(lambda k, x: ("sl" if rails and rails.get("sl_pct") and x["v"] <= cost * (1 - rails["sl_pct"] / 100.0) else
                                                      "grad_exit" if x["fill"] is not None and x["fill"] >= SELL_BEFORE_FILL and not x["pool"] else
                                                      "trail" if rails and rails.get("trail_pct") and x["v"] <= x["peak"] * (1 - rails["trail_pct"] / 100.0) else None))
    f["alternatives"] = {k: {"R": _r(v[0], 4), "reason": v[1], "after_s": _r(v[2], 1), "censored": v[0] is None} for k, v in alts.items()}
    tags = []
    if exit_info:
        te = exit_info["exit_ts"]
        # THE REAL EXIT ON THE SAME BASIS as the alternatives: the SOL proceeds of EVERY proven disposal leg (derisk + final) on the tape,
        # native fees, over the entry cost. A leg that cannot be bound (signature + mint + SELL side + token quantity) censors the number.
        legs = exit_info.get("legs")
        if legs is None and exit_info.get("exit_sig"):
            legs = [{"sig": exit_info["exit_sig"], "qty": None}]
        proceeds, sold, proven = 0.0, 0.0, bool(legs)
        for leg in legs or ():
            ex = next((r for r in tape.rows if r["signature"] == leg["sig"] and not r["is_buy"]), None)
            parts = _curve_fee_parts(ex) if ex else None
            if ex is None or parts is None or not ex.get("quote_atomic"):
                proven = False
                break
            if leg.get("qty") is not None:
                if abs(ex["token_atomic"] / 1e6 - leg["qty"]) > 1e-6 * max(1.0, leg["qty"]):
                    proven = False
                    break
                sold += leg["qty"]
            gross = int(ex["quote_atomic"])
            proceeds += (gross - sum(-(-gross * bps // 10_000) for bps in parts)) / 1e9
        if proven and legs and all(g.get("qty") is not None for g in legs) and abs(sold - qty / 1e6) > 0.01 * (qty / 1e6):
            proven = False
            f["censored"].append("disposal_qty_mismatch")
        r_sol = proceeds / cost - 1 if proven else None
        pnl, notional = exit_info.get("realized_pnl_usd"), exit_info.get("entry_notional_usd")
        ledger_r = pnl / notional if _fin(pnl) and _fin(notional) and notional > 0 else None
        f["exit"] = {"reason": exit_info.get("exit_reason"), "hold_s": _r(te - t0, 1), "realized_R": _r(r_sol, 4),
                     "realized_R_basis": "sol_tape_all_legs" if r_sol is not None else None, "legs": len(legs or ()),
                     "realized_R_ledger_usd": _r(ledger_r, 4),
                     "realized_pnl_usd": _r(exit_info["realized_pnl_usd"], 2), "entry_notional_usd": _r(exit_info["entry_notional_usd"], 2)}
        if r_sol is None:
            f["censored"].append("realized_sol_basis_unknown")
        after = {}
        for w in (30, 60, 300, 900):
            v = tape.value_at(te + w, qty)
            after["+%ds" % w] = None if v is None else _r(v / cost - 1, 4)
        f["after_exit_R_hindsight"] = after
        held = [x for x in seconds if x["s"] <= te and x["v"] is not None]
        if held:
            f["path"] = {"max_runup_R": _r(max(x["v"] for x in held) / cost - 1, 4),
                         "max_drawdown_from_peak": _r(max(1 - x["v"] / x["peak"] for x in held), 4),
                         "secs_to_peak": _r(max(held, key=lambda x: x["v"])["s"] - t0, 1)}
        # EXIT REGRET = what the same bag fetched at its best in the HOLD_S seconds AFTER our exit, over what we got (never the peak before it)
        later = [v for v in (tape.value_at(te + w, qty) for w in range(1, HOLD_S + 1)) if v is not None]
        exit_complete = len(later) == HOLD_S                      # EVERY second after the exit has a value; a watermark alone proves nothing
        f["exit_window"] = {"after_s": HOLD_S, "known_s": len(later), "complete": exit_complete}
        f["exit_best_after_R_observed_lb"] = _r(max(later) / cost - 1, 4) if later else None
        f["exit_best_after_R_hindsight"] = f["exit_best_after_R_observed_lb"] if exit_complete else None
        f["exit_regret_R"] = (_r(max(0.0, max(later) / cost - 1 - r_sol), 4) if exit_complete and r_sol is not None else None)
        # ---- code tags (deterministic): computed from the facts above, never from model text
        if f.get("entry_rank_pct") is not None and f["entry_rank_pct"] >= 90 and (f.get("path") or {}).get("max_drawdown_from_peak", 0) > 0:
            tags.append("entered_top")
        if f.get("curve_fill_at_entry") is not None and f["curve_fill_at_entry"] >= 0.9:
            tags.append("late_curve")
        rec = [x for x in (after.get("+60s"), after.get("+300s")) if x is not None]
        if r_sol is not None and r_sol <= 0 and rec and max(rec) > 0:
            tags.append("shaken_out")                     # we left at a loss or flat and the same bag was worth more than its cost within 5 min
        if r_sol is not None and any(x["flag"] for x in seconds if x["s"] <= te) and after.get("+60s") is not None and after["+60s"] < r_sol:
            tags.append("held_through_death")
        if f["graduated_in_window"] and te >= tape.done_t - 1:
            tags.append("migration_step")
    return f, tags


def mcap_stage(mcap_usd, migrated=False):
    """Recorded USD-cap bands; never infer an actual fill cap from the fixed-FX tape model."""
    if migrated:
        return "post_migration_entry_censored"
    if not _fin(mcap_usd) or mcap_usd < 0:
        return "unknown"
    if mcap_usd < 4000:
        return "below_4k"
    return next((name for limit, name in zip((10000, 20000, 30000), LEARNING_STAGE_NAMES) if mcap_usd < limit), LEARNING_STAGE_NAMES[-1])


def _curve_proof_error(row):
    """Strict proof for the new native model; the legacy fact fields retain their original evidence contract."""
    from sniper.execution.pump_curve_buy import SOL_QUOTE_MINTS
    if row.get("quote_mint") is None:
        return "quote_mint_unknown"
    if row["quote_mint"] not in SOL_QUOTE_MINTS:
        return "non_sol_quote_mint"
    for field, reason in (("fee_bps", "protocol_fee_unknown"), ("creator_fee_bps", "creator_fee_unknown"),
                          ("cashback_fee_bps", "cashback_fee_unknown")):
        value = row.get(field)
        try:
            if value is None or isinstance(value, bool) or int(value) != float(value) or not 0 <= int(value) < 10000:
                return reason
        except (TypeError, ValueError, OverflowError):
            return reason
    if int(row["cashback_fee_bps"]) != 0:
        return "cashback_fee_model_unverified"
    if int(row["fee_bps"]) + int(row["creator_fee_bps"]) >= 10000:
        return "venue_fees_invalid"
    return None


class _LearningTape(Tape):
    """One strict view; no global monkeypatch or change to historical fee/currency assumptions."""

    def __init__(self, tape):
        rows = sorted(tape.rows, key=lambda r: (r["received_at"], r["slot"],
                                              -1 if r.get("tx_index") is None else r["tx_index"], r["ordinal"]))
        super().__init__(rows, tape.pool, tape.coverage_until, _ordered=True, pool_invalid_states=tape.pool_invalid)
        self.used_errors = set()
        self.errors = [_curve_proof_error(r) for r in rows]
        tied = {}
        for i, row in enumerate(rows):
            tied.setdefault((row["received_at"], row["slot"]), []).append(i)
        for indices in tied.values():
            signatures = {rows[i]["signature"] for i in indices}
            if len(signatures) > 1 and (any(rows[i].get("tx_index") is None or rows[i]["tx_index"] < 0 for i in indices)
                                        or len({(rows[i]["tx_index"], rows[i]["ordinal"]) for i in indices}) < len(indices)):
                for i in indices:
                    self.errors[i] = "tape_order_ambiguous"
        for i, error in enumerate(self.errors):
            if error:
                self.parts[i] = None
                self.price[i] = self.fill[i] = None
                self.complete[i] = False
        self.done = next((i for i, complete in enumerate(self.complete) if complete), None)
        self.done_t = self.t[self.done] if self.done is not None else None

    def buy_tokens(self, i, cost_sol):
        if self.errors[i]:
            self.used_errors.add(self.errors[i])
            return 0
        return super().buy_tokens(i, cost_sol)

    def v(self, i, qty):
        if i >= 0 and self.errors[i]:
            self.used_errors.add(self.errors[i])
            return None
        return super().v(i, qty)

    def _pool_proven(self, j):
        from sniper.execution.pump_curve_buy import SOL_QUOTE_MINTS
        state = self.pool[j]
        error = None
        if state.get("quote_mint") not in SOL_QUOTE_MINTS:
            error = "pool_quote_mint_unknown_or_non_sol"
        elif (len(state["bps"]) != 4 or any(not isinstance(b, int) or not 0 <= b < 10000 for b in state["bps"])
              or sum(state["bps"]) >= 10000):
            error = "pool_fee_unknown"
        elif state["bps"][-1] != 0:
            error = "cashback_fee_model_unverified"
        if error:
            self.used_errors.add(error)
        return error is None

    def pool_value_at(self, s, qty):
        j = bisect.bisect_right(self.pool_t, s) - 1
        bad = bisect.bisect_right(self.pool_invalid_t, s) - 1
        if bad >= 0 and (j < 0 or self.pool_invalid_t[bad] >= self.pool_t[j]):
            self.used_errors.add(self.pool_invalid[bad]["reason"])
            return None
        if j >= 0 and self.pool[j].get("known_at", self.pool_t[j]) > s:
            self.used_errors.add("pool_state_not_yet_known")
            return None
        return super().pool_value_at(s, qty) if j >= 0 and self._pool_proven(j) else None

    def migration_value(self, qty):
        if self.done_t is None:
            return None
        j = bisect.bisect_left(self.pool_t, self.done_t + LAG_S)
        if j >= len(self.pool) or self.pool_t[j] - self.done_t > MIGRATION_MAX_S:
            return None
        return self.pool_value_at(self.pool_t[j], qty)


def learning_facts(unit, tape, watch_coverage_until=None, clock_offset_s=0.0):
    """Report-only native tape model. R is fractional after known venue costs and a named 2.5 pp residual; all-in net is unknown."""
    t_fill, response, touch = (unit.get(k) for k in ("t_fill", "watch_started_at", "trigger_at"))
    expiry, cost, qty = (unit.get(k) for k in ("watch_expires_at", "cost_sol", "qty"))
    corrected = lambda t: t - clock_offset_s if _fin(t) else None
    migrated = unit.get("entry_venue") in ("pumpswap", "pool", "post_migration") or unit.get("migrated") is True
    names = ("live_rails",) + tuple("trail_%dpct" % t for t in LEARNING_TRAILS) + tuple("fixed_%ds" % t for t in FIXED_EXITS) + (
        "hold300_cat", "hold_plan", "no_exit_mark900", "sell_before_migration")
    f = {"basis": "tape_model_net_after_known_venue_costs", "all_in_net": "unknown", "censored": [],
         "closed_at_corrected": unit.get("closed_at_corrected"),
         "costs": {"residual_model_cost_pp": COST * 100, "entry_lag_s": ENTRY_LAG_S, "exit_lag_s": LAG_S,
                   "return_horizon_s": RETURN_HORIZON_S, "venue_fees": "native_integer_protocol_creator_cashback_zero",
                   "clip_impact": "native_exact_quantity", "unmeasured": ["network_priority_fees", "route_availability", "landing", "failed_transactions"]},
         "times": {"fill_utc": corrected(t_fill), "response_utc": corrected(response), "touch_utc": corrected(touch),
                   "watch_expires_utc": corrected(expiry)},
         "stages": {"judge": mcap_stage(unit.get("judge_mcap_usd")), "fill": mcap_stage(unit.get("fill_mcap_usd"), migrated),
                    "judge_mcap_usd": unit.get("judge_mcap_usd"), "fill_mcap_usd": unit.get("fill_mcap_usd")},
         "watch_entry": {"upper_bound_hindsight": None, "complete": False, "counts": {"states": 0, "known": 0, "censored": 0}},
         "returns": {"R_fill": None, "R_response": None, "R_touch": None}, "return_details": {},
         "alternatives": {name: {"R": None, "reason": "entry_unproven", "censored": True,
                                  "basis": "tape_model_net_after_known_venue_costs", "residual_model_cost_pp": COST * 100,
                                  "mark_only": name == "no_exit_mark900"} for name in names}, "realized_R_tape_model": None}

    def censor(reason):
        if reason not in f["censored"]:
            f["censored"].append(reason)
    if migrated:
        censor("post_migration_entry_censored")
        return f
    if not _fin(t_fill) or not _fin(cost) or cost <= 0 or not _fin(qty) or qty <= 0 or int(qty) != qty:
        censor("actual_entry_quantity_or_cost_unknown")
        return f
    strict = _LearningTape(tape)
    i_fill = strict.idx(t_fill)
    if i_fill < 0:
        censor("no_state_at_entry")
        return f
    fill_error = strict.errors[i_fill]
    if fill_error:
        censor(fill_error)
    watch = f["watch_entry"]
    if not _fin(response) or not _fin(expiry):
        censor("watch_window_unknown")
    else:
        end, first = min(expiry, t_fill), response + ENTRY_LAG_S
        watch.update(window_start_utc=corrected(response), modeled_entry_start_utc=corrected(first), window_end_utc=corrected(end),
                     coverage_until_utc=corrected(watch_coverage_until))
        covered = (_fin(watch_coverage_until) and watch_coverage_until >= end and strict.coverage_until >= end
                   and strict.idx(response) >= 0 and first <= end)
        if not covered:
            censor("watch_coverage_incomplete")
        else:
            initial = strict.idx(first)
            candidates = [(first, initial)] if initial >= 0 and strict.t[initial] >= response else []
            candidates += [(at, j) for j, at in enumerate(strict.t) if first < at <= end]
            prices = []
            for at, j in candidates:
                amount = strict.buy_tokens(j, cost)
                if amount > 0:
                    prices.append((cost / (amount / 1e6), at))
                else:
                    censor(strict.errors[j] or "watch_buy_unproven")
            watch["counts"] = {"states": len(candidates), "known": len(prices), "censored": len(candidates) - len(prices)}
            watch["complete"] = bool(candidates) and len(prices) == len(candidates)
            if watch["complete"]:
                best, at = min(prices)
                watch["upper_bound_hindsight"] = {"price_sol_per_token": best, "entry_at_utc": corrected(at),
                                                  "regret_pct": (cost / (qty / 1e6) / best - 1) * 100}
            elif not candidates:
                censor("watch_buy_unproven")

    for name, entered_at in (("R_fill", t_fill), ("R_response", response + ENTRY_LAG_S if _fin(response) else None),
                              ("R_touch", touch + ENTRY_LAG_S if _fin(touch) else None)):
        exit_at = entered_at + RETURN_HORIZON_S + LAG_S if _fin(entered_at) else None
        detail = {"basis": (("actual_acquired_bag" if unit.get("kind") == "realized" else "modeled_admission_inventory")
                            if name == "R_fill" else "same_clip_native_modeled_buy"),
                  "entry_at_utc": corrected(entered_at), "exit_at_utc": corrected(exit_at), "horizon_s": RETURN_HORIZON_S,
                  "exit_lag_s": LAG_S, "cost_sol": cost, "qty_atomic": None, "censored": True}
        f["return_details"][name] = detail
        reason = None
        i = strict.idx(entered_at) if _fin(entered_at) else -1
        if i < 0:
            reason = "entry_time_or_state_unknown"
        elif name != "R_fill" and (entered_at > t_fill or (_fin(expiry) and entered_at > expiry)):
            reason = "modeled_entry_after_watch_or_fill"
        elif name != "R_fill" and (not _fin(watch_coverage_until) or watch_coverage_until < exit_at):
            reason = "watch_return_coverage_incomplete"
        elif strict.errors[i]:
            reason = strict.errors[i]
        elif name != "R_fill" and strict.done_t is not None and entered_at >= strict.done_t:
            reason = "post_migration_entry_censored"
        else:
            amount = int(qty) if name == "R_fill" else strict.buy_tokens(i, cost)
            detail["qty_atomic"] = amount if amount > 0 else None
            value = strict.value_at(exit_at, amount) if amount > 0 else None
            if value is not None:
                f["returns"][name] = value / cost - 1 - COST
                detail["censored"] = False
            else:
                reason = "return_coverage_or_venue_unknown"
        if reason:
            detail["reason"] = reason
            censor(reason)

    if not fill_error:
        replay, _ = facts_for({**unit, "kind": unit.get("kind", "realized"), "t_ref": t_fill}, strict,
                              launch_at=None, fill_latency=ENTRY_LAG_S, _learning_grid=True)
        for name, alt in replay.get("alternatives", {}).items():
            f["alternatives"][name] = {**alt, "R": alt["R"] - COST if _fin(alt["R"]) else None,
                                       "basis": f["basis"], "residual_model_cost_pp": COST * 100,
                                       "mark_only": name == "no_exit_mark900" or alt.get("reason") == "mark_900"}
        for reason in replay.get("censored", []):
            censor(reason)
    for alt in f["alternatives"].values():
        if alt["censored"]:
            censor(alt["reason"])

    legs = (unit.get("exit_info") or {}).get("legs")
    if legs and not fill_error:
        proceeds, sold, proven = 0, 0, True
        for leg in legs:
            matches = [(i, row) for i, row in enumerate(strict.rows) if row["signature"] == leg.get("sig") and not row["is_buy"]]
            leg_qty = leg.get("qty")
            if len(matches) != 1 or not _fin(leg_qty) or leg_qty <= 0:
                proven = False
                break
            i, row = matches[0]
            amount = row.get("token_atomic")
            if strict.errors[i] or amount is None or abs(amount - leg_qty * 1e6) > max(1, amount * 1e-6) or row.get("quote_atomic") is None:
                proven = False
                break
            gross = int(row["quote_atomic"])
            if gross < 0:
                proven = False
                break
            proceeds += gross - sum(-(-gross * bps // 10000) for bps in strict.parts[i])
            sold += amount
        if proven and abs(sold - qty) <= max(1, qty * 1e-6):
            f["realized_R_tape_model"] = proceeds / 1e9 / cost - 1 - COST
    if f["realized_R_tape_model"] is None:
        censor("realized_native_disposals_unproven")
    for reason in sorted(strict.used_errors):
        censor(reason)
    return f


def store(db, key, kind, mint, position_id, ticket_id, now, t0, facts, tags):
    """Append one IMMUTABLE row: a second write of the same (key, feature_version) is ignored, never an update. True when written."""
    cur = db.execute("INSERT OR IGNORE INTO lessons(key, feature_version, kind, mint, position_id, ticket_id, computed_at, t0, facts_json, tags_json) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?)", (key, FEATURE_VERSION, kind, mint, position_id, ticket_id, now, t0,
                                                      json.dumps(facts, sort_keys=True), json.dumps(tags)))
    db.commit()
    return cur.rowcount == 1


def good_exit_band(prior_regrets):
    """Derived, not fixed: the running MEDIAN of every earlier trade's exit regret (R units) and its n. A trade whose regret is at or below
    it is a 'good_exit'. None when there are fewer than 5 earlier trades."""
    xs = [x for x in prior_regrets if _fin(x)]
    return (statistics.median(xs), len(xs)) if len(xs) >= 5 else (None, len(xs))


# ----------------------------------------------------------------------------------------------------------- loaders


def utc_day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def ro(path):
    p = Path(path)
    if not p.is_file():
        return None
    try:
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def q(path, sql, args=(), strict=False):
    """One statement, fully fetched, handle closed: a read lasts milliseconds (no snapshot is ever held). strict=True RAISES on a
    missing file or a SQL error (a required table that cannot be read is a failure, not an empty answer)."""
    c = ro(path)
    if c is None:
        if strict:
            raise RuntimeError("cannot open %s" % path)
        return []
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    except sqlite3.Error:
        if strict:
            raise
        return []
    finally:
        c.close()


def days_between(lo, hi):
    """Every UTC day from lo to hi inclusive (a 3-day lookback must read the middle day too)."""
    out, d = [], datetime.fromtimestamp(lo, timezone.utc).date()
    end = datetime.fromtimestamp(hi, timezone.utc).date()
    while d <= end:
        out.append(d.isoformat())
        d = d.fromordinal(d.toordinal() + 1)
    return out


def load_curve(desk_dir, mint, lo, hi):
    out = []
    for day in days_between(lo, hi):
        f = Path(desk_dir) / "swaps" / ("%s.db" % day)
        columns = {r["name"] for r in q(f, "PRAGMA table_info(swaps)")}
        optional = ", ".join(name if name in columns else "NULL AS " + name for name in (
            "real_quote", "real_token", "quote_mint", "cashback_fee_bps", "tx_index"))
        out += q(f, "SELECT slot, ordinal, signature, is_buy, quote_atomic, token_atomic, actor, virtual_quote, virtual_token, fee_bps, "
                    "creator_fee_bps, received_at, %s FROM swaps WHERE mint=? AND received_at BETWEEN ? AND ? ORDER BY received_at" % optional,
                 (mint, lo, hi))
    return out


def load_pool(desk_dir, mint, lo, hi, stats=None, invalid_states=None):
    """The coin's executable PumpSwap pool states from the PS tape, NATIVE: [{t, base, rq, vq, bps (lp, protocol, creator, cashback), known_at,
    from_next, + readable q/b/fee}]. rq = the REAL quote reserve (the vault cap), vq = the proven virtual quote (carried forward from the
    last proven legacy buy), each fee component kept apart (the buyback share is not a fee). A row with a NULL fee component (cashback
    included), no proof, or a next-event pre-state that straddles a capture gap is DROPPED and counted in `stats`, never guessed.
    None = the PS tape could not be read (unknown); [] = it holds no usable state for the coin. Optional invalid_states receives typed
    receipt-time blockers for the strict learning view; the legacy usable-state output and drop counts stay unchanged."""
    from sniper.services import memecoin_desk_ps_tape as PT
    stats = stats if stats is not None else {}
    try:
        conn = PT._ro(desk_dir)
        if conn is None:
            return []
        pool = PT.canonical_pool(mint)
        try:
            rows = PT.window_rows(conn, (
                'received_at, slot, ordinal, post_base_reserve_atomic, post_quote_reserve_atomic, '
                'pre_base_reserve_atomic, pre_quote_reserve_atomic, lp_fee_bps, protocol_fee_bps, '
                'creator_fee_bps, cashback_fee_bps, quote_mint, mint, CASE WHEN is_buy=1 AND payload_json LIKE ? '
                'THEN payload_json END, is_buy '
            ), pool, lo, hi, params=('%' + PT.VQ_SOURCE + '%',))
            vq = PT._proof_before(conn, pool, lo) if rows else None
            gaps = [r[0] for r in conn.execute(
                "SELECT at FROM gaps_ps WHERE at>=? AND at<=? AND cause NOT IN (%s)" % ",".join("?" * len(PT.DECODE_CAUSES)),
                (lo, hi, *PT.DECODE_CAUSES))]
        except sqlite3.Error:
            return None
        finally:
            conn.close()
    except Exception:                                    # PT.TapeUnreadable, a bad canonical-pool derivation: unknown, never empty-as-quiet
        return None
    def invalid(at, reason):
        if invalid_states is not None:
            invalid_states.append({"t": at, "reason": reason})

    for row in rows:
        if row[12] not in (None, mint):
            invalid(row[0], "pool_mint_mismatch")
        elif row[11] not in (None, PT.WSOL_MINT):
            invalid(row[0], "pool_quote_mint_unknown_or_non_sol")
    rows = [r for r in rows if r[12] in (None, mint) and r[11] in (None, PT.WSOL_MINT)]
    out = []
    for i, (at, slot, ordinal, post_b, post_q, pre_b, pre_q, lp, proto, creator, cashback, _q, _m, payload, is_buy) in enumerate(rows):
        proven = PT._proof(payload)
        if proven is not None:
            vq = proven
        from_next = post_b is None or not is_buy      # a sell's decoded post is an observation; the next pre confirms it
        if from_next:
            if i + 1 >= len(rows):
                stats["dropped_no_next"] = stats.get("dropped_no_next", 0) + 1
                invalid(at, "pool_next_state_unknown")
                continue
            nxt = rows[i + 1]
            if any(at < g <= nxt[0] for g in gaps):          # a capture gap between the sell and the next event: the pre-state is not proven
                stats["dropped_gap_before_next"] = stats.get("dropped_gap_before_next", 0) + 1
                invalid(at, "pool_state_interval_unproven")
                continue
            post_b, post_q = PT.sell_post((post_b, post_q), (nxt[5], nxt[6])) if not is_buy else (nxt[5], nxt[6])
            if post_b is None:
                stats["dropped_sell_post_mismatch"] = stats.get("dropped_sell_post_mismatch", 0) + 1
                invalid(at, "pool_state_unconfirmed")
                continue
        fees = [lp, proto, creator, cashback]
        if vq is None or post_b is None or post_q is None or any(f is None for f in fees):
            key = "dropped_null_fee" if any(f is None for f in fees) else "dropped_no_proof"
            stats[key] = stats.get(key, 0) + 1
            invalid(at, "cashback_fee_unknown" if cashback is None else "pool_fee_unknown" if any(f is None for f in fees)
                    else "pool_reserve_proof_unknown")
            continue
        try:
            base, rq = int(post_b), int(post_q)
            bps = tuple(int(f) for f in fees)
        except (TypeError, ValueError, OverflowError):
            stats["dropped_bad_number"] = stats.get("dropped_bad_number", 0) + 1
            invalid(at, "pool_state_malformed")
            continue
        if base > 0 and rq > 0:
            # a state taken from the next row's pre is KNOWN only when that row arrived; never backdated to the sell
            out.append({"t": at, "base": base, "rq": rq, "vq": vq, "bps": bps,
                        "known_at": rows[i + 1][0] if from_next else at, "from_next": from_next,
                        "q": rq + vq, "b": base, "fee": sum(bps) / 1e4, "quote_mint": _q})
        else:
            invalid(at, "pool_reserves_unusable")
    return out


def launch_time(desk_dir, mint, near):
    """The launch row's tape receipt time, or None when the launch is not in the retained tape."""
    for d in (0, 1, 2):
        rows = q(Path(desk_dir) / "launches" / ("%s.db" % utc_day(near - 86400 * d)), "SELECT received_at FROM launches WHERE mint=?", (mint,))
        if rows and rows[0]["received_at"] is not None:
            return float(rows[0]["received_at"])
    return None


def tape_day_readable(desk_dir, ts):
    """True when the swaps day file of ts exists and opens: a missing file is a tape OUTAGE to retry, not evidence."""
    conn = ro(Path(desk_dir) / "swaps" / ("%s.db" % utc_day(ts)))
    if conn is None:
        return False
    conn.close()
    return True


def capture_coverage(desk_dir, t0, hi):
    """The last second the curve capture is PROVEN alive after t0: min(the day tape's newest swap of ANY coin, the first capture gap after
    t0). None when it cannot be read."""
    newest = None
    for day in sorted({utc_day(t0), utc_day(hi)}):
        f = Path(desk_dir) / "swaps" / ("%s.db" % day)
        conn = ro(f)
        if conn is None:
            continue
        conn.close()
        rows = q(f, "SELECT received_at FROM swaps ORDER BY slot DESC LIMIT 1")
        if rows and rows[0]["received_at"] is not None:
            newest = max(newest or 0.0, float(rows[0]["received_at"]))
    if newest is None:
        return None
    gaps = []
    for day in days_between(t0, hi):
        gp = Path(desk_dir) / "gaps" / ("%s.db" % day)
        if not gp.is_file():
            continue                                         # the writer creates the gaps file at the first gap: absent = none that day
        try:
            gaps += [r["at"] for r in q(gp, "SELECT at FROM gaps WHERE at>? AND at<=?", (t0, hi), strict=True)]
        except (RuntimeError, sqlite3.Error):
            return None                                      # a gaps file that exists but cannot be read: coverage is UNKNOWN, never 'no gap'
    return min([newest] + gaps)


def hash_sampled(ticket_id, pct):
    """Deterministic share of tickets (by hash, never by outcome): the same ticket is in or out on every run."""
    return int(hashlib.sha256(ticket_id.encode()).hexdigest()[:8], 16) % 100 < pct


def unit_tags(code_tags, regret_R, band):
    tags = list(code_tags)
    if band[0] is not None and _fin(regret_R) and regret_R <= band[0]:
        tags.append("good_exit")
    return tags

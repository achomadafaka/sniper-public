#!/usr/bin/env python3
"""Replay trail ARMING on funded desk closes (descriptive; operator question 2026-10-26). usage: [SINCE_ISO]  (default 2026-10-22)
For each realized desk position with booked rails (tp/sl/trail) and a proven entry swap, rebuild the per-second exact sell value of the acquired bag from the fill (as memecoin_desk_lessons does), then replay the
booked SL and TP unchanged and a trail of the booked % that is ARMED only once the peak value reaches cost x (1 + arm), arm in {0, 5, 10, 15, 20}%. arm 0 = today's rails. Exit executes 1 s after the trigger second (LAG_S).
No trigger within 900 s: marked at 900 s (label `mark`); a trigger or the horizon in the PumpSwap era: label `pool`. R = exit value / cost - 1 (known venue fees, no extra residual); USD = R x entry notional."""
import importlib.util, json, sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("lessons_script", ROOT / "scripts" / "memecoin_desk_lessons.py")
S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
L = S.L
ARMS = (0, 5, 10, 15, 20)


def series(tape, qty, cost, t0):
    seconds, peak = [], cost
    for k in range(1, L.HOLD_S + 1):
        s = t0 + k
        if s > tape.coverage_until:
            break
        i = tape.idx(s)
        pool = tape.done is not None and s >= tape.done_t
        v = tape.pool_value_at(s, qty) if pool else tape.v(i, qty)
        if v is not None:
            peak = max(peak, v)
        seconds.append({"s": s, "v": v, "peak": peak, "pool": pool})
    return seconds


def replay(seconds, cost, sl, tp, tr, arm):
    gap = False
    for k, x in enumerate(seconds):
        if x["v"] is None:
            gap = True; continue
        why = None
        if sl and x["v"] <= cost * (1 - sl): why = "sl"
        elif tp and x["v"] >= cost * (1 + tp): why = "tp"
        elif tr and x["peak"] >= cost * (1 + arm / 100.0) and x["v"] <= x["peak"] * (1 - tr): why = "trail"
        if why:
            if gap:
                return None, "unknown_before_trigger", False
            j = k + int(L.LAG_S)
            if j >= len(seconds) or seconds[j]["v"] is None:
                return None, "no_exit_value", False
            return seconds[j]["v"] / cost - 1, why, seconds[j]["pool"]
    if len(seconds) >= L.HOLD_S and seconds[-1]["v"] is not None and not gap:
        return seconds[-1]["v"] / cost - 1, "mark", seconds[-1]["pool"]
    return None, "coverage", False


def main(since="2026-10-22"):
    import time
    from sniper.util import clock as clockmod
    from sniper.util.state_paths import state_path
    clock = S.Clock(clockmod.offset_s())
    ledger, desk_db, desk_dir = str(state_path("sniper_tracker.db")), str(state_path("data", "memecoin_desk.db")), Path(state_path("data", "desk"))
    now = time.time()
    units, _ = S.discover(ledger, desk_db, now, clock, 0, set())
    rows = []
    for u in units:
        if u["kind"] != "realized" or not u.get("rails") or _ts(u["t_ref"], clock) < _ts_since(since):
            continue
        r = rails_of(u)
        res = unit_series(u, desk_dir, now)
        if isinstance(res, str):
            rows.append({"pos": u["position_id"], "skip": res}); continue
        seconds, cost, notional, t0 = res
        out = {"pos": u["position_id"], "mint": u["mint"], "rails": u["rails"], "notional": notional, "actual_usd": u["exit_info"]["realized_pnl_usd"],
               "actual_reason": u["exit_info"]["exit_reason"], "arms": {}}
        for arm in ARMS:
            R, why, pool = replay(seconds, cost, *r, arm)
            out["arms"][arm] = {"R": R, "why": why, "pool": pool}
        rows.append(out)
    report(rows)


def _ts_since(iso):
    from datetime import datetime, timezone
    d = datetime.fromisoformat(iso)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _ts(t, clock):
    return t - clock.offset_s


def rails_of(u):
    r = u["rails"]
    return ((r.get("sl_pct") or 0) / 100.0, (r.get("tp_pct") or 0) / 100.0, (r.get("trail_pct") or 0) / 100.0)


def unit_series(u, desk_dir, now):
    if u.get("entry_venue") == "pumpswap":
        return "post_migration_entry"
    if not u.get("entry_sig") or u.get("multi_entry"):
        return "no_single_entry_fill"
    es, status = S.entry_swap(desk_dir, u["mint"], u["entry_sig"], u.get("entry_qty"), u["t_ref"])
    if es is None:
        return "entry_swap_" + status
    from sniper.execution.pump_curve_buy import SOL_QUOTE_MINTS
    if es.get("quote_mint") is not None and es["quote_mint"] not in SOL_QUOTE_MINTS:
        return "non_sol_quote_mint"
    parts = L._curve_fee_parts(es)
    if parts is None:
        return "entry_fee_unknown"
    cost = L.curve_spend_for_net(int(es["quote_atomic"]), parts) / 1e9
    qty, t_fill = es["token_atomic"], es["received_at"]
    hi = max(u["t_ref"], t_fill) + L.HOLD_S + 60
    launch_at = L.launch_time(desk_dir, u["mint"], u["t_ref"])
    lo = min(u["t_ref"] - 1500, launch_at - 5) if launch_at is not None else u["t_ref"] - 1500
    if not (L.tape_day_readable(desk_dir, u["t_ref"]) and L.tape_day_readable(desk_dir, hi)):
        return "tape_day_missing"
    rows = L.load_curve(desk_dir, u["mint"], lo, hi)
    if not rows:
        return "no_curve_rows"
    cov = L.capture_coverage(desk_dir, t_fill, hi)
    if cov is None:
        return "capture_unproven"
    stats, invalid = {}, []
    pool = L.load_pool(desk_dir, u["mint"], u["t_ref"] - 5, hi, stats=stats, invalid_states=invalid)
    if pool is None:
        return "pool_tape_unreadable"
    tape = L.Tape(rows, pool, coverage_until=cov, pool_invalid_states=invalid)
    i0 = tape.idx(t_fill)
    if i0 < 0:
        return "no_state_at_entry"
    return series(tape, qty, cost, t_fill), cost, u["exit_info"]["entry_notional_usd"], t_fill


def report(rows):
    ok = [r for r in rows if "arms" in r]
    print("funded desk positions with rails:", len(rows), "replayed:", len(ok), "skipped:", {k: sum(1 for r in rows if r.get("skip") == k) for k in {r.get("skip") for r in rows if r.get("skip")}})
    print("%5s %-9s %-13s %10s %9s | %s" % ("pos", "mint", "rails tp/sl/tr", "actual_usd", "actual", "  ".join("arm%-2d R/why" % a for a in ARMS)))
    for r in ok:
        rr = r["rails"]
        print("%5d %-9s %3s/%3s/%3s %10.2f %9s | %s" % (r["pos"], r["mint"][:8], rr.get("tp_pct"), rr.get("sl_pct"), rr.get("trail_pct"), r["actual_usd"] or 0, (r["actual_reason"] or "")[:9],
              "  ".join(("%+.2f %s%s" % (r["arms"][a]["R"], r["arms"][a]["why"][:4], "*" if r["arms"][a]["pool"] else "")) if r["arms"][a]["R"] is not None else "  --%s" % r["arms"][a]["why"][:6] for a in ARMS)))
    full = [r for r in ok if all(r["arms"][a]["R"] is not None for a in ARMS)]
    print("\nreplayed with a value under every arm: %d of %d  (* = exit in the PumpSwap era)" % (len(full), len(ok)))
    base = {a: [r["arms"][a]["R"] * r["notional"] for r in full] for a in ARMS}
    for a in ARMS:
        v = np.array(base[a]); mints = np.array([r["mint"] for r in full])
        d = v - np.array(base[0]) if a else None
        extra = ""
        if a:
            diff = int(np.sum(np.abs(d) > 1e-9))
            u_, inv = np.unique(mints, return_inverse=True); s = np.bincount(inv, weights=d); n = np.bincount(inv)
            mean = s.sum() / n.sum(); se = np.sqrt(((s - mean * n) ** 2).sum()) / n.sum()
            extra = " | vs arm0: total %+.2f USD, differs on %d trades, mean %+.3f lo90 %+.3f hi90 %+.3f" % (d.sum(), diff, mean, mean - 1.2816 * se, mean + 1.2816 * se)
        print("arm %2d%%: total net %+8.2f USD over %d trades (mean %+.3f)%s" % (a, v.sum(), len(v), v.mean(), extra))
    print("actual booked (same trades): total %+.2f USD" % sum(r["actual_usd"] or 0 for r in full))
    mig = sum(1 for r in full for a in ARMS if r["arms"][a]["pool"])
    print("arm-trade exits in the PumpSwap era (labelled *): %d" % mig)


if __name__ == "__main__":
    main(*sys.argv[1:])

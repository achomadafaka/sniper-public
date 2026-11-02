"""TP/SL pool replay on chain 4663 (pre-registration docs/audits/2026-09-30-per-wallet-tp-sl-preregistration.md,
amendment A1; Gamma's rulings 2026-09-30). Research only: read-only, nothing changes live.

Order is the pre-registration's: the instrument check first, and the cells are read only if 4663
passes it. Price path: `memecoin_position_marks` source='fastlane_rpc' (exact whole-bag preview) x the
entry quantity, plus our ACTUAL whole-bag exit fill appended as the final point (Gamma ACCEPTED, with
the guard that a fill on it is never graded on return). Returns are gross of gas: 4663 gas is not
booked, so the exit fee is unknown; it cancels in the paired test.

Usage: python3 scripts/research/tpsl_pool_replay.py OUT_DIR
       TPSL_CHAIN=solana python3 scripts/research/tpsl_pool_replay.py OUT_DIR

Solana (amendment A2, 474644ac8): the path is `memecoin_route_series` seam `exit_decision`, the
runner's whole-balance Jupiter quote every ~2 s (`expected_usdc` = the whole bag's value in USDC).
A1 withheld Solana on the false premise that no such series exists.
"""
import json
import os
import random
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone

CHAIN = os.environ.get("TPSL_CHAIN", "4663")
TPSL = {"real_tp", "b_stop", "real_tp_shared_balance"}
TP_REASONS = {"real_tp", "real_tp_shared_balance"}
CELLS = [(tp, stop) for tp in (10, 15, 20, 25) for stop in (15, 20, 25, 35)]
INSTRUMENT_TIME_S, INSTRUMENT_RET_PTS, INSTRUMENT_PASS = 10.0, 2.0, 0.90
MAX_TRAIN_CENSORED, DRAWS, SEED = 0.20, 4000, 20260930


# ---------------------------------------------------------------- pure rules
def replay_cell(points, *, cost, tp, stop, debounce, lag_s, actual_reason, actual_ret, appended_last=False,
                own=False):
    """One unit under one (tp, stop) cell. `points` = [(t, whole-bag value)] in time order, ending at
    our exit. TP fires on the first point with return >= tp; the stop on the `debounce`-th consecutive
    point with return <= -stop. The fill is the first point at or after trigger + lag_s. Unfired: our
    own non-TP/SL exit is carried; an unfired cell on a TP/stop exit is censored, never zero.
    With `appended_last` the final point is our own exit fill: a fill candidate, never a trigger
    (Gamma's guard: triggers come from marks, so the replay cannot grade itself)."""
    run = 0
    marks = points[:-1] if appended_last else points
    for i, (t, v) in enumerate(marks):
        ret = (v / cost - 1.0) * 100.0
        side = None
        if ret >= tp:
            side = "tp"
        run = run + 1 if ret <= -stop else 0
        if side is None and run >= max(1, int(debounce)):
            side = "stop"
        if side:
            for j in range(i, len(points)):
                if points[j][0] >= t + lag_s:
                    return {"status": side, "ret": (points[j][1] / cost - 1.0) * 100.0, "t_trigger": t,
                            "t_fill": points[j][0], "fill_appended": bool(appended_last and j == len(points) - 1)}
            if own and appended_last and points[-1][0] >= t:
                # A3: the unit's OWN contract replay. The series ends at our exit, so a fill faster than
                # the median lag has no later point; our actual fill is that trigger's fill (judged on
                # the trigger only, as every appended fill is).
                return {"status": side, "ret": (points[-1][1] / cost - 1.0) * 100.0, "t_trigger": t,
                        "t_fill": points[-1][0], "fill_appended": True}
            return {"status": "censored", "ret": None, "t_trigger": t, "t_fill": None, "fill_appended": False}
    if actual_reason in TPSL:
        return {"status": "censored", "ret": None, "t_trigger": None, "t_fill": None, "fill_appended": False}
    return {"status": "actual", "ret": actual_ret, "t_trigger": None, "t_fill": None, "fill_appended": False}


def instrument_match(out, *, actual_reason, t_decision, actual_ret):
    """Gamma's guard: judged on the TRIGGER (same side, within 10 s of our decision); the return
    criterion (within 2 points) only for a fill on a mark, never on our own appended exit."""
    side = "tp" if actual_reason in TP_REASONS else "stop"
    if out["status"] != side or out["t_trigger"] is None or t_decision is None:
        return False
    if abs(out["t_trigger"] - t_decision) > INSTRUMENT_TIME_S:
        return False
    return out["fill_appended"] or abs(out["ret"] - actual_ret) <= INSTRUMENT_RET_PTS


def chronological_split(units, *, key):
    """First two-thirds train, last third test, rounded so that test >= 4."""
    ordered = sorted(units, key=lambda u: u[key])
    n_train = min(int(len(ordered) * 2 / 3), len(ordered) - 4)
    return ordered[:max(0, n_train)], ordered[max(0, n_train):]


def cluster_bootstrap_p(diffs, *, draws=DRAWS, seed=SEED):
    """One-sided p that the mean paired difference is <= 0, resampling MINTS with replacement.
    `diffs` = [(mint, difference)]."""
    by = defaultdict(list)
    for mint, d in diffs:
        by[mint].append(d)
    mints = sorted(by)
    rng = random.Random(seed)
    worse = 0
    for _ in range(draws):
        sample = [d for m in (rng.choice(mints) for _ in mints) for d in by[m]]
        worse += statistics.mean(sample) <= 0
    return worse / draws


# ---------------------------------------------------------------- data
def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp() if s else None
    except ValueError:
        return None


def _usable(times, t0, t1):
    pts = [t0] + sorted(t for t in times if t0 <= t <= t1) + [t1]
    gaps = list(zip(pts, pts[1:]))
    return (max([b - a for a, b in gaps if a < t0 + 120] or [0.0]) <= 5.0
            and max([b - a for a, b in gaps if a >= t0 + 120] or [0.0]) <= 30.0)


def load_units(conn):
    """The pre-registered population's 4663 units (pairs with more than 5 filtered closes), with
    everything the replay needs. Raises when an entry cost cannot be traced (a pre-registered stop)."""
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        "SELECT id, source_user_id, network_id, mint, COALESCE(opened_recorded_at, opened_at) AS opened, "
        "closed_at, closed_recorded_at, exit_reason, signal_source, funded_contract_json FROM memecoin_positions "
        "WHERE execution_mode='real' AND is_open=0 AND closed_at IS NOT NULL")]
    pop = []
    for r in rows:
        contract = json.loads(r["funded_contract_json"]) if r["funded_contract_json"] else {}
        pol = contract.get("policy") or {}
        if (r["signal_source"] == "manual" or pol.get("policy_id") == "manual-unmanaged-v1"
                or contract.get("engine_authoritative") is True):
            continue
        r["policy"] = pol
        pop.append(r)
    count = defaultdict(int)
    for r in pop:
        count[(r["source_user_id"], r["network_id"])] += 1
    units = []
    for r in pop:
        if r["network_id"] != CHAIN or count[(r["source_user_id"], CHAIN)] <= 5:
            continue
        fills = conn.execute("SELECT kind, qty, notional, fraction, created_at FROM memecoin_fills "
                             "WHERE position_id=? ORDER BY id", (r["id"],)).fetchall()
        entry = [f for f in fills if f["kind"] == "entry"]
        exits = [f for f in fills if f["kind"] != "entry"]
        if not entry or not all(f["notional"] and f["qty"] for f in entry):
            raise SystemExit("STOP (pre-registered): entry cost of position %s not traceable" % r["id"])
        t0, t1 = _ts(r["opened"]), _ts(r["closed_at"])
        u = {"id": r["id"], "uid": r["source_user_id"], "mint": r["mint"], "t_open": t0, "t_close": t1,
             "reason": r["exit_reason"], "band": r["policy"].get("coin_band"),
             "tp": r["policy"].get("tp_pct"), "stop": r["policy"].get("stop_pct"),
             "debounce": r["policy"].get("stop_debounce_sweeps"),
             "cost": sum(f["notional"] for f in entry), "qty": sum(f["qty"] for f in entry),
             "exit_notional": sum(f["notional"] or 0.0 for f in exits),
             "partial": len(exits) != 1}
        u["actual_ret"] = (u["exit_notional"] / u["cost"] - 1.0) * 100.0 if exits else None
        if CHAIN == "solana":
            # whole-bag value already (A2): the exit_decision quote is for the full open balance
            # The preview that fired the exit is stamped AFTER closed_at: closed_at is the sweep's
            # start stamp (= exit attempted_at) and the preview completes ~0.1-1 s later (44702: TP
            # preview 14:13:07.34, closed_at 14:13:06.44). The window therefore ends at our recorded
            # FILL, or every TP's own trigger point is cut off and the unit reads censored.
            marks = conn.execute("SELECT observed_at, expected_usdc FROM memecoin_route_series "
                                 "WHERE position_id=? AND seam='exit_decision' AND observed_at BETWEEN ? AND ? "
                                 "ORDER BY observed_at",
                                 (r["id"], r["opened"], r["closed_recorded_at"] or r["closed_at"])).fetchall()
            pts = [(_ts(m[0]), m[1]) for m in marks if m[1] and _ts(m[0]) is not None]
        else:
            marks = conn.execute("SELECT sweep_ts, executable_exit_value FROM memecoin_position_marks "
                                 "WHERE chain_id=? AND mint=? AND source='fastlane_rpc' AND quality!='quarantined_spike' "
                                 "AND sweep_ts BETWEEN ? AND ? ORDER BY sweep_ts",
                                 (CHAIN, r["mint"], r["opened"], r["closed_at"])).fetchall()
            pts = [(_ts(m[0]), m[1] * u["qty"]) for m in marks if m[1] and _ts(m[0]) is not None]
        u["usable"] = bool(pts) and _usable([p[0] for p in pts], t0, t1)
        # Our fill is RECORDED at closed_recorded_at. closed_at equals the decision stamp on 205 of 357
        # closed 4663 attempts, so it cannot time the appended fill (Lambda, 2026-09-30).
        t_fill = max(t1, _ts(r["closed_recorded_at"]) or t1)
        u["t_fill_actual"] = t_fill
        u["path"] = pts + ([(t_fill, u["exit_notional"])] if exits else [])
        att = conn.execute("SELECT attempted_at FROM memecoin_exit_execution_attempts WHERE position_id=? "
                           "AND status='closed' ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
        u["t_decision"] = _ts(att[0]) if att else None
        units.append(u)
    return units


def median_lag(conn, lo_iso, hi_iso):
    if CHAIN == "solana":
        # A3: on the PREVIEW clock. The trigger time is the preview's observed_at, which lands
        # ~0.1-1 s after attempted_at (the sweep's start stamp); measuring from attempted_at counted
        # that preview delay twice. Lag = recorded fill - the last exit_decision preview before it.
        lags = []
        for pid, fill in conn.execute(
                "SELECT p.id, p.closed_recorded_at FROM memecoin_positions p WHERE p.execution_mode='real' "
                "AND p.network_id=? AND p.is_open=0 AND p.closed_recorded_at BETWEEN ? AND ? "
                "AND p.exit_reason IN ('real_tp','b_stop','real_tp_shared_balance')", (CHAIN, lo_iso, hi_iso)):
            row = conn.execute("SELECT MAX(observed_at) FROM memecoin_route_series WHERE position_id=? "
                               "AND seam='exit_decision' AND observed_at<=?", (pid, fill)).fetchone()
            a, c = _ts(row[0] if row else None), _ts(fill)
            if a is not None and c is not None and c >= a:
                lags.append(c - a)
        return statistics.median(lags), len(lags)
    lags = []
    for att, closed in conn.execute(
            "SELECT a.attempted_at, COALESCE(p.closed_recorded_at, p.closed_at) FROM memecoin_exit_execution_attempts a "
            "JOIN memecoin_positions p "
            "ON p.id=a.position_id WHERE a.network_id=? AND a.status='closed' AND a.attempted_at BETWEEN ? AND ?",
            (CHAIN, lo_iso, hi_iso)):
        a, c = _ts(att), _ts(closed)
        if a is not None and c is not None and c >= a:
            lags.append(c - a)
    return statistics.median(lags), len(lags)


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    sys.path.insert(0, os.environ.get("SNIPER_STATE_ROOT", "/home/bigpapi/Desktop/Sniper"))
    from sniper.persistence.db_core import get_readonly_connection
    conn = get_readonly_connection("%s/sniper_tracker.db" % os.environ.get(
        "SNIPER_STATE_ROOT", "/home/bigpapi/Desktop/Sniper"))
    units = load_units(conn)
    lo = datetime.fromtimestamp(min(u["t_open"] for u in units), timezone.utc).isoformat()
    hi = datetime.fromtimestamp(max(u["t_close"] for u in units), timezone.utc).isoformat()
    lag, n_lag = median_lag(conn, lo, hi)
    summary = {"units": len(units), "lag_s": lag, "lag_n": n_lag, "usable": sum(u["usable"] for u in units),
               "no_contract": sum(u["tp"] is None for u in units), "partial": sum(u["partial"] for u in units)}

    def rep(u, tp, stop):
        return replay_cell(u["path"], cost=u["cost"], tp=tp, stop=stop, debounce=u["debounce"] or 1,
                           lag_s=lag, actual_reason=u["reason"], actual_ret=u["actual_ret"], appended_last=True)

    gradable = [u for u in units if u["usable"] and u["tp"] is not None and not u["partial"]]
    inst = []
    for u in gradable:
        if u["reason"] not in TPSL:
            continue
        own = replay_cell(u["path"], cost=u["cost"], tp=u["tp"], stop=u["stop"] if u["stop"] else 1e9,
                          debounce=u["debounce"] or 1, lag_s=lag, actual_reason=u["reason"],
                          actual_ret=u["actual_ret"], appended_last=True, own=True)
        inst.append({"id": u["id"], "reason": u["reason"], "t_decision": u["t_decision"], "actual_ret": u["actual_ret"],
                     "own": own, "match": instrument_match(own, actual_reason=u["reason"], t_decision=u["t_decision"],
                                                          actual_ret=u["actual_ret"])})
    judged = [i for i in inst if i["t_decision"] is not None]
    summary["instrument"] = {"tpsl_units": len(inst), "judged": len(judged), "matched": sum(i["match"] for i in judged),
                             "share": (sum(i["match"] for i in judged) / len(judged)) if judged else None,
                             "own_filled_on_appended": sum(i["own"]["fill_appended"] for i in inst)}
    with open(os.path.join(out_dir, "instrument.jsonl"), "w") as f:
        for i in inst:
            f.write(json.dumps(i) + "\n")
    passed = summary["instrument"]["share"] is not None and summary["instrument"]["share"] >= INSTRUMENT_PASS
    summary["instrument"]["passed"] = passed
    if not passed:
        summary["stop"] = "%s withheld: instrument check below 90%%; cells not read (pre-registered)" % CHAIN
        print(json.dumps(summary, indent=1))
        return summary
    rows_out = open(os.path.join(out_dir, "cells.jsonl"), "w")
    summary["pools"] = {}
    for band in sorted({u["band"] for u in gradable}, key=str):
        pool = [u for u in gradable if u["band"] == band]
        train, test = chronological_split(pool, key="t_open")
        train_ids = {u["id"] for u in train}
        res = {"n": len(pool), "n_train": len(train), "n_test": len(test)}
        if len(train) < 10 or len(test) < 4:
            res["result"] = "too few units (pre-registered minimum 10 train + 4 test)"
            summary["pools"][str(band)] = res
            continue
        grid = {}
        for tp, stop in CELLS:
            outs = []
            for u in pool:
                o = rep(u, tp, stop)
                rows_out.write(json.dumps({"id": u["id"], "band": band, "tp": tp, "stop": stop,
                                           "split": "train" if u["id"] in train_ids else "test", **o}) + "\n")
                outs.append((u, o))
            tr = [o for u, o in outs if u["id"] in train_ids]
            obs = [o["ret"] for o in tr if o["status"] != "censored"]
            grid[(tp, stop)] = {"train_mean": statistics.mean(obs) if obs else None,
                                "train_censored": 1 - len(obs) / len(tr),
                                "appended_fill_share": sum(o["fill_appended"] for o in tr) / len(tr)}
        ok = {k: v for k, v in grid.items() if v["train_mean"] is not None and v["train_censored"] <= MAX_TRAIN_CENSORED}
        sel = max(ok, key=lambda k: ok[k]["train_mean"]) if ok else None
        res["grid"] = {"%d/%d" % k: v for k, v in grid.items()}
        res["selected"] = "%d/%d" % sel if sel else None
        if sel:
            diffs = []
            for u in test:
                a = rep(u, *sel)
                b = replay_cell(u["path"], cost=u["cost"], tp=u["tp"], stop=u["stop"] if u["stop"] else 1e9,
                                debounce=u["debounce"] or 1, lag_s=lag, actual_reason=u["reason"],
                                actual_ret=u["actual_ret"], appended_last=True, own=True)   # A4
                if a["status"] != "censored" and b["status"] != "censored":
                    diffs.append((u["mint"], a["ret"] - b["ret"]))
            vals = [d for _m, d in diffs]
            p = cluster_bootstrap_p(diffs) if diffs else None
            mean = statistics.mean(vals) if vals else None
            without_best = statistics.mean(sorted(vals)[:-1]) if len(vals) > 1 else None
            res["test"] = {"paired_n": len(diffs), "dropped_censored": len(test) - len(diffs), "mean_pts": mean,
                           "mean_without_best": without_best, "p_one_sided": p,
                           "pass": bool(p is not None and p <= 0.10 and mean is not None and mean >= 1.0
                                        and without_best is not None and without_best > 0 and sel != (25, 35))}
        summary["pools"][str(band)] = res
    rows_out.close()
    print(json.dumps(summary, indent=1, default=str))
    return summary


def benjamini_hochberg(pvalues):
    """BH-adjusted q per position (step-up), capped at 1."""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q, running = [None] * m, 1.0
    for rank in range(m, 0, -1):
        i = order[rank - 1]
        running = min(running, pvalues[i] * m / rank)
        q[i] = running
    return q


def per_wallet(units, *, lag, overrides, shrink_k=10.0):
    """The pre-registered per-wallet read (A2-A4): per (wallet, CHAIN) pair with >= 10 train + 4 test
    usable units, each cell's train mean shrunk toward the pool (CHAIN x the pair's modal coin_band),
    selection on train among cells with <= 20% train censored, ONE paired test on test units (selected
    cell minus our actual fill, A4), BH q <= 0.10 over the pairs that reach the test."""
    def rep(u, tp, stop):
        return replay_cell(u["path"], cost=u["cost"], tp=tp, stop=stop, debounce=u["debounce"] or 1,
                           lag_s=lag, actual_reason=u["reason"], actual_ret=u["actual_ret"], appended_last=True)

    def own(u):
        return replay_cell(u["path"], cost=u["cost"], tp=u["tp"], stop=u["stop"] if u["stop"] else 1e9,
                           debounce=u["debounce"] or 1, lag_s=lag, actual_reason=u["reason"],
                           actual_ret=u["actual_ret"], appended_last=True, own=True)

    gradable = [u for u in units if u["usable"] and u["tp"] is not None and not u["partial"]]
    pool_mean = {}
    for band in {u["band"] for u in gradable}:
        train, _test = chronological_split([u for u in gradable if u["band"] == band], key="t_open")
        for cell in CELLS:
            obs = [o["ret"] for o in (rep(u, *cell) for u in train) if o["status"] != "censored"]
            pool_mean[(band, cell)] = statistics.mean(obs) if obs else None
    by_pair = defaultdict(list)
    for u in gradable:
        by_pair[u["uid"]].append(u)
    rows = []
    for uid, us in by_pair.items():
        train, test = chronological_split(us, key="t_open")
        band = max({u["band"] for u in us}, key=lambda b: sum(u["band"] == b for u in us))
        row = {"uid": uid, "n": len(us), "n_train": len(train), "n_test": len(test), "band": band,
               "bands": dict(sorted(((str(b), sum(u["band"] == b for u in us)) for b in {u["band"] for u in us}))),
               "override": overrides.get(uid)}
        if len(train) < 10 or len(test) < 4:
            row["result"] = "too few units (10 train + 4 test)"
            rows.append(row)
            continue
        best = None
        for cell in CELLS:
            outs = [rep(u, *cell) for u in train]
            obs = [o["ret"] for o in outs if o["status"] != "censored"]
            cens = 1 - len(obs) / len(outs)
            if not obs or cens > MAX_TRAIN_CENSORED:
                continue
            pm = pool_mean.get((band, cell))
            shrunk = ((len(obs) * statistics.mean(obs) + shrink_k * pm) / (len(obs) + shrink_k)
                      if pm is not None else statistics.mean(obs))
            if best is None or shrunk > best[1]:
                best = (cell, shrunk, statistics.mean(obs), cens)
        if best is None:
            row["result"] = "no cell with <= 20% train censored"
            rows.append(row)
            continue
        cell = best[0]
        diffs = []
        for u in test:
            a, b = rep(u, *cell), own(u)
            if a["status"] != "censored" and b["status"] != "censored":
                diffs.append((u["mint"], a["ret"] - b["ret"]))
        vals = [d for _m, d in diffs]
        row.update({"selected": "%d/%d" % cell, "train_shrunk": best[1], "train_raw": best[2],
                    "train_censored": best[3], "paired_n": len(diffs), "dropped_censored": len(test) - len(diffs),
                    "mean_pts": statistics.mean(vals) if vals else None,
                    "mean_without_best": statistics.mean(sorted(vals)[:-1]) if len(vals) > 1 else None,
                    "p_one_sided": cluster_bootstrap_p(diffs) if diffs else None})
        rows.append(row)
    tested = [r for r in rows if r.get("p_one_sided") is not None]
    for r, q in zip(tested, benjamini_hochberg([r["p_one_sided"] for r in tested])):
        current = (r["override"] or {}).get("tp_pct") or 25, (r["override"] or {}).get("stop_pct") or 35
        r["q"] = q
        r["current"] = "%s/%s" % current
        r["result"] = ("recommend_override" if (q <= 0.10 and r["mean_pts"] is not None and r["mean_pts"] >= 1.0
                                               and r["mean_without_best"] is not None and r["mean_without_best"] > 0
                                               and r["selected"] != "%d/%d" % tuple(int(x) for x in current))
                       else "not_shown")
    return rows


if __name__ == "__main__":
    main(sys.argv[1])

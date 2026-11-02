#!/usr/bin/env python3
"""Position shape table, stage 1: how every closed memecoin position moved between fill and exit.

Asked 2026-09-27 by Alpha for the operator's shape-based-exit direction. DESCRIPTIVE ONLY: nothing
here is pre-registered and nothing recommends an exit.

ONE ROW PER CLOSED POSITION with closed_at >= --since, funded (execution_mode 'real') and paper.
Returns are in UNIT space, r = price / entry_price - 1, so a partial exit does not distort the path;
with no partial exit this equals whole-bag value / entry cost.

  funded path   memecoin_route_series seam='exit_decision' (writer: _keep_route_quote in
                memecoin_real_runner), ORDER BY id. EXECUTABLE: unit_price = exact whole-bag quote
                of the bag then open / open_qty. Present only while the exit sweep quoted the
                position.
  paper path    memecoin_position_marks (chain_id = network_id, mint, sweep_ts in [fill, close]),
                `mid`. A MID, not executable (no impact), and per MINT, shared by every position
                on the coin. Present only while the mint was in the marks universe.
  paper stored  memecoin_positions.max_gain_pct / time_to_peak_s, written by the paper close
                (memecoin_exits._close / _track_peak) from the sweep MID; time_to_peak_s runs from
                opened_at, the DECISION START, not the fill.

Per row: MFE %, MAE %, seconds from the fill (opened_recorded_at) to each (first occurrence), r at
+10/+30/+60/+120/+300 s (the last path point at or before the horizon, within HORIZON_TOL_S of it,
and only while the position was still open), final realized % (realized_pnl / entry_notional),
exit_reason. Keys: source_user_id, handle, chain, lane, signal_source, strategy; funded rows add
coin_band / liquidity_class / effective_mcap / volume_h1 / volume_m5 from the entry attempt's
classification_json (joined on memecoin_entry_attempts.position_id), both modes add
entry_token_age_s, entry_mcap, their_size_usd.

Ledger read-only (read-only URI). No network. Writes ONE CSV to --out (never under data/).
    python3 scripts/research/position_shape.py --out <scratchpad>/position_shape.csv
    python3 scripts/research/position_shape.py --selftest
"""
import argparse
import contextlib
import csv
import json
import sqlite3
import statistics
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HORIZONS = (10, 30, 60, 120, 300)
HORIZON_TOL_S = 5.0


def ts(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()


def shape(points, entry_price, fill_at, closed_at):
    """points: [(t_iso, price)] in path order. Returns MFE/MAE, their times, horizon values, n."""
    out = {"n_points": 0, "mfe_pct": None, "mae_pct": None, "t_mfe_s": None, "t_mae_s": None,
           "first_point_s": None, "r_first_pct": None}
    for h in HORIZONS:
        out["r%d_pct" % h] = None
    try:
        entry = float(entry_price)
        f0, cl = ts(fill_at), ts(closed_at)
    except (TypeError, ValueError):
        return out
    if entry <= 0:
        return out
    path = []
    for t_iso, price in points:
        if price is None or price <= 0:
            continue
        t = ts(t_iso) - f0
        if t < 0 or t > cl - f0:
            continue
        path.append((t, (price / entry - 1.0) * 100.0))
    if not path:
        return out
    out["n_points"] = len(path)
    out["first_point_s"] = round(path[0][0], 3)
    out["r_first_pct"] = round(path[0][1], 3)     # funded: the sell-back haircut right after the fill
    best = max(range(len(path)), key=lambda i: (path[i][1], -i))    # first occurrence of the max
    worst = min(range(len(path)), key=lambda i: (path[i][1], i))    # first occurrence of the min
    out["mfe_pct"], out["t_mfe_s"] = round(path[best][1], 3), round(path[best][0], 3)
    out["mae_pct"], out["t_mae_s"] = round(path[worst][1], 3), round(path[worst][0], 3)
    for h in HORIZONS:
        if cl - f0 <= h:
            continue                      # already closed at the horizon: no held value exists
        upto = [p for p in path if p[0] <= h]
        if upto and h - upto[-1][0] <= HORIZON_TOL_S:
            out["r%d_pct" % h] = round(upto[-1][1], 3)
    return out


def pct(xs, p):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def realized_pct(pnl, notional):
    try:
        return round(float(pnl) / float(notional) * 100.0, 3) if float(notional) > 0 else None
    except (TypeError, ValueError):
        return None


BASE_COLS = ("id, execution_mode, strategy, source_user_id, source_handle, network_id, signal_source, "
             "mint, entry_price, entry_notional, opened_at, opened_recorded_at, closed_at, exit_reason, "
             "realized_pnl, entry_token_age_s, entry_mcap, their_size_usd, max_gain_pct, time_to_peak_s")


def load(since):
    rows = []
    with contextlib.closing(sqlite3.connect("file:%s?mode=ro" % (REPO / "sniper_tracker.db"),
                                            uri=True, timeout=10)) as c:
        c.row_factory = sqlite3.Row
        positions = c.execute(
            "SELECT %s FROM memecoin_positions WHERE is_open=0 AND closed_at >= ? "
            "AND execution_mode IN ('real', 'paper') ORDER BY id" % BASE_COLS, (since,)).fetchall()
        for p in positions:
            real = p["execution_mode"] == "real"
            fill_at = p["opened_recorded_at"] or p["opened_at"]
            if real:
                points = [(r[0], r[1]) for r in c.execute(
                    "SELECT observed_at, unit_price FROM memecoin_route_series WHERE position_id=? "
                    "AND seam='exit_decision' ORDER BY id", (p["id"],))]
                att = c.execute(
                    "SELECT delivery_lane, classification_json FROM memecoin_entry_attempts "
                    "WHERE position_id=? LIMIT 1", (p["id"],)).fetchone()
                cls = json.loads(att[1]) if att and att[1] else {}
                facts = cls.get("facts") or {}
                lane = (att[0] if att and att[0] else None) or p["signal_source"]
            else:
                points = [(r[0], r[1]) for r in c.execute(
                    "SELECT sweep_ts, mid FROM memecoin_position_marks WHERE chain_id=? AND mint=? "
                    "AND sweep_ts >= ? AND sweep_ts <= ? ORDER BY sweep_ts",
                    (p["network_id"], p["mint"], fill_at, p["closed_at"]))]
                cls, facts, lane = {}, {}, p["signal_source"]
            s = shape(points, p["entry_price"], fill_at, p["closed_at"])
            rows.append({
                "id": p["id"], "mode": p["execution_mode"], "strategy": p["strategy"],
                "path_basis": "route_exit_decision_executable" if real else "marks_mid",
                "source_user_id": p["source_user_id"], "handle": p["source_handle"],
                "chain": p["network_id"], "lane": lane, "signal_source": p["signal_source"],
                "coin_band": cls.get("coin_band"), "liquidity_class": cls.get("liquidity_class"),
                "effective_mcap": facts.get("effective_mcap"), "volume_h1": facts.get("volume_h1"),
                "volume_m5": facts.get("volume_m5"), "entry_token_age_s": p["entry_token_age_s"],
                "entry_mcap": p["entry_mcap"], "their_size_usd": p["their_size_usd"],
                "entry_notional": p["entry_notional"],
                "fill_stamp": "opened_recorded_at" if p["opened_recorded_at"] else "opened_at",
                "fill_at": fill_at, "closed_at": p["closed_at"],
                "held_s": round(ts(p["closed_at"]) - ts(fill_at), 3) if fill_at else None,
                "exit_reason": p["exit_reason"],
                "realized_pct": realized_pct(p["realized_pnl"], p["entry_notional"]),
                "realized_pnl": p["realized_pnl"],
                "stored_max_gain_pct_mid": None if real else p["max_gain_pct"],
                "stored_time_to_peak_s_from_decision": None if real else p["time_to_peak_s"],
                **s})
    return rows


def coverage(rows, mode):
    rs = [r for r in rows if r["mode"] == mode]
    cols = ("n_points", "mfe_pct", "mae_pct", "r10_pct", "r30_pct", "r60_pct", "r120_pct",
            "r300_pct", "realized_pct", "coin_band", "effective_mcap", "volume_h1",
            "entry_token_age_s", "entry_mcap", "stored_max_gain_pct_mid")
    out = {"n": len(rs)}
    for col in cols:
        out[col] = sum(1 for r in rs if r[col] not in (None, 0) or (col != "n_points" and r[col] == 0))
    out["held_over"] = {h: sum(1 for r in rs if r["held_s"] is not None and r["held_s"] > h)
                        for h in HORIZONS}
    return out


def by_wallet(rows, mode, strategy=None):
    groups = {}
    for r in rows:
        if (r["mode"] == mode and r["mfe_pct"] is not None
                and (strategy is None or r["strategy"] == strategy)):
            groups.setdefault(r["handle"] or r["source_user_id"], []).append(r)
    out = []
    for handle, rs in groups.items():
        out.append({
            "handle": handle, "n": len(rs), "flag": "n<10" if len(rs) < 10 else "",
            "r_first_p50": pct([r["r_first_pct"] for r in rs], .5),
            "mfe_p50": pct([r["mfe_pct"] for r in rs], .5), "mae_p50": pct([r["mae_pct"] for r in rs], .5),
            "t_mfe_p50": pct([r["t_mfe_s"] for r in rs], .5), "t_mae_p50": pct([r["t_mae_s"] for r in rs], .5),
            "realized_p50": pct([r["realized_pct"] for r in rs], .5),
            "pnl_sum": sum(float(r["realized_pnl"] or 0) for r in rs),
            "mfe_ge15": sum(1 for r in rs if r["mfe_pct"] >= 15.0)})
    return sorted(out, key=lambda w: -w["n"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-09-13T00:00:00")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.out or "/data/" in str(Path(a.out).resolve()) + "/":
        raise SystemExit("--out is required and must not be under data/")
    rows = load(a.since)
    with open(a.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("rows %d -> %s" % (len(rows), a.out))
    for mode in ("real", "paper"):
        print("coverage", mode, json.dumps(coverage(rows, mode)))
    arms = [("real", None)] + [("paper", s) for s in sorted({r["strategy"] for r in rows
                                                               if r["mode"] == "paper"})]
    for mode, strategy in arms:
        print("\nper wallet, %s %s (path points present):" % (mode, strategy or ""))
        for wv in by_wallet(rows, mode, strategy)[:40]:
            print("  %-18s n=%4d %-4s first %+7.2f MFE p50 %+7.2f MAE p50 %+7.2f tMFE p50 %7.1fs "
                  "tMAE p50 %7.1fs realized p50 %+7.2f pnl %+9.2f MFE>=15%% %d" % (
                      str(wv["handle"])[:18], wv["n"], wv["flag"], wv["r_first_p50"],
                      wv["mfe_p50"], wv["mae_p50"],
                      wv["t_mfe_p50"], wv["t_mae_p50"], wv["realized_p50"] or 0.0, wv["pnl_sum"],
                      wv["mfe_ge15"]))


def selftest():
    fill, close = "2026-09-27T20:00:00+00:00", "2026-09-27T20:01:40+00:00"     # held 100 s
    pts = [("2026-09-27T19:59:59+00:00", 5.0),     # before the fill: ignored
           ("2026-09-27T20:00:02+00:00", 1.10),    # +10% at 2 s
           ("2026-09-27T20:00:08+00:00", 0.90),    # -10% at 8 s
           ("2026-09-27T20:00:20+00:00", 1.10),    # +10% again at 20 s: MFE keeps the FIRST
           ("2026-09-27T20:00:29+00:00", None),    # unpriced: ignored
           ("2026-09-27T20:00:40+00:00", 0.0),     # a zero price is not -100%: ignored
           ("2026-09-27T20:00:58+00:00", 1.00),
           ("2026-09-27T20:01:39+00:00", 0.90),    # -10% again at 99 s: MAE keeps the FIRST
           ("2026-09-27T20:01:45+00:00", 9.0)]     # after the close: ignored
    s = shape(pts, 1.0, fill, close)
    assert s["n_points"] == 5 and s["first_point_s"] == 2.0 and s["r_first_pct"] == 10.0, s
    assert (s["mfe_pct"], s["t_mfe_s"]) == (10.0, 2.0), s
    assert (s["mae_pct"], s["t_mae_s"]) == (-10.0, 8.0), s
    assert s["r10_pct"] == -10.0          # last point at or before 10 s is the 8 s point (2 s old)
    assert s["r30_pct"] is None           # last point before 30 s is 20 s: 10 s old > 5 s tolerance
    assert s["r60_pct"] == 0.0            # the 58 s point
    assert s["r120_pct"] is None and s["r300_pct"] is None      # closed at 100 s
    assert shape(pts, 0, fill, close)["mfe_pct"] is None
    # Closed at 118 s with a point at 117 s: that point is within 5 s of +120 s, but the position
    # was no longer held at +120 s, so no held value exists there.
    late = shape([("2026-09-27T20:01:57+00:00", 1.2)], 1.0, fill, "2026-09-27T20:01:58+00:00")
    assert late["n_points"] == 1 and late["r120_pct"] is None, late
    assert realized_pct(-5.0, 100.0) == -5.0 and realized_pct(1.0, 0) is None
    print("selftest ok")


if __name__ == "__main__":
    main()

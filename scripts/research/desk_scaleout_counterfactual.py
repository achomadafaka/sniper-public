"""Desk hold-winners counterfactual: scale-out (sell half at TP, wide trail on the rest) vs what the desk did.

READ-ONLY. Reads the funded ledger (desk positions, fills, rails timeline, route-quote ticks) and the desk tapes
(data/desk/swaps/<day>.db curve swaps; data/desk/pumpswap/tape.db AMM swaps after a graduation).

PRICE MODEL = exact executable sell math from the tape's post-swap reserves, never spot:
  curve: out_sol(Q) = Q * vq / (vt + Q), less (fee_bps + creator_fee_bps)
  AMM:   out_sol(Q) = Q * quote_res / (base_res + Q), less (lp + protocol + creator fee bps)
sol_usd_c is calibrated per position from OUR entry swap. Validated against our own exit swaps where the curve tape
holds them (`val` = [model, actual]). Rails are evaluated like the funded runner: whole-bag exact unit price vs
entry_price, stop / TP / trail from the ratcheted executable peak, polled on the position's real exit_decision ticks
(then 1 s), executed LAG_S after the trigger. The rails in force are the real override timeline (RISK tightens included).

NOT MODELLED: RISK decisions on the remainder (a real desk_risk_exit is replayed at its real time), priority fees,
sell failures, the market's reaction to our sale. A path that ends (no swaps left in the tape) is CENSORED and valued
at the last tape state; it is flagged, never extended. A PumpSwap path is a DIRECT AMM sell quote, not our Jupiter route.
"""
from __future__ import annotations

import argparse
import bisect
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

LAG_S = 1.0
TICK_S = 1.0
TRAIL_CAP_PCT = 25.0                  # MEMECOIN_DESK_RISK_TRAIL_MAX_PCT default


def _ep(iso):
    return datetime.fromisoformat(iso).timestamp()


def _i(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def load_positions(ledger, ids):
    c = sqlite3.connect("file:%s?mode=ro" % ledger, uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    out = []
    for p in c.execute("SELECT * FROM memecoin_positions WHERE signal_source='desk' AND is_open=0 ORDER BY id"):
        if ids and p["id"] not in ids:
            continue
        fills = {f["kind"]: dict(f) for f in c.execute("SELECT * FROM memecoin_fills WHERE position_id=? ORDER BY id", (p["id"],))}
        pol = json.loads(p["funded_contract_json"] or "{}").get("policy") or {}
        timeline = [(None, {"tp": pol.get("tp_pct"), "sl": pol.get("stop_pct"), "trail": pol.get("trail_pct")})]
        for o in c.execute("SELECT tp_pct,sl_pct,trail_pct,created_at FROM memecoin_position_exit_overrides "
                           "WHERE position_id=? AND source IN ('desk','desk_risk') ORDER BY id", (p["id"],)):
            timeline.append((o["created_at"], {"tp": o["tp_pct"], "sl": o["sl_pct"], "trail": o["trail_pct"]}))
        ticks = [r[0] for r in c.execute("SELECT observed_at FROM memecoin_route_series WHERE position_id=? "
                                         "AND seam='exit_decision' ORDER BY observed_at", (p["id"],))]
        out.append({"pos": dict(p), "fills": fills, "timeline": timeline, "ticks": ticks})
    return out


def load_path(desk_dir, mint, lo, hi):
    """[(received_at, state)]: curve swaps, then AMM swaps after a graduation, ordered by time."""
    path = []
    days = sorted({datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d") for t in (lo, (lo + hi) / 2, hi)})
    for day in days:
        f = Path(desk_dir) / "swaps" / ("%s.db" % day)
        if not f.exists():
            continue
        s = sqlite3.connect("file:%s?mode=ro" % f, uri=True, timeout=5)
        s.row_factory = sqlite3.Row
        for r in s.execute("SELECT slot, ordinal, signature, is_buy, quote_atomic, token_atomic, virtual_quote, virtual_token, "
                           "fee_bps, creator_fee_bps, received_at FROM swaps WHERE mint=? AND received_at BETWEEN ? AND ? "
                           "ORDER BY slot, ordinal", (mint, lo, hi)):
            path.append((r["received_at"], {"kind": "curve", "q": r["virtual_quote"], "b": r["virtual_token"],
                                            "fee": ((r["fee_bps"] or 0) + (r["creator_fee_bps"] or 0)) / 1e4,
                                            "sig": r["signature"], "slot": r["slot"], "ord": r["ordinal"], "row": dict(r)}))
    f = Path(desk_dir) / "pumpswap" / "tape.db"
    if f.exists():
        s = sqlite3.connect("file:%s?mode=ro" % f, uri=True, timeout=5)
        s.row_factory = sqlite3.Row
        pools = [r[0] for r in s.execute("SELECT DISTINCT pool FROM ps_grads WHERE mint=?", (mint,))]
        for r in s.execute("SELECT slot, ordinal, signature, post_base_reserve_atomic, post_quote_reserve_atomic, lp_fee_bps, "
                           "protocol_fee_bps, creator_fee_bps, received_at FROM swaps_ps WHERE pool IN (%s) AND received_at BETWEEN ? AND ? "
                           "ORDER BY slot, ordinal" % ",".join("?" * len(pools)), (*pools, lo, hi)) if pools else ():
            b, q = _i(r["post_base_reserve_atomic"]), _i(r["post_quote_reserve_atomic"])
            if b > 0 and q > 0:
                fee = (_i(r["lp_fee_bps"]) + _i(r["protocol_fee_bps"]) + _i(r["creator_fee_bps"])) / 1e4
                path.append((r["received_at"], {"kind": "amm", "q": q, "b": b, "fee": fee, "sig": r["signature"],
                                                "slot": r["slot"], "ord": r["ordinal"]}))
    path.sort(key=lambda x: (x[0], x[1]["slot"], x[1]["ord"]))
    return path


def sell_usd(state, qty_atomic, sol_usd):
    if state is None or qty_atomic <= 0 or not state["q"] or not state["b"]:
        return None
    return qty_atomic * state["q"] / (state["b"] + qty_atomic) / 1e9 * (1 - state["fee"]) * sol_usd


class Tape:
    def __init__(self, path):
        self.t = [p[0] for p in path]
        self.s = [p[1] for p in path]

    def at(self, T):
        i = bisect.bisect_right(self.t, T) - 1
        return self.s[i] if i >= 0 else None

    @property
    def end(self):
        return self.t[-1] if self.t else None


def rails_at(timeline, T, off):
    cur = timeline[0][1]
    for at, r in timeline[1:]:
        if _ep(at) + off <= T:
            cur = r
    return cur


def poll_grid(ticks, off, t_end, start):
    grid = [_ep(t) + off for t in ticks]
    t = (grid[-1] if grid else start) + TICK_S
    while t <= t_end:
        grid.append(t)
        t += TICK_S
    return [g for g in grid if g >= start]


def simulate(P, grid, timeline, off, *, qty, sol, entry_price, scale=None, risk_exit_t=None, trail_mult=1.0):
    """scale=None: the rails as they were. scale={'widen','trigger_pct'}: sell half at the trigger, wide trail on the rest.
    Returns proceeds (USD, both legs), scaled, censored, exit_t, reason."""
    peak = entry_price
    half_q, rem_q = qty // 2, qty - qty // 2
    pend, stage, half, rem_peak = None, "full", None, None
    for T in grid:
        if pend is not None and T >= pend[1] + LAG_S - 1e-9:
            st = P.at(pend[1] + LAG_S)
            if pend[0] == "half":
                half = (sell_usd(st, half_q, sol), pend[1] + LAG_S)
                stage, pend = "rem", None
                rem_peak = max(peak, sell_usd(st, rem_q, sol) / (rem_q / 1e6))
            elif stage == "rem":
                return {"proceeds": half[0] + sell_usd(st, rem_q, sol), "scaled": True, "censored": False,
                        "exit_t": pend[1] + LAG_S, "reason": pend[0]}
            else:
                return {"proceeds": sell_usd(st, qty, sol), "scaled": False, "censored": False,
                        "exit_t": pend[1] + LAG_S, "reason": pend[0]}
        if pend is not None:
            continue
        if risk_exit_t is not None and T >= risk_exit_t - 1e-9:                       # a real RISK exit, replayed at its real time
            q = rem_q if stage == "rem" else qty
            return {"proceeds": (half[0] if half else 0) + sell_usd(P.at(risk_exit_t), q, sol), "scaled": stage == "rem",
                    "censored": False, "exit_t": risk_exit_t, "reason": "risk_exit"}
        st = P.at(T)
        if st is None:
            continue
        r = rails_at(timeline, T, off)
        if stage == "full":
            u = sell_usd(st, qty, sol)
            if u is None:
                continue
            unit = u / (qty / 1e6)
            peak = max(peak, unit)
            trail = min(TRAIL_CAP_PCT, r["trail"] * trail_mult) if r["trail"] else None
            if scale and unit >= entry_price * (1 + (r["tp"] or 0) * scale["mult"] / 100.0) and r["tp"]:
                pend = ("half", T)
            elif unit <= entry_price * (1 - r["sl"] / 100.0):
                pend = ("b_stop", T)
            elif not scale and r["tp"] and unit >= entry_price * (1 + r["tp"] / 100.0):
                pend = ("real_tp", T)
            elif trail and unit <= peak * (1 - trail / 100.0):
                pend = ("real_trail", T)
        else:
            u = sell_usd(st, rem_q, sol)
            if u is None:
                continue
            unit = u / (rem_q / 1e6)
            rem_peak = max(rem_peak, unit)
            wide = min(TRAIL_CAP_PCT, (timeline[0][1]["trail"] or 0) * scale["widen"]) or None
            if unit <= entry_price:
                pend = ("be_stop", T)                                                 # tighten-only: break-even
            elif wide and unit <= rem_peak * (1 - wide / 100.0):
                pend = ("scale_trail", T)
    end = P.end
    q = rem_q if stage == "rem" else qty
    mark = sell_usd(P.at(end), q, sol) if end else None
    return {"proceeds": (half[0] if half else 0) + (mark or 0), "scaled": stage == "rem", "censored": True,
            "exit_t": end, "reason": None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="sniper_tracker.db")
    ap.add_argument("--desk-dir", default="data/desk")
    ap.add_argument("--ids", default="")
    ap.add_argument("--horizon-s", type=float, default=900.0)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    rows = []
    for p in load_positions(a.ledger, {int(x) for x in a.ids.split(",") if x}):
        pos, fills, timeline = p["pos"], p["fills"], p["timeline"]
        entry, ex = fills.get("entry"), fills.get("bracket_exit")
        row = {"id": pos["id"], "ticker": pos["ticker"], "exit_reason": pos["exit_reason"],
               "actual_pnl": round(pos["realized_pnl"], 3)}
        dur = _ep(pos["closed_at"]) - _ep(pos["opened_at"])
        o = _ep(pos["opened_at"])
        full = load_path(a.desk_dir, pos["mint"], o - 120, o + dur + a.horizon_s)
        hit = next(((t, s) for t, s in full if entry and s["sig"] == entry["tx_sig"]), None)
        if hit is None:
            rows.append({**row, "skip": "entry_swap_not_in_tape"})
            continue
        t0, ent = hit
        qty = ent["row"]["token_atomic"]
        sol = pos["entry_notional"] * (1 - ent["fee"]) * 1e9 / ent["row"]["quote_atomic"]
        ep = pos["entry_notional"] / (qty / 1e6)
        P = Tape([x for x in full if x[0] >= t0])
        off = 0.0                                                                     # host clock == corrected clock to ~0.1 s (measured on the exit swaps)
        te = _ep(pos["closed_at"])                                                    # the real exit on the tape clock
        real_unit = ex["price"] / ep - 1 if ex else None
        te_note = "closed_at"
        unit_at = lambda T: (sell_usd(P.at(T), qty, sol) / (qty / 1e6) / ep - 1) if P.at(T) and sell_usd(P.at(T), qty, sol) else None
        if real_unit is not None and not any(s["sig"] == ex["tx_sig"] for _t, s in full):
            cur = unit_at(te)
            if cur is None or abs(cur - real_unit) > 0.03:                            # exit swap missing AND the clock says another price:
                cands = [(abs(unit_at(t) - real_unit), t) for t in P.t if te - 15 <= t <= te and unit_at(t) is not None]
                if cands:
                    te = min(cands)[1]                                                # match the tape state by the REAL fill price
                    te_note = "price_matched"
        grid = poll_grid(p["ticks"], off, P.end if P.end else te, t0)
        row.update({"hold_s": round(dur, 1), "sol_usd_c": round(sol, 2), "rails0": timeline[0][1],
                    "tape_end_after_exit_s": round((P.end or te) - te, 1), "te_basis": te_note, "amm": any(s["kind"] == "amm" for _t, s in full)})
        exit_sw = next((s for _t, s in full if ex and s["sig"] == ex["tx_sig"]), None)
        if exit_sw:
            prev = [s for _t, s in full if (s["slot"], s["ord"]) < (exit_sw["slot"], exit_sw["ord"])]
            row["val"] = [round(sell_usd(prev[-1], qty, sol), 3), round(ex["notional"], 3)]
        base = sell_usd(P.at(te), qty, sol)
        row["pct_at_exit"] = round((base / (qty / 1e6) / ep - 1) * 100, 1) if base else None
        row["base_pnl"] = round(base - pos["entry_notional"], 3) if base else None
        rp = simulate(P, grid, timeline, off, qty=qty, sol=sol, entry_price=ep)
        row["replay"] = {"reason": rp["reason"], "dt_vs_real_s": None if rp["censored"] else round(rp["exit_t"] - te, 1),
                         "censored": rp["censored"], "pnl": round(rp["proceeds"] - pos["entry_notional"], 3)}
        row["replay_ok"] = (None if rp["censored"] or pos["exit_reason"] == "desk_risk_exit"
                            else abs(rp["exit_t"] - te) <= 3.0)                        # rule-only replay reproduces the real rail exit
        after = {}
        for w in (30, 60, 300, 900):
            vals = []
            for t, s in zip(P.t, P.s):
                if te < t <= te + w:
                    u = sell_usd(s, qty, sol)
                    if u:
                        vals.append(u / (qty / 1e6) / ep - 1)
            after["+%ds" % w] = round(max(vals) * 100, 1) if vals else None
        row["max_pct_after_exit"] = after
        risk_t = te if pos["exit_reason"] == "desk_risk_exit" else None
        row["tp_in_force_at_exit"] = rails_at(timeline, te, off)["tp"]
        row["arms"] = {}
        for name, widen, mult in (("S1.5x@TP", 1.5, 1.0), ("S2x@TP", 2.0, 1.0), ("S2x@TP/2", 2.0, 0.5)):
            r = simulate(P, grid, timeline, off, qty=qty, sol=sol, entry_price=ep, risk_exit_t=risk_t,
                         scale={"widen": widen, "mult": mult})
            row["arms"][name] = {"pnl": round(r["proceeds"] - pos["entry_notional"], 3), "scaled": r["scaled"],
                                 "censored": r["censored"], "reason": r["reason"],
                                 "exit_after_entry_s": round(r["exit_t"] - t0, 1) if r["exit_t"] else None}
        for name, mult in (("W1.5x-whole", 1.5), ("W2x-whole", 2.0)):                 # wide trail on the WHOLE bag from entry
            r = simulate(P, grid, timeline, off, qty=qty, sol=sol, entry_price=ep, risk_exit_t=risk_t, trail_mult=mult)
            row["arms"][name] = {"pnl": round(r["proceeds"] - pos["entry_notional"], 3), "scaled": False,
                                 "censored": r["censored"], "reason": r["reason"],
                                 "exit_after_entry_s": round(r["exit_t"] - t0, 1) if r["exit_t"] else None}
        rows.append(row)
    text = json.dumps(rows, indent=1)
    if a.out:
        Path(a.out).write_text(text)
    print(text)


if __name__ == "__main__":
    main()

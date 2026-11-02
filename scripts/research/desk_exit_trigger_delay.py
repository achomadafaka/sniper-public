#!/usr/bin/env python3
"""Trigger-to-landing delay of real desk RAIL exits, from the tape (descriptive): for each funded desk position whose booked exit reason is a rail (real_trail / real_tp / b_stop), find the first second at which the
exact tape value would have triggered the booked rails (arm 0, SL/TP/trail), and compare with the received_at of the real exit swap. delay = real exit swap time - trigger second."""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts" / "research"))
import desk_trail_arming as A
S, L = A.S, A.L


def first_trigger(seconds, cost, sl, tp, tr):
    for k, x in enumerate(seconds):
        if x["v"] is None:
            continue
        if sl and x["v"] <= cost * (1 - sl): return k, "sl"
        if tp and x["v"] >= cost * (1 + tp): return k, "tp"
        if tr and x["v"] <= x["peak"] * (1 - tr): return k, "trail"
    return None, None


def main():
    import time
    from sniper.util import clock as clockmod
    from sniper.util.state_paths import state_path
    clock = S.Clock(clockmod.offset_s())
    ledger, desk_db, desk_dir = str(state_path("sniper_tracker.db")), str(state_path("data", "memecoin_desk.db")), Path(state_path("data", "desk"))
    units, _ = S.discover(ledger, desk_db, time.time(), clock, 0, set())
    out, skipped = [], {}
    for u in units:
        if u["kind"] != "realized" or not u.get("rails"):
            continue
        reason = (u["exit_info"] or {}).get("exit_reason") or ""
        if reason not in ("real_trail", "real_tp", "b_stop", "bracket_stop"):
            skipped["non_rail:" + reason] = skipped.get("non_rail:" + reason, 0) + 1; continue
        res = A.unit_series(u, desk_dir, time.time())
        if isinstance(res, str):
            skipped[res] = skipped.get(res, 0) + 1; continue
        seconds, cost, notional, t0 = res
        sl, tp, tr = A.rails_of(u)
        k, why = first_trigger(seconds, cost, sl, tp, tr)
        legs = [l for l in (u["exit_info"].get("legs") or []) if l.get("kind") != "entry"]
        if k is None or not legs:
            skipped["no_trigger_or_leg"] = skipped.get("no_trigger_or_leg", 0) + 1; continue
        rr = None
        for day in {L.utc_day(u["t_ref"]), L.utc_day(u["t_ref"] + 86400)}:
            path = Path(desk_dir) / "swaps" / ("%s.db" % day)
            if path.exists():
                r = L.q(path, "SELECT received_at FROM swaps WHERE signature=? AND mint=? AND is_buy=0", (legs[0]["sig"], u["mint"]))
                if len(r) == 1:
                    rr = r[0]["received_at"]; break
        if rr is None:
            skipped["no_exit_swap_row"] = skipped.get("no_exit_swap_row", 0) + 1; continue
        out.append({"pos": u["position_id"], "reason": reason, "trigger_why": why, "delay": rr - seconds[k]["s"]})
    d = np.array([o["delay"] for o in out])
    print("rail exits measured:", len(out), "skipped:", skipped)
    if len(d):
        print("trigger-second -> real exit swap (s): median %.2f mean %.2f p25 %.2f p75 %.2f p90 %.2f min %.2f max %.2f" % (np.median(d), d.mean(), np.percentile(d, 25), np.percentile(d, 75), np.percentile(d, 90), d.min(), d.max()))
        print("sorted:", " ".join("%.1f" % x for x in sorted(d)))


if __name__ == "__main__":
    main()

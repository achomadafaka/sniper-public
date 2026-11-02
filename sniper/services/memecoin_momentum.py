"""Pure momentum-rollover exit decision for Arm A (fast herd-pump ride).

No DB, no clock — the runner supplies price/held_s and persists the returned peak state.
Exit priority: hard stop (catastrophe) -> trailing (sharp reversal) -> rollover (stall)
-> time cap. A new-high sweep never triggers a PRICE exit (stop/trailing/rollover), but the
time cap can still fire (it is time-based, not price-based)."""
from __future__ import annotations


def momentum_decision(*, entry_price, peak_price, no_new_high_sweeps, price, held_s,
                      rollover_sweeps, trail_pct, stop_pct, max_hold_s):
    if price > peak_price:
        peak_price, no_new_high_sweeps = price, 0
    else:
        no_new_high_sweeps += 1
    reason = None
    if entry_price > 0 and price <= entry_price * (1 - stop_pct / 100.0):
        reason = "a_stop"
    elif peak_price > 0 and price <= peak_price * (1 - trail_pct / 100.0):
        reason = "a_trailing"
    elif no_new_high_sweeps >= rollover_sweeps:
        reason = "a_rollover"
    elif held_s > max_hold_s:
        reason = "a_timecap"
    return {"peak_price": peak_price, "no_new_high_sweeps": no_new_high_sweeps,
            "exit_reason": reason}

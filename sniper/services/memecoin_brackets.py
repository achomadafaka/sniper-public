"""SL/TP bracket evaluator for open memecoin positions (paper).

Cadence-checked (~15s from the runner). Fills the REMAINING open_qty at the
current price (gap-through honest — SL/TP are soft triggers, the fill can be
worse than +/-threshold on a gap), realizes via _realized_from_exits, tags exit_reason.
"""
from __future__ import annotations

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_copy_trader import _realized_from_exits
from sniper.execution.solana_quote import modeled_fill, quote_for


def control_exit_reason(pnl_pct, sl_pct, tp_pct):
    """Control arm's SL/TP trigger on the mid-price PnL — reused by the strategy sweep."""
    return ("stop_loss" if pnl_pct <= -sl_pct
            else "take_profit" if pnl_pct >= tp_pct else None)


def trail_d_exit_reason(*, entry_price, mid, peak_price, held_s,
                        activate_pct, trail_pct, stop_pct, max_hold_s):
    """trail_d arm — ride winners with a trailing stop that ARMS only after the position has peaked
    >= +activate_pct, then exits when price falls trail_pct OFF that peak. The thesis: capture the
    fat tail that alpha_b's fixed rails give back and control's fixed TP caps off, while still
    locking gains before a full round-trip. A wide catastrophe stop and a wide timecap backstop it;
    the trader-sell mirror (the proven profit engine) is handled in the copy engine — trail_d is in
    MIRROR_ARMS. Pure / testable (no I/O). Returns 'd_trail' | 'd_stop' | 'd_timecap' | None.

    Precedence: catastrophe stop (always live, even pre-activation) > trailing stop (post-activation)
    > wide timecap. Before activation only the stop and the mirror protect the position."""
    if not entry_price or entry_price <= 0 or not mid or mid <= 0:
        return None
    pnl = (mid / entry_price - 1.0) * 100.0
    if stop_pct and pnl <= -stop_pct:
        return "d_stop"
    peak = max(peak_price or entry_price, mid)
    peak_gain = (peak / entry_price - 1.0) * 100.0
    if peak_gain >= activate_pct:                       # trailing arms only after a real gain
        draw_from_peak = (peak - mid) / peak * 100.0
        if draw_from_peak >= trail_pct:
            return "d_trail"
    if max_hold_s and held_s >= max_hold_s:
        return "d_timecap"
    return None


def peak_lock_e_exit_reason(*, entry_price, mid, peak_price, no_new_high_sweeps, held_s,
                            activate_pct, stall_sweeps, stop_pct, max_hold_s):
    """Paper-only peak capture: activate on gain, harvest after a bounded stall."""
    if not entry_price or entry_price <= 0 or not mid or mid <= 0:
        return None
    pnl = (mid / entry_price - 1.0) * 100.0
    if stop_pct and pnl <= -stop_pct:
        return "e_stop"
    peak = max(peak_price or entry_price, mid)
    peak_gain = (peak / entry_price - 1.0) * 100.0
    if (peak_gain >= activate_pct and pnl > 0
            and int(no_new_high_sweeps or 0) >= int(stall_sweeps)):
        return "e_peak_stall"
    if max_hold_s and held_s >= max_hold_s:
        return "e_timecap"
    return None


def combo_f_exit_reason(*, entry_price, mid, peak_price, derisked, held_s,
                        trigger_pct, trail_pct, stop_pct, max_hold_s):
    """combo_f arm — the P1/P2 combination from the 2026-07-22 stop/derisk redesign study:
    sell HALF at the first +trigger_pct% crossing (the partial de-risk, fired via the exits
    engine's partial machinery), then trail the REMAINDER trail_pct% off the running peak.
    Counterfactual on the n=29 funded cohort: recovers +$85-92 of the -$167 bleed with bounded
    tail cost (each winner keeps (1-fraction) of its run) — scripts/research/
    memecoin_partial_derisk_counterfactual.py. Unlike trail_d the trail arms ONLY after the
    partial: pre-partial the position is protected by the catastrophe stop and the mirror.
    Pure / testable. Returns 'f_partial' | 'f_trail' | 'f_stop' | 'f_timecap' | None.

    Precedence: catastrophe stop > partial trigger (not yet derisked) > trail (derisked) > timecap."""
    if not entry_price or entry_price <= 0 or not mid or mid <= 0:
        return None
    pnl = (mid / entry_price - 1.0) * 100.0
    if stop_pct and pnl <= -stop_pct:
        return "f_stop"
    if not derisked:
        if pnl >= trigger_pct:
            return "f_partial"
    else:
        peak = max(peak_price or entry_price, mid)
        draw_from_peak = (peak - mid) / peak * 100.0
        if draw_from_peak >= trail_pct:
            return "f_trail"
    if max_hold_s and held_s >= max_hold_s:
        return "f_timecap"
    return None


def evaluate_brackets(conn, price_map, now_iso, sl_pct=None, tp_pct=None):
    """LEGACY / test-only: applies control SL/TP to list_open_positions = ALL strategies.
    Do NOT wire this into the multi-arm runner — it would wrongly SL/TP momentum_a and alpha_b.
    The live path is sweep_exits() (per-strategy dispatch); this is kept only for older tests."""
    if sl_pct is None or tp_pct is None:
        from sniper.settings.memecoin import MEMECOIN_STOP_LOSS_PCT, MEMECOIN_TAKE_PROFIT_PCT
        sl_pct = MEMECOIN_STOP_LOSS_PCT if sl_pct is None else sl_pct
        tp_pct = MEMECOIN_TAKE_PROFIT_PCT if tp_pct is None else tp_pct
    closed = []
    for pos in m.list_open_positions(conn):
        entry = pos.get("entry_price")
        if not entry or entry <= 0:
            continue
        st = quote_for(price_map, pos["mint"], pos.get("network_id"))
        mid = st and st.get("price")
        if not mid or mid <= 0:
            continue
        # trigger on the mid, but FILL with modeled slippage (honest exit, not raw mid)
        pnl_pct = (mid / entry - 1.0) * 100.0
        reason = ("stop_loss" if pnl_pct <= -sl_pct
                  else "take_profit" if pnl_pct >= tp_pct else None)
        if not reason:
            continue
        if not m.claim_close(conn, pos["id"]):    # manual/mirror already closed it
            continue
        qty = m.open_qty(conn, pos["id"])         # remaining after any partials
        if qty <= 0:
            continue
        price = modeled_fill(mid, mid * qty, st.get("liquidity"), "sell")
        m.insert_fill(conn, position_id=pos["id"], kind="bracket_exit", price=price,
                      qty=qty, notional=price * qty, fill_source="dexscreener",
                      fraction=1.0, created_at=now_iso)
        m.finalize_close(conn, pos["id"], now_iso,
                         _realized_from_exits(conn, pos["id"], entry), exit_reason=reason)
        closed.append({"position_id": pos["id"], "exit_reason": reason, "price": price})
    return closed

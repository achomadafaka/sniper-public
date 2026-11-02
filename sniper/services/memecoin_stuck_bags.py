"""Stuck-bag sweep — free a slot only when it actually buys us something.

A "stuck bag" is an open real position that is untradeable (DEAD recovery verdict: liquidity below the
routable floor) or already flagged stuck_bag by the runner. Its capital is effectively gone — selling it
into dead liquidity just realizes the loss via slippage, so there's no rush. The ONLY reason to cut one
is to free a concurrency SLOT for a live opportunity. So this surfaces stuck bags and raises a redeploy
recommendation ONLY when all slots are full AND a hot consensus setup is waiting (the user's own
"pair it with #3"). It never auto-sells: the cut is a one-click operator action, because silently
market-selling real money is not something to automate.
"""
from __future__ import annotations

import math
from datetime import datetime

from sniper.services.memecoin_position_intel import position_verdict, last_armed_buy_minutes


def stuck_bag_report(conn, *, quote_map=None, max_slots=7, hot_setups=None, now_iso=None):
    """Return {stuck, slots_used, slots_free, blocked_setup, recommendation, unpriced}. `quote_map`
    supplies live stats per position; `hot_setups` is the ranked consensus-setup list (from
    entry_signals) — a recommendation is only raised when slots are full and a hot setup exists.
    `unpriced` lists the open positions with no known liquidity: DEAD cannot be ruled in OR out for
    them, so an empty `stuck` next to a non-empty `unpriced` means "I cannot tell", not "all fine"."""
    from sniper.persistence import db_memecoin as m
    from sniper.settings import memecoin as cfg
    from sniper.util.clock import real_utc_now_iso
    now_iso = now_iso or real_utc_now_iso()
    now = datetime.fromisoformat(now_iso)
    max_quote_age_s = cfg.MEMECOIN_STUCK_MARK_MAX_AGE_S
    quote_map = quote_map or {}
    positions = conn.execute(
        "SELECT * FROM memecoin_positions WHERE execution_mode='real' AND is_open=1").fetchall()
    # The gates' own count (Gamma LOW on 2558f33da): automatic only, reservations in, long holds
    # out past MEMECOIN_SLOT_FREE_AFTER_S, so "all slots full" never fires while the gates have room.
    slots_used = m.automatic_funded_count(conn)
    stuck = []
    # DEAD fires only on a KNOWN positive-but-tiny liquidity, so a position we cannot price is
    # reported as UNPRICED rather than counted healthy. Without this an empty quote map renders as
    # "nothing is stuck" when the truth is "I cannot tell" -- the failure mode that hid a dead
    # quote cache for two days.
    unpriced = []
    for p in positions:
        key = m.accounting_mark_key(p["network_id"], p["mint"])
        q = quote_map.get(key) or quote_map.get((p["network_id"], p["mint"])) or {}
        # Solana's live mark tape is a mid; the position holds its own resolved whole-bag quote.
        position_exact = False
        try:
            price = float(p["last_executable_price"])
            quoted_at = datetime.fromisoformat(p["last_executable_at"])
            age_s = (now - quoted_at).total_seconds()
            position_exact = (quoted_at.utcoffset() is not None and math.isfinite(price)
                              and price > 0 and math.isfinite(max_quote_age_s)
                              and 0 <= age_s <= max_quote_age_s)
        except (TypeError, ValueError, OverflowError):
            pass
        # A fresh exact exit-route quote (`exact_route_at`) proves the bag sells even with depth unknown.
        if not ((q.get("liquidity") or 0) > 0 or q.get("exact_route_at") or position_exact):
            unpriced.append(p["id"])
        lb = last_armed_buy_minutes(conn, p["network_id"], p["mint"], now_iso=now_iso)
        v = position_verdict(dict(p), q, last_armed_buy_min=lb, now_iso=now_iso)
        if v["verdict"] == "DEAD" or p["stuck_bag"]:
            stuck.append({"id": p["id"], "ticker": p["ticker"], "network_id": p["network_id"],
                          "mint": p["mint"], "recovery_score": v["recovery_score"],
                          "pnl_pct": v["pnl_pct"], "liquidity": q.get("liquidity"),
                          "runner_stuck": bool(p["stuck_bag"]),
                          "reason": v["suggested"]})
    slots_free = max(0, int(max_slots) - slots_used)
    hot = [s for s in (hot_setups or []) if s.get("hot")]
    blocked_setup = hot[0] if (slots_free == 0 and hot and stuck) else None
    if blocked_setup:
        worst = min(stuck, key=lambda x: x["recovery_score"])
        recommendation = (f"All {max_slots} slots full and a hot setup is waiting "
                          f"({blocked_setup.get('ticker') or blocked_setup['mint'][:6]}, "
                          f"×{blocked_setup['armed']} armed) — free a slot by cutting the deadest bag "
                          f"(#{worst['id']} {worst['ticker'] or worst['mint'][:6]}, "
                          f"score {worst['recovery_score']}).")
    else:
        recommendation = None
    return {"stuck": stuck, "slots_used": slots_used, "slots_free": slots_free,
            "blocked_setup": blocked_setup, "recommendation": recommendation,
            "unpriced": unpriced}

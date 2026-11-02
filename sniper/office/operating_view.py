"""Head/Judge operating view (Gamma prereg 5273d4f7c section 4): own book's open positions, typed marked equity with mark
ages, today's realized PnL against the daily-loss halt, and a snapshot id every decision records. Never grade rows,
closed per-coin PnL, win rates, intervals, NET after C, the look statistic or other epochs. One bounded read per turn."""
import hashlib

from . import ledger as L, store as S

MARK_MAX_AGE_S = 60.


def view(conn, *, book_id, now, sol_usd, sol_at, daily_loss_usd, max_concurrent, entry_call=lambda order_id: None):
    """entry_call(order_id) -> {'public_reason','disproof'} of the committed entry call (read-only), or None = unknown."""
    rows = conn.execute("SELECT order_id,mint,pool,state,filled_at,spend_usd FROM orders INDEXED BY orders_book_state "
                        "WHERE book_id=? AND state IN ('pending','open') ORDER BY requested_at,order_id LIMIT ?",
                        (book_id, max_concurrent + 1)).fetchall()
    positions = []
    for r in rows[:max_concurrent]:
        mark = conn.execute("SELECT at,lamports FROM marks WHERE order_id=? AND at<=? ORDER BY at DESC LIMIT 1",
                            (r["order_id"], now)).fetchone()
        priced = mark is not None and mark["lamports"] is not None and now - mark["at"] <= MARK_MAX_AGE_S   # agrees with equity
        positions.append({"order_id": r["order_id"], "mint": r["mint"], "pool": r["pool"], "state": r["state"],
                          "filled_at": r["filled_at"], "spend_usd": r["spend_usd"],
                          "mark_at": mark["at"] if mark else None, "mark_age_s": now - mark["at"] if mark else None,
                          "mark_lamports": mark["lamports"] if priced else None, "priced": priced,
                          "entry_call": entry_call(r["order_id"])})
    eq = L.equity(conn, now=now, mark_max_age_s=MARK_MAX_AGE_S, sol_usd=sol_usd, sol_at=sol_at, book_id=book_id)
    known = eq.state == "known" and eq.known_liq_lower is not None
    realized = L.daily_realized_usd(conn, book_id=book_id, now=now)
    out = {"book_id": book_id, "as_of": now, "positions": positions, "positions_over_cap": len(rows) > max_concurrent,
           "equity": {"state": eq.state, "usd": eq.cash + eq.known_liq_lower if known else None,
                      "unpriced_count": eq.unpriced_count, "mark_max_age_s": MARK_MAX_AGE_S},
           "daily_realized_usd": realized, "daily_loss_usd": daily_loss_usd, "daily_loss_halted": realized <= -daily_loss_usd}
    out["equity_snapshot_id"] = hashlib.sha256(S.canonical_json(out)).hexdigest()
    return out

"""Mark open memecoin positions with live DexScreener prices -> unrealized PnL."""
from __future__ import annotations

from datetime import datetime, timezone

from sniper.persistence import db_memecoin as m
from sniper.execution.solana_quote import dexscreener_stats, quote_for


def _parse(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def stored_position_marks(conn, positions, now_iso, *, stale_s=120.0):
    """Mark paper positions from runner-persisted quotes with one fill rollup query."""
    positions = list(positions)
    if not positions:
        return []
    ids = [int(pos["id"]) for pos in positions]
    entry_marks = ",".join("?" for _ in m.ENTRY_KINDS)
    exit_marks = ",".join("?" for _ in m.EXIT_KINDS)
    rows = conn.execute(
        f"""
        SELECT position_id,
               SUM(CASE WHEN kind IN ({entry_marks}) THEN qty ELSE 0 END) AS entry_qty,
               SUM(CASE WHEN kind IN ({exit_marks}) THEN qty ELSE 0 END) AS exit_qty,
               SUM(CASE WHEN kind IN ({exit_marks}) THEN price * qty ELSE 0 END) AS exit_proceeds
        FROM memecoin_fills
        WHERE position_id IN ({','.join('?' for _ in ids)})
        GROUP BY position_id
        """,
        (*m.ENTRY_KINDS, *m.EXIT_KINDS, *m.EXIT_KINDS, *ids),
    ).fetchall()
    rollups = {int(row["position_id"]): dict(row) for row in rows}
    now = _parse(now_iso)
    output = []
    for pos in positions:
        entry = float(pos.get("entry_price") or 0.0)
        if entry <= 0:
            continue
        rollup = rollups.get(int(pos["id"]), {})
        entered = float(rollup.get("entry_qty") or 0.0)
        if entered <= 0:
            entered = float(pos.get("entry_notional") or 0.0) / entry
        exited = float(rollup.get("exit_qty") or 0.0)
        open_qty = max(0.0, entered - exited)
        booked = float(rollup.get("exit_proceeds") or 0.0) - entry * exited
        price = pos.get("last_quote_price")
        price = float(price) if price is not None and float(price) > 0 else None
        quote_at = _parse(pos.get("last_quote_ts"))
        age = max(0.0, (now - quote_at).total_seconds()) if now and quote_at else None
        unrealized = (price - entry) * open_qty if price is not None else None
        output.append({
            "position_id": pos["id"], "mint": pos["mint"],
            "current_price": price, "open_qty": open_qty,
            "booked_pnl": booked,
            "unrealized_pnl": unrealized,
            "full_book_pnl": booked + unrealized if unrealized is not None else None,
            "liquidity": None, "volume_24h": None,
            "source": "stored_runner_quote" if price is not None else None,
            "quote_ts": pos.get("last_quote_ts"), "quote_age_s": age,
            "stale": (age > stale_s) if age is not None else None,
        })
    return output


def mark_positions(conn, stats_fn=dexscreener_stats, price_map=None):
    out = []
    for pos in m.list_open_positions(conn):
        if not pos.get("entry_price") or pos["entry_price"] <= 0:
            continue
        # share one batched fetch with the bracket evaluator when price_map is given
        # (normalize the mint so EVM checksum-case addresses match the batch keys)
        if price_map is not None:
            st = quote_for(price_map, pos["mint"], pos.get("network_id"))
        else:
            try:
                st = stats_fn(pos["mint"], network_id=pos.get("network_id"))
            except TypeError as exc:
                if "network_id" not in str(exc):
                    raise
                st = stats_fn(pos["mint"])
        if not st or not st.get("price"):
            # flaky/intermittently-indexed coin: this load's batch missed it, but the bracket
            # sweep may hold a recent stored quote — surface THAT (tagged stale + aged) instead
            # of a blank row. Truly never-quoted positions still render no mark.
            lq, lq_ts = pos.get("last_quote_price"), pos.get("last_quote_ts")
            if not lq or lq <= 0:
                continue
            qty = m.open_qty(conn, pos["id"])
            out.append({
                "position_id": pos["id"], "mint": pos["mint"],
                "current_price": lq,
                "unrealized_pnl": (lq - pos["entry_price"]) * qty,
                "liquidity": None, "volume_24h": None,
                "stale": True, "quote_ts": lq_ts,
            })
            continue
        qty = m.open_qty(conn, pos["id"])  # remaining after any partial sells
        out.append({
            "position_id": pos["id"], "mint": pos["mint"],
            "current_price": st["price"],
            "unrealized_pnl": (st["price"] - pos["entry_price"]) * qty,
            "liquidity": st.get("liquidity"), "volume_24h": st.get("volume_24h"),
        })
    return out

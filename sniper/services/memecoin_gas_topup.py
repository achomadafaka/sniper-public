"""Buy a small, capped amount of SOL with USDC when the funded wallet runs low on gas.

WHY (operator, 2026-08-26). On 2026-08-25 the wallet fell to 0.002196 SOL against a 0.002800
requirement and every funded entry was refused "insufficient SOL for entry + full-book exit
reserve" from 18:35 until the operator noticed the next day and topped up by hand. The tape looked
quiet; it was an outage. Nothing in the system could buy its own gas.

The rent sweep (memecoin_rent_sweep) returns the SOL that closed positions leave locked in token
accounts, which is the larger number and the slower drain. This covers the other end: true burn is
only transaction fees, about 0.00002 SOL a position, but a wallet that is never refilled still
crosses the gate eventually, and crossing it stops trading silently.

THE SAFETY PROPERTY IS THE CAP, NOT THE TRIGGER. An automated swapper holding the funded keypair
can, with one bad comparison, convert the entire trading bankroll into gas. So:

  - A top-up is a FIXED dollar amount, never "however much reaches the target". There is no price
    arithmetic to get wrong and no target-seeking loop to overshoot.
  - A DAILY CAP bounds the total, and it is read from a durable ledger rather than memory. A cap
    held in a variable resets on restart, which is exactly when a misbehaving loop restarts.
  - An ambiguous submit COUNTS against the cap. Money that may have been spent is spent, for the
    purposes of a budget.
  - USDC already encumbered by unresolved reservations is not spendable, read through the same
    `pending_totals` the runner's own sizing uses.
  - An unreadable balance is never a low balance. Unknown refuses.

Like the rent sweep this runs inside the runner and takes the `sol-<pubkey>` execution lock
non-blocking, because every real execution on this wallet serializes behind it and a chore must
never make a live exit queue behind a swap.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SLIPPAGE_BPS = 50

# Per-day dedupe for the cap page. Process-local on purpose: a restart re-pages once, which is the
# right side to err on for an alert that means "an operator should look".
_CAP_PAGED = {"day": None}


def _alert(text):
    """Telegram, fail-soft. An alert must never be able to break a money path."""
    try:
        from sniper.services.memecoin_telegram import send_telegram
        send_telegram(text)
    except Exception:
        logger.exception("gas top-up alert failed to send")


def _seams():
    from sniper.execution.solana_real_seams import (
        _quote_with_fallback, jupiter_swap_tx, sign_swap_tx, rpc_send, rpc_confirm)
    return _quote_with_fallback, jupiter_swap_tx, sign_swap_tx, rpc_send, rpc_confirm


def free_usdc(conn, usdc_balance):
    """USDC not already encumbered by an unresolved Solana reservation.

    A read failure returns None (unknown), and unknown must refuse: spending the bankroll out from
    under a mid-flight entry is the failure this guard exists for, so it cannot fall back to
    "assume nothing is reserved" the way a human-run script reasonably can.
    """
    try:
        from sniper.persistence import db_memecoin as m
        reserved, _native = m.pending_funding_totals(conn, "solana")
    except Exception:
        logger.exception("gas top-up could not read reserved USDC; refusing this pass")
        return None
    return float(usdc_balance) - float(reserved or 0.0)


DROP_AFTER_S = 1200.0   # same 20-minute proxy the entry reservations use; ~15x a blockhash life


def sol_buffer_allocation(conn, *, sol_balance, sol_usd, cap_usd):
    """Off-path allocation for maintenance and display. Entry admission remains authoritative.

    Reserve the same full-book gas/rent as entry admission plus the largest native builder's
    account overhead. Pending SOL is unavailable until its exact transaction resolves.
    Unknown balances, prices or ledger reads raise; they never become an empty buffer/book.
    """
    import math
    from sniper.persistence import db_memecoin as m
    from sniper.services.memecoin_risk import required_entry_sol
    from sniper.services.memecoin_slot_lane import NATIVE_ATA_RENT_RESERVE_LAMPORTS
    from sniper.settings import memecoin as cfg

    values = (sol_balance, sol_usd, cap_usd, cfg.MEMECOIN_SOL_EXEC_BUFFER_HEADROOM_USD)
    if any(v is None or not math.isfinite(float(v)) for v in values):
        raise ValueError("SOL buffer balance or price unavailable")
    sol, price, cap, cushion = map(float, values)
    if sol < 0 or price <= 0 or cap < 0 or cushion < 0:
        raise ValueError("invalid SOL buffer balance or policy")
    snap = m.real_open_snapshot(conn, network="solana")
    pending_count, _ = m.pending_totals(conn, network="solana")
    _, pending_atomic = m.pending_funding_totals(conn, "solana")
    gas = required_entry_sol(snap["count"] + pending_count, cfg.MEMECOIN_GAS_ENTRY_SOL,
                             cfg.MEMECOIN_GAS_EXIT_SOL, cfg.MEMECOIN_GAS_RETRY_FACTOR)
    reserve = gas + (2 * NATIVE_ATA_RENT_RESERVE_LAMPORTS / 1e9 if cap > 0 else 0)
    if not math.isfinite(reserve) or reserve < 0 or pending_atomic < 0:
        raise ValueError("invalid SOL reserve or pending funding")
    available = max(0.0, sol - pending_atomic / 1e9 - reserve) * price
    funds = min(cap, available)
    headroom = min(cushion, max(0.0, available - cap)) if cap > 0 else 0.0
    return {"reserve_sol": reserve, "reserve_usd": min(sol, reserve) * price,
            "pending_sol_lamports": pending_atomic, "available_usd": available,
            "funds_usd": funds, "headroom_usd": headroom,
            "idle_usd": max(0.0, available - funds - headroom),
            "target_native_usd": cap + reserve * price + (cushion if cap > 0 else 0)}


def _submitted_age_s(conn, sig, now_iso):
    """Seconds since the write-ahead row; -1 when unreadable (never old enough to drop)."""
    from datetime import datetime
    row = conn.execute("SELECT MIN(created_at) FROM memecoin_gas_topups "
                       "WHERE tx_sig=? AND purpose='buffer' AND status='submitted'",
                       (sig,)).fetchone()
    try:
        parse = lambda v: datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return (parse(now_iso) - parse(row[0])).total_seconds()
    except (TypeError, ValueError):
        return -1.0


def buffer_topup_once(conn, *, sol_balance, usdc_balance, sol_usd, keypair, pubkey, rpc_url,
                      now_iso, cap_usd, topup_usd, daily_cap_usd, min_usdc_remaining_usd,
                      lock_dir=None, sol_balance_fn=None, usdc_balance_fn=None,
                      quote_fn=None, build_fn=None, sign_fn=None, send_fn=None, confirm_fn=None):
    """One bounded USDC->SOL maintenance pass; quote/build/send/confirm stay off the wallet lock."""
    import math
    from sniper.persistence import db_memecoin as m
    from sniper.execution.exec_lock import wallet_exec_lock
    tally = {"state": None, "spent_usd": 0.0, "sig": None}
    q_fn, b_fn, s_fn, send, conf = _seams()
    quote_fn, build_fn = quote_fn or q_fn, build_fn or b_fn
    sign_fn = sign_fn or s_fn
    send_fn = send_fn or (lambda payload, rpc: send(payload, rpc, classify_preflight=True))
    confirm_fn = confirm_fn or conf
    try:
        old_sig = m.unresolved_gas_topup(conn, purpose="buffer")
        if old_sig:
            status = confirm_fn(old_sig, rpc_url)
            if status in ("confirmed", "failed"):
                old = conn.execute("SELECT day, sol_before, usdc_before, usdc_spent "
                                   "FROM memecoin_gas_topups "
                                   "WHERE tx_sig=? AND purpose='buffer' AND status='submitted' "
                                   "ORDER BY id DESC LIMIT 1", (old_sig,)).fetchone()
                if status == "confirmed" and (
                        old[2] is None or usdc_balance_fn is None or
                        (seen_usdc := usdc_balance_fn()) is None or
                        float(seen_usdc) > float(old[2]) - float(old[3]) + 1e-6):
                    tally.update(state="confirmed_balance_pending", sig=old_sig)
                    return tally
                m.record_gas_topup(conn, now_iso=now_iso, day=old[0], usdc_spent=0.0,
                                   sol_received=None, sol_before=old[1], tx_sig=old_sig,
                                   status=status, purpose="buffer", usdc_before=old[2])
                tally.update(state="prior_" + status, sig=old_sig)
            elif status == "not_found" and _submitted_age_s(conn, old_sig, now_iso) >= DROP_AFTER_S:
                # A signature is only valid for ~150 blocks (60-90 s) after its blockhash, so a
                # not_found read (history search included) far past that is a drop, not a pending
                # send. Live 2026-09-28 row 169: without this the buffer stayed blocked for 55 h
                # and its write-ahead USDC stayed encumbered in funded sizing. `dropped` is
                # terminal but, unlike `failed`, keeps its spend in the daily cap.
                old = conn.execute("SELECT day, sol_before, usdc_before "
                                   "FROM memecoin_gas_topups "
                                   "WHERE tx_sig=? AND purpose='buffer' AND status='submitted' "
                                   "ORDER BY id DESC LIMIT 1", (old_sig,)).fetchone()
                m.record_gas_topup(conn, now_iso=now_iso, day=old[0], usdc_spent=0.0,
                                   sol_received=None, sol_before=old[1], tx_sig=old_sig,
                                   status="dropped", purpose="buffer", usdc_before=old[2])
                logger.warning("SOL execution buffer: top-up %s not found %.0fs after submit; "
                               "recorded dropped", old_sig, _submitted_age_s(conn, old_sig, now_iso))
                tally.update(state="prior_dropped", sig=old_sig)
            else:
                tally.update(state="unresolved_topup", sig=old_sig)
            return tally
        values = (sol_balance, usdc_balance, sol_usd, cap_usd, topup_usd,
                  daily_cap_usd, min_usdc_remaining_usd)
        if any(v is None or not math.isfinite(float(v)) for v in values):
            tally["state"] = "balance_or_price_unavailable"
            return tally
        sol, usd, price, cap, amount, daily, keep = map(float, values)
        if min(sol, usd, daily) < 0 or min(price, cap, amount, keep) <= 0:
            tally["state"] = "invalid_policy"
            return tally
        try:
            allocation = sol_buffer_allocation(conn, sol_balance=sol, sol_usd=price, cap_usd=cap)
        except Exception:
            logger.exception("SOL buffer allocation unavailable; refusing refill")
            tally["state"] = "buffer_state_unavailable"
            return tally
        tally["native_usd"] = allocation["available_usd"]      # for the BufferWatch heartbeat/page
        if allocation["pending_sol_lamports"] > 0:
            tally["state"] = "pending_native_entry"
            return tally
        if allocation["available_usd"] >= cap:
            tally["state"] = "buffer_cap"
            return tally
        # Ceiling to cents restores the reserve rather than leaving a fractional-cent deficit.
        # The cushion prevents tiny fee drains from causing another swap on every maintenance pass.
        amount = min(amount, math.ceil(max(0.0, allocation["target_native_usd"] - sol * price)
                                       * 100) / 100)
        spendable = free_usdc(conn, usd)
        tally["free_usdc"] = spendable
        if spendable is None or spendable - amount < keep:
            tally["state"] = "insufficient_free_usdc"
            return tally
        day = str(now_iso)[:10]
        if daily > 0 and m.gas_topup_spent_today(conn, day, purpose="buffer") + amount > daily + 1e-9:
            tally["state"] = "daily_cap"
            return tally
        # One off-path Jupiter read/build. The wallet lock is taken only after bytes are ready.
        quote = quote_fn(USDC_MINT, SOL_MINT, int(round(amount * 1e6)), SLIPPAGE_BPS, None)
        if not isinstance(quote, dict) or int(quote.get("outAmount") or 0) <= 0:
            tally["state"] = "no_quote"
            return tally
        unsigned = build_fn(quote, pubkey)
        if not unsigned:
            tally["state"] = "build_failed"
            return tally
        # Re-read money before lock acquisition. Any intervening funded fill changes the ledger
        # watermark; unresolved reservations and top-ups are counted again under the lock.
        fill_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM memecoin_fills").fetchone()[0]
        fresh_sol = sol_balance_fn() if sol_balance_fn else None
        fresh_usdc = usdc_balance_fn() if usdc_balance_fn else None
        if fresh_sol is None or fresh_usdc is None:
            tally["state"] = "balance_unavailable"
            return tally
        with wallet_exec_lock("sol-%s" % pubkey, blocking=False, lock_dir=lock_dir) as held:
            if not held:
                tally["state"] = "wallet_busy"
                return tally
            if (conn.execute("SELECT COALESCE(MAX(id),0) FROM memecoin_fills").fetchone()[0]
                    != fill_id):
                tally["state"] = "book_changed"
                return tally
            if m.unresolved_gas_topup(conn, purpose="buffer") or m.unresolved_withdraws(conn, "solana"):
                tally["state"] = "unresolved_transfer"
                return tally
            spendable = free_usdc(conn, fresh_usdc)
            try:
                fresh_allocation = sol_buffer_allocation(
                    conn, sol_balance=fresh_sol, sol_usd=price, cap_usd=cap)
            except Exception:
                logger.exception("fresh SOL buffer allocation unavailable; refusing refill")
                tally["state"] = "buffer_state_unavailable"
                return tally
            if (spendable is None or spendable - amount < keep
                    or fresh_allocation["pending_sol_lamports"] > 0
                    or fresh_allocation["available_usd"] >= cap
                    or float(fresh_sol) * price + amount
                    > fresh_allocation["target_native_usd"] + .01 + 1e-9):
                tally["state"] = "balance_changed"
                return tally
            signed = sign_fn(unsigned, keypair)
            if not signed:
                tally["state"] = "sign_failed"
                return tally
            signed_b64, sig = signed[0], signed[1]
            m.record_gas_topup(conn, now_iso=now_iso, day=day, usdc_spent=amount,
                               usdc_before=fresh_usdc, sol_received=None,
                               sol_before=fresh_sol, tx_sig=sig,
                               status="submitted", purpose="buffer")
        tally.update(spent_usd=amount, sig=sig)
        # The durable write-ahead spend protects the bankroll while the RPC call is in flight.
        sent = send_fn(signed_b64, rpc_url)
        status = ("failed" if sent == "explicit_rejected" else
                  confirm_fn(sig, rpc_url) if sent is not None else "unknown")
        tally["state"] = status
        if status == "confirmed" and (
                (seen_usdc := usdc_balance_fn()) is None or
                float(seen_usdc) > float(fresh_usdc) - amount + 1e-6):
            tally["state"] = "confirmed_balance_pending"
            return tally
        if status in ("confirmed", "failed"):
            m.record_gas_topup(conn, now_iso=now_iso, day=day, usdc_spent=0.0,
                               usdc_before=fresh_usdc, sol_received=None,
                               sol_before=fresh_sol, tx_sig=sig,
                               status=status, purpose="buffer")
        return tally
    except Exception:
        logger.exception("SOL execution buffer top-up failed")
        tally["state"] = "error"
        return tally


def gas_topup_once(conn, *, sol_balance, usdc_balance, keypair, pubkey, rpc_url, now_iso,
                   low_water_sol, topup_usd, daily_cap_usd, lock_dir=None,
                   sol_balance_fn=None, reclaim_fn=None, quote_fn=None, build_fn=None,
                   sign_fn=None, send_fn=None, confirm_fn=None):
    """One top-up pass. Returns a tally; never raises.

    `sol_balance_fn` re-reads the balance inside the lock. Gas can arrive between the outer read
    and the signature — an exit returns rent, or the rent sweep lands — and buying gas we no longer
    need spends bankroll for nothing.
    """
    from sniper.persistence import db_memecoin as m
    tally = {"state": None, "spent_usd": 0.0, "sol_before": sol_balance, "sig": None,
             "reclaimed": 0}
    try:
        if sol_balance is None:
            tally["state"] = "sol_unreadable"      # unknown is not low
            return tally
        if float(sol_balance) >= float(low_water_sol):
            tally["state"] = "above_low_water"
            return tally
        if usdc_balance is None:
            tally["state"] = "usdc_unreadable"
            return tally

        spendable = free_usdc(conn, usdc_balance)
        if spendable is None:
            tally["state"] = "reserved_unreadable"
            return tally
        amount = float(topup_usd)
        if spendable < amount:
            tally["state"] = "insufficient_free_usdc"
            return tally

        day = str(now_iso)[:10]
        spent = m.gas_topup_spent_today(conn, day)
        if (float(daily_cap_usd) > 0
                and spent + amount > float(daily_cap_usd) + 1e-9):
            # Loud: hitting the cap means either an unusual burn rate or a loop misfiring, and both
            # want an operator to look. It is not a routine skip.
            logger.warning("gas top-up DAILY CAP reached day=%s spent=%.2f cap=%.2f request=%.2f",
                           day, spent, float(daily_cap_usd), amount)
            # Page ONCE per day, not per pass. At a 300s period an un-deduped alert fires ~288
            # times before midnight, which is how an alert gets muted and then missed.
            if _CAP_PAGED.get("day") != day:
                _CAP_PAGED["day"] = day
                _alert("\u26fd memecoin gas top-up CAPPED | spent $%.2f of $%.2f today, gas is "
                       "%.6f SOL and still under the %.4f trigger. Top-ups are stopped until UTC "
                       "midnight." % (spent, float(daily_cap_usd), float(sol_balance),
                                      float(low_water_sol)))
            tally["state"] = "daily_cap"
            return tally

        from sniper.execution.exec_lock import wallet_exec_lock
        with wallet_exec_lock("sol-%s" % pubkey, blocking=False, lock_dir=lock_dir) as held:
            if not held:
                tally["state"] = "wallet_busy"
                return tally
            if m.unresolved_withdraws(conn, "solana"):
                tally["state"] = "withdrawal_unresolved"
                return tally
            # Gas may have arrived while we were deciding.
            if sol_balance_fn is not None:
                fresh = sol_balance_fn()
                tally["sol_before"] = fresh
                if fresh is None:
                    tally["state"] = "sol_unreadable"
                    return tally
                if float(fresh) >= float(low_water_sol):
                    tally["state"] = "recovered_before_swap"
                    return tally

            # RECLAIM BEFORE BUYING. Operator, 2026-08-26, after spotting that a $3 top-up fired
            # while 0.0122 SOL sat unclaimed in empty token accounts.
            #
            # The two thresholds were set in separate commits and never checked against each other:
            # the sweep waits for 8 accounts (0.0163 SOL parked) and the buy fires under 0.0200 SOL,
            # 0.0037 apart. So the wallet can fall under the BUY trigger while holding almost as
            # much in rent it already owns. Each number is defensible alone; as a pair they mean
            # "buy gas you already have".
            #
            # MIN_ACCOUNTS IS BYPASSED HERE ON PURPOSE. That floor exists so a routine pass does not
            # spend a signature to reclaim one deposit. When the alternative is spending BANKROLL,
            # one account's rent is worth a signature.
            #
            # A reclaim that fails falls through to the buy. A failed cleanup must never be able to
            # starve the runner of gas — that would turn a housekeeping fault into an outage.
            if reclaim_fn is not None:
                try:
                    swept = reclaim_fn()
                    tally["reclaimed"] = swept
                    if swept and sol_balance_fn is not None:
                        after = sol_balance_fn()
                        if after is not None:
                            tally["sol_before"] = after
                            if float(after) >= float(low_water_sol):
                                # We already owned the gas. Spend nothing.
                                tally["state"] = "recovered_by_reclaim"
                                logger.warning(
                                    "gas top-up: reclaimed rent covered the shortfall (%.6f SOL); "
                                    "no USDC spent", float(after))
                                return tally
                except Exception:
                    logger.exception("gas top-up: reclaim failed; continuing to the buy")

            q_fn, b_fn, s_fn, send, conf = _seams()
            quote_fn = quote_fn or q_fn
            build_fn = build_fn or b_fn
            sign_fn = sign_fn or s_fn
            send_fn = send_fn or send
            confirm_fn = confirm_fn or conf

            quote = quote_fn(USDC_MINT, SOL_MINT, int(round(amount * 1e6)), SLIPPAGE_BPS, None)
            if not quote:
                tally["state"] = "no_quote"
                return tally
            tx_b64 = build_fn(quote, pubkey)
            if not tx_b64:
                tally["state"] = "build_failed"
                return tally
            signed = sign_fn(tx_b64, keypair)
            if not signed:
                tally["state"] = "sign_failed"
                return tally
            signed_b64, sig = signed[0], signed[1]

            # WRITE-AHEAD. The row goes in BEFORE the broadcast, so a crash between send and record
            # cannot hand the next pass a fresh budget. Status is corrected after confirmation.
            m.record_gas_topup(conn, now_iso=now_iso, day=day, usdc_spent=amount,
                               sol_received=None, sol_before=tally["sol_before"],
                               tx_sig=sig, status="submitted")
            tally["spent_usd"] = amount
            tally["sig"] = sig

            sent = send_fn(signed_b64, rpc_url)
            status = confirm_fn(sig, rpc_url) if sent is not None else "not_sent"
            tally["state"] = status
            if status == "confirmed":
                out_sol = int(quote.get("outAmount") or 0) / 1e9
                m.record_gas_topup(conn, now_iso=now_iso, day=day, usdc_spent=0.0,
                                   sol_received=out_sol, sol_before=tally["sol_before"],
                                   tx_sig=sig, status="confirmed")
                tally["sol_received"] = out_sol
                logger.warning("gas top-up CONFIRMED %.2f USDC -> %.6f SOL sig=%s",
                               amount, out_sol, sig)
                # Money moved by itself. The operator learns that from a notification, not from a
                # ledger query they did not know to run (the funded-leg-DARK class).
                before = tally["sol_before"]
                _alert("\u26fd memecoin gas top-up | spent $%.2f USDC for %.6f SOL\n"
                       "gas %.6f -> %.6f SOL\n%s"
                       % (amount, out_sol, float(before or 0.0),
                          float(before or 0.0) + out_sol, sig))
            else:
                # The write-ahead row stands and still counts against the cap. An ambiguous submit
                # may have spent the money; a budget that forgives what it cannot confirm is not a
                # budget.
                logger.warning("gas top-up UNCONFIRMED status=%s sig=%s — %.2f USDC counted "
                               "against today's cap", status, sig, amount)
            return tally
    except Exception:
        logger.exception("gas top-up pass failed")
        tally["state"] = "error"
        return tally

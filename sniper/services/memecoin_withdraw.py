"""Operator withdrawals from the dashboard (2026-09-13, operator: "add it to the dash, withdraw to
that wallet ... I can choose the amount").

The web queues an AMOUNT. The destination is the one configured address,
MEMECOIN_WITHDRAW_SOLANA_ADDRESS, read again here at send time, so the dashboard cannot direct
money anywhere else. The runner sends under the `sol-<pubkey>` lock, taken non-blocking like the
other chores, so a withdrawal never lands inside an entry or exit whose fill is priced from the
wallet's balance change. USDC reserved for an unresolved entry is never spent.

A signed transfer stays 'submitted' until the chain answers: confirmed, failed on chain, or past
its last valid block height with a pinned read that does not find it.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)


def _alert(text):
    try:
        from sniper.services.memecoin_telegram import enqueue_telegram
        enqueue_telegram(text, kind="operational:WITHDRAW")
    except Exception:
        logger.exception("withdrawal alert failed to send")


def _short(address):
    return "%s…%s" % (address[:4], address[-4:])


def withdraw_once(conn, *, keypair, pubkey, rpc_url, now_iso, to_address, usdc_fn,
                  lock_dir=None, call_fn=None, send_fn=None, confirm_fn=None, alert_fn=None,
                  send_factory_fn=None):
    """Claim one queued withdrawal and send it. Returns a tally; never raises."""
    from sniper.persistence import db_memecoin as m
    from sniper.execution.solana_usdc_transfer import (
        USDC_DECIMALS, build_signed_transfer, valid_wallet)
    alert_fn = alert_fn or _alert
    tally = {"state": None, "intent_id": None, "sig": None}
    try:
        from sniper.execution.exec_lock import wallet_exec_lock
        with wallet_exec_lock("sol-%s" % pubkey, blocking=False, lock_dir=lock_dir) as held:
            if not held:
                tally["state"] = "wallet_busy"
                return tally
            m.recover_withdraw_claims(conn, "solana", now_iso=now_iso)
            if m.unresolved_withdraws(conn, "solana"):
                tally["state"] = "unresolved"
                return tally
            from sniper.services.memecoin_other_holdings import solana_book_count
            book = solana_book_count(conn)
            if book is None or book > 0:
                tally["state"] = "book_unreadable" if book is None else "book_busy"
                return tally
            intent = m.claim_withdraw(conn, "solana", now_iso=now_iso)
            if intent is None:
                tally["state"] = "idle"
                return tally
            iid = intent["intent_id"]
            tally["intent_id"] = iid

            def fail(reason):
                m.resolve_withdraw(conn, iid, "failed", now_iso=now_iso, reason=reason)
                tally.update(state="failed", reason=reason)
                return tally

            if not to_address or intent["to_address"] != to_address or not valid_wallet(to_address):
                return fail("the withdrawal address changed or is not a wallet; queue it again")
            amount = float(intent["amount_usdc"])
            usdc = usdc_fn()
            if usdc is None:
                return fail("could not read the USDC balance")
            try:
                _n, reserved = m.pending_totals(conn, "solana")
            except Exception:
                return fail("could not read the reserved USDC")
            free = float(usdc) - float(reserved or 0.0)
            if amount > free + 1e-9:
                return fail("$%.2f is more than the free balance $%.2f (reserved $%.2f)"
                            % (amount, free, float(reserved or 0.0)))

            alert_fn("withdrawal claimed | $%.2f USDC to %s | request %s"
                     % (amount, _short(to_address), iid))

            if call_fn is None:
                from sniper.services.memecoin_rent_sweep import rpc_call as call_fn
            if (send_fn is None and send_factory_fn is None) or confirm_fn is None:
                from sniper.execution.solana_real_seams import rpc_confirm, rpc_send
                if send_fn is None and send_factory_fn is None:
                    send_fn = lambda signed, url: rpc_send(
                        signed, url, preflight_commitment="confirmed", classify_preflight=True)
                confirm_fn = confirm_fn or (lambda sig, url: rpc_confirm(sig, url, max_attempts=1))
            try:
                signed_b64, sig, last_valid, created = build_signed_transfer(
                    keypair, pubkey, to_address, int(round(amount * 10 ** USDC_DECIMALS)),
                    rpc_url=rpc_url, call_fn=call_fn)
            except Exception as exc:
                return fail("could not build the transfer: %s" % exc)

            # WRITE-AHEAD: the sig and its expiry height are on the intent before the broadcast.
            m.resolve_withdraw(conn, iid, "submitted", now_iso=now_iso, sig=sig,
                               last_valid_height=last_valid)
            tally["sig"] = sig
            if send_factory_fn is not None:
                sender = send_factory_fn(conn, iid)
                sent = sender(signed_b64, validity={"last_valid_height": last_valid})
            else:
                sent = send_fn(signed_b64, rpc_url)
            if sent is False:
                return fail("the transfer was rejected before broadcast")
            if sent == "explicit_rejected":
                from sniper.execution.solana_real_seams import take_send_error
                return fail("the transfer was rejected before broadcast: %s"
                            % (take_send_error() or "simulation rejected"))
            # An unavailable send response can still land; retain its write-ahead identity.
            status = confirm_fn(sig, rpc_url)
            if status == "confirmed":
                m.resolve_withdraw(conn, iid, "sent", now_iso=now_iso, sig=sig)
                tally["state"] = "sent"
                logger.warning("withdrawal SENT %.2f USDC to %s sig=%s", amount, to_address, sig)
                alert_fn("\U0001f4b8 withdrawal sent | $%.2f USDC to %s%s\nhttps://solscan.io/tx/%s"
                         % (amount, _short(to_address),
                            " (created its USDC account)" if created else "", sig))
                return tally
            if status == "failed":
                return fail("the transfer landed and failed on chain")
            tally["state"] = "unresolved"
            logger.warning("withdrawal UNRESOLVED status=%s sig=%s", status, sig)
            return tally
    except Exception:
        logger.exception("withdrawal pass failed")
        if tally.get("intent_id") and not tally.get("sig") and tally.get("state") is None:
            try:
                m.resolve_withdraw(conn, tally["intent_id"], "failed", now_iso=now_iso,
                                   reason="internal error before sending; check the runner log")
            except Exception:
                logger.exception("withdrawal: could not record the failure")
        tally["state"] = "error"
        return tally


def resolve_withdraws(conn, *, now_iso, confirm_once_fn, proof_fn, height_fn, alert_fn=None):
    """Settle 'submitted' withdrawals. Returns {intent_id: new state}; never raises."""
    from sniper.persistence import db_memecoin as m
    alert_fn = alert_fn or _alert
    out = {}
    for row in m.unresolved_withdraws(conn, "solana"):
        iid, sig = row["intent_id"], row["sig"]
        try:
            status = confirm_once_fn(sig)
            if status == "confirmed":
                m.resolve_withdraw(conn, iid, "sent", now_iso=now_iso)
                out[iid] = "sent"
                alert_fn("\U0001f4b8 withdrawal sent (confirmed late) | $%.2f USDC to %s\n"
                         "https://solscan.io/tx/%s"
                         % (row["amount_usdc"], _short(row["to_address"]), sig))
                continue
            if status == "failed":
                m.resolve_withdraw(conn, iid, "failed", now_iso=now_iso,
                                   reason="the transfer landed and failed on chain")
                out[iid] = "failed"
                continue
            if status not in ("not_found", "unavailable"):
                continue                 # Positive pending evidence cannot be erased by a null peer.
            last_valid = row["last_valid_height"]
            height = height_fn() if last_valid is not None else None
            if (isinstance(height, (int, float)) and height > last_valid
                    and proof_fn(sig) == "not_found"):
                m.resolve_withdraw(conn, iid, "failed", now_iso=now_iso,
                                   reason="the transfer expired and never landed; nothing was sent")
                out[iid] = "failed"
        except Exception:
            logger.exception("withdrawal resolve failed intent=%s", iid)
    return out


_WORKER = None
_WORKER_LOCK = threading.Lock()


def start_pass(ctx, *, state, keypair, rpc_url, to_address, now_fn, log_fn=print,
               connection_fn=None):
    """Run one receipt check or transfer on a worker with a separate database connection."""
    global _WORKER
    if state not in ("queued", "claimed", "submitted"):
        return False
    if connection_fn is None:
        from sniper.persistence.db_core import get_connection
        connection_fn = get_connection
    withdrawal_rpc = ctx.get("withdraw_rpc_url") or rpc_url
    confirm_once = ctx.get("confirm_once_fn")

    def run():
        conn = connection_fn()
        try:
            if state == "submitted":
                if ctx.get("confirm_once_fn") and ctx.get("block_height_fn"):
                    from sniper.execution.solana_real_seams import rpc_confirm
                    result = resolve_withdraws(
                        conn, now_iso=now_fn(), confirm_once_fn=ctx["confirm_once_fn"],
                        # Pin the archival check to the withdrawal's endpoint; never rotate a null proof.
                        proof_fn=ctx.get('confirm_proof_once_fn') or (lambda sig: rpc_confirm(
                            sig, withdrawal_rpc, max_attempts=1, poll_s=0)),
                        height_fn=ctx["block_height_fn"])
                    if result:
                        log_fn("[memecoin] withdrawal resolved: %s" % result)
            elif keypair is not None and ctx.get("pubkey") and ctx.get("usdc_fn"):
                result = withdraw_once(
                    conn, keypair=keypair, pubkey=str(ctx["pubkey"]), rpc_url=withdrawal_rpc,
                    now_iso=now_fn(), to_address=to_address, usdc_fn=ctx["usdc_fn"],
                    call_fn=ctx.get("withdraw_call_fn"),
                    send_factory_fn=ctx.get("withdraw_send_factory_fn"),
                    confirm_fn=(lambda sig, _url: confirm_once(sig)) if confirm_once else None)
                if result.get("state") not in ("wallet_busy", "book_busy", "idle"):
                    log_fn("[memecoin] withdrawal: %s" % result)
        except Exception:
            logger.exception("withdrawal worker failed")
        finally:
            conn.close()

    with _WORKER_LOCK:
        if _WORKER is not None and _WORKER.is_alive():
            return False
        _WORKER = threading.Thread(target=run, name="memecoin-withdraw", daemon=True)
        _WORKER.start()
        return True

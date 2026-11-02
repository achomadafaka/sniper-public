"""Send, confirm and book one PREBUILT funded Solana entry (the slot lane).

The slot lane (docs/superpowers/specs/2026-09-26-solana-slot-lane-design.md) builds and signs its
transaction before this module sees it, so there is no quote, no reverse quote and no pre-balance
read here: every one of those is a network round trip the lane exists to avoid. What stays is the
part of `solana_swap_executor.execute_entry` that protects money:

- the signed identity is bound durably BEFORE broadcast (the send seam persists it);
- a broadcast refusal or an on-chain failure releases the reservation with the chain's own reason;
- an unknown outcome keeps the reservation and opens nothing, and nothing is ever re-signed. The
  only repeat is `resender` (2026-09-28): the IDENTICAL signed bytes, same signature, on its own
  thread, stopped once the outcome is known (solana_slot_resend.py);
- the fill is the transaction's OWN receipt: the owner's USDC decrease and token increase, bounded
  by the reserved notional (`receipt_entry_fill`). A confirmed transaction whose receipt cannot be
  read is an anomaly that stays reserved, never a guessed fill.

New automatic entries carry no source-slot deadline and retain their signed minimum output.
Legacy explicitly bounded transactions still reconcile: an exact failed receipt proves
`slot_window_expired`, records the bounds and releases the reservation. Pump.fun 6042 failures
retain the available and required tokens (`pump_slippage_failure`).
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone

from sniper.execution.entry_attribution import (
    buy_drift, positive_price, slot_model_benchmark, trace_mark, trace_mark_submission,
)
from sniper.persistence import db_memecoin as m
from sniper.execution.solana_swap_executor import _trace_substage, signed_priority_fee_lamports

logger = logging.getLogger(__name__)

RECEIPT_READS = 4          # a confirmed tx can lag getTransaction at `confirmed` by a beat
RECEIPT_READ_SLEEP_S = 0.4
_PUMP_6042 = "Error Code: BuySlippageBelowMinTokensOut. Error Number: 6042."


def pump_slippage_failure(receipt):
    """Pump curve 6042 or PumpSwap 6040 from an already-read failed receipt, or None.

    The program logs `Left` (tokens the spend would have bought when it executed) and `Right` (the
    signed minimum) right after the AnchorError line; that pair is the whole diagnosis, and the
    error name alone loses it. Only a pump.fun failure whose Left is positive and below Right
    counts; anything else is None, never a guess."""
    import re
    from sniper.execution.pump_native_exit import PUMP_PROGRAM
    from sniper.execution.pumpswap_buy import AMM_PROGRAM
    try:
        meta = receipt["meta"]
        _index, error = meta["err"]["InstructionError"]
        if error == {"Custom": 6042}:
            program, marker = PUMP_PROGRAM, _PUMP_6042
        elif error == {"Custom": 6040}:
            program = AMM_PROGRAM
            marker = "Error Code: BuySlippageBelowMinBaseAmountOut. Error Number: 6040."
        else:
            return None
        logs = meta["logMessages"]
        if not any(line.startswith("Program %s failed:" % program) for line in logs):
            return None
        for i, line in enumerate(logs):
            if marker not in line:
                continue
            left = re.fullmatch(r"Program log: Left: (\d+)", logs[i + 1])
            right = re.fullmatch(r"Program log: Right: (\d+)", logs[i + 2])
            if left and right and 0 < int(left[1]) < int(right[1]):
                fee = meta.get("fee")
                return {"reason": "price_moved_beyond_limit",
                        "available_tokens_atomic": int(left[1]), "min_tokens_atomic": int(right[1]),
                        "fee_lamports": fee if isinstance(fee, int) and fee >= 0 else None,
                        "landing_slot": receipt.get("slot"), "no_tokens_bought": True,
                        **({"program_id": program} if program == AMM_PROGRAM else {})}
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return None


MAX_SURPLUS_CREDIT_FRACTION = 0.05   # a credit above 5% of the spend is a misread, not a surplus


class DeferredSlotEntry:
    """One already-sent identity. Settlement owns its connection and never signs or sends again."""

    def __init__(self, settle, trace=None):
        import threading
        self.settle, self.trace = settle, trace
        self._lock = threading.Lock()
        self._resolved = False
        self._result = None

    def resolve(self, conn):
        with self._lock:  # background only; duplicate completion cannot book twice
            if self._resolved:
                return self._result
            self._resolved = True
            if self.trace is not None:
                self.trace.conn = conn
            try:
                self._result = self.settle(conn)
            except Exception:
                self._result = {"status": "unknown", "reason": "settlement_error"}
                logger.exception("slot lane background settlement failed")
            return self._result


def execute_prebuilt_entry(conn, *, pending, signed_b64, sig, validity, send_fn, confirm_fn,
                           receipt_fill_fn, now_iso, trace=None, sleep_fn=None, venue="slot_lane",
                           sol_surplus_fn=None, sol_usd=None, resender=None, defer_confirmation=False):
    """Broadcast one signed entry for `pending` and book the result. Returns a result dict whose
    `status` is filled, failed, anomaly or unknown, the same vocabulary as execute_entry."""
    pending_id = pending["id"]
    mint = pending["mint"]
    maximum = float(pending.get("reserved_notional_usd") or 0.0)
    funding_asset = pending.get("funding_asset") or "usdc"
    funding_amount = pending.get("funding_amount_atomic")
    funding_price = pending.get("funding_sol_usd")
    provenance = {
        "attempt_id": pending.get("attempt_id"),
        "origin_event_id": pending.get("origin_event_id"),
        "network_id": "solana",
        "signal_source": pending.get("signal_source"),
    }
    if maximum <= 0:
        m.release_pending(conn, pending_id, "slot_lane_no_notional")
        return {"status": "aborted", "reason": "reservation has no notional", **provenance}
    if funding_asset == "sol":
        try:
            if (funding_amount is None or not str(funding_amount).isdigit()
                    or int(funding_amount) <= 0 or funding_price is None
                    or not math.isfinite(float(funding_price)) or float(funding_price) <= 0):
                raise ValueError("invalid SOL funding evidence")
        except (TypeError, ValueError, OverflowError):
            return {"status": "anomaly", "reason": "funding_contract_invalid", **provenance}
        if (not isinstance(validity, dict) or validity.get("funding_asset") != "sol"
                or validity.get("funding_amount_atomic") != int(funding_amount)):
            m.release_pending(conn, pending_id, "slot_lane_funding_mismatch")
            return {"status": "aborted", "reason": "slot_lane_funding_mismatch", **provenance}
    elif funding_asset != "usdc":
        return {"status": "anomaly", "reason": "funding_asset_unknown", **provenance}
    contract_fields = m.validated_pending_funded_contract(conn, pending)
    # The builder's entry pool keys (leg ammKeys, then the curve PDA or the PumpSwap pool), the shape
    # the J7 path stores; vol_death matches exit routes against them. A venue label never matches.
    route_target = (validity or {}).get("route_target") if isinstance(validity, dict) else None
    route_target = route_target or venue

    # The legacy seam cannot persist identity itself; write the signature ahead of the send.
    if not getattr(send_fn, "persists_identity", False):
        identity_started = time.perf_counter()
        m.set_pending_sig(conn, pending_id, sig, now_iso, signed_payload_b64=signed_b64)
        if trace is not None:
            try:
                trace.mark_substage("slot_transport_identity_bound",
                    duration_ms=(time.perf_counter() - identity_started) * 1000,
                    timing_basis="legacy_durable_signature_commit")
            except Exception:
                logger.debug("legacy identity timing failed", exc_info=True)
    # This is entry to the transport seam, not proof of network departure or leader receipt.
    if trace is not None:
        trace.mark_substage("slot_send_call", duration_ms=0, tx_sig=sig,
                            timing_basis="send_seam_call_before_identity_binding")
    send_started = time.perf_counter()
    if getattr(send_fn, "accepts_validity", False):
        send_result = send_fn(signed_b64, validity=validity)
    else:
        send_result = send_fn(signed_b64)
    if trace is not None:
        trace.mark_substage("slot_send_return", duration_ms=(time.perf_counter() - send_started) * 1000,
                            tx_sig=sig, accepted=(True if send_result else False if send_result is False else None),
                            timing_basis="send_seam_return_including_identity_binding")
    if send_result is False:
        m.release_pending(conn, pending_id, "broadcast_rejected")
        return {"status": "failed", "reason": "broadcast_rejected", "sig": sig, **provenance}
    if resender is not None:
        # After the first send returned, on its own thread: the first send's path and timing are
        # unchanged. It is stopped the moment this thread has the outcome.
        try:
            resender.start(send_started)
        except Exception:
            logger.warning("slot lane resend did not start sig=%s", sig, exc_info=True)
            resender = None
    if send_result:
        trace_mark_submission(trace, "transaction_submitted", 1)

    def settle(conn, sleep_fn=sleep_fn):
        try:
            status = confirm_fn(sig)
        finally:
            if resender is not None:
                resender.stop()
        landing_slot = getattr(status, "landing_slot", None)
        resends = _resend_report(resender, landing_slot, trace)
        if status in ("confirmed", "failed"):
            trace_mark_submission(trace, "chain_confirmation", 1, landing_slot=landing_slot)

        if status == "failed":
            pump_slippage = getattr(status, "pump_slippage", None)
            slot_guard = getattr(status, "slot_guard", None)
            reason = getattr(status, "reason_code", None) or "tx_failed"
            if pump_slippage and trace is not None:
                trace.mark_substage("slot_pump_slippage", **pump_slippage)
            if slot_guard:
                _trace_substage(trace, "slot_guard_result", **slot_guard)
            m.release_pending(conn, pending_id, str(reason)[:64])
            return {"status": "failed", "reason": reason, "sig": sig,
                    "chain_err": getattr(status, "err", None),
                    "program_error": getattr(status, "program_error", None),
                    "failed_program": getattr(status, "failed_program", None),
                    **({"pump_slippage": pump_slippage} if pump_slippage else {}),
                    **({"slot_guard": slot_guard} if slot_guard else {}),
                    **provenance, "landing_slot": landing_slot, **resends}

        if status != "confirmed":
            # May still land: keep the reservation, open nothing, NEVER resubmit.
            return {"status": "unknown", "sig": sig, **provenance, **resends}

        if sleep_fn is None:
            sleep_fn = time.sleep
        fill = None
        native_detail = None
        for attempt in range(RECEIPT_READS):
            if funding_asset == "sol":
                native_detail = _receipt_sol_fill(receipt_fill_fn, sig, mint, maximum,
                                                  int(funding_amount), float(funding_price))
                fill = native_detail[:3] if native_detail is not None else None
            else:
                fill = _receipt_fill(receipt_fill_fn, sig, mint, maximum)
            if fill is not None:
                break
            if attempt + 1 < RECEIPT_READS:
                sleep_fn(RECEIPT_READ_SLEEP_S)
        trace_mark(trace, "balance_delta")
        if fill is None:
            # Confirmed but no provable spend/receipt: leave it reserved so recovery reconciles it.
            return {"status": "anomaly", "sig": sig, "reason": "receipt_unreadable", **provenance, **resends}
        spent, got, _decimals = fill
        # SOL SURPLUS CREDIT (2026-09-26, operator order). The lane buys through a USDC -> SOL leg and
        # the curve spends only the leg's MINIMUM out, so the difference stays in the wallet as SOL
        # (+1.39% of cost on 65813). The USDC delta counts it as spent; credit it back at the lane's
        # SOL/USD so entry_notional is the cost of the TOKENS. Only a positive, finite credit smaller
        # than the spend is applied; anything else books the plain USDC delta as before.
        # PLAUSIBILITY CAP: the surplus is bounded by the leg's slippage (150 bps) plus leg drift, so a
        # credit above MAX_SURPLUS_CREDIT_FRACTION of the spend is a misread, and the plain USDC delta is
        # booked (conservative). An unreadable receipt is UNKNOWN (None), never a zero credit.
        usdc_spent, credit_usd, surplus_lamports, credit_note = (
            spent if funding_asset == "usdc" else None), 0.0, None, "no_seam"
        if funding_asset == "usdc" and sol_surplus_fn is not None and sol_usd:
            try:
                surplus_lamports = sol_surplus_fn(sig)
                if surplus_lamports is None:
                    credit_note = "surplus_unknown"
                else:
                    credit = int(surplus_lamports) / 1e9 * float(sol_usd)
                    if not math.isfinite(credit) or credit <= 0:
                        credit_note = "no_surplus"
                    elif credit > MAX_SURPLUS_CREDIT_FRACTION * spent:
                        credit_note = "credit_over_cap"
                    else:
                        credit_usd, credit_note = credit, "credited"
                        spent = spent - credit
            except Exception:
                credit_note = "surplus_read_failed"
                logger.warning("slot lane SOL surplus read failed sig=%s; booking the USDC delta", sig,
                               exc_info=True)
        if trace is not None:
            try:
                trace.mark_substage("sol_surplus_credit", usdc_spent=usdc_spent,
                                    surplus_lamports=surplus_lamports, sol_usd=sol_usd,
                                    credit_usd=credit_usd, outcome=credit_note)
            except Exception:
                logger.debug("sol surplus trace mark failed", exc_info=True)
        fill_price = spent / got
        their_px = positive_price(pending.get("source_price"))
        attribution = "exact_event" if their_px is not None else "unavailable"
        drift = buy_drift(their_px, None, fill_price)
        entry_mcap = positive_price(pending.get("source_market_cap"))
        pid, fill_id = m.finalize_pending_entry(
            conn, pending_id=pending_id,
            position_kwargs=dict(
                source_user_id=pending.get("source_user_id"), source_handle="(real)", mint=mint,
                ticker="", entry_price=fill_price, entry_notional=spent, entry_fill_source="chain",
                entry_lag_s=m.resolve_source_lag_s(conn, pending.get("origin_event_id")),
                their_size_usd=m.resolve_source_size_usd(
                    conn, pending.get("origin_event_id"), network_id=pending.get("network_id"),
                    mint=pending.get("mint"), source_user_id=pending.get("source_user_id")),
                entry_liquidity=pending.get("source_liquidity"),
                entry_token_age_s=pending.get("source_token_age_s"),
                opened_at=now_iso, strategy=pending.get("strategy"),
                execution_mode="real", network_id="solana",
                signal_source=pending.get("signal_source") or "slot_lane",
                origin_event_id=pending.get("origin_event_id"), entry_mcap=entry_mcap,
                their_price=their_px, fill_gap_pct=drift["total_copy_drift_pct"],
                attempt_id=pending.get("attempt_id"), price_attribution=attribution,
                quote_age_ms=None, cluster_decision_id=pending.get("cluster_decision_id"),
                quote_price=None, quote_observed_at=None, quote_id=None, quote_route_target=route_target,
                **contract_fields, **drift),
            fill_kwargs=dict(
                kind="entry", price=fill_price, qty=got, notional=spent, fill_source="chain",
                fraction=1.0, created_at=now_iso, tx_sig=sig, source_price=their_px,
                **({"funding_asset": "sol", "funding_spent_atomic": native_detail[5],
                    "funding_sol_usd": float(funding_price),
                    "network_fee_lamports": native_detail[3], "rent_lamports": native_detail[4],
                    "priority_fee_lamports": signed_priority_fee_lamports(signed_b64),
                    "returned_rent_lamports": 0} if native_detail is not None else {}),
                price_attribution=attribution, quote_age_ms=None, **provenance,
                quote_price=None, quote_observed_at=None, quote_id=None, quote_route_target=route_target,
                **drift))
        trace_mark(trace, "persistence")
        if trace is not None:
            try:
                model = slot_model_benchmark(
                    (validity or {}).get("pump_price_limit"), spent_usd=spent, acquired_tokens=got)
                if model is not None:
                    trace.mark_substage("slot_model_benchmark", **model)
            except Exception:
                logger.debug("slot model benchmark trace failed", exc_info=True)
        return {"status": "filled", "position_id": pid, "fill_id": fill_id, "fill_price": fill_price,
                "notional": spent, "usdc_spent": usdc_spent, "sol_surplus_credit_usd": credit_usd,
                "funding_asset": funding_asset,
                **({"network_fee_lamports": native_detail[3], "rent_lamports": native_detail[4],
                    "sol_spent_lamports": native_detail[5]} if native_detail is not None else {}),
                "sig": sig, "source_price": their_px,
                "price_attribution": attribution, "landing_slot": landing_slot, **provenance, **drift, **resends}

    if defer_confirmation:
        return {"status": "submitted", "pending_id": pending_id, "sig": sig, **provenance,
                "_settlement": DeferredSlotEntry(settle, trace)}
    return settle(conn)


def _resend_report(resender, landing_slot, trace):
    """{'resends_sent': n} for the result, and one `slot_resend` mark on THIS (the lane's) thread:
    per send the offset, the head slot when it went out and landing_slot - head. Never raises."""
    if resender is None:
        return {}
    try:
        report = resender.report()
        for send in report["sends"]:
            head = send.get("head_slot")
            send["landing_minus_head"] = (landing_slot - head if landing_slot is not None
                                          and head is not None else None)
        if trace is not None:
            trace.mark_substage("slot_resend", landing_slot=landing_slot, **report)
        return {"resends_sent": report["sent"]}
    except Exception:
        logger.warning("slot lane resend report failed", exc_info=True)
        return {}


def _receipt_read_failed(sig):
    """A receipt read that RAISED saw nothing: the caller retries it like an empty read (2026-09-27,
    Alpha GO F1). Letting it escape threw a CONFIRMED buy out of the executor with no position
    until recovery. Stamped, because this line reaches runner.log through logging's last-resort
    handler as a bare message; exc_info keeps a parser bug visible instead of silently retried."""
    logger.warning("%s slot lane receipt read raised sig=%s; treated as unreadable and retried",
                   datetime.now(timezone.utc).isoformat(), sig, exc_info=True)


def _receipt_fill(receipt_fill_fn, sig, mint, maximum):
    """A finite receipt fill within the reserved spend, or None."""
    try:
        spent, got, decimals = receipt_fill_fn(sig, mint, maximum)
        spent, got, decimals = float(spent), float(got), int(decimals)
    except (TypeError, ValueError, OverflowError, ArithmeticError):
        return None
    except Exception:
        _receipt_read_failed(sig)
        return None
    if (not math.isfinite(spent) or not math.isfinite(got) or spent <= 0 or got <= 0
            or spent > maximum or not 0 <= decimals <= 255):
        return None
    return spent, got, decimals


def _receipt_sol_fill(receipt_fill_fn, sig, mint, maximum, amount_lamports, sol_usd):
    try:
        proof = receipt_fill_fn(sig, mint, maximum)
        lamports = int(proof["sol_spent_lamports"])
        got, decimals = float(proof["got"]), int(proof["decimals"])
        fee, rent = int(proof["fee_lamports"]), int(proof["rent_lamports"])
        spent = lamports / 1e9 * sol_usd
    except (KeyError, TypeError, ValueError, OverflowError, ArithmeticError):
        return None
    except Exception:
        _receipt_read_failed(sig)
        return None
    if (lamports <= 0 or lamports > amount_lamports or fee < 0 or rent < 0
            or not math.isfinite(spent) or spent <= 0 or spent > maximum + 0.01
            or not math.isfinite(got) or got <= 0 or not 0 <= decimals <= 255):
        return None
    return spent, got, decimals, fee, rent, lamports


def receipt_owner_sol_surplus(tx, owner):
    """Lamports the owner GAINED in native plus wrapped SOL across one transaction; 0 for a net
    outflow; None when the receipt does not carry the owner's balances (UNKNOWN, never zero).

    Both builders end with the surplus as native lamports: the curve leg unwraps, and the PumpSwap
    builder closes its wSOL account in the same transaction. The wSOL term only matters if a
    builder ever leaves wrapped SOL behind. Known over-credit: closing a wSOL account that existed
    BEFORE the transaction returns rent this transaction never paid (~0.002 SOL); the caller's
    plausibility cap bounds any larger misread."""
    wsol = "So11111111111111111111111111111111111111112"
    meta = (tx or {}).get("meta") or {}
    keys = ((tx or {}).get("transaction") or {}).get("message", {}).get("accountKeys") or []
    names = [k.get("pubkey") if isinstance(k, dict) else k for k in keys]
    if owner not in names or "preBalances" not in meta or "postBalances" not in meta:
        return None
    i = names.index(owner)
    native = int(meta["postBalances"][i]) - int(meta["preBalances"][i])
    wrapped = 0
    for entries, sign in ((meta.get("preTokenBalances") or [], -1),
                          (meta.get("postTokenBalances") or [], 1)):
        for e in entries:
            if e.get("mint") == wsol and e.get("owner") == owner:
                wrapped += sign * int((e.get("uiTokenAmount") or {}).get("amount") or 0)
    return max(0, native + wrapped)

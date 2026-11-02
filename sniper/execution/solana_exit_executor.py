"""Real Solana exit executor (S3, DISARMED build). Exit is scarier than entry.

`execute_exit` sells a real position's ENTIRE token balance (no decimals math — sell what the
wallet holds), prices realized PnL from the on-chain USDC delta (never the quote), and walks a
slippage ladder (e.g. 3% -> 10% -> 25%) on failure. Contract:

  - confirmed  -> finalize the claimed close, realized = usdc_received - entry_notional, record an
                  exit-reason-specific fill with its tx_sig.
  - failed     -> widen slippage (next rung).
  - unknown/timeout -> LEAVE the position open with durable signed identity and STOP (the sell may
                  have landed; every later sweep returns already_closing until reconciliation).
  - ladder exhausted -> mark a STUCK BAG (stays open + flagged + alerting), NEVER a fake clean close.

Injected seams (quote/sign/send/confirm/balances) so every branch is unit-tested without a live
endpoint. NOT integration-tested — a live smoke test must precede any arm. Every exit reason
(mirror/SL/TP/timecap) routes through this on the real path.
"""
from __future__ import annotations

import inspect
import logging
import math
import threading
import time

from sniper.persistence import db_memecoin as m
from sniper.execution.solana_wallet import TokenBalanceReadError

logger = logging.getLogger("memecoin")





def exit_route_fields(quote):
    """The signed exit's route, for the fill row (operator 2026-10-21, Gamma's exit audit).

    quote_route_target: the routePlan's ammKey legs in order (the pools the signed swap trades
    through, since the transaction is built from this quote), else the target bound onto a native
    quote; None when neither is known (never the bare 'jupiter' literal entries fall back to).
    executed_pool: that pool when the route has exactly ONE leg, else None (multi-hop is not one
    pool). Pure, never raises."""
    try:
        legs = [str(((leg or {}).get("swapInfo") or {}).get("ammKey"))
                for leg in ((quote or {}).get("routePlan") or ())
                if ((leg or {}).get("swapInfo") or {}).get("ammKey")]
        target = ",".join(legs) if legs else ((quote or {}).get("_sniper_route_target") or None)
        single = legs[0] if len(legs) == 1 else (
            target if target and not legs and "," not in str(target) else None)
        return {"quote_route_target": target, "executed_pool": single}
    except Exception:
        return {"quote_route_target": None, "executed_pool": None}

def _quoted_unit_price(quote, tokens):
    """USD/token the SIGNED route was quoted at, for the exit's quote-vs-fill column.

    Solana exits always quote to USDC (`jupiter_exit_quote` fixes `outputMint=USDC_MINT`), so
    `outAmount` is USDC atomic at 6 decimals and needs no price lookup. `quote` is the same
    iteration's object that `sign_fn(quote)` signed, so this is the executed route and not a
    later re-quote. A venue quote without `outAmount` (Raydium fallback) yields None: a
    measurement must never raise on the way out of a confirmed sell.
    """
    try:
        out = (quote or {}).get("outAmount")
        if out is None or not tokens or float(tokens) <= 0:
            return None
        expected_usdc = int(out) / 1_000_000.0
        return (expected_usdc / float(tokens)) if expected_usdc > 0 else None
    except (TypeError, ValueError, AttributeError):
        return None


def _oversized(signed_tx):
    """True when the signed payload cannot fit one Solana packet (the send seam refuses it)."""
    from sniper.execution.solana_swap_executor import _payload_bytes
    from sniper.execution.submission_wiring import SOLANA_PACKET_LIMIT
    size = _payload_bytes(signed_tx)
    return size is not None and size > SOLANA_PACKET_LIMIT


def _exit_retry_accounts():
    from sniper.settings.memecoin import MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS
    return int(MEMECOIN_ENTRY_OVERSIZE_RETRY_MAX_ACCOUNTS or 0) or None


def _accepts(fn, name):
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == name or p.kind == inspect.Parameter.VAR_KEYWORD for p in params)


def _quote(quote_fn, mint, tokens, slip, max_accounts, atomic=None):
    """The rung's quote, capped once an earlier rung signed an oversized payload (2026-09-13).
    A quote_fn that predates the cap is called exactly as before. `atomic` (the raw balance the rung
    already read) goes only to a quote_fn that takes it, which then skips its own second read."""
    extra = ({"atomic": int(atomic)} if atomic is not None and int(atomic) > 0
             and _accepts(quote_fn, "atomic") else {})
    if max_accounts:
        try:
            return quote_fn(mint, tokens, slip, max_accounts=max_accounts, **extra)
        except TypeError:
            pass
    return quote_fn(mint, tokens, slip, **extra)


def _ms_since(started):
    return (time.monotonic() - started) * 1000.0


class _Read:
    """One read on its own thread, joined where the serial read used to run (2026-09-28, Alpha GO
    "Step 1"). `result()` re-raises the read's own exception on the exit thread, so a failed read
    takes exactly the branch the serial read took. The thread ends with its read; nothing it holds
    outlives the call."""

    def __init__(self, fn):
        self._fn = fn
        self.value = self.error = self.read_ms = self.waited_ms = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="exit-read")
        self._thread.start()

    def _run(self):
        started = time.monotonic()
        try:
            self.value = self._fn()
        except BaseException as exc:          # re-raised on the exit thread by result()
            self.error = exc
        finally:
            self.read_ms = _ms_since(started)

    def result(self):
        started = time.monotonic()
        self._thread.join()
        self.waited_ms = _ms_since(started)
        if self.error is not None:
            raise self.error
        return self.value


def _overlap_reads_on():
    from sniper.settings import memecoin as cfg
    return bool(getattr(cfg, "MEMECOIN_EXIT_OVERLAP_READS", True))


def _verified_receipt_fill(receipt_fill_fn, sig, mint, quote):
    """Receipt-owned sold atoms must match the exact input the signed route carries."""
    if receipt_fill_fn is None:
        return None
    try:
        proof = receipt_fill_fn(sig, mint)
        if proof is None:
            return None
        received = float(proof['received'])
        atomic, decimals = proof['sold_atomic'], proof['decimals']
        expected = int(quote['inAmount'])
        if (isinstance(proof['received'], bool) or type(atomic) is not int or type(decimals) is not int
                or atomic <= 0 or atomic != expected or not 0 <= decimals <= 255
                or not math.isfinite(received) or received <= 0):
            return None
        sold_qty = atomic / 10 ** decimals
        return (received, sold_qty) if math.isfinite(sold_qty) and sold_qty > 0 else None
    except Exception:
        logger.warning('exit receipt unreadable sig=%s mint=%s', sig, mint, exc_info=True)
        return None


def execute_exit(conn, position, exit_reason, now_iso, *, quote_fn, sign_fn, send_fn, confirm_fn,
                 usdc_fn, token_balance_fn, slippage_ladder=None, sleep_fn=None,
                 decision_state_id=None, decision_state_id_fn=None,
                 floor_usd_fn=None, reference_value_usd_fn=None, dust_hold_fn=None, usdc_checked_fn=None,
                 receipt_proceeds_fn=None, now_fn=None, quote_check_fn=None,
                 token_snapshot_fn=None, receipt_first=False, receipt_fill_fn=None):
    """Execute one real exit. Returns {status: closed|stuck|unknown|gone|superseded, ...}.

    OVERLAPPED READS (2026-09-28, Alpha GO "Step 1", MEMECOIN_EXIT_OVERLAP_READS, default on):
    every rung read the token balance three times and USDC once, all serially, before broadcast.
      - `token_snapshot_fn(mint)` -> TokenBalanceSnapshot gives the rung's human AND raw balance
        from ONE response; the raw amount goes into the quote, whose seam then skips its own
        second read. Without the seam, the rung reads through token_balance_fn as before.
      - The USDC pre-read starts beside the quote and is joined where it used to run; a raise
        still releases the claim and returns balance_unreadable before anything is signed.
      - The signing re-read runs beside the swap BUILD, for a sign_fn that takes `before_sign`:
        the seam calls it between build and signature, and verify_exit_intent judges the read
        there, so a changed balance still supersedes before anything is signed (WO-6).
    All of it runs inside the wallet lock the caller holds, as the serial reads did. 0 = the
    serial order."""
    from sniper.services.memecoin_exit_intent import (
        CLOSE, bind_quote, build_exit_intent, exit_floor_refused, quote_binding_complete,
        verify_exit_intent)
    if slippage_ladder is None:
        from sniper.settings.memecoin import MEMECOIN_EXIT_SLIPPAGE_LADDER
        slippage_ladder = MEMECOIN_EXIT_SLIPPAGE_LADDER
    if sleep_fn is None:
        sleep_fn = time.sleep
    # MEASUREMENT ONLY, no decision changes (2026-09-08). `now_iso` remains the decision-time
    # value for every existing call (claims, resets, intent status) -- unchanged. `now_fn`, when
    # supplied, is called FRESH at the two writes this docstring used to warn about:
    # close_submitted_at (right before broadcast) and closed_at (right after confirmation). Unset,
    # it falls back to reusing `now_iso`, so a caller that does not opt in gets byte-identical
    # behavior. Opting in makes the two columns independently-clocked wall-clock facts instead of
    # one frozen instant copied twice -- see tests/test_solana_exit_executor_timestamps.py.
    _caller_now = now_fn

    def now_fn():
        """The clock, fail-closed to the decision instant. A MEASUREMENT MUST NEVER FAIL A SELL,
        so a caller's clock that raises is caught here and not just one that returns None: `or`
        alone would have let a broken clock abort a real exit between signing and broadcast."""
        if _caller_now is None:
            return now_iso
        try:
            return _caller_now() or now_iso
        except Exception:
            return now_iso
    from sniper.execution.exit_timing import ExitTiming
    timing = ExitTiming()
    mint = position["mint"]
    pid = position["id"]
    entry_notional = position["entry_notional"] or 0.0

    overlap = _overlap_reads_on()
    snapshot_on = overlap and token_snapshot_fn is not None

    def _read_tokens():
        """(human balance, raw atomic or None); an unavailable balance raises."""
        if snapshot_on:
            snap = token_snapshot_fn(mint)
            # An empty owner index is valid before entry, but cannot prove a funded bag vanished.
            if (type(snap.atomic) is not int or not 0 <= snap.atomic < 2**64
                    or type(snap.decimals) is not int or not 0 <= snap.decimals <= 255):
                raise TokenBalanceReadError("account_balance_unproved")
            return float(snap.human), snap.atomic
        value = token_balance_fn(mint)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise TokenBalanceReadError("invalid_balance")
        return value, None

    def _balance_unreadable(exc):
        logger.warning("exit token balance unreadable pos=%s mint=%s failure=%s", pid, mint,
                       getattr(exc, "failure", "rpc_error"))
        m.release_position_close_claim(conn, pid, claim_id)
        return {"status": "balance_unreadable", "reason": "token_balance_unreadable"}

    usdc_reader = usdc_checked_fn or usdc_fn

    # Re-read settled state, then acquire a durable owner token BEFORE signing. is_open remains the
    # ledger fact until a confirmed fill and finalization commit atomically.
    _t = time.monotonic()
    fresh = m.get_position(conn, pid)
    if not fresh or not fresh.get("is_open"):
        return {"status": "already_closed"}
    claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
    timing.sub("claim_db", _ms_since(_t))
    if not claim_id:
        return {"status": "already_closing"}
    intent = None                              # WO-6: bound at the first quotable rung with balance

    def _verify_current(bound, balance):
        try:
            current_state_id = decision_state_id_fn() if decision_state_id_fn else None
        except Exception:
            current_state_id = None
        return verify_exit_intent(
            bound, m.get_position(conn, pid), balance=balance,
            current_decision_state_id=current_state_id)

    # OVERSIZE (2026-09-13). Buys re-quote an oversized payload with a capped account count; sells
    # sent it and let the send seam refuse it, burning a rung on a route that could come back just
    # as large ({CAT} 56745: 1,446 / 1,290 bytes; POCHACCO 56510: 1,296). Once a rung signs an
    # oversized payload, it is dropped before the write-ahead and every later rung is capped.
    cap_accounts = None
    for slip in slippage_ladder:
        _t = time.monotonic()
        try:
            tokens, atomic = _read_tokens()
            for _ in range(3):
                if tokens > 0:
                    break
                sleep_fn(1.0)
                tokens, atomic = _read_tokens()
        except Exception as exc:
            return _balance_unreadable(exc)
        timing.sub("balance_read", _ms_since(_t))
        # POSITION-AWARE SIZING (2026-08-13). Selling the whole wallet balance is correct while
        # ONE funded position owns the mint, and silently wrong the moment two do: the first
        # position's exit sells both buys' tokens while being costed against one, inflating
        # reported profit by the untracked buy and orphaning its reservation (EUB1eZBt: $33.04
        # booked, $1.03 true). Constrain ONLY when the mint is genuinely shared, so the ordinary
        # single-position path — where the whole-balance read also absorbs dust and
        # fee-on-transfer remainders — keeps its exact previous behaviour.
        if m.shared_mint_open_real_count(conn, mint, position.get("network_id")) > 1:
            share = m.open_qty(conn, pid)
            if share > 0:
                # min(): never sell more than the wallet holds, and never more than this position
                # owns. A share LARGER than the balance means someone already sold into it, and
                # selling the balance would repeat the very theft this guard exists to stop.
                tokens = min(tokens, share)
        if tokens <= 0:
            m.release_position_close_claim(conn, pid, claim_id)
            m.mark_stuck_bag(conn, pid)
            return {"status": "stuck", "reason": "token_balance_zero"}
        timing.rung()
        # The USDC pre-read has no dependency on the quote, so it rides beside it.
        usdc_early = _Read(usdc_reader) if overlap else None
        _t = time.monotonic()
        try:
            quote = _quote(quote_fn, mint, tokens, slip, cap_accounts,
                           atomic=atomic if snapshot_on else None)
        except TokenBalanceReadError as exc:
            return _balance_unreadable(exc)
        timing.sub("quote_call", _ms_since(_t))
        timing.stage("quote")
        if not quote:
            continue                                   # no route at this rung -> widen
        if quote_check_fn is not None:
            try:
                allowed, detail = quote_check_fn(m.get_position(conn, pid), quote, tokens, slip)
            except Exception:
                logger.exception("execution quote check failed pos=%s", pid)
                allowed, detail = True, {"code": "execution_quote_check_failed_sweep_verdict_stands"}  # D2: unknown, not hold
            if not allowed:
                if not m.release_position_close_claim(conn, pid, claim_id):
                    return {"status": "already_closing"}
                return {"status": "deferred", "reason": detail["code"]}
        # WO-6: bind the immutable intent once (with the exact balance we are about to sell), then
        # re-verify against a FRESH read right before signing — a superseded decision (controller
        # revision moved), a lost claim, or a position closed mid-ladder is refused, never signed.
        if intent is None:
            intent = bind_quote(build_exit_intent(
                fresh, claim_id=claim_id, action=CLOSE, fraction=1.0,
                balance=tokens, decision_state_id=decision_state_id), quote, quote_time=now_iso)
            if not quote_binding_complete(intent):
                intent = None
                continue
            # Same floor sanity as the full exit, against a value derived OUTSIDE this quote.
            # `tokens` is what is ACTUALLY sold: valuing the whole bag on a partial
            # would refuse legitimate small de-risks (a 4%% de-risk reads as 25x).
            held = dust_hold_fn(intent, quote, fresh, tokens) if dust_hold_fn else None
            if held:
                # DUST-ROUTE HOLD (memecoin_exit_dust_guard): sign nothing, keep the bag.
                m.release_position_close_claim(conn, pid, claim_id)
                return {"status": "dust_route_hold", "reason": held}
            if exit_floor_refused(intent, quote, fresh, tokens,
                                  floor_usd_fn=floor_usd_fn,
                                  reference_value_usd_fn=reference_value_usd_fn,
                                  logger=logger, position_id=pid):
                intent = None
                continue
            if not m.bind_exit_intent(conn, pid, claim_id, intent.to_json()):
                return {"status": "already_closing"}
        refusal = _verify_current(intent, tokens)
        if refusal:
            m.release_position_close_claim(conn, pid, claim_id)
            return {"status": "superseded", "reason": refusal}
        # CHECKED pre-read (2026-08-17, pos#38107): `usdc_fn` collapses a failed RPC read to 0.0,
        # and a zero pre on a funded wallet books the ENTIRE balance as sale proceeds (JEW booked
        # +$49.48 on a sell that returned $10.87). A failed read aborts BEFORE signing — retryable.
        try:
            if usdc_early is not None:
                usdc_pre = usdc_early.result()
                timing.sub("usdc_pre_wait", usdc_early.waited_ms)
            else:
                _t = time.monotonic()
                usdc_pre = usdc_reader()
                timing.sub("usdc_pre_wait", _ms_since(_t))
        except Exception:
            m.release_position_close_claim(conn, pid, claim_id)
            return {"status": "balance_unreadable", "reason": "usdc_balance_unreadable"}
        finally:
            if usdc_early is not None:
                timing.sub("usdc_pre_read", usdc_early.read_ms)
        if overlap and _accepts(sign_fn, "before_sign"):
            # The signing re-read rides beside the swap BUILD. The seam calls `before_sign` after
            # the build and before the signature: it joins the read and runs verify_exit_intent,
            # so a changed balance or a superseded decision is still refused before anything is
            # signed, recorded or sent (WO-6). A seam that never reaches it (a failed build, a
            # one-call builder) is judged right after, before any other branch.
            resign = _Read(lambda: _read_tokens()[0])
            verdict = {}

            def _gate():
                if "refusal" not in verdict:
                    try:
                        balance_now = resign.result()
                    except Exception as exc:
                        verdict["balance_error"] = exc
                        verdict["refusal"] = "token_balance_unreadable"
                        return False
                    timing.sub("signing_read", resign.read_ms)
                    timing.sub("signing_read_wait", resign.waited_ms)
                    verdict["refusal"] = _verify_current(intent, balance_now)
                return not verdict["refusal"]
            _t = time.monotonic()
            signed = sign_fn(quote, before_sign=_gate)
            timing.sub("sign_call", _ms_since(_t))
            if not _gate():
                if "balance_error" in verdict:
                    return _balance_unreadable(verdict["balance_error"])
                m.release_position_close_claim(conn, pid, claim_id)
                return {"status": "superseded", "reason": verdict["refusal"]}
        else:
            _t = time.monotonic()
            try:
                signing_balance = _read_tokens()[0]
            except Exception as exc:
                return _balance_unreadable(exc)
            timing.sub("signing_read_wait", _ms_since(_t))
            refusal = _verify_current(intent, signing_balance)
            if refusal:
                m.release_position_close_claim(conn, pid, claim_id)
                return {"status": "superseded", "reason": refusal}
            _t = time.monotonic()
            signed = sign_fn(quote)
            timing.sub("sign_call", _ms_since(_t))
        try:
            _split = getattr(sign_fn, "sign_timing", None)
            _split = _split() if callable(_split) else None
            if isinstance(_split, dict):
                timing.sub("swap_build", _split.get("build_ms"))
                timing.sub("local_sign", _split.get("sign_ms"))
        except Exception:
            pass
        timing.stage("sign")
        if not signed:
            m.release_position_close_claim(conn, pid, claim_id)
            claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
            if not claim_id:
                return {"status": "already_closing"}
            fresh, intent = m.get_position(conn, pid), None
            continue
        signed_tx, sig = signed[:2]
        if cap_accounts is None and _oversized(signed_tx) and _exit_retry_accounts():
            cap_accounts = _exit_retry_accounts()
            m.release_position_close_claim(conn, pid, claim_id)
            claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
            if not claim_id:
                return {"status": "already_closing"}
            fresh, intent = m.get_position(conn, pid), None
            continue
        validity = signed[2] if len(signed) > 2 and isinstance(signed[2], dict) else {}
        submitted_intent = intent.with_validity(
            blockhash=validity.get("blockhash"),
            last_valid_height=validity.get("last_valid_height")).with_signed_payload(
                signed_tx, tx_identity=sig)
        # Independently clocked (2026-09-08): a fresh read right before broadcast, not the
        # decision-time `now_iso` reused from the top of this call.
        submit_ts = now_fn()
        if not m.mark_position_close_submitted(
                conn, pid, claim_id, submit_ts, sig,
                expected_intent_json=intent.to_json(),
                submitted_intent_json=submitted_intent.to_json(),
                expected_controller_revision=intent.controller_revision):
            refusal = _verify_current(intent, tokens)
            m.release_position_close_claim(conn, pid, claim_id)
            return ({"status": "superseded", "reason": refusal}
                    if refusal else {"status": "already_closing"})
        intent = submitted_intent
        # Signed identity and submitted_unknown were committed atomically before this network call.
        send_result = send_fn(signed_tx)
        timing.stage("submit")
        if send_result is False:
            if not m.reset_position_close_submission(
                    conn, pid, claim_id, sig, now_iso, intent_status="dropped"):
                return {"status": "unknown", "sig": sig}
            m.release_position_close_claim(conn, pid, claim_id)
            claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
            if not claim_id:
                return {"status": "already_closing"}
            fresh, intent = m.get_position(conn, pid), None
            continue
        status = confirm_fn(sig)
        timing.stage("confirm")
        if status == "confirmed":
            # a just-confirmed sell can lag the balance surface -> re-read before booking a
            # received=0 close (which would record -entry_notional on money that DID arrive).
            # CHECKED post-read: a failed read here must never book usdc_pre-sized garbage —
            # the sell IS confirmed, so an unreadable balance goes durable submitted_unknown
            # and reconciliation settles it from chain evidence.
            received, sold_qty = None, tokens
            if receipt_first:
                proof = _verified_receipt_fill(receipt_fill_fn, sig, mint, quote)
                if proof is not None:
                    received, sold_qty = proof
            else:
                for attempt in range(4):
                    try:
                        received = (usdc_checked_fn or usdc_fn)() - usdc_pre
                    except Exception:
                        received = None
                    if received is not None and received > 0:
                        break
                    if attempt < 3:
                        sleep_fn(1.0)
                if received is None or received <= 0:
                    try:
                        receipt_received = receipt_proceeds_fn(sig) if receipt_proceeds_fn else None
                    except Exception:
                        receipt_received = None
                    received = receipt_received if receipt_received and receipt_received > 0 else None
            if received is None:
                m.set_exit_intent_status(conn, claim_id, "submitted_unknown", now_iso)
                return {"status": "unknown", "sig": sig}
            # realized = EVERYTHING the position returned (prior de-risk legs + this final leg)
            # minus what it cost. Without the derisk term a de-risked winner books as a loss.
            realized = m.realized_after_exit(conn, pid, received, entry_notional)   # None: a partial is unpriced
            price = (received / sold_qty) if sold_qty > 0 else None
            # Independently clocked (2026-09-08): a fresh read right after confirmation, not
            # `now_iso` and not `submit_ts` -- the third of three now-distinct instants.
            closed_ts = now_fn()
            if not m.book_full_exit_and_finalize(
                    conn, position_id=pid, claim_id=claim_id, sig=sig, closed_at=closed_ts,
                    realized_pnl=realized, exit_reason=exit_reason,
                    fill_kind=m.full_exit_fill_kind(exit_reason), price=price, qty=sold_qty,
                    notional=received, quote_price=_quoted_unit_price(quote, sold_qty),
                    **exit_route_fields(quote)):
                return {"status": "already_closing", "sig": sig, "received": received}
            # After booking, in the existing timing write: no new write, no I/O (Alpha 2026-10-07).
            timing.note_native_leg(quote)
            m.record_exit_timing(conn, pid, timing.to_json())
            # total_ms() is None when the clock could not be read. A measurement must never turn a
            # completed sell into an exception on the way out.
            _total, _pre = timing.total_ms(), timing.pre_broadcast_ms()
            return {"status": "closed", "realized": realized, "received": received, "sig": sig,
                    "timing_ms": None if _total is None else round(_total, 1),
                    "pre_broadcast_ms": None if _pre is None else round(_pre, 1)}
        if status == "failed":
            if not m.reset_position_close_submission(
                    conn, pid, claim_id, sig, now_iso, intent_status="failed"):
                return {"status": "unknown", "sig": sig}
            m.release_position_close_claim(conn, pid, claim_id)
            claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
            if not claim_id:
                return {"status": "already_closing"}
            fresh, intent = m.get_position(conn, pid), None
            continue                                   # widen slippage and retry
        # unknown / timeout: the sell may have landed -> leave open, do NOT retry this call.
        m.set_exit_intent_status(conn, claim_id, "submitted_unknown", now_iso)
        return {"status": "unknown", "sig": sig}

    # every rung failed to confirm -> real, un-exited stuck bag (stays open + flagged + alerting)
    m.release_position_close_claim(conn, pid, claim_id)
    m.mark_stuck_bag(conn, pid)
    return {"status": "stuck"}


def execute_partial_exit(conn, position, fraction, now_iso, *, quote_fn, sign_fn, send_fn,
                         confirm_fn, usdc_fn, token_balance_fn, slippage_ladder=None,
                         sleep_fn=None, reason="derisk", decision_state_id=None,
                         decision_state_id_fn=None,
                         floor_usd_fn=None, reference_value_usd_fn=None, dust_hold_fn=None, usdc_checked_fn=None,
                         receipt_first=False, receipt_fill_fn=None, source_request=None,
                         token_snapshot_fn=None):
    """Sell `fraction` of the position (the recover-the-entry de-risk leg). The position STAYS
    OPEN; proceeds are recorded as a kind='derisk' fill and added back at final close. Contract:

      - confirmed  -> record the derisk fill + set derisked_at (one-shot). Position stays open.
      - failed     -> widen slippage (next rung).
      - unknown    -> set derisked_at ANYWAY (the sell may have landed; re-firing a maybe-landed
                      partial is a double-sell that eats the riding half) but record NO fill —
                      reconcile surfaces the drift if it landed.
      - ladder exhausted -> {'status': 'derisk_failed'}: no stuck-bag (the position is healthy,
                      it just couldn't route the partial); the next sweep retries.

    `quote_fn` must quote the PARTIAL amount (the seam bakes `fraction` into the raw-atomic
    sizing); `fraction` here is used only for bookkeeping qty."""
    if slippage_ladder is None:
        from sniper.settings.memecoin import MEMECOIN_EXIT_SLIPPAGE_LADDER
        slippage_ladder = MEMECOIN_EXIT_SLIPPAGE_LADDER
    if sleep_fn is None:
        import time
        sleep_fn = time.sleep
    ledger = m
    source = source_request is not None
    if source:
        from sniper.services.memecoin_manual_exit_intents import SourcePartialExecution
        try:
            ledger = SourcePartialExecution(conn, position, source_request)
            position = ledger.position
            fraction = ledger.fraction
        except (ValueError, KeyError, TypeError):
            return {"status": "partial_failed", "reason": "invalid_source_request"}
    mint = position["mint"]
    pid = position["id"]
    if not (0.0 < float(fraction) < 1.0):
        return {"status": "derisk_failed", "reason": "fraction out of range"}
    from sniper.services.memecoin_exit_intent import (
        DERISK, PARTIAL, bind_quote, build_exit_intent, exit_floor_refused, quote_binding_complete,
        verify_exit_intent)

    # Atomic one-shot CLAIM before any submission (P0-2): a race loser refuses here, before ever
    # touching balance/quote/send. Released on every failure path below so a healthy rail that
    # simply couldn't route this sweep is retried, never permanently burned.
    fresh = ledger.get_position(conn, pid)
    claim_id = ledger.claim_position_derisk(conn, pid, now_iso)
    if not claim_id:
        return {"status": "partial_pending" if source else "already_derisked"}
    intent = None                              # WO-6: bound at the first quotable rung with balance

    def _verify_current(bound, balance, atomic=None):
        try:
            current_state_id = decision_state_id_fn() if decision_state_id_fn else None
        except Exception:
            current_state_id = None
        return verify_exit_intent(
            bound, ledger.get_position(conn, pid), balance=balance,
            current_decision_state_id=current_state_id,
            **({"balance_atomic": ledger.atomic if atomic is None else atomic} if source else {}))

    cap_accounts = None                    # see the OVERSIZE note in execute_exit
    for slip in slippage_ladder:
        try:
            tokens = ledger.snapshot(token_snapshot_fn) if source else token_balance_fn(mint)
        except Exception:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "partial_failed", "reason": "exact_balance_unavailable"}
        if source and ledger.part <= 0:
            finished = ledger.finish_noop(conn, pid, claim_id, now_iso)
            return {"status": "partial_noop" if finished else "partial_pending",
                    "reason": "partial_amount_zero"}
        for _ in range(3):
            if tokens > 0:
                break
            sleep_fn(1.0)
            try:
                tokens = token_balance_fn(mint)
            except Exception:
                ledger.release_position_derisk_claim(conn, pid, claim_id)
                return {"status": "partial_failed", "reason": "exact_balance_unavailable"}
        if tokens <= 0:
            # nothing to de-risk; let the FULL exit path own 'gone' semantics on its next sweep.
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "derisk_failed", "reason": "no balance"}
        try:
            quote = _quote(quote_fn, mint, tokens, slip, cap_accounts,
                           atomic=ledger.part if source else None)
        except TokenBalanceReadError:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "partial_failed", "reason": "exact_balance_unavailable"}
        if not quote:
            continue
        if source:
            try:
                exact = int(quote.get("inAmount", quote.get("sellAmount", 0))) == ledger.part
            except (ValueError, TypeError):
                exact = False
            if not exact:
                ledger.release_position_derisk_claim(conn, pid, claim_id)
                return {"status": "partial_failed", "reason": "partial_quote_amount_mismatch"}
        # WO-6: bind + re-verify the intent before signing (same decision-supersession / double-sell
        # guard as the full exit; the derisk one-shot claim already blocks a concurrent partial).
        if intent is None:
            intent = bind_quote(build_exit_intent(
                fresh, claim_id=claim_id, action=PARTIAL if source else DERISK, fraction=float(fraction),
                balance=tokens, decision_state_id=decision_state_id,
                **(ledger.intent_fields() if source else {})), quote, quote_time=now_iso)
            if not quote_binding_complete(intent):
                intent = None
                continue
            # Same floor sanity as the full exit, against a value derived OUTSIDE this quote.
            # `tokens * float(fraction)` is what is ACTUALLY sold: valuing the whole bag on a partial
            # would refuse legitimate small de-risks (a 4%% de-risk reads as 25x).
            held = (dust_hold_fn(intent, quote, fresh, tokens * float(fraction))
                    if dust_hold_fn else None)
            if held:
                ledger.release_position_derisk_claim(conn, pid, claim_id)
                return {"status": "dust_route_hold", "reason": held}
            if exit_floor_refused(intent, quote, fresh, tokens * float(fraction),
                                  floor_usd_fn=floor_usd_fn,
                                  reference_value_usd_fn=reference_value_usd_fn,
                                  logger=logger, position_id=pid):
                intent = None
                continue
            if not ledger.bind_exit_intent(conn, pid, claim_id, intent.to_json()):
                return {"status": "partial_pending" if source else "already_derisked", "reason": "claim ownership lost"}
        refusal = _verify_current(intent, tokens)
        if refusal:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "superseded", "reason": refusal}
        # CHECKED pre-read — same defect class as the full exit (pos#38107): a silent-zero pre
        # books the whole wallet as derisk proceeds. Abort before signing; the next sweep retries.
        try:
            usdc_pre = (usdc_checked_fn or usdc_fn)()
        except Exception:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "derisk_failed", "reason": "usdc balance unreadable"}
        try:
            raw_balance, _, signing_balance = ledger.read_tokens(token_snapshot_fn) if source else (None, None, token_balance_fn(mint))
        except Exception:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "partial_failed", "reason": "exact_balance_unavailable"}
        refusal = _verify_current(intent, signing_balance, atomic=raw_balance)
        if refusal:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "superseded", "reason": refusal}
        signed = sign_fn(quote)
        if not signed:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            claim_id = ledger.claim_position_derisk(conn, pid, now_iso)
            if not claim_id:
                return {"status": "partial_pending" if source else "already_derisked"}
            fresh, intent = ledger.get_position(conn, pid), None
            continue
        signed_tx, sig = signed[:2]
        if cap_accounts is None and _oversized(signed_tx) and _exit_retry_accounts():
            cap_accounts = _exit_retry_accounts()
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            claim_id = ledger.claim_position_derisk(conn, pid, now_iso)
            if not claim_id:
                return {"status": "partial_pending" if source else "already_derisked"}
            fresh, intent = ledger.get_position(conn, pid), None
            continue
        # The signed identity is durable BEFORE broadcast. A crash after send therefore remains
        # submitted-unknown and can never be mistaken for a releasable unsigned claim.
        validity = signed[2] if len(signed) > 2 and isinstance(signed[2], dict) else {}
        submitted_intent = intent.with_validity(
            blockhash=validity.get("blockhash"),
            last_valid_height=validity.get("last_valid_height")).with_signed_payload(
                signed_tx, tx_identity=sig)
        if not ledger.mark_position_derisk_submitted(
                conn, pid, claim_id, now_iso, sig,
                expected_intent_json=intent.to_json(),
                submitted_intent_json=submitted_intent.to_json(),
                expected_controller_revision=intent.controller_revision):
            refusal = _verify_current(intent, tokens)
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return ({"status": "superseded", "reason": refusal} if refusal else
                    {"status": "partial_pending" if source else "already_derisked", "reason": "claim ownership lost"})
        intent = submitted_intent
        send_result = send_fn(signed_tx)
        if send_result is False:
            if not ledger.reset_position_derisk_submission(
                    conn, pid, claim_id, sig, now_iso, intent_status="dropped"):
                return {"status": "unknown", "sig": sig}
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            claim_id = ledger.claim_position_derisk(conn, pid, now_iso)
            if not claim_id:
                return {"status": "partial_pending" if source else "already_derisked"}
            fresh, intent = ledger.get_position(conn, pid), None
            continue
        status = confirm_fn(sig)
        if status == "confirmed":
            # CHECKED post-read (see full exit): confirmed but unpriceable -> durable
            # submitted_unknown; recovery settles it from chain evidence. Never book garbage.
            received, sold_qty = None, None
            proof = ledger.receipt(receipt_fill_fn, sig, mint, quote) if source else None
            if source:
                if proof is not None:
                    received = float(proof['received'])
                    sold_qty = proof['sold_atomic'] / 10 ** proof['decimals']
            elif receipt_first:
                proof = _verified_receipt_fill(receipt_fill_fn, sig, mint, quote)
                if proof is not None:
                    received, sold_qty = proof
            else:
                for attempt in range(4):
                    try:
                        received = (usdc_checked_fn or usdc_fn)() - usdc_pre
                    except Exception:
                        received = None
                    if received is not None and received > 0:
                        break
                    if attempt < 3:
                        sleep_fn(1.0)
                sold_qty = tokens * float(fraction)
            if received is None:
                ledger.set_exit_intent_status(conn, claim_id, "submitted_unknown", now_iso)
                return {"status": "unknown", "sig": sig}
            price = (received / sold_qty) if sold_qty > 0 else None
            # fill + stamp in ONE transaction: a landed de-risk must never exist unstamped,
            # or the one-shot can fire again into an already-sold bag.
            booked = ledger.book_derisk_fill(conn, position_id=pid, at=now_iso, price=price, qty=sold_qty,
                               quote_price=_quoted_unit_price(quote, sold_qty),
                               notional=received, fraction=float(fraction), tx_sig=sig,
                               claim_id=claim_id,
                               **({"sold_atomic": proof["sold_atomic"], "sell_decimals": proof["decimals"]}
                                  if source else exit_route_fields(quote)))
            if source and not booked:
                return {"status": "partial_pending", "sig": sig}
            return {"status": "partial_sold" if source else "derisked", "received": received, "sig": sig}
        if status == "failed":
            if not ledger.reset_position_derisk_submission(
                    conn, pid, claim_id, sig, now_iso, intent_status="failed"):
                return {"status": "unknown", "sig": sig}
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            claim_id = ledger.claim_position_derisk(conn, pid, now_iso)
            if not claim_id:
                return {"status": "partial_pending" if source else "already_derisked"}
            fresh, intent = ledger.get_position(conn, pid), None
            continue
        # Unknown is already durable as claim-owned submitted state. Recovery alone may settle
        # or re-arm it after chain evidence; this executor must never submit it again.
        ledger.set_exit_intent_status(conn, claim_id, "submitted_unknown", now_iso)
        return {"status": "unknown", "sig": sig}

    ledger.release_position_derisk_claim(conn, pid, claim_id)
    return {"status": "derisk_failed", "reason": "ladder exhausted"}

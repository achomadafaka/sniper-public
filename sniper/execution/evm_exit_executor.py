"""Item 9 — Real EVM exit executor (S3-EVM, DISARMED build). Exit is scarier than entry.

`execute_exit_evm` sells a real position's ENTIRE token balance, prices realized PnL from the on-chain
USDC delta (never the quote), and walks a slippage ladder (e.g. 3% -> 10% -> 25%) on failure. Contract
mirrors the Solana exit, plus one EVM step: an EXACT-AMOUNT approve() of the TOKEN (not USDC) for the
router before the sell.

  - confirmed        -> finalize the claimed close, realized = usdc_received - entry_notional, record an
                        exit-reason-specific fill with its tx hash.
  - no route / failed -> widen slippage (next rung).
  - unknown/timeout  -> LEAVE it open with durable signed identity; later sweeps return
                        already_closing until reconciliation.
  - ladder exhausted -> mark a STUCK BAG (stays open + flagged + alerting), NEVER a fake clean close.

Injected seams (quote/approve/sign/send/confirm/balances) so every branch is unit-tested without a
live endpoint. NOT integration-tested — a live smoke test must precede any arm.
"""
from __future__ import annotations

import base64
import json
import logging

from sniper.persistence import db_memecoin as m

logger = logging.getLogger("memecoin")


def _receipt_proceeds(receipt_proceeds_fn, tx_hash):
    if receipt_proceeds_fn is None:
        return None
    try:
        received = float(receipt_proceeds_fn(tx_hash))
    except Exception:
        return None
    return received if received > 0 else None


def _confirmed_proceeds(usdc_fn, usdc_pre, sleep_fn, receipt_proceeds_fn, tx_hash,
                        *, require_receipt=False):
    received = _receipt_proceeds(receipt_proceeds_fn, tx_hash)
    if received is not None:
        return received
    if require_receipt:
        # THE SELL'S OWN RECEIPT ONLY (Alpha ruling 2026-10-06): a node a block behind is read
        # again, never answered by the wallet balance window, which carries every other transfer.
        for _ in range(3):
            sleep_fn(1.0)
            received = _receipt_proceeds(receipt_proceeds_fn, tx_hash)
            if received is not None:
                return received
        return None
    for attempt in range(4):
        try:
            received = float(usdc_fn()) - float(usdc_pre)
        except Exception:
            received = None
        if received is not None and received > 0:
            return received
        if attempt < 3:
            sleep_fn(1.0)
    return None


def _close_unpriced(conn, pid, claim_id, tx_hash, exit_reason, tokens, mint, closed_at):
    """A CONFIRMED sell whose proceeds its receipt cannot prove (Alpha ruling 2026-10-06): the
    tokens are gone, so the position CLOSES now, with realized, price and notional NULL (never 0)
    and fill_source 'chain_confirmed_unpriced', the state recover_unverified_closes reconciles
    from the receipt later, once (reconcile_unpriced_full_exit). Never left open for the rails to
    sell again. One alert; the booking source is logged at WARNING (the runner drops INFO)."""
    settled = m.book_full_exit_and_finalize(
        conn, position_id=pid, claim_id=claim_id, sig=tx_hash, closed_at=closed_at,
        realized_pnl=None, exit_reason=exit_reason, fill_kind=m.full_exit_fill_kind(exit_reason),
        price=None, qty=tokens, notional=None, fill_source="chain_confirmed_unpriced")
    if not settled:
        return {"status": "already_closing", "tx_hash": tx_hash}
    logger.warning("evm.exit_proceeds pos=%s tx=%s source=unknown closed_unpriced=1", pid, tx_hash)
    try:
        from sniper.services.memecoin_alerts import send_alert
        send_alert("EXIT-UNPRICED", "pos#%s %s %s sold (tx %s) but its receipt does not prove the "
                   "proceeds: closed with realized unknown, the recovery books it from the receipt"
                   % (pid, mint, exit_reason, tx_hash))
    except Exception:
        logger.exception("exit unpriced alert failed pos=%s", pid)
    return {"status": "closed", "realized": None, "received": None, "tx_hash": tx_hash,
            "proceeds_source": "unknown"}


def _quoted_unit_price(quote, tokens, quote_asset_usd, quote_asset_decimals):
    """USD per token that the SIGNED exit route was quoted at, for the quote-vs-fill column.

    An EVM exit settles in the chain's quote token (WETH on 4663, USDC elsewhere), so `buyAmount`
    is quote-token atomic and needs a price. `quote_asset_usd` is the SAME snapshot that priced
    `received` -- `_execution_balance_usd_fn` returns the balance reader and its price together and
    the seam already passes it here -- so this adds NO network read to the exit critical path.

    That shared snapshot is also why a slightly stale price costs nothing: the identical factor
    appears in `received` and in `expected`, so it divides out of fill/quote and the percentage is
    exact. Only the absolute `quote_price` carries the snapshot's own error.

    `quote` is the object signed in this iteration, so it is the executed route rather than a later
    re-quote. Anything missing or unparseable yields None: a measurement must never turn a confirmed
    sell into an exception on the way out.
    """
    try:
        out = int((quote or {}).get("buyAmount") or 0)
        if out <= 0 or not tokens or float(tokens) <= 0:
            return None
        if not quote_asset_usd or quote_asset_decimals is None:
            return None
        expected = (out / (10 ** int(quote_asset_decimals))) * float(quote_asset_usd)
        return (expected / float(tokens)) if expected > 0 else None
    except (TypeError, ValueError, ZeroDivisionError, AttributeError):
        return None


def _waited(approval):
    """A successful approval we must treat as WAITED: anything but a pipelined dict. A pipelined
    approval signs the quote at nonce+1 at once; every other success may have waited for a receipt
    and leaves the quote stale (BUNKER 72163). Inverted on purpose (Gamma, review of ac08bd938):
    an approver that answers a hash or a plain dict re-quotes rather than signs a stale floor."""
    return bool(approval) and not (isinstance(approval, dict) and approval.get("pipelined"))


def execute_exit_evm(conn, position, exit_reason, now_iso, *, quote_fn, approve_fn, sign_fn, send_fn,
                     confirm_fn, usdc_fn, token_balance_fn, slippage_ladder=None, sleep_fn=None,
                     allowance_fn=None,
                     decision_state_id=None, decision_state_id_fn=None,
                     floor_usd_fn=None, reference_value_usd_fn=None, dust_hold_fn=None,
                     receipt_proceeds_fn=None, quote_miss_fn=None, lifi_quote_fn=None,
                     terminal_finalize_fn=None, quote_asset_usd=None,
                     quote_asset_decimals=None,
                     partial_exit_fn=None, partial_fraction=0.5, prepared_preview=None, now_fn=None,
                     quote_check_fn=None, token_decimals_fn=None,
                     require_receipt_proceeds=False):
    """Execute one real EVM exit. Returns {status: closed|stuck|unknown|gone|superseded, ...}."""
    from sniper.services.memecoin_exit_intent import (
        CLOSE, bind_quote, build_exit_intent, exit_floor_refused, quote_binding_complete,
        verify_exit_intent)
    if slippage_ladder is None:
        from sniper.settings.memecoin import MEMECOIN_EXIT_SLIPPAGE_LADDER
        slippage_ladder = MEMECOIN_EXIT_SLIPPAGE_LADDER
    if sleep_fn is None:
        import time
        sleep_fn = time.sleep
    # MEASUREMENT ONLY, mirroring the Solana exit fix (2026-09-08). `now_iso` stays the
    # decision-time value everywhere it was already used -- unchanged. `now_fn`, when supplied, is
    # called FRESH at close_submitted_at (before broadcast) and closed_at (after confirmation).
    # Unset, it falls back to reusing `now_iso`: byte-identical behavior for a caller that does not
    # opt in. See tests/test_evm_exit_executor_timestamps.py.
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

    # DOUBLE-SUBMIT GUARD (HIGH-2) — re-read is_open FRESH (the `position` dict is stale). Execution
    # is serialized per (wallet,chain), so a concurrent winner's claim_close is committed before we
    # get here; seeing it, abort BEFORE broadcasting a sell for tokens already gone.
    fresh = m.get_position(conn, pid)
    if not fresh or not fresh.get("is_open"):
        return {"status": "already_closed"}
    claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
    if not claim_id:
        return {"status": "already_closing"}
    intent = None                              # WO-6: bound at the first quotable rung with balance
    last_quote_failure = None
    retry_quote, retry_tokens = None, None
    approved = None                            # (spender, amount) a waited approval mined

    def _prepared_quote(tokens, slip):
        source = getattr(prepared_preview, "preview", prepared_preview)
        if isinstance(source, dict):
            amount = source.get("amount_atomic")
            quote = source.get("quote")
            evidence = source.get("quote_evidence")
        else:
            amount = getattr(source, "amount_atomic", None)
            quote = getattr(source, "quote", None)
            evidence = getattr(source, "quote_evidence", None)
        # SAME SLIPPAGE (Codex P1, 2026-09-13). The prepared transaction's calldata carries the
        # minimum output of the slippage it was quoted at; no metadata change can tighten it. Reuse
        # only a preview quoted at this rung's slippage; anything else, or unknown, re-quotes.
        try:
            if int((evidence or {}).get("slippage_bps")) != int(round(float(slip) * 100)):
                return None
        except (TypeError, ValueError, AttributeError):
            return None
        try:
            matching_balance = int(amount) == int(tokens)
        except (TypeError, ValueError):
            matching_balance = False
        # SAME UNIT (2026-09-11, Gamma D7). The preview stores the RAW atomic balance and the
        # executor reads a HUMAN one, so this never matched when decimals > 0 and every EVM exit
        # paid a second full venue-ladder quote at rung 0. Convert through the token's decimals.
        if not matching_balance and token_decimals_fn is not None:
            try:
                dec = token_decimals_fn(position["mint"])
                human = int(amount) / (10 ** int(dec))
                matching_balance = dec is not None and human > 0 and \
                    abs(human - float(tokens)) <= human * 1e-9
            except (TypeError, ValueError, KeyError, OverflowError):
                matching_balance = False
        if not matching_balance or not isinstance(quote, dict):
            return None
        if not isinstance(quote.get("transaction"), dict):
            return None
        return quote

    def _verify_current(bound, balance):
        try:
            current_state_id = decision_state_id_fn() if decision_state_id_fn else None
        except Exception:
            current_state_id = None
        return verify_exit_intent(
            bound, m.get_position(conn, pid), balance=balance,
            current_decision_state_id=current_state_id)

    requote = []

    def _rungs():
        """Each rung once, and once more right after a waited approval (`requote`)."""
        for item in enumerate(slippage_ladder):
            yield item
            if requote:
                requote.clear()
                yield item
                requote.clear()          # at most one re-quote per rung; never carried forward

    for rung, slip in _rungs():
        tokens = token_balance_fn(mint)
        # erc20_balance fails CLOSED to 0.0 — one bad RPC read must not book a -100% 'gone'
        # close while the tokens still sit in the wallet. Bounded re-read (Solana's pattern)
        # before believing zero.
        for _ in range(3):
            if tokens > 0:
                break
            sleep_fn(1.0)
            tokens = token_balance_fn(mint)
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
            if position.get("lifi_dependent"):
                return {"status": "unknown", "reason": "lifi_settlement_required"}
            m.release_position_close_claim(conn, pid, claim_id)
            m.mark_stuck_bag(conn, pid)
            return {"status": "stuck", "reason": "token_balance_zero"}
        timing.rung()
        quote = _prepared_quote(tokens, slip) if rung == 0 else None
        if quote is None and retry_quote is not None and tokens == retry_tokens:
            refresh = retry_quote.get("_requote")
            if callable(refresh):
                from sniper.execution.evm_swap_executor import _requote_matches
                allowance = (retry_quote.get("issues") or {}).get("allowance") or {}
                spender = allowance.get("spender") or (retry_quote.get("transaction") or {}).get("to")
                try:
                    candidate = refresh(slippage_bps=int(round(float(slip) * 100)))
                    if (isinstance(candidate, dict)
                            and isinstance(candidate.get("transaction"), dict)
                            and _requote_matches(candidate, retry_quote.get("sellAmount"), spender)):
                        quote = candidate
                except Exception:
                    pass
        retry_quote, retry_tokens = None, None
        if quote is None:
            quote = quote_fn(mint, tokens, slip)
        timing.stage("quote")
        if not quote:
            miss = {}
            if quote_miss_fn is not None:
                try:
                    miss = quote_miss_fn(slip) or {}
                except Exception:
                    miss = {}
            code = miss.get("code") if isinstance(miss, dict) else None
            if code:
                last_quote_failure = str(code)
            # WHICH VENUE REFUSED (2026-09-23, Lambda). The miss carries the per-rung trace and
            # only the code was kept; a stuck bag then could not say which venue said no.
            try:
                venues = ",".join(str(r.get("rung") or "?") + ("" if r.get("hit") else "!")
                                  for r in (miss.get("rungs") or ()) if isinstance(r, dict))
            except Exception:
                venues = "?"
            logger.warning("exit quote miss pos=%s slip=%s code=%s rungs=%s detail=%s",
                           pid, slip, code, venues or "none",
                           (miss.get("detail") if isinstance(miss, dict) else None) or "-")
            if lifi_quote_fn is not None and position.get("lifi_dependent"):
                if code == "no_route":
                    return lifi_quote_fn(
                        position=position, claim_id=claim_id, exit_reason=exit_reason,
                        slippage_ladder=tuple(slippage_ladder), fraction=1.0,
                        direct_miss=miss, now_iso=now_iso,
                        quote_check_fn=quote_check_fn,
                    )
                if code:
                    m.release_position_close_claim(conn, pid, claim_id)
                    return {
                        "status": "deferred", "reason": "direct_" + code,
                        "failure_type": code,
                    }
            continue                                          # no route at this rung -> widen
        try:
            sell_atomic = int(quote.get("sellAmount") or 0)
        except (TypeError, ValueError):
            sell_atomic = 0
        tx = quote.get("transaction")
        if not tx:
            # NAME THE SILENT SKIP (2026-09-23). RATBRAIN 64081 walked three tiers, quoted at each,
            # signed nothing and reported `stuck` with no failure type, because the skips below
            # said nothing. A skip that leaves no code is a stuck bag nobody can explain.
            last_quote_failure = "quote_without_transaction"
            logger.warning("exit quote without a transaction pos=%s slip=%s protocol=%s",
                           pid, slip, (quote.get("route") or {}).get("protocol"))
            continue
        if quote_check_fn is not None:
            try:
                allowed, detail = quote_check_fn(m.get_position(conn, pid), quote, tokens)
            except Exception:
                logger.exception("execution quote check failed pos=%s", pid)
                allowed, detail = True, {"code": "execution_quote_check_failed_sweep_verdict_stands"}  # D2: unknown, not hold
            if not allowed:
                if not m.release_position_close_claim(conn, pid, claim_id):
                    return {"status": "already_closing"}
                return {"status": "deferred", "reason": detail["code"]}
        # WO-6: bind + re-verify the intent before signing — refuse a superseded decision / lost
        # claim / position closed mid-ladder rather than broadcast a stale EVM sell.
        if intent is None:
            intent = bind_quote(build_exit_intent(
                fresh, claim_id=claim_id, action=CLOSE, fraction=1.0,
                balance=tokens, decision_state_id=decision_state_id), quote, quote_time=now_iso,
                quote_asset_usd=quote_asset_usd)
            if not quote_binding_complete(intent):
                last_quote_failure = "quote_binding_incomplete"
                logger.warning("exit quote binding incomplete pos=%s slip=%s protocol=%s "
                               "sellAmount=%s minBuyAmount=%s", pid, slip,
                               (quote.get("route") or {}).get("protocol"),
                               quote.get("sellAmount"), quote.get("minBuyAmount"))
                intent = None
                continue
            # The floor must be plausible against a value derived OUTSIDE this quote. A quote
            # taken during a corrupted-tape window is internally consistent and still authorises
            # selling the bag for nothing (UNIFROG 27097: ~$6 bag, $0.02 floor, $0.02 received).
            # Refusing here costs one sweep; signing costs the position.
            held = dust_hold_fn(intent, quote, fresh, tokens) if dust_hold_fn else None
            if held:
                # DUST-ROUTE HOLD (memecoin_exit_dust_guard): sign nothing, keep the bag.
                m.release_position_close_claim(conn, pid, claim_id)
                return {"status": "dust_route_hold", "reason": held}
            if exit_floor_refused(intent, quote, fresh, tokens,
                                  floor_usd_fn=floor_usd_fn,
                                  reference_value_usd_fn=reference_value_usd_fn,
                                  logger=logger, position_id=pid):
                last_quote_failure = "exit_floor_refused"
                intent = None
                continue
            if not m.bind_exit_intent(conn, pid, claim_id, intent.to_json()):
                return {"status": "already_closing"}
        refusal = _verify_current(intent, tokens)
        if refusal:
            m.release_position_close_claim(conn, pid, claim_id)
            return {"status": "superseded", "reason": refusal}
        # Approval is itself an on-chain funded side effect. It belongs after the decision/claim
        # re-verification, never before it.
        allow = (quote.get("issues") or {}).get("allowance")
        approval = None
        if allow and allow.get("spender"):
            try:
                approval_needed = int(allowance_fn(mint, allow["spender"])) < sell_atomic
            except Exception:
                # unreadable: approve, unless our waited approval for this amount just mined
                approval_needed = (allow["spender"], sell_atomic) != approved
            approval = approve_fn(mint, allow["spender"], sell_atomic) if approval_needed else None
            timing.stage("approve")
            if approval_needed and not approval:
                m.release_position_close_claim(conn, pid, claim_id)
                claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
                if not claim_id:
                    return {"status": "already_closing"}
                fresh, intent = m.get_position(conn, pid), None
                continue
            if _waited(approval):
                approved = (allow["spender"], sell_atomic)
        refusal = _verify_current(intent, tokens)
        if refusal:
            m.release_position_close_claim(conn, pid, claim_id)
            return {"status": "superseded", "reason": refusal}
        if _waited(approval):
            # WAITED APPROVAL (BUNKER 72163, 2026-10-08). A non-pipelined success (`_waited`) is a
            # receipt we waited for (chain 1 17.3 s, Base p90 65 s); a pipelined dict signs at
            # nonce+1 at once. The quote, its minimum output and its deadline predate the wait, so
            # signing it sold into a 5.5% fall with a stale floor and Protect held the reverting
            # swap to its deadline. The decision was re-verified above; quote this rung again.
            prepared_preview = None
            m.release_position_close_claim(conn, pid, claim_id)
            claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
            if not claim_id:
                return {"status": "already_closing"}
            fresh, intent = m.get_position(conn, pid), None
            requote.append(True)
            continue
        usdc_pre = usdc_fn()
        signing_balance = token_balance_fn(mint)
        refusal = _verify_current(intent, signing_balance)
        if refusal:
            m.release_position_close_claim(conn, pid, claim_id)
            return {"status": "superseded", "reason": refusal}
        signed = sign_fn(tx)
        timing.stage("sign")
        timing.note_quote(quote)      # E10 phase 1: the ranking behind the quote signed (no I/O)
        if not signed:
            m.release_position_close_claim(conn, pid, claim_id)
            claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
            if not claim_id:
                return {"status": "already_closing"}
            fresh, intent = m.get_position(conn, pid), None
            continue
        raw, tx_hash, nonce = signed
        submitted_intent = intent.with_signed_payload(raw, tx_identity=tx_hash, nonce=nonce)
        # Independently clocked (2026-09-08): fresh, not the decision-time `now_iso`.
        submit_ts = now_fn()
        if not m.mark_position_close_submitted(
                conn, pid, claim_id, submit_ts, tx_hash,
                expected_intent_json=intent.to_json(),
                # The signed rung rides beside the intent (not an ExitIntent field): a chain-1
                # Protect replacement widens from it (Gamma LOW-3 on 657e12117).
                submitted_intent_json=json.dumps({**json.loads(submitted_intent.to_json()),
                                                  "signed_slippage_pct": float(slip)},
                                                 sort_keys=True),
                expected_controller_revision=intent.controller_revision):
            refusal = _verify_current(intent, tokens)
            m.release_position_close_claim(conn, pid, claim_id)
            return ({"status": "superseded", "reason": refusal}
                    if refusal else {"status": "already_closing"})
        intent = submitted_intent
        # Signed identity and submitted_unknown were committed atomically before this network call.
        send_result = send_fn(raw)
        timing.stage("submit")
        if send_result is False:
            if not m.reset_position_close_submission(
                    conn, pid, claim_id, tx_hash, now_iso, intent_status="dropped"):
                return {"status": "unknown", "tx_hash": tx_hash}
            m.release_position_close_claim(conn, pid, claim_id)
            claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
            if not claim_id:
                return {"status": "already_closing"}
            fresh, intent = m.get_position(conn, pid), None
            continue                                          # positively rejected: safe to re-sign
        status = confirm_fn(tx_hash)
        timing.stage("confirm")
        if status == "confirmed":
            received = _confirmed_proceeds(
                usdc_fn, usdc_pre, sleep_fn, receipt_proceeds_fn, tx_hash,
                require_receipt=require_receipt_proceeds,
            )
            timing.stage("proceeds")
            if received is None and require_receipt_proceeds and not position.get("lifi_dependent"):
                return _close_unpriced(conn, pid, claim_id, tx_hash, exit_reason, tokens, mint,
                                       now_fn())
            if received is None:
                m.set_exit_intent_status(conn, claim_id, "submitted_unknown", now_iso)
                return {"status": "unknown", "tx_hash": tx_hash}
            # realized = EVERYTHING the position returned (prior de-risk legs + this final leg)
            # minus what it cost. Without the derisk term a de-risked winner books as a LOSS —
            # flagged against this file on 2026-07-11 and dormant only because no EVM derisk_fn was
            # wired. The EVM leg trades for real now, so it is fixed here rather than left armed.
            realized = m.realized_after_exit(conn, pid, received, entry_notional)   # None: a partial is unpriced
            price = (received / tokens) if tokens > 0 else None
            # Independently clocked (2026-09-08): fresh, not `now_iso` and not `submit_ts` --
            # the third of three now-distinct instants.
            closed_ts = now_fn()
            exit_kwargs = {
                "position_id": pid, "claim_id": claim_id, "sig": tx_hash,
                "closed_at": closed_ts, "realized_pnl": realized,
                "exit_reason": exit_reason, "fill_kind": m.full_exit_fill_kind(exit_reason),
                "price": price, "qty": tokens, "notional": received,
                "quote_price": _quoted_unit_price(
                    quote, tokens, quote_asset_usd, quote_asset_decimals),
            }
            if position.get("lifi_dependent"):
                settled = terminal_finalize_fn(exit_kwargs) if terminal_finalize_fn else False
            else:
                settled = m.book_full_exit_and_finalize(conn, **exit_kwargs)
            if not settled:
                return {"status": "already_closing", "tx_hash": tx_hash, "received": received}
            timing.stage("settlement")
            m.record_exit_timing(conn, pid, timing.to_json())
            # total_ms() is None when the clock could not be read; a measurement must never turn a
            # completed sell into an exception on the way out.
            _total, _pre = timing.total_ms(), timing.pre_broadcast_ms()
            return {"status": "closed", "realized": realized, "received": received,
                    "tx_hash": tx_hash,
                    "timing_ms": None if _total is None else round(_total, 1),
                    "pre_broadcast_ms": None if _pre is None else round(_pre, 1)}
        if status == "failed":
            if not m.reset_position_close_submission(
                    conn, pid, claim_id, tx_hash, now_iso, intent_status="failed"):
                return {"status": "unknown", "tx_hash": tx_hash}
            m.release_position_close_claim(conn, pid, claim_id)
            claim_id = m.claim_position_close(conn, pid, now_iso, exit_reason)
            if not claim_id:
                return {"status": "already_closing"}
            fresh, intent = m.get_position(conn, pid), None
            retry_quote, retry_tokens = quote, tokens
            continue                                          # widen slippage and retry
        # unknown / timeout: the sell may have landed -> leave open, do NOT retry this call.
        m.set_exit_intent_status(conn, claim_id, "submitted_unknown", now_iso)
        return {"status": "unknown", "tx_hash": tx_hash}

    m.release_position_close_claim(conn, pid, claim_id)
    if last_quote_failure == "route_capacity" and partial_exit_fn is not None:
        try:
            fraction = float(partial_fraction)
        except (TypeError, ValueError):
            fraction = 0.0
        if 0.0 < fraction < 1.0:
            try:
                partial = partial_exit_fn(fraction) or {}
            except Exception:
                logger.exception("route-capacity partial exit failed pos=%s", pid)
            else:
                if partial.get("status") in {"derisked", "unknown"}:
                    return dict(partial, fallback_reason="route_capacity",
                                fallback_fraction=fraction)
                if partial.get("status") == "deferred":
                    return partial
    logger.warning("sell ladder exhausted pos=%s mint=%s tiers=%s last_failure=%s -> stuck bag",
                   pid, mint, list(slippage_ladder), last_quote_failure)
    m.mark_stuck_bag(conn, pid)
    # FORGET THE ROUTE THAT WILL NOT EXECUTE (2026-09-04). The ladder retries the SAME stored
    # route at smaller sizes, which cannot help when the route ITSELF is the problem. FROGE 50786
    # is the case: both multihop legs quoted cleanly from 100% down to 1%, but its only path to
    # native ran through a pool carrying hook 0x4e3468951d and every swap reverted on chain with no
    # reason after burning 899k gas. Five attempts, five identical failures, and the position then
    # blocked deploys as well.
    #
    # Clearing `evm_route_json` makes the NEXT sweep rediscover from scratch instead of reusing the
    # one that reverts. It sells nothing and decides nothing -- the rails still choose, and if the
    # only route really is the bad one, rediscovery finds it again and we are exactly where we were.
    # The entry/exit venue symmetry this normally preserves is worth less than an exit that works.
    try:
        conn.execute("UPDATE memecoin_positions SET evm_route_json=NULL WHERE id=?", (pid,))
        conn.commit()
    except Exception:
        logger.exception("could not clear the stored route for stuck pos=%s", pid)
    result = {"status": "stuck"}
    if last_quote_failure:
        result["failure_type"] = last_quote_failure
    return result


def execute_partial_exit_evm(conn, position, fraction, now_iso, *, quote_fn, approve_fn,
                             sign_fn, send_fn, confirm_fn, usdc_fn, token_balance_fn,
                             slippage_ladder=None, sleep_fn=None, decision_state_id=None,
                             decision_state_id_fn=None,
                             floor_usd_fn=None, reference_value_usd_fn=None, dust_hold_fn=None,
                             receipt_proceeds_fn=None, quote_miss_fn=None,
                             lifi_quote_fn=None, quote_asset_usd=None,
                             quote_asset_decimals=None, quote_check_fn=None,
                             require_receipt_proceeds=False, source_request=None, token_snapshot_fn=None,
                             receipt_fill_fn=None):
    """Sell `fraction` of an EVM position (the recover-the-entry de-risk leg). The position
    STAYS OPEN; proceeds are recorded as a kind='derisk' fill and added back at final close.
    Mirrors the Solana execute_partial_exit contract (2026-07-17 — until now the EVM leg had
    NO derisk executor at all: 'venue for pos=… has no derisk_fn (skipped)'):

      - confirmed        -> record the derisk fill + set derisked_at (one-shot). Stays open.
      - failed/no-route  -> widen slippage (next rung).
      - unknown          -> set derisked_at + persist derisk_sig (recovery pass settles it;
                            re-firing a maybe-landed partial is a double-sell) — book NO fill.
      - ladder exhausted -> {'status': 'derisk_failed'}: no stuck-bag (position is healthy,
                            the partial just couldn't route); the next sweep retries.

    `quote_fn(mint, tokens, slip, fraction)` must size the quote as fraction × the EXACT raw
    balance (the seam owns 2^53-safe atomic sizing); `fraction` here is bookkeeping qty only."""
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

    fresh = ledger.get_position(conn, pid)
    if not fresh or not fresh.get("is_open"):
        return {"status": "already_closed"}

    # Atomic one-shot CLAIM before any submission (P0-2, mirrors solana_exit_executor): a race
    # loser refuses here, before touching balance/quote/send. Released on every failure path below.
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

    requote, approved = [], None

    def _rungs():
        """Each rung once, and once more right after a waited approval (the full exit's rule)."""
        for item in slippage_ladder:
            yield item
            if requote:
                requote.clear()
                yield item
                requote.clear()

    for slip in _rungs():
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
            tokens = token_balance_fn(mint)
        if tokens <= 0:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "derisk_failed", "reason": "no balance"}
        quote = (quote_fn(mint, tokens, slip, fraction, atomic=ledger.part) if source
                 else quote_fn(mint, tokens, slip, fraction))
        if not quote:
            miss = {}
            if quote_miss_fn is not None:
                try:
                    miss = quote_miss_fn(slip) or {}
                except Exception:
                    miss = {}
            code = miss.get("code") if isinstance(miss, dict) else None
            if lifi_quote_fn is not None and position.get("lifi_dependent"):
                if code == "no_route":
                    return lifi_quote_fn(
                        position=position, claim_id=claim_id, exit_reason="derisk",
                        slippage_ladder=tuple(slippage_ladder), fraction=float(fraction),
                        direct_miss=miss, now_iso=now_iso,
                        quote_check_fn=quote_check_fn,
                    )
                if code:
                    ledger.release_position_derisk_claim(conn, pid, claim_id)
                    return {
                        "status": "deferred", "reason": "direct_" + code,
                        "failure_type": code,
                    }
            continue
        try:
            sell_atomic = int(quote.get("sellAmount") or 0)
        except (TypeError, ValueError):
            sell_atomic = 0
        if source:
            try:
                exact = int(quote.get("inAmount", quote.get("sellAmount", 0))) == ledger.part
            except (ValueError, TypeError):
                exact = False
            if not exact:
                ledger.release_position_derisk_claim(conn, pid, claim_id)
                return {"status": "partial_failed", "reason": "partial_quote_amount_mismatch"}
        tx = quote.get("transaction")
        if not tx:
            continue
        # WO-6: bind + re-verify the intent before signing (same guard as the full EVM exit).
        if quote_check_fn is not None:
            try:
                allowed, detail = quote_check_fn(
                    ledger.get_position(conn, pid), quote, tokens * float(fraction))
            except Exception:
                logger.exception("partial execution quote check failed pos=%s", pid)
                allowed, detail = True, {"code": "execution_quote_check_failed_sweep_verdict_stands"}  # D2: unknown, not hold
            if not allowed:
                if not ledger.release_position_derisk_claim(conn, pid, claim_id):
                    return {"status": "partial_pending" if source else "already_derisked"}
                return {"status": "deferred", "reason": detail["code"]}
        if intent is None:
            intent = bind_quote(build_exit_intent(
                fresh, claim_id=claim_id, action=PARTIAL if source else DERISK, fraction=float(fraction),
                balance=tokens, decision_state_id=decision_state_id,
                **(ledger.intent_fields() if source else {})), quote, quote_time=now_iso,
                quote_asset_usd=quote_asset_usd)
            if not quote_binding_complete(intent):
                intent = None
                continue
            # Same floor sanity as the full exit, against a value derived OUTSIDE this quote.
            # `tokens * fraction` is what is ACTUALLY sold: valuing the whole bag on a partial
            # would refuse legitimate small de-risks (a 4% de-risk reads as 25x).
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
        allow = (quote.get("issues") or {}).get("allowance")
        approval = None
        if allow and allow.get("spender") and (allow["spender"], sell_atomic) != approved:
            approval = approve_fn(mint, allow["spender"], sell_atomic)
            if not approval:
                ledger.release_position_derisk_claim(conn, pid, claim_id)
                claim_id = ledger.claim_position_derisk(conn, pid, now_iso)
                if not claim_id:
                    return {"status": "partial_pending" if source else "already_derisked"}
                fresh, intent = ledger.get_position(conn, pid), None
                continue
            if _waited(approval):
                approved = (allow["spender"], sell_atomic)
        refusal = _verify_current(intent, tokens)
        if refusal:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "superseded", "reason": refusal}
        if _waited(approval):
            # WAITED APPROVAL: the quote predates the wait; quote this rung again (BUNKER 72163,
            # see execute_exit_evm). The decision was re-verified above.
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            claim_id = ledger.claim_position_derisk(conn, pid, now_iso)
            if not claim_id:
                return {"status": "partial_pending" if source else "already_derisked"}
            fresh, intent = ledger.get_position(conn, pid), None
            requote.append(True)
            continue
        usdc_pre = usdc_fn()
        try:
            raw_balance, _, signing_balance = ledger.read_tokens(token_snapshot_fn) if source else (None, None, token_balance_fn(mint))
        except Exception:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "partial_failed", "reason": "exact_balance_unavailable"}
        refusal = _verify_current(intent, signing_balance, atomic=raw_balance)
        if refusal:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return {"status": "superseded", "reason": refusal}
        signed = sign_fn(tx)
        if not signed:
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            claim_id = ledger.claim_position_derisk(conn, pid, now_iso)
            if not claim_id:
                return {"status": "partial_pending" if source else "already_derisked"}
            fresh, intent = ledger.get_position(conn, pid), None
            continue
        raw, tx_hash, nonce = signed
        # Persist signed transaction identity before send. A process death after broadcast then
        # remains submitted-unknown and cannot be stale-released into a second partial sell.
        submitted_intent = intent.with_signed_payload(raw, tx_identity=tx_hash, nonce=nonce)
        if not ledger.mark_position_derisk_submitted(
                conn, pid, claim_id, now_iso, tx_hash,
                expected_intent_json=intent.to_json(),
                submitted_intent_json=submitted_intent.to_json(),
                expected_controller_revision=intent.controller_revision):
            refusal = _verify_current(intent, tokens)
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            return ({"status": "superseded", "reason": refusal} if refusal else
                    {"status": "partial_pending" if source else "already_derisked", "reason": "claim ownership lost"})
        intent = submitted_intent
        send_result = send_fn(raw)
        if send_result is False:
            if not ledger.reset_position_derisk_submission(
                    conn, pid, claim_id, tx_hash, now_iso, intent_status="dropped"):
                return {"status": "unknown", "tx_hash": tx_hash, "sig": tx_hash}
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            claim_id = ledger.claim_position_derisk(conn, pid, now_iso)
            if not claim_id:
                return {"status": "partial_pending" if source else "already_derisked"}
            fresh, intent = ledger.get_position(conn, pid), None
            continue
        status = confirm_fn(tx_hash)
        if status == "confirmed":
            proof = ledger.receipt(receipt_fill_fn, tx_hash, mint, quote) if source else None
            received = (float(proof['received']) if proof else None) if source else _confirmed_proceeds(
                usdc_fn, usdc_pre, sleep_fn, receipt_proceeds_fn, tx_hash,
                require_receipt=require_receipt_proceeds,
            )
            if received is None:
                ledger.set_exit_intent_status(conn, claim_id, "submitted_unknown", now_iso)
                return {"status": "unknown", "tx_hash": tx_hash, "sig": tx_hash}
            sold_qty = (proof["sold_atomic"] / 10 ** proof["decimals"]) if source else tokens * float(fraction)
            price = (received / sold_qty) if sold_qty > 0 else None
            # fill + stamp in ONE transaction (see solana_exit_executor for the rationale).
            booked = ledger.book_derisk_fill(conn, position_id=pid, at=now_iso, price=price, qty=sold_qty,
                               notional=received, fraction=float(fraction), tx_sig=tx_hash,
                               claim_id=claim_id,
                               quote_price=_quoted_unit_price(
                                   quote, sold_qty, quote_asset_usd, quote_asset_decimals),
                               **({"sold_atomic": proof["sold_atomic"], "sell_decimals": proof["decimals"]} if source else {}))
            if source and not booked:
                return {"status": "partial_pending", "sig": tx_hash}
            return {"status": "partial_sold" if source else "derisked", "received": received, "tx_hash": tx_hash}
        if status == "failed":
            if not ledger.reset_position_derisk_submission(
                    conn, pid, claim_id, tx_hash, now_iso, intent_status="failed"):
                return {"status": "unknown", "tx_hash": tx_hash, "sig": tx_hash}
            ledger.release_position_derisk_claim(conn, pid, claim_id)
            claim_id = ledger.claim_position_derisk(conn, pid, now_iso)
            if not claim_id:
                return {"status": "partial_pending" if source else "already_derisked"}
            fresh, intent = ledger.get_position(conn, pid), None
            continue
        # Unknown is already durable as claim-owned submitted state. Recovery owns resolution.
        ledger.set_exit_intent_status(conn, claim_id, "submitted_unknown", now_iso)
        return {"status": "unknown", "tx_hash": tx_hash, "sig": tx_hash}

    ledger.release_position_derisk_claim(conn, pid, claim_id)
    return {"status": "derisk_failed", "reason": "ladder exhausted"}


def replace_exit_evm(conn, position, now_iso, *, quote_fn, sign_fn, send_fn, token_balance_fn,
                     slippage_ladder, quote_asset_usd=None, floor_usd_fn=None,
                     reference_value_usd_fn=None, dust_hold_fn=None, max_replacements=2):
    """Sign a SAME-NONCE replacement of a held full exit (chain-1 Protect, operator via Alpha,
    2026-10-08). Returns {"status": "replaced", ...} or {"status": "skipped", "reason": ...}.

    Called by the recovery pass, in the exit lane, only after the status watcher saw the held send
    simulate as a revert on consecutive polls. The held bytes and the replacement share one nonce,
    so at most one of them can ever land: no double sell. NOTHING here releases the claim or
    re-arms the exit; every skip leaves the held send exactly as it was, and the existing deadline
    proof still releases it if neither lands.

    The replacement quotes the first ladder rung above the held send's signed rung (`signed_slippage_pct`,
    written at signing), else the widest: a fresh quote at the same width still replaces a stale floor.
    NO STOP RE-CHECK (operator ruling 2026-10-08 on Gamma MEDIUM-1: "Our exits should be instant,
    not rechecked"). The replacement continues an exit that was already decided and confirmed, so it
    does not run `confirm_execution_stop` on the fresh quote. Accepted risk: a coin that bounced back
    above its stop in the ~20 s before the replacement is sold anyway.
    The claim is kept, and every earlier hash goes into the intent's `replaced_tx_identities`, so
    recovery settles on whichever transaction lands.

    Not handled: a mint two open funded positions share. The replacement quotes the whole balance,
    which no longer equals the held sell amount there, so it skips (`sell_amount_changed`) and the
    held sell waits for its deadline as before (Gamma LOW-5).
    """
    from sniper.services.memecoin_exit_intent import (
        CLOSE, ExitIntent, bind_quote, build_exit_intent, exit_floor_refused,
        quote_binding_complete)

    def skip(reason):
        logger.warning("chain1 replace: skipped pos=%s reason=%s", position.get("id"), reason)
        return {"status": "skipped", "reason": reason}

    pid = position["id"]
    fresh = m.get_position(conn, pid)
    if not fresh or not fresh.get("is_open") or not fresh.get("close_claim_id") \
            or not fresh.get("close_sig"):
        return skip("no_held_close")
    if str(fresh["close_sig"]) != str(position.get("close_sig")):
        return skip("held_send_moved")             # the flag was read on another hash (LOW-6)
    held_json = fresh.get("exit_intent_json")
    try:
        held_raw = json.loads(held_json or "null")
    except (TypeError, ValueError):
        held_raw = None
    held = ExitIntent.from_json(held_json) if isinstance(held_raw, dict) else None
    if (held is None or held.action != CLOSE or held.tx_identity != fresh["close_sig"]
            or str(held.claim_id) != str(fresh["close_claim_id"])
            or not isinstance(held.nonce, int) or not held.signed_payload_b64
            or not held.sell_amount_atomic or held.balance_snapshot is None):
        return skip("held_intent_unusable")
    replaced = [str(h) for h in (held_raw.get("replaced_tx_identities") or [])]
    if len(replaced) >= int(max_replacements):
        return skip("replacement_limit")
    ladder = list(slippage_ladder or ())
    if not ladder:
        return skip("no_ladder")
    try:
        held_slip = float(held_raw.get("signed_slippage_pct"))
    except (TypeError, ValueError):
        held_slip = None
    if held_slip is None:                          # signed before the rung was recorded
        slip = ladder[min(len(replaced) + 1, len(ladder) - 1)]
    else:
        slip = next((r for r in ladder if float(r) > held_slip), max(ladder))
    try:
        tokens = token_balance_fn(position["mint"])
    except Exception:
        tokens = None
    if tokens is None or float(tokens) < float(held.balance_snapshot) * 0.999999:
        return skip("bag_changed")                 # something moved the bag: recovery decides
    quote = quote_fn(position["mint"], tokens, slip)
    tx = (quote or {}).get("transaction") if isinstance(quote, dict) else None
    if not tx:
        return skip("requote_miss")
    try:
        same_amount = int(quote.get("sellAmount")) == int(held.sell_amount_atomic)
    except (TypeError, ValueError):
        same_amount = False
    if not same_amount:
        return skip("sell_amount_changed")
    intent = bind_quote(build_exit_intent(
        fresh, claim_id=held.claim_id, action=CLOSE, fraction=1.0, balance=tokens,
        decision_state_id=held.decision_state_id), quote, quote_time=now_iso,
        quote_asset_usd=quote_asset_usd)
    if not quote_binding_complete(intent):
        return skip("quote_binding_incomplete")
    if dust_hold_fn is not None and dust_hold_fn(intent, quote, fresh, tokens):
        return skip("dust_route_hold")
    if exit_floor_refused(intent, quote, fresh, tokens, floor_usd_fn=floor_usd_fn,
                          reference_value_usd_fn=reference_value_usd_fn, logger=logger,
                          position_id=pid):
        return skip("exit_floor_refused")
    try:
        held_bytes = base64.b64decode(held.signed_payload_b64).decode()
    except Exception:
        return skip("held_payload_unreadable")
    signed = sign_fn(tx, nonce=held.nonce, replaces=held_bytes)
    if not signed:
        return skip("sign_failed")
    raw, tx_hash, nonce = signed
    if nonce != held.nonce or str(tx_hash) == str(held.tx_identity):
        return skip("sign_nonce_mismatch")
    new_json = json.dumps({
        **json.loads(intent.with_signed_payload(raw, tx_identity=tx_hash, nonce=nonce).to_json()),
        "replaced_tx_identities": replaced + [held.tx_identity],
        "signed_slippage_pct": float(slip)}, sort_keys=True)
    if not m.swap_close_identity(conn, pid, held.claim_id, held.tx_identity, tx_hash,
                                 expected_intent_json=held_json, new_intent_json=new_json,
                                 at=now_iso):
        return skip("cas_lost")
    sent = send_fn(raw)
    if sent is False:
        # Positively refused everywhere (an underpriced replacement): the held bytes stay the claim's.
        m.swap_close_identity(conn, pid, held.claim_id, tx_hash, held.tx_identity,
                              expected_intent_json=new_json, new_intent_json=held_json, at=now_iso)
        return skip("replacement_refused")
    logger.warning("chain1 replace: pos=%s nonce=%d held=%s replacement=%s slip=%s min_out=%s",
                   pid, nonce, held.tx_identity, tx_hash, slip, intent.min_output_atomic)
    return {"status": "replaced", "tx_hash": tx_hash, "replaced": held.tx_identity, "slip": slip,
            "send": sent}

"""Activated chain-4663 LI.FI callback wiring."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from types import SimpleNamespace

from sniper.execution.lifi_executor import (
    LifiAuthorityError,
    LifiClient,
    LifiLifecycle,
    approve_exact_and_wait,
    audit_receipt_block,
    execute_lifi_entry,
    execute_lifi_exit,
    finalize_lifi_failed_entry,
    finalize_lifi_unmutated_entry,
    lifi_entry_blocked,
    prove_execution_block,
    prove_quote_authority,
    revoke_and_wait,
    settle_entry,
    settle_exit,
)
from sniper.execution.evm_wallet import CHAIN_QUOTE
from sniper.execution.lifi_route import GateResult, QuotaGuard
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from sniper.util.clock import real_utc_now_iso


_QUOTE_MAX_AGE_S = 60.0


def build_lifi_request(
        side, amount, *, mint, sell_token, custody, manifest, slippage="0.03"):
    input_token, output_token = (
        (sell_token, mint) if side == "buy" else (mint, sell_token)
    )
    return {
        "fromChain": "4663", "toChain": "4663",
        "fromToken": input_token, "toToken": output_token,
        "fromAddress": str(custody).lower(), "toAddress": str(custody).lower(),
        "fromAmount": str(int(amount)), "allowBridges": [],
        "allowExchanges": manifest["allow_exchanges"], "fee": "0",
        "integrator": "sniper", "referrer": str(custody).lower(), "simulation": True,
        "slippage": slippage,
        "maxPriceImpact": str(float(cfg.MEMECOIN_ENTRY_IMPACT_CAP_PCT) / 100.0),
    }


def _sha256(value):
    if isinstance(value, str):
        value = value.encode()
    return hashlib.sha256(bytes(value)).hexdigest()


def _age_s(observed_at, now_iso):
    try:
        observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        now = datetime.fromisoformat(str(now_iso).replace("Z", "+00:00"))
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return max(0.0, (now - observed).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _trace(trace, stage, data):
    marker = getattr(trace, "mark_substage", None) if trace is not None else None
    if marker is not None:
        marker("lifi." + stage, **data)


def _settlement_balances(tx_hash, decoded, before, balance_fn):
    values = {"expected_tx_hash": tx_hash, "current": False}
    if before is None or not callable(balance_fn):
        return values
    try:
        after_input = int(balance_fn(decoded.path[0]))
        after_output = int(balance_fn(decoded.path[-1]))
    except Exception:
        return values
    input_delta = int(before[0]) - after_input
    output_delta = after_output - int(before[1])
    if input_delta == 0 and output_delta == 0:
        return values
    values.update({
        "current": True,
        "input_delta_atomic": input_delta,
        "output_delta_atomic": output_delta,
    })
    return values


def build_lifi_runtime(
        *, conn, chain_id, custody, rpc_url, proof_rpc_url, http, acct, manifest,
        manifest_hash, release_sha, activation, submission_transport, decimals_fn,
        canary=False):
    """Build callbacks only after the caller validates the immutable activation."""
    guard = QuotaGuard(remaining=int(activation["quota_remaining"]))
    guard.limit = int(activation["quota_limit"])
    guard.reset = int(activation["quota_reset"])
    recovery_context = {"authenticated": False}
    client = LifiClient(
        os.getenv("MEMECOIN_LIFI_API_URL", "https://li.quest/v1/quote").strip(),
        api_key=os.getenv("MEMECOIN_LIFI_API_KEY", "").strip(),
        manifest=manifest, http=http, corrected_now=real_utc_now_iso,
        quota_guard=guard, authentication_state=recovery_context,
    )
    used_quote_hashes = set()

    def request_for(side, amount, *, mint, sell_token, slippage="0.03"):
        return build_lifi_request(
            side, amount, mint=mint, sell_token=sell_token, custody=custody,
            manifest=manifest, slippage=slippage,
        )

    probe_mint = None
    canary_lifecycle_id = str((activation or {}).get("canary_lifecycle_id") or "")
    if canary_lifecycle_id:
        latest = m.latest_lifi_lifecycle(conn, canary_lifecycle_id)
        try:
            allowances = json.loads((latest or {}).get("detail_json") or "{}").get(
                "allowances") or []
        except (TypeError, ValueError):
            allowances = []
        quote_token = CHAIN_QUOTE["4663"]["token"].lower()
        probe_mint = next((str(item.get("token") or "") for item in allowances
                           if isinstance(item, dict)
                           and str(item.get("token") or "").lower() != quote_token), None)

    def authenticate_fn():
        if not probe_mint:
            return False
        client.quote(
            request_for(
                "buy", 10**15, mint=probe_mint,
                sell_token=CHAIN_QUOTE["4663"]["token"],
            ),
            role="entry", stage="authentication_probe",
        )
        return bool(recovery_context.get("authenticated"))

    def lifecycle_for(call_conn, lifecycle_id, now_iso, local):
        return LifiLifecycle(
            conn=call_conn, lifecycle_id=lifecycle_id, custody=str(custody).lower(),
            chain_id=str(chain_id), nonce_fn=local.get("nonce_fn"),
            gas_price_fn=local.get("gas_price_fn"), sign_fn=local.get("sign_fn"),
            send_fn=local.get("send_fn"), receipt_fn=local.get("receipt_fn"),
            allowance_fn=local.get("allowance_fn"), now_fn=lambda: now_iso,
            approve_gas=int(local.get("approve_gas") or 100_000),
        )

    def proof(result, stage):
        return prove_quote_authority(
            result.decoded, result.request_sha256, result.response_sha256,
            rpc_url, proof_rpc_url, manifest=manifest, stage=stage,
            received_at=result.received_at, used_quote_hashes=used_quote_hashes,
            block_tag="latest",
            http=http,
        )

    def entry_fn(**kwargs):
        call_conn = kwargs["conn"]
        pending_id = kwargs["pending_id"]
        market_only = getattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", False)

        def abort(status, reason):
            m.release_pending(call_conn, pending_id, reason)
            return {"status": status, "reason": reason}

        direct_miss = kwargs.get("direct_miss")
        if not isinstance(direct_miss, dict) or direct_miss.get("code") != "no_route":
            return abort("not_selected", "direct_not_no_route")
        if canary and float(kwargs.get("usd_amount") or 0) != 5.0:
            return abort("aborted", "invalid_canary_amount")
        slots_used = m.lifi_slots_used(call_conn, chain_id, custody)
        admission = (guard.admit_canary(in_flight=guard.in_flight) if canary
                     else guard.admit_entry(slots_used, in_flight=guard.in_flight))
        if not admission.allowed and admission.code != "entry_disabled":
            return abort("aborted", admission.code)
        if lifi_entry_blocked(call_conn, chain_id, custody):
            return abort("aborted", "unresolved_lifi_authority")
        local = kwargs.get("local") or {}
        try:
            sell_decimals = decimals_fn(kwargs["sell_token"])
            input_atomic = int(round(
                float(kwargs["usd_amount"]) / float(kwargs["sell_token_usd"])
                * 10 ** int(sell_decimals)
            ))
        except (TypeError, ValueError, ZeroDivisionError):
            m.release_pending(call_conn, pending_id, "lifi_input_unavailable")
            return {"status": "aborted", "reason": "lifi_input_unavailable"}
        if input_atomic <= 0:
            m.release_pending(call_conn, pending_id, "lifi_input_unavailable")
            return {"status": "aborted", "reason": "lifi_input_unavailable"}
        baseline = None
        balance_fn = local.get("balance_raw_fn")
        if callable(balance_fn):
            try:
                input_decimals = int(sell_decimals)
                output_decimals = int(decimals_fn(kwargs["mint"]))
                baseline = {
                    "input_token": str(kwargs["sell_token"]).lower(),
                    "input_atomic": str(int(balance_fn(kwargs["sell_token"]))),
                    "input_decimals": input_decimals,
                    "input_usd": float(kwargs["sell_token_usd"]),
                    "output_token": str(kwargs["mint"]).lower(),
                    "output_atomic": str(int(balance_fn(kwargs["mint"]))),
                    "output_decimals": output_decimals,
                }
            except Exception:
                baseline = None
        lifecycle_id = m.start_lifi_lifecycle(
            call_conn, pending_id, chain_id=chain_id, custody_wallet=custody,
            token=kwargs["mint"], release_sha=release_sha, manifest_hash=manifest_hash,
            created_at=kwargs["now_iso"],
            detail=({"balance_baseline": baseline} if baseline is not None else None),
        )
        if lifecycle_id is None:
            return abort("aborted", "lifi_lifecycle_conflict")
        lifecycle = lifecycle_for(call_conn, lifecycle_id, kwargs["now_iso"], local)
        pending = m.pending_row(call_conn, pending_id) or {}
        proofs = {}

        def proof_fn(result, stage):
            value = proof(result, stage)
            proofs[stage] = value
            return value

        def cleanup(reason):
            latest = m.latest_lifi_lifecycle(call_conn, lifecycle_id)
            if latest is None or latest["state"] in m.LIFI_TERMINAL_STATES:
                return
            if latest["state"] == "reserved":
                finalize_lifi_unmutated_entry(lifecycle, pending_id, reason)
                return
            if latest["state"] in {
                    "approval_signed", "approval_submitted", "approval_submitted_unknown",
                    "swap_signed", "swap_submitted", "swap_submitted_unknown"}:
                return
            spender = latest.get("spender")
            if not spender:
                return
            token = latest.get("token") or kwargs["sell_token"]
            try:
                allowance = int(local["allowance_fn"](token, str(custody).lower(), spender))
            except Exception:
                return
            if allowance == 0:
                zero = m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state=latest["state"],
                    new_state="allowance_zero", event_type="allowance_zero", stage="entry",
                    token=token, spender=spender, allowance_atomic=0,
                    created_at=kwargs["now_iso"])
            else:
                zero = revoke_and_wait(lifecycle, token, spender).status == "allowance_zero"
            if zero:
                finalize_lifi_failed_entry(lifecycle, pending_id, reason)

        def unit_price(buy):
            token_decimals = decimals_fn(kwargs["mint"])
            if token_decimals is None or buy.output_min_atomic <= 0:
                return None
            quantity = buy.output_min_atomic / (10 ** int(token_decimals))
            return float(kwargs["usd_amount"]) / quantity if quantity > 0 else None

        def gate(name, buy, sell):
            if name == "token_safety":
                check = kwargs.get("safety_gate_fn")
                try:
                    safe, reason = check() if callable(check) else (False, "token_safety_missing")
                except Exception:
                    safe, reason = False, "token_safety_error"
                return GateResult(bool(safe), "ok" if safe else str(reason))
            if name == "chase":
                if market_only:
                    from sniper.execution.entry_attribution import market_movement_check
                    movement = market_movement_check(cap_pct=cfg.copy_premium_cap(
                        pending.get("source_user_id"), pending.get("network_id"))[0])
                    _trace(kwargs.get("trace"), "copy_market_movement", movement)
                    return GateResult(movement["state"] != "refuse", "market_movement_" + movement["state"])
                exact = unit_price(buy)
                source = pending.get("source_price")
                cap = float(kwargs.get("chase_max_pct") or 0)
                hard = cfg.copy_premium_cap(pending.get("source_user_id"),
                                            pending.get("network_id"))[0]
                if hard > 0:
                    cap = min(cap, hard) if cap > 0 else hard
                    if source and exact is None:
                        return GateResult(False, 'copy_price_unavailable')
                if exact is None or not source or cap <= 0:
                    return GateResult(True, "not_available")
                allowed = exact <= float(source) * (1.0 + cap / 100.0)
                return GateResult(allowed, "ok" if allowed else "chase_cap")
            if name == "gas_sufficiency":
                estimate = local.get("estimate_gas_fn")
                balance = local.get("native_balance_fn")
                gas_price = local.get("gas_price_fn")
                try:
                    gas = (buy.transaction or {}).get("gasLimit")
                    gas = int(gas) if gas is not None else int(
                        estimate(dict(buy.transaction or {})))
                    required = (gas + int(local.get("approve_gas") or 100_000)) \
                        * int(gas_price())
                    available = int(float(balance()) * 10 ** 18)
                    allowed = gas > 0 and available >= required
                except Exception:
                    allowed = False
                return GateResult(allowed, "ok" if allowed else "gas_sufficiency")
            if name == "impact":
                from sniper.execution.lifi_route import economic_roundtrip
                decision = economic_roundtrip(
                    input_atomic, buy, sell,
                    int(round(cfg.entry_roundtrip_cap_pct(
                        kwargs.get("impact_cap_pct") or cfg.MEMECOIN_ENTRY_IMPACT_CAP_PCT) * 100)),
                )
                return GateResult(decision.allowed, "ok" if decision.allowed else decision.code)
            contract = m.validated_pending_funded_contract(call_conn, pending)
            if name == "execution_premium":
                from sniper.execution.evm_swap_executor import _execution_premium, _premium_refusal
                # E11 off on this chain: no display price; the LI.FI leg has no post-trade read,
                # so the missing-display posture decides on the source fill (spec 2026-10-08).
                # E11 on: byte-for-byte today (no source price passed).
                onchain = cfg.dexscreener_off("E11", str(chain_id))
                premium = _execution_premium(
                    contract, unit_price(buy), kwargs.get("trace"),
                    attempt_no=1, revision="lifi", network_id=str(chain_id),
                    source_price=pending.get("source_price") if onchain else None,
                )
                refusal = _premium_refusal(premium)
                return GateResult(refusal is None, "ok" if refusal is None else refusal[1])
            if name == "post_peak":
                from sniper.execution.evm_swap_executor import _post_peak_confirmation
                decision = _post_peak_confirmation(
                    contract, unit_price(buy), kwargs.get("trace"),
                    attempt_no=1, revision="lifi",
                )
                return GateResult(not (decision and decision.confirmed), "post_peak_confirmed")
            return GateResult(True, "ok")

        def execute(refreshed_buy, final_sell, _quote_proofs):
            quote_proof = proofs["refreshed_buy"]
            now = real_utc_now_iso()
            if max(
                    _age_s(quote_proof.received_at, now),
                    _age_s(proofs["final_sell"].received_at, now),
            ) > _QUOTE_MAX_AGE_S:
                cleanup("stale_quote")
                return {"status": "aborted", "reason": "stale_quote"}
            required = (
                "estimate_gas_fn", "construct_tx_fn", "allowance_at_block_fn",
                "simulate_fn", "sign_complete_fn", "receipt_fn",
            )
            send_swap = local.get("send_swap_fn")
            send_factory = local.get("send_swap_fn_factory")
            if callable(send_factory):
                send_swap = send_factory(
                    call_conn, pending_id, action="entry", owner_kind="reservation")
            if any(not callable(local.get(name)) for name in required) or not callable(send_swap):
                cleanup("local_executor_unavailable")
                return {"status": "aborted", "reason": "local_executor_unavailable"}
            if baseline is None:
                cleanup("balance_baseline_unavailable")
                return {"status": "aborted", "reason": "balance_baseline_unavailable"}
            balance_fn = local.get("balance_raw_fn")
            before = ((int(baseline["input_atomic"]), int(baseline["output_atomic"]))
                      if baseline is not None else None)
            sell_tx = {
                **dict(final_sell.transaction or {}),
                "from": str(custody).lower(), "chainId": int(chain_id),
            }
            if local["simulate_fn"](sell_tx, proofs["final_sell"].block_number) is not True:
                cleanup("final_sell_simulation_failed")
                return {"status": "aborted", "reason": "final_sell_simulation_failed"}
            signed_box = {}

            def persist(authority, signed):
                raw, tx_hash = signed[:2]
                nonce = signed[2] if len(signed) > 2 else None
                if nonce is None:
                    raise LifiAuthorityError("LI.FI signed nonce is absent")
                if not m.bind_entry_submission(
                        call_conn, pending_id, tx_identity=str(tx_hash).lower(),
                        signed_payload_b64=str(raw), signed_payload_sha256=_sha256(raw),
                        blockhash=authority.block_hash, last_valid_height=None,
                        nonce=nonce, backend="lifi", bound_at=kwargs["now_iso"]):
                    raise LifiAuthorityError("LI.FI entry submission binding failed")
                if not m.append_lifi_event(
                        call_conn, lifecycle_id, expected_state="approval_confirmed",
                        new_state="swap_signed", event_type="swap_signed", stage="entry",
                        token=refreshed_buy.path[0], spender=refreshed_buy.spender,
                        amount_atomic=refreshed_buy.input_atomic,
                        input_atomic=refreshed_buy.input_atomic,
                        output_min_atomic=refreshed_buy.output_min_atomic,
                        fee_atomic=refreshed_buy.fee_atomic, tx_hash=str(tx_hash).lower(),
                        tx_identity=str(tx_hash).lower(), nonce=nonce,
                        signed_payload_sha256=_sha256(raw), block_number=authority.block_number,
                        block_hash=authority.block_hash, proof_sha256=authority.proof_sha256,
                        request_sha256=refreshed_buy.request_sha256,
                        response_sha256=refreshed_buy.response_sha256,
                        calldata_sha256=refreshed_buy.calldata_sha256,
                        decoded_semantics_sha256=refreshed_buy.decoded_semantics_sha256,
                        tool=refreshed_buy.tool, created_at=kwargs["now_iso"]):
                    raise LifiAuthorityError("LI.FI swap identity write failed")
                signed_box.update(raw=raw, tx_hash=str(tx_hash).lower(), nonce=nonce)

            try:
                prove_execution_block(
                    dict(refreshed_buy.transaction or {}), refreshed_buy,
                    rpc=rpc_url, proof_rpc=proof_rpc_url, stage="refreshed_buy",
                    received_at=quote_proof.received_at, custody=custody,
                    estimate_gas=local["estimate_gas_fn"],
                    construct_tx=local["construct_tx_fn"],
                    allowance_reader=local["allowance_at_block_fn"],
                    simulate=local["simulate_fn"], sign=local["sign_complete_fn"],
                    persist=persist, manifest=manifest, http=http,
                    authority=quote_proof,
                )
            except Exception as exc:
                _trace(kwargs.get("trace"), "execution_proof", {
                    "exception_type": type(exc).__name__, "reason": str(exc),
                })
                cleanup("execution_proof_failed")
                return {"status": "aborted", "reason": "execution_proof_failed"}
            sent = send_swap(signed_box["raw"])
            if sent is False:
                m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="swap_signed",
                    new_state="swap_rejected", event_type="swap_rejected", stage="entry",
                    tx_hash=signed_box["tx_hash"], created_at=kwargs["now_iso"])
                cleanup("swap_rejected")
                return {"status": "failed", "reason": "swap_rejected"}
            submitted = "swap_submitted" if sent else "swap_submitted_unknown"
            m.append_lifi_event(
                call_conn, lifecycle_id, expected_state="swap_signed", new_state=submitted,
                event_type=submitted, stage="entry", tx_hash=signed_box["tx_hash"],
                created_at=kwargs["now_iso"])
            if not sent:
                return {"status": "unknown", "reason": "lifi_submitted_unknown",
                        "tx_hash": signed_box["tx_hash"]}
            receipt = local["receipt_fn"](signed_box["tx_hash"])
            if not isinstance(receipt, dict):
                m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="swap_submitted",
                    new_state="swap_submitted_unknown", event_type="swap_submitted_unknown",
                    stage="entry", tx_hash=signed_box["tx_hash"],
                    created_at=kwargs["now_iso"])
                return {"status": "unknown", "reason": "lifi_submitted_unknown",
                        "tx_hash": signed_box["tx_hash"]}
            if receipt.get("status") in ("0x0", "0x00", 0):
                m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="swap_submitted",
                    new_state="swap_reverted", event_type="swap_reverted", stage="entry",
                    tx_hash=signed_box["tx_hash"], created_at=kwargs["now_iso"])
                cleanup("swap_reverted")
                return {"status": "failed", "reason": "swap_reverted",
                        "tx_hash": signed_box["tx_hash"]}
            audit = audit_receipt_block(
                receipt, refreshed_buy, rpc=rpc_url, proof_rpc=proof_rpc_url,
                stage="entry_receipt", manifest=manifest, http=http,
            )
            settlement = settle_entry(
                receipt, _settlement_balances(
                    signed_box["tx_hash"], refreshed_buy, before, balance_fn),
                refreshed_buy,
            )
            if not settlement.ok:
                return {"status": "unknown", "reason": settlement.code,
                        "tx_hash": signed_box["tx_hash"]}
            if not m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="swap_submitted",
                    new_state="entry_settled", event_type="entry_settled", stage="entry",
                    settlement_in_atomic=settlement.input_atomic,
                    settlement_out_atomic=settlement.output_atomic,
                    tx_hash=signed_box["tx_hash"], created_at=kwargs["now_iso"],
                    detail={"settlement_source": settlement.source,
                            "receipt_audit_ok": bool(audit.ok)}):
                return {"status": "unknown", "reason": "settlement_write_conflict",
                        "tx_hash": signed_box["tx_hash"]}
            token_decimals = decimals_fn(kwargs["mint"])
            sell_decimals = decimals_fn(kwargs["sell_token"])
            if token_decimals is None or sell_decimals is None:
                return {"status": "unknown", "reason": "token_decimals_unavailable",
                        "tx_hash": signed_box["tx_hash"]}
            spent = (settlement.input_atomic / (10 ** int(sell_decimals))
                     * float(kwargs["sell_token_usd"]))
            got = settlement.output_atomic / (10 ** int(token_decimals))
            if spent <= 0 or got <= 0:
                return {"status": "unknown", "reason": "invalid_settlement_amount",
                        "tx_hash": signed_box["tx_hash"]}
            fill_price = spent / got
            their_price = pending.get("source_price")
            attribution = "exact_event" if their_price else "unavailable"
            contract = m.validated_pending_funded_contract(call_conn, pending)
            position_id = m.finalize_lifi_entry(
                call_conn, lifecycle_id=lifecycle_id, pending_id=pending_id,
                position_kwargs={
                    "source_user_id": pending.get("source_user_id"),
                    "source_handle": "(real)", "mint": kwargs["mint"], "ticker": "",
                    "entry_price": fill_price, "entry_notional": spent,
                    "entry_fill_source": "chain",
                    "entry_lag_s": m.resolve_source_lag_s(
                        call_conn, pending.get("origin_event_id")),
                    "their_size_usd": m.resolve_source_size_usd(
                        call_conn, pending.get("origin_event_id"), network_id=pending.get("network_id"),
                        mint=pending.get("mint"), source_user_id=pending.get("source_user_id")),
                    "entry_liquidity": pending.get("source_liquidity"),
                    "entry_token_age_s": pending.get("source_token_age_s"),
                    "opened_at": kwargs["now_iso"],
                    "strategy": pending.get("strategy") or "control",
                    "execution_mode": "real", "network_id": str(chain_id),
                    "signal_source": pending.get("signal_source") or "global_feed",
                    "origin_event_id": pending.get("origin_event_id"),
                    "entry_mcap": pending.get("source_market_cap"),
                    "their_price": their_price, "attempt_id": pending.get("attempt_id"),
                    "price_attribution": attribution, **contract,
                },
                fill_kwargs={
                    "kind": "entry", "price": fill_price, "qty": got,
                    "notional": spent, "fill_source": "chain", "fraction": 1.0,
                    "created_at": kwargs["now_iso"], "tx_sig": signed_box["tx_hash"],
                    "attempt_id": pending.get("attempt_id"),
                    "origin_event_id": pending.get("origin_event_id"),
                    "network_id": str(chain_id),
                    "signal_source": pending.get("signal_source") or "global_feed",
                    "source_price": their_price, "price_attribution": attribution,
                },
                created_at=kwargs["now_iso"],
            )
            if position_id is None:
                return {"status": "unknown", "reason": "entry_finalize_conflict",
                        "tx_hash": signed_box["tx_hash"]}
            try:
                allowance = int(local["allowance_fn"](
                    refreshed_buy.path[0], str(custody).lower(), refreshed_buy.spender))
            except Exception:
                allowance = None
            if allowance:
                revoke_and_wait(lifecycle, refreshed_buy.path[0], refreshed_buy.spender)
            elif allowance is None:
                m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="position_open",
                    new_state="allowance_unresolved", event_type="allowance_unresolved",
                    stage="entry", token=refreshed_buy.path[0],
                    spender=refreshed_buy.spender, created_at=kwargs["now_iso"])
            if not audit.ok:
                latest = m.latest_lifi_lifecycle(call_conn, lifecycle_id)
                m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state=latest["state"],
                    new_state="receipt_authority_mismatch_open",
                    event_type="receipt_authority_mismatch", stage="entry",
                    created_at=kwargs["now_iso"])
            return {"status": "filled", "position_id": position_id,
                    "lifecycle_id": lifecycle_id,
                    "fill_price": fill_price, "notional": spent,
                    "tx_hash": signed_box["tx_hash"]}

        def approve(token, spender, amount):
            return approve_exact_and_wait(lifecycle, token, spender, amount)

        try:
            result = execute_lifi_entry(
                direct_miss=direct_miss, input_atomic=input_atomic,
                request_fn=lambda side, amount, _stage: request_for(
                    side, amount, mint=kwargs["mint"], sell_token=kwargs["sell_token"]),
                client=client, proof_fn=proof_fn,
                approval_fn=approve,
                execute_fn=execute, cleanup_fn=cleanup, quota_guard=guard,
                slots_used=slots_used, in_flight=guard.in_flight, gate_fn=gate,
                roundtrip_cap_bps=int(round(cfg.entry_roundtrip_cap_pct(
                    kwargs.get("impact_cap_pct") or cfg.MEMECOIN_ENTRY_IMPACT_CAP_PCT) * 100)),
                trace_fn=lambda stage, data: _trace(kwargs.get("trace"), stage, data),
                canary=canary,
            )
        except Exception:
            cleanup("lifi_internal_error")
            raise
        latest = m.latest_lifi_lifecycle(call_conn, lifecycle_id)
        if latest and latest["state"] == "reserved":
            finalize_lifi_unmutated_entry(
                lifecycle, pending_id, result.get("reason") or "lifi_entry_failed")
        return result

    def recover_confirmed(row, receipt, now_iso, *, conn):
        """Finalize a confirmed swap only from its durable intent and receipt facts."""
        stage = str(row.get("stage") or "")
        if stage not in {"entry", "exit"}:
            return False
        resume = str(row.get("state") or "") in {
            "entry_settled", "exit_settled", "allowance_zero",
        }
        signed_row = conn.execute(
            "SELECT * FROM memecoin_lifi_authority_events "
            "WHERE lifecycle_id=? AND event_type='swap_signed' "
            "ORDER BY sequence DESC LIMIT 1",
            (row["lifecycle_id"],),
        ).fetchone()
        if signed_row is None:
            return False
        signed = dict(signed_row)
        try:
            detail = json.loads(row.get("detail_json") or "{}")
            baseline = detail["balance_baseline"]
            decoded = SimpleNamespace(
                path=(str(baseline["input_token"]).lower(),
                      str(baseline["input_token"]).lower(),
                      str(baseline["output_token"]).lower()),
                input_atomic=int(signed["input_atomic"]),
                output_min_atomic=int(signed["output_min_atomic"]),
                input_recipients=(str(signed["spender"]).lower(),),
                custody=str(custody).lower(),
                spender=str(signed["spender"]).lower(),
                request_sha256=signed["request_sha256"],
                response_sha256=signed["response_sha256"],
            )
            input_decimals = int(baseline["input_decimals"])
            output_decimals = int(baseline["output_decimals"])
            if stage == "entry":
                input_usd = float(baseline["input_usd"])
            else:
                output_usd = float(baseline["output_usd"])
                fraction = float(baseline["fraction"])
                claim_id = str(baseline["claim_id"])
                exit_reason = str(baseline["exit_reason"])
        except (KeyError, TypeError, ValueError):
            return False
        if resume:
            settled_row = conn.execute(
                "SELECT * FROM memecoin_lifi_authority_events "
                "WHERE lifecycle_id=? AND event_type=? ORDER BY sequence DESC LIMIT 1",
                (row["lifecycle_id"], stage + "_settled"),
            ).fetchone()
            if settled_row is None:
                return False
            settled = dict(settled_row)
            try:
                settled_detail = json.loads(settled.get("detail_json") or "{}")
                settlement = SimpleNamespace(
                    input_atomic=int(settled["settlement_in_atomic"]),
                    output_atomic=int(settled["settlement_out_atomic"]),
                    source=settled_detail.get("settlement_source") or "receipt",
                )
                audit = SimpleNamespace(ok=bool(settled_detail["receipt_audit_ok"]))
            except (KeyError, TypeError, ValueError):
                return False
        else:
            audit = audit_receipt_block(
                receipt, decoded, rpc=rpc_url, proof_rpc=proof_rpc_url,
                stage=stage + "_receipt_recovery", manifest=manifest, http=http,
            )
            settle = settle_entry if stage == "entry" else settle_exit
            settlement = settle(
                receipt, {"expected_tx_hash": row.get("tx_hash"), "current": False}, decoded,
            )
            if not settlement.ok:
                return False
            latest = m.latest_lifi_lifecycle(conn, row["lifecycle_id"])
            if latest is None or latest["state"] not in {
                    "swap_signed", "swap_submitted", "swap_submitted_unknown"}:
                return False
            settled_state = stage + "_settled"
            if not m.append_lifi_event(
                    conn, row["lifecycle_id"], expected_state=latest["state"],
                    new_state=settled_state, event_type=settled_state, stage=stage,
                    settlement_in_atomic=settlement.input_atomic,
                    settlement_out_atomic=settlement.output_atomic,
                    tx_hash=row.get("tx_hash"), created_at=now_iso,
                    detail={"settlement_source": settlement.source,
                            "receipt_audit_ok": bool(audit.ok)}):
                return False
        if stage == "exit":
            allowance_fn = recovery_context.get("allowance_fn")
            try:
                allowance = int(allowance_fn(
                    decoded.path[0], str(custody).lower(), decoded.spender))
            except Exception:
                return False
            if str(row.get("state") or "") == "allowance_zero":
                if allowance != 0:
                    return False
            elif allowance == 0:
                if not m.append_lifi_event(
                        conn, row["lifecycle_id"], expected_state="exit_settled",
                        new_state="allowance_zero", event_type="allowance_zero", stage="exit",
                        allowance_atomic=0, created_at=now_iso):
                    return False
            else:
                lifecycle = lifecycle_for(
                    conn, row["lifecycle_id"], now_iso, recovery_context)
                if revoke_and_wait(
                        lifecycle, decoded.path[0], decoded.spender).status != "allowance_zero":
                    return False
            position_id = row.get("position_id")
            position = m.get_position(conn, position_id) if position_id is not None else None
            sold_qty = settlement.input_atomic / (10 ** input_decimals)
            received = settlement.output_atomic / (10 ** output_decimals) * output_usd
            if not position or sold_qty <= 0 or received <= 0:
                return False
            price = received / sold_qty
            quote_price = (
                decoded.output_min_atomic / (10 ** output_decimals) * output_usd / sold_qty
            )
            if fraction == 1.0:
                realized = m.realized_after_exit(
                    conn, position_id, received, position.get("entry_notional"))
                return m.finalize_lifi_close(
                    conn, lifecycle_id=row["lifecycle_id"], expected_state="allowance_zero",
                    exit_kwargs={
                        "position_id": position_id, "claim_id": claim_id,
                        "sig": row.get("tx_hash"), "closed_at": now_iso,
                        "realized_pnl": realized, "exit_reason": exit_reason,
                        "fill_kind": m.full_exit_fill_kind(exit_reason),
                        "price": price, "qty": sold_qty, "notional": received,
                        "quote_price": quote_price,
                    },
                    created_at=now_iso,
                    final_state=("position_closed" if audit.ok
                                 else "receipt_authority_mismatch_closed"),
                )
            m.book_derisk_fill(
                conn, position_id=position_id, at=now_iso, price=price,
                qty=sold_qty, notional=received, fraction=fraction,
                tx_sig=row.get("tx_hash"), claim_id=claim_id, quote_price=quote_price,
            )
            return m.append_lifi_event(
                conn, row["lifecycle_id"], expected_state="allowance_zero",
                new_state=("position_open" if audit.ok
                           else "receipt_authority_mismatch_open"),
                event_type="partial_exit_settled", stage="exit", created_at=now_iso,
            )
        pending = m.pending_row(conn, row["pending_id"]) or {}
        spent = settlement.input_atomic / (10 ** input_decimals) * input_usd
        got = settlement.output_atomic / (10 ** output_decimals)
        if spent <= 0 or got <= 0:
            return False
        their_price = pending.get("source_price")
        attribution = "exact_event" if their_price else "unavailable"
        contract = m.validated_pending_funded_contract(conn, pending)
        position_id = m.finalize_lifi_entry(
            conn, lifecycle_id=row["lifecycle_id"], pending_id=row["pending_id"],
            position_kwargs={
                "source_user_id": pending.get("source_user_id"),
                "source_handle": "(real)", "mint": decoded.path[-1], "ticker": "",
                "entry_price": spent / got, "entry_notional": spent,
                "entry_fill_source": "chain",
                "entry_lag_s": m.resolve_source_lag_s(conn, pending.get("origin_event_id")),
                "their_size_usd": m.resolve_source_size_usd(
                    conn, pending.get("origin_event_id"), network_id=pending.get("network_id"),
                    mint=pending.get("mint"), source_user_id=pending.get("source_user_id")),
                "entry_liquidity": pending.get("source_liquidity"),
                "entry_token_age_s": pending.get("source_token_age_s"),
                "opened_at": now_iso, "strategy": pending.get("strategy") or "control",
                "execution_mode": "real", "network_id": str(chain_id),
                "signal_source": pending.get("signal_source") or "global_feed",
                "origin_event_id": pending.get("origin_event_id"),
                "entry_mcap": pending.get("source_market_cap"),
                "their_price": their_price, "attempt_id": pending.get("attempt_id"),
                "price_attribution": attribution, **contract,
            },
            fill_kwargs={
                "kind": "entry", "price": spent / got, "qty": got,
                "notional": spent, "fill_source": "chain", "fraction": 1.0,
                "created_at": now_iso, "tx_sig": row.get("tx_hash"),
                "attempt_id": pending.get("attempt_id"),
                "origin_event_id": pending.get("origin_event_id"),
                "network_id": str(chain_id),
                "signal_source": pending.get("signal_source") or "global_feed",
                "source_price": their_price, "price_attribution": attribution,
            },
            created_at=now_iso,
        )
        if position_id is None:
            return False
        allowance_fn = recovery_context.get("allowance_fn")
        try:
            allowance = int(allowance_fn(
                decoded.path[0], str(custody).lower(), decoded.spender))
        except Exception:
            allowance = None
        if allowance and allowance > 0:
            lifecycle = lifecycle_for(conn, row["lifecycle_id"], now_iso, recovery_context)
            if revoke_and_wait(
                    lifecycle, decoded.path[0], decoded.spender).status == "allowance_zero":
                m.append_lifi_event(
                    conn, row["lifecycle_id"], expected_state="allowance_zero",
                    new_state="position_open", event_type="authority_clean", stage="entry",
                    created_at=now_iso,
                )
        elif allowance is None:
            m.append_lifi_event(
                conn, row["lifecycle_id"], expected_state="position_open",
                new_state="allowance_unresolved", event_type="allowance_unresolved",
                stage="entry", token=decoded.path[0], spender=decoded.spender,
                created_at=now_iso,
            )
        if not audit.ok:
            latest = m.latest_lifi_lifecycle(conn, row["lifecycle_id"])
            m.append_lifi_event(
                conn, row["lifecycle_id"], expected_state=latest["state"],
                new_state="receipt_authority_mismatch_open",
                event_type="receipt_authority_mismatch", stage="entry", created_at=now_iso,
            )
        return True

    def preview_fn(**kwargs):
        from sniper.analysis.memecoin_quote_failures import QuoteFailure
        from sniper.execution.exit_preview import ExitPreview, PreviewResult

        miss = kwargs.get("direct_miss") or {}
        if miss.get("code") != "no_route":
            return None
        amount = int(kwargs.get("amount_atomic") or 0)
        sell_token = kwargs.get("sell_token")
        sell_token_usd = kwargs.get("sell_token_usd")
        if amount <= 0 or not sell_token or not sell_token_usd:
            return PreviewResult.failed(
                QuoteFailure.MALFORMED_RESPONSE, provider_id="4663:lifi:nordstern")
        request = request_for(
            "sell", amount, mint=kwargs["position"]["mint"], sell_token=sell_token,
            slippage=str(float(cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER[-1]) / 100.0),
        )
        result = client.quote(request, role="exit", stage="preview")
        if not result.ok:
            failure = {
                "no_route": QuoteFailure.NO_ROUTE,
                "timeout": QuoteFailure.PROVIDER_TIMEOUT,
                "rate_limited": QuoteFailure.RATE_LIMIT,
                "provider_error": QuoteFailure.RPC_TRANSPORT,
            }.get(result.failure_type, QuoteFailure.MALFORMED_RESPONSE)
            return PreviewResult.failed(failure, provider_id="4663:lifi:nordstern")
        try:
            proof(result, "preview")
            decimals = decimals_fn(sell_token)
            expected = (result.decoded.output_min_atomic / (10 ** int(decimals))
                        * float(sell_token_usd))
        except Exception:
            return PreviewResult.failed(
                QuoteFailure.MALFORMED_RESPONSE, provider_id="4663:lifi:nordstern")
        from sniper.execution.exit_preview import quote_evidence
        evidence = quote_evidence({
            "_venue": "lifi", "sellAmount": amount,
            "buyAmount": result.decoded.output_expected_atomic,
            "minBuyAmount": result.decoded.output_min_atomic,
            "transaction": {"to": result.decoded.target},
        }, quote_asset=sell_token, quote_asset_decimals=int(decimals), quote_asset_usd=float(sell_token_usd),
            slippage_bps=int(round(cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER[-1] * 100)))
        evidence["valuation_basis"] = "minimum_output"
        return PreviewResult.succeeded(
            ExitPreview(expected, amount, result.decoded.target, result.received_at, quote_evidence=evidence),
            provider_id="4663:lifi:nordstern",
        )

    def exit_fn(**kwargs):
        position = kwargs["position"]
        call_conn = kwargs["conn"]
        claim_id = kwargs["claim_id"]
        fraction = float(kwargs.get("fraction") or 1.0)
        now_iso = kwargs["now_iso"]
        local = kwargs.get("local") or {}

        def release_claim():
            if fraction == 1.0:
                m.release_position_close_claim(call_conn, position["id"], claim_id)
            else:
                m.release_position_derisk_claim(call_conn, position["id"], claim_id)

        matches = [
            row for row in m.unresolved_lifi_lifecycles(call_conn, chain_id, custody)
            if row.get("position_id") == position["id"]
        ]
        if len(matches) != 1:
            release_claim()
            return {"status": "deferred", "reason": "lifi_lifecycle_missing"}
        lifecycle_id = matches[0]["lifecycle_id"]
        lifecycle = lifecycle_for(call_conn, lifecycle_id, now_iso, local)
        sell_token = kwargs["sell_token"]
        sell_token_usd = float(kwargs["sell_token_usd"])
        balance_fn = local.get("balance_raw_fn")
        if not callable(balance_fn):
            release_claim()
            return {"status": "deferred", "reason": "lifi_balance_unavailable"}
        try:
            held_atomic = int(balance_fn(position["mint"]))
            before = (held_atomic, int(balance_fn(sell_token)))
            input_decimals = int(decimals_fn(position["mint"]))
            output_decimals = int(decimals_fn(sell_token))
        except Exception:
            release_claim()
            return {"status": "deferred", "reason": "lifi_balance_unavailable"}
        amount_atomic = held_atomic if fraction == 1.0 else int(held_atomic * fraction)
        if amount_atomic <= 0:
            release_claim()
            return {"status": "deferred", "reason": "lifi_balance_unavailable"}
        if not m.append_lifi_event(
                call_conn, lifecycle_id, expected_state="position_open",
                new_state="position_open", event_type="exit_balance_baseline",
                stage="exit", token=position["mint"], created_at=now_iso,
                detail={"balance_baseline": {
                    "input_token": str(position["mint"]).lower(),
                    "input_atomic": str(before[0]),
                    "input_decimals": input_decimals,
                    "output_token": str(sell_token).lower(),
                    "output_atomic": str(before[1]),
                    "output_decimals": output_decimals,
                    "output_usd": sell_token_usd,
                    "fraction": fraction,
                    "claim_id": claim_id,
                    "exit_reason": kwargs["exit_reason"],
                }}):
            release_claim()
            return {"status": "deferred", "reason": "lifi_baseline_conflict"}
        proofs = {}

        def cleanup(reason):
            latest = m.latest_lifi_lifecycle(call_conn, lifecycle_id)
            if latest is None:
                return
            if latest["state"] == "position_open":
                release_claim()
                return
            if latest["state"] in {
                    "approval_signed", "approval_submitted", "approval_submitted_unknown",
                    "swap_signed", "swap_submitted", "swap_submitted_unknown"}:
                return
            spender = latest.get("spender")
            if not spender:
                return
            token = latest.get("token") or position["mint"]
            try:
                allowance = int(local["allowance_fn"](token, str(custody).lower(), spender))
            except Exception:
                return
            if allowance == 0:
                zero = m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state=latest["state"],
                    new_state="allowance_zero", event_type="allowance_zero", stage="exit",
                    token=token, spender=spender, allowance_atomic=0, created_at=now_iso)
            else:
                zero = revoke_and_wait(lifecycle, token, spender).status == "allowance_zero"
            if zero and m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="allowance_zero",
                    new_state="position_open", event_type="authority_clean", stage="exit",
                    created_at=now_iso, detail={"reason": str(reason)}):
                release_claim()

        def quote_fn(rung):
            result = client.quote(
                request_for(
                    "sell", amount_atomic, mint=position["mint"], sell_token=sell_token,
                    slippage=str(float(rung) / 100.0),
                ),
                role="exit", stage="exit_%s" % rung,
            )
            if not result.ok:
                return result
            try:
                proofs[id(result)] = proof(result, "exit_%s" % rung)
            except Exception:
                return SimpleNamespace(ok=False, failure_type="authority_mismatch")
            return result

        def freshness(result):
            return _age_s(result.received_at, real_utc_now_iso()) <= _QUOTE_MAX_AGE_S

        def execute(result, rung):
            decoded = result.decoded
            token_decimals = decimals_fn(position["mint"])
            sell_decimals = decimals_fn(sell_token)
            if token_decimals is None or sell_decimals is None:
                cleanup("token_decimals_unavailable")
                return {"status": "deferred", "reason": "token_decimals_unavailable"}
            sold_qty = decoded.input_atomic / (10 ** int(token_decimals))
            quote_usd = (decoded.output_min_atomic / (10 ** int(sell_decimals))
                         * sell_token_usd)
            quote_payload = {
                "_venue": "lifi", "_requested_slippage_bps": int(round(rung * 100)),
                "sellAmount": str(decoded.input_atomic),
                "buyAmount": str(decoded.output_min_atomic),
                "minBuyAmount": str(decoded.output_min_atomic),
                "transaction": decoded.transaction,
            }
            check = kwargs.get("quote_check_fn")
            if check is not None:
                try:
                    expected = int(decoded.output_expected_atomic)
                    if expected < decoded.output_min_atomic:
                        raise ValueError("expected output below minimum")
                    allowed, detail = check(
                        m.get_position(call_conn, position["id"]),
                        dict(quote_payload, buyAmount=str(expected)), sold_qty,
                    )
                except Exception:
                    if kwargs["exit_reason"] in {"real_tp", "mcap_target"}:
                        allowed, detail = False, {"code": "execution_target_check_failed"}
                    else:
                        allowed, detail = True, {"code": "execution_quote_check_failed_sweep_verdict_stands"}  # D2: unknown, not hold
                if not allowed:
                    cleanup(detail["code"])
                    return {"status": "deferred", "reason": detail["code"]}
            from sniper.services.memecoin_exit_intent import (
                CLOSE, DERISK, bind_quote, build_exit_intent, verify_exit_intent,
            )
            action = CLOSE if fraction == 1.0 else DERISK
            intent = bind_quote(build_exit_intent(
                m.get_position(call_conn, position["id"]), claim_id=claim_id,
                action=action, fraction=fraction, balance=sold_qty,
                decision_state_id=position.get("_engine_decision_state_id"),
            ), quote_payload, quote_time=result.received_at)
            current_balance = int(balance_fn(position["mint"])) / (10 ** int(token_decimals))
            refusal = verify_exit_intent(
                intent, m.get_position(call_conn, position["id"]), balance=current_balance,
                current_decision_state_id=(
                    position.get("_engine_current_state_id_fn")() if callable(
                        position.get("_engine_current_state_id_fn")) else None),
            )
            if refusal or not m.bind_exit_intent(
                    call_conn, position["id"], claim_id, intent.to_json()):
                release_claim()
                return {"status": "superseded", "reason": refusal or "intent_bind_failed"}
            approval = approve_exact_and_wait(
                lifecycle, decoded.path[0], decoded.spender, decoded.input_atomic)
            if approval.status != "confirmed":
                if approval.status != "submitted_unknown" and approval.cleanup_required:
                    cleanup("approval_" + approval.status)
                return {
                    "status": "unknown" if approval.status == "submitted_unknown" else "deferred",
                    "reason": "lifi_approval_" + approval.status,
                }
            if not freshness(result):
                cleanup("stale_quote")
                return {"status": "deferred", "reason": "stale_quote"}
            required = (
                "estimate_gas_fn", "construct_tx_fn", "allowance_at_block_fn",
                "simulate_fn", "sign_complete_fn", "receipt_fn",
            )
            send_swap = local.get("send_swap_fn")
            send_factory = local.get("send_swap_fn_factory")
            if callable(send_factory):
                send_swap = send_factory(
                    call_conn, position["id"],
                    action="exit" if fraction == 1.0 else "partial_exit",
                    owner_kind="close_claim" if fraction == 1.0 else "derisk_claim",
                )
            if any(not callable(local.get(name)) for name in required) or not callable(send_swap):
                cleanup("local_executor_unavailable")
                return {"status": "deferred", "reason": "local_executor_unavailable"}
            signed_box = {}

            def persist(authority, signed):
                raw, tx_hash, nonce = signed[:3]
                submitted = intent.with_signed_payload(
                    raw, tx_identity=tx_hash, nonce=nonce)
                call_conn.execute("BEGIN IMMEDIATE")
                try:
                    mark = (m.mark_position_close_submitted if fraction == 1.0
                            else m.mark_position_derisk_submitted)
                    if not mark(
                            call_conn, position["id"], claim_id, now_iso, tx_hash,
                            expected_intent_json=intent.to_json(),
                            submitted_intent_json=submitted.to_json(),
                            expected_controller_revision=intent.controller_revision,
                            commit=False):
                        raise LifiAuthorityError("LI.FI exit claim binding failed")
                    if not m.append_lifi_event(
                            call_conn, lifecycle_id, expected_state="approval_confirmed",
                            new_state="swap_signed", event_type="swap_signed", stage="exit",
                            token=decoded.path[0], spender=decoded.spender,
                            amount_atomic=decoded.input_atomic,
                            input_atomic=decoded.input_atomic,
                            output_min_atomic=decoded.output_min_atomic,
                            fee_atomic=decoded.fee_atomic, tx_hash=tx_hash,
                            tx_identity=tx_hash, nonce=nonce,
                            signed_payload_sha256=_sha256(raw),
                            block_number=authority.block_number, block_hash=authority.block_hash,
                            proof_sha256=authority.proof_sha256,
                            request_sha256=decoded.request_sha256,
                            response_sha256=decoded.response_sha256,
                            calldata_sha256=decoded.calldata_sha256,
                            decoded_semantics_sha256=decoded.decoded_semantics_sha256,
                            tool=decoded.tool, created_at=now_iso, commit=False):
                        raise LifiAuthorityError("LI.FI exit identity write failed")
                    call_conn.commit()
                except Exception:
                    call_conn.rollback()
                    raise
                signed_box.update(raw=raw, tx_hash=str(tx_hash).lower(), nonce=nonce)

            try:
                prove_execution_block(
                    dict(decoded.transaction or {}), decoded, rpc=rpc_url,
                    proof_rpc=proof_rpc_url, stage="exit_%s" % rung,
                    received_at=proofs[id(result)].received_at, custody=custody,
                    estimate_gas=local["estimate_gas_fn"],
                    construct_tx=local["construct_tx_fn"],
                    allowance_reader=local["allowance_at_block_fn"],
                    simulate=local["simulate_fn"], sign=local["sign_complete_fn"],
                    persist=persist, manifest=manifest, http=http,
                )
            except Exception:
                cleanup("execution_proof_failed")
                return {"status": "deferred", "reason": "execution_proof_failed"}
            sent = send_swap(signed_box["raw"])
            if sent is False:
                m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="swap_signed",
                    new_state="swap_rejected", event_type="swap_rejected", stage="exit",
                    tx_hash=signed_box["tx_hash"], created_at=now_iso)
                reset = (m.reset_position_close_submission if fraction == 1.0
                         else m.reset_position_derisk_submission)
                reset(call_conn, position["id"], claim_id, signed_box["tx_hash"], now_iso,
                      intent_status="dropped")
                cleanup("swap_rejected")
                return {"status": "deferred", "reason": "swap_rejected"}
            submitted_state = "swap_submitted" if sent else "swap_submitted_unknown"
            m.append_lifi_event(
                call_conn, lifecycle_id, expected_state="swap_signed",
                new_state=submitted_state, event_type=submitted_state, stage="exit",
                tx_hash=signed_box["tx_hash"], created_at=now_iso)
            if not sent:
                return {"status": "unknown", "reason": "lifi_submitted_unknown",
                        "tx_hash": signed_box["tx_hash"]}
            receipt = local["receipt_fn"](signed_box["tx_hash"])
            if not isinstance(receipt, dict):
                m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="swap_submitted",
                    new_state="swap_submitted_unknown", event_type="swap_submitted_unknown",
                    stage="exit", tx_hash=signed_box["tx_hash"], created_at=now_iso)
                return {"status": "unknown", "reason": "lifi_submitted_unknown",
                        "tx_hash": signed_box["tx_hash"]}
            if receipt.get("status") in ("0x0", "0x00", 0):
                m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="swap_submitted",
                    new_state="swap_reverted", event_type="swap_reverted", stage="exit",
                    tx_hash=signed_box["tx_hash"], created_at=now_iso)
                reset = (m.reset_position_close_submission if fraction == 1.0
                         else m.reset_position_derisk_submission)
                reset(call_conn, position["id"], claim_id, signed_box["tx_hash"], now_iso,
                      intent_status="failed")
                cleanup("swap_reverted")
                return {"status": "deferred", "reason": "swap_reverted",
                        "tx_hash": signed_box["tx_hash"]}
            audit = audit_receipt_block(
                receipt, decoded, rpc=rpc_url, proof_rpc=proof_rpc_url,
                stage="exit_receipt", manifest=manifest, http=http,
            )
            settlement = settle_exit(
                receipt, _settlement_balances(
                    signed_box["tx_hash"], decoded, before, balance_fn),
                decoded,
            )
            if not settlement.ok:
                return {"status": "unknown", "reason": settlement.code,
                    "tx_hash": signed_box["tx_hash"]}
            if not m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="swap_submitted",
                    new_state="exit_settled", event_type="exit_settled", stage="exit",
                    settlement_in_atomic=settlement.input_atomic,
                    settlement_out_atomic=settlement.output_atomic,
                    tx_hash=signed_box["tx_hash"], created_at=now_iso,
                    detail={"settlement_source": settlement.source,
                            "receipt_audit_ok": bool(audit.ok)}):
                return {"status": "unknown", "reason": "settlement_write_conflict",
                        "tx_hash": signed_box["tx_hash"]}
            try:
                allowance = int(local["allowance_fn"](
                    decoded.path[0], str(custody).lower(), decoded.spender))
            except Exception:
                return {"status": "unknown", "reason": "allowance_unavailable",
                        "tx_hash": signed_box["tx_hash"]}
            if allowance == 0:
                m.append_lifi_event(
                    call_conn, lifecycle_id, expected_state="exit_settled",
                    new_state="allowance_zero", event_type="allowance_zero", stage="exit",
                    allowance_atomic=0, created_at=now_iso)
            elif revoke_and_wait(lifecycle, decoded.path[0], decoded.spender).status \
                    != "allowance_zero":
                return {"status": "unknown", "reason": "allowance_cleanup_unresolved",
                        "tx_hash": signed_box["tx_hash"]}
            received = (settlement.output_atomic / (10 ** int(sell_decimals))
                        * sell_token_usd)
            price = received / sold_qty
            if fraction == 1.0:
                realized = m.realized_after_exit(
                    call_conn, position["id"], received, position.get("entry_notional"))
                closed = m.finalize_lifi_close(
                    call_conn, lifecycle_id=lifecycle_id, expected_state="allowance_zero",
                    exit_kwargs={
                        "position_id": position["id"], "claim_id": claim_id,
                        "sig": signed_box["tx_hash"], "closed_at": now_iso,
                        "realized_pnl": realized, "exit_reason": kwargs["exit_reason"],
                        "fill_kind": m.full_exit_fill_kind(kwargs["exit_reason"]),
                        "price": price, "qty": sold_qty, "notional": received,
                        "quote_price": quote_usd / sold_qty,
                    },
                    created_at=now_iso,
                    final_state=("position_closed" if audit.ok
                                 else "receipt_authority_mismatch_closed"),
                )
                return ({"status": "closed", "realized": realized, "received": received,
                         "tx_hash": signed_box["tx_hash"]} if closed else
                        {"status": "unknown", "reason": "close_finalize_conflict",
                         "tx_hash": signed_box["tx_hash"]})
            m.book_derisk_fill(
                call_conn, position_id=position["id"], at=now_iso, price=price,
                qty=sold_qty, notional=received, fraction=fraction,
                tx_sig=signed_box["tx_hash"], claim_id=claim_id,
                quote_price=quote_usd / sold_qty,
            )
            m.append_lifi_event(
                call_conn, lifecycle_id, expected_state="allowance_zero",
                new_state=("position_open" if audit.ok
                           else "receipt_authority_mismatch_open"),
                event_type="partial_exit_settled", stage="exit",
                created_at=now_iso)
            return {"status": "derisked", "received": received,
                    "tx_hash": signed_box["tx_hash"]}

        result = execute_lifi_exit(
            slippage_ladder=kwargs["slippage_ladder"], quote_fn=quote_fn,
            execute_fn=execute, freshness_fn=freshness, cleanup_fn=cleanup,
            trace_fn=lambda stage, data: _trace(kwargs.get("trace"), "exit_" + stage, data),
        )
        latest = m.latest_lifi_lifecycle(call_conn, lifecycle_id)
        if latest and latest["state"] == "position_open" \
                and result.get("status") not in {"closed", "derisked", "unknown"}:
            release_claim()
        return result

    recovery_context.update({
        "chain_id": str(chain_id),
        "custody_wallet": str(custody).lower(), "release_sha": str(release_sha).lower(),
        "manifest_hash": str(manifest_hash).lower(), "quota_guard": guard,
        "effective_limit": guard.limit, "remaining": guard.remaining,
        "reset_s": guard.reset, "in_flight": 0, "custody_rpc": rpc_url,
        "proof_rpc": proof_rpc_url,
        "recover_confirmed_fn": recover_confirmed,
        "authenticate_fn": authenticate_fn,
    })
    return {
        "entry_fn": entry_fn, "preview_fn": preview_fn, "exit_fn": exit_fn,
        "recovery_context": recovery_context,
    }

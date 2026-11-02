"""Read one operator result from the existing entry and execution ledgers."""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_links import operator_links

logger = logging.getLogger(__name__)

LABELS = {
    "source_received": "Source received", "evaluating": "Evaluating",
    "retry_pending": "Retry scheduled", "transaction_unresolved": "Transaction unresolved",
    "submitted_unknown": "Submitted, confirmation unknown", "filled": "Bought",
    "refused": "Refused", "failed_before_submission": "Failed before submission",
    "transaction_failed": "Transaction failed", "no_fill": "No fill", "needs_review": "Needs review",
    "not_evaluated": "Not evaluated", "shadow": "Shadow observation",
    "funding_handoff": "Funding handoff",
}
# These terminal release reasons come from the runner or reviewed operator reconciliations.
_NO_FILL_REASONS = frozenset({
    "expired_unfound_proven", "dropped_blockhash_expired_proven",
    "dropped_blockhash_expired_operator_verified", "evm_nonces_superseded",
    "evm_confirmed_pretransaction_no_token_proven", "native_cleanup_recovered",
    "native_cleanup_confirmed_after_binding_conflict",
    "manual_reconcile_tx_absent_nonce_unconsumed",
    "evm_native_unwrap_rewrapped_proven_onchain",
    # 2026-09-11 (Alpha): resolved, no coin received, and not an on-chain failure of the swap.
    "nonce_cancel_confirmed", "broadcast_rejected", "execution_premium",
    # 2026-09-14 (SPERM ref 52a0ba39): the 4663 quote-over-source cap refused after the
    # pre-transaction, the cleanup re-wrap confirmed, and the signed row read "Needs review".
    "quote_over_source",
    # 2026-09-23 (LINTCHA ref 9021c685): the SAME shape from the pre-transaction itself. The
    # unwrap confirmed on chain (block 70750681), the swap never went out, the cleanup re-wrap
    # confirmed at nonce 2311, and the row still read "Needs review" -- so an operator was asked
    # to reconcile something already fully reconciled. Nothing is outstanding on these: no fill,
    # no position, and the native balance is back where it started.
    "pre_tx_failed", "pipeline_not_visible",
    # 2026-10-07 (X402ID, 8453 reservation 4885): the same shape again, the confirmed unwrap still
    # invisible to the swap after 3 s (evm.native_visible visible=false), re-wrap confirmed.
    "pre_tx_not_visible",
})
# A recorded chain verdict on the swap itself. The transaction landed and failed, or reverted.
# Before 2026-09-11 these fell through to "needs_review", an UNRESOLVED status, so every future
# on-chain failure (Solana 0x1771 slippage, an EVM min-out revert) would page as NEEDS REVIEW.
_CHAIN_FAILED_REASONS = frozenset({
    "tx_failed_recovery", "tx_failed", "transaction_reverted", "receipt_failed", "reverted",
    "approve_failed", "slot_window_expired", "rh_fast_reverted", "bsc_fast_reverted",
})
_CHAIN_FAILED_PREFIXES = ("chain_error:", "reverted_")


def _recorded_chain_failure(pending):
    """True when the ledger's terminal resolution says the transaction failed on chain."""
    if pending.get("status") != "failed":
        return False
    reason = pending.get("resolution_reason") or ""
    return reason in _CHAIN_FAILED_REASONS or reason.startswith(_CHAIN_FAILED_PREFIXES)


# A pre-transaction resolution only settles the row once its cleanup is PROVEN back on chain.
# Without that the native ETH is still out there and the row genuinely needs a human.
_PRE_TX_REASONS = frozenset({"pre_tx_failed", "pipeline_not_visible", "pre_tx_not_visible"})


def _recorded_no_fill(pending):
    """Read a terminal ledger resolution; do not infer a new chain result."""
    if pending.get("status") != "failed":
        return False
    reason = pending.get("resolution_reason") or ""
    if reason in _PRE_TX_REASONS:
        return str(pending.get("cleanup_status") or "") == "confirmed"
    if reason == "broadcast_rejected" and pending.get("cleanup_status") is not None:
        # 2026-10-07 (1937, 3350, 4581, 4817): a re-wrap was started, so an unwrap went out; the
        # rejection settles the row only once that re-wrap is confirmed. NULL = no unwrap made.
        return str(pending.get("cleanup_status")) == "confirmed"
    if reason in _NO_FILL_REASONS:
        return True
    if re.fullmatch(r"evm_entry_never_admitted: latest == pending == nonce \d+, wallet holds none of the coin, unmined for \d+s", reason):
        return True
    expiry = re.fullmatch(
        r"operator_recovery: blockhash expired \((\d+) < (\d+)\), sig absent from full history — crash orphaned the row before settle",
        reason)
    return bool(expiry and int(expiry[1]) < int(expiry[2]))


_ATTEMPT_FIELDS = (
    "attempt_id", "network_id", "decision_started_at", "terminal_status", "terminal_reason",
    "completed_at", "pending_id", "position_id", "fill_id", "tx_sig",
)
_GATE_FIELDS = frozenset({
    "visible", "wait_ms",                         # evm.native_visible: the unwrap the swap waited on
    "name", "completed_at", "allowed", "active", "reason", "code", "value", "threshold", "units",
    "outcome",
    "mcap", "volume_m5", "source_usd", "need_usd", "configured_ceiling", "effective_ceiling",
    "effective_ratio", "organic_volume_m5", "tested_volume_m5", "cap_pct", "roundtrip_pct",
    "buy_usd", "sell_back_usd", "roundtrip_basis", "buy_expected_atomic", "sell_input_atomic",
    "sell_output_atomic", "buy_quote_slot", "sell_quote_slot", "input_value", "input_threshold",
    "state", "effective", "rule_id", "version", "measurement_basis",
    "required_usd", "source_volume_excluded_usd", "boundary", "configured_m5_floor",
    "volume_h1", "h1_scope", "h1_floor", "h1_abs_floor", "waived_m5_reason",
    "independent_roundtrip_pct", "measured_roundtrip_pct", "measurement_status",
    "basis", "cost_pct", "scaled_sell_usd", "sell_expected_atomic", "buy_context_slot",
    "sell_context_slot", "used", "pre_bytes", "post_bytes", "limit", "fits",
    "floor_usd", "effective_floor_usd", "floor_tolerance_pct", "judged_usd", "prior_net_usd",
    "gap_pct", "source_price", "reference_price", "reference_basis", "reference_note", "quote_price",
    "impact_pct", "cap", "ran_pct", "buy_decimals",
})
_MEASUREMENT_FIELDS = frozenset({
    "status", "version", "reason", "buy_spent_atomic", "sell_received_atomic",
    "buy_received_atomic", "sell_spent_atomic", "simulation_slot", "slot", "cost_pct",
})


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _epoch(value):
    from datetime import datetime
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None


def _chain_timing(conn, **facts):
    """`_chain_times`, or None when it cannot be built: a display line never fails the view."""
    try:
        return _chain_times(conn, **facts)
    except Exception:
        logger.exception("entry view chain timing failed mint=%s", facts.get("mint"))
        return None


# Origins that name their source transaction; the rest after the first ':' is the detection's
# signature ('<chain>:<tx>:<mint>' on EVM, the bare signature on Solana).
_TX_NAMED_ORIGINS = ("onchain", "rhfast", "bscfast", "slotlane")


def _chain_times(conn, *, origin_event_id, network_id, mint, source_user_id, observed,
                 source_created_at, trust):
    """Times on CHAIN time (D-a, 2026-10-16, Alpha). DISPLAY ONLY.

    The J7 stamp is not chain time: our first sight minus the J7 stamp ran p50 +1.72 s / p90 +4.89 s
    on 4663 and p50 +2.6 s / p90 +5.6 s on 56 (brain finding 98b1498d); CLAWDMKT's J7 stamp read
    11:21:20 for a source block at 11:21:25. The anchor is the source block's timestamp, else, for a J7
    entry, our detection. A block timestamp is whole seconds, so an offset from it carries up to 1 s;
    a BSC row with the header's milliTimestamp (push_head) anchors on that instead, offsets in hundredths.

    Identity before proximity (Gamma B1/B2): an origin that names its transaction reads that one
    detection row and never a window, and its "our detection" is our own stamps of that transaction;
    with no row or no block time the card keeps its old "detected" line. A J7 entry uses the wallet's
    detection of the coin within 30 s only when it is the only one there (a drip buyer's earlier buy
    is not this one), and its J7 receipt is never "our detection". None otherwise."""
    lane, _, signature = str(origin_event_id or "").partition(":")
    j7 = _epoch(source_created_at) if trust == "claimed_untrusted" else None
    if lane in _TX_NAMED_ORIGINS and signature:
        found = m.onchain_detection_by_signature(conn, signature=signature,
                                                 source_user_id=source_user_id)
        if not found or not found["block_time"]:
            return None                  # "detected HH:MM:SS" already says it
        own = (observed, source_created_at if trust == "observed_detection" else None)
    elif j7 is not None:
        found = m.sole_onchain_detection_near(
            conn, source_user_id=source_user_id, network_id=network_id, mint=mint,
            around=observed or source_created_at)
        own = ()
    else:
        return None
    if found is None:
        return None                      # the J7 receipt alone is not our detection (B2)
    seen = [_epoch(v) for v in (found["detected_at"], *own)]
    seen = [v for v in seen if v is not None]
    detection = min(seen) if seen else None
    from sniper.settings import memecoin as _cfg
    fine = bool(found.get("block_ms")) and _cfg.MEMECOIN_CARD_BLOCK_MS_ENABLED
    if fine:                             # BSC header milliTimestamp (7eb5c92c): offsets in hundredths
        anchor, basis = found["block_ms"] / 1000.0, "chain"
    elif found["block_time"]:
        anchor, basis = float(found["block_time"]), "chain"
    elif detection is not None and j7 is not None:
        anchor, basis = detection, "our_detection"
    else:
        return None
    from datetime import datetime, timezone
    at = datetime.fromtimestamp(anchor, timezone.utc)
    clock = at.strftime("%H:%M:%S" if at.date() == datetime.now(timezone.utc).date()
                        else "%Y-%m-%d %H:%M:%S")
    offset = "%+.1fs"
    if fine:
        clock, offset = clock + ".%03d" % (at.microsecond // 1000), "%+.2fs"
    parts = ["%s %s" % ("chain" if basis == "chain" else "our detection", clock)]
    if basis == "chain" and detection is not None:
        parts.append((offset + " our detection") % (detection - anchor))
    if j7 is not None:
        parts.append((offset + " J7 stamp") % (j7 - anchor))
    return {"anchor_at": at.isoformat(), "basis": basis,
            "our_detection_at": (None if detection is None
                                 else datetime.fromtimestamp(detection, timezone.utc).isoformat()),
            "text": " · ".join(parts)}


def _gate_evidence(attempt):
    trace = _object(attempt.get("trace_json"))
    out = []
    for row in trace.get("substage_events", []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if not any(word in name for word in ("gate", "cap", "safety", "classification", "roundtrip",
                                             "oversize", "source_buy_floor", "chase_quote_confirmation",
                                             "slot_lane_skip", "native_visible")):
            continue
        out.append({k: v for k, v in row.items() if k in _GATE_FIELDS
                    and (v is None or isinstance(v, (str, int, float, bool)))})
        if name == "roundtrip_sequential" and isinstance(row.get("evidence"), dict):
            out[-1]["evidence"] = {k: v for k, v in row["evidence"].items() if k in _MEASUREMENT_FIELDS
                                   and (v is None or isinstance(v, (str, int, float, bool)))}
    return out


def entry_view(conn, *, origin_event_id=None, attempt_id=None, event=None):
    """Return one coherent read of the source and execution evidence."""
    with m.operator_read_snapshot(conn):
        return _entry_view_snapshot(conn, origin_event_id=origin_event_id,
                                    attempt_id=attempt_id, event=event)


def _entry_view_snapshot(conn, *, origin_event_id=None, attempt_id=None, event=None):
    """Return an exact attributed result. Keep historical verdicts separate."""
    if event is not None:
        event = dict(event) if isinstance(event, dict) or hasattr(event, "keys") else vars(event)
        origin_event_id = origin_event_id or event.get("fomo_event_id") or event.get("origin_event_id")
    if not origin_event_id and not attempt_id:
        return None
    evidence = m.entry_view_evidence(conn, origin_event_id=origin_event_id, attempt_id=attempt_id)
    source = evidence["event"] or event or {}
    chosen = evidence["chosen"] or {}
    origin_event_id = chosen.get("origin_event_id") or origin_event_id
    if not chosen and not source and not evidence["reservations"] and not evidence["fills"]:
        return None
    identity = chosen or source or (evidence["reservations"] or evidence["fills"])[0]
    net = m.normalized_coin_network(identity.get("network_id"), identity.get("mint"))
    mint = identity.get("mint")
    conflict = False

    def exact(rows):
        nonlocal conflict
        out = []
        for row in rows:
            row_net = m.normalized_coin_network(row.get("network_id"), row.get("mint") or mint)
            if row_net != net or (row.get("mint") and m.accounting_mark_key(net, row["mint"])
                                  != m.accounting_mark_key(net, mint)):
                conflict = True
                continue
            out.append(row)
        return out

    attempts = exact(evidence["attempts"])
    reservations = exact(evidence["reservations"])
    positions = exact(evidence["positions"])
    fills = exact(evidence["fills"])
    retry = evidence["retry"]
    if retry and not exact([retry]):
        retry = None
    pos_by_id = {p["id"]: p for p in positions}
    selected_fill = fills[0] if fills else None
    if selected_fill and any(
            r.get("attempt_id") != selected_fill.get("attempt_id") and r.get("status") == "reserved"
            and (r.get("tx_sig") or r.get("has_signed_payload") or r.get("cleanup_tx_sig"))
            for r in reservations):
        conflict = True
    if selected_fill and selected_fill.get("attempt_id"):
        chosen = next((a for a in attempts if a["attempt_id"] == selected_fill["attempt_id"]), chosen)
    pending = next((r for r in reservations if r.get("attempt_id") == chosen.get("attempt_id")), None)
    pending = pending or (reservations[0] if reservations else {})
    if not selected_fill:
        active_signed = next((r for r in reservations if r.get("status") == "reserved"
                              and (r.get("tx_sig") or r.get("has_signed_payload")
                                   or r.get("cleanup_tx_sig"))), None)
        if active_signed:
            pending = active_signed
            chosen = next((a for a in attempts if a["attempt_id"] == pending.get("attempt_id")),
                          {"attempt_id": pending.get("attempt_id")})
    position = pos_by_id.get(selected_fill.get("position_id")) if selected_fill else None
    terminal = str(chosen.get("terminal_status") or "")
    reason = chosen.get("terminal_reason") or source.get("real_decision_reason")
    signed = bool(pending.get("tx_sig") or pending.get("has_signed_payload")
                  or pending.get("cleanup_tx_sig") or chosen.get("tx_sig"))
    submitted = bool(pending.get("broadcast_at") or chosen.get("transaction_submitted_at"))
    resolution = pending.get("resolution_reason") or ""
    chain_failed = _recorded_chain_failure(pending)
    if conflict or len(fills) > 1 or any(len(evidence[k]) > 200 for k in ("attempts", "fills", "positions", "reservations")):
        status, reason = "needs_review", "Entry evidence has conflicting or incomplete identities."
    elif selected_fill and position:
        status = "filled"
        reason = "Confirmed entry fill recorded."
    elif not selected_fill and not positions and signed and _recorded_no_fill(pending):
        # SIGNED only (2026-09-11): an unsigned reservation released by a gate is a refusal and
        # keeps its own reason. OPAI read "No fill · execution_premium" for a refusal whose real
        # reason was "route quote is 1.52x the source price (cap 1.50x)".
        status, reason = "no_fill", resolution
    elif not selected_fill and not positions and chain_failed:
        status, reason = "transaction_failed", resolution
    elif selected_fill or positions or pending.get("recovery_status") == "orphaned" or pending.get("status") == "confirmed":
        status, reason = "needs_review", "The confirmed transaction and position accounting require reconciliation."
    elif chain_failed:
        status, reason = "transaction_failed", resolution
    elif signed and (pending.get("status") != "failed" or not resolution):
        status = "submitted_unknown" if submitted else "transaction_unresolved"
    elif terminal in ("submitted_unknown", "unknown", "anomaly", "transaction_unresolved") and not resolution:
        status = "submitted_unknown" if submitted else "transaction_unresolved"
    elif retry:
        status = "retry_pending"
    elif signed:
        status, reason = "needs_review", resolution or reason or "Transaction resolution requires review."
    elif pending.get("status") == "failed" and resolution in {
            "runner_stopped_before_signing", "never_broadcast"}:
        status, reason = "failed_before_submission", resolution
    elif terminal in ("refused", "safety_failed", "rejected", "unsafe", "route_failed",
                      "source_initiation_unknown"):
        # route_failed = no sellable route / exact-route simulation refused, before any signature
        # (a signed row is classified above). 47 in 30 days read "Needs review" and would page.
        status = "refused"
    elif terminal == "aborted" and pending.get("status") == "failed" and resolution in {
            "roundtrip_cap", "impact_cap", "token_safety", "entry_drift", "post_peak",
            "price_impact", "route_not_sellable"}:
        status = "refused"
    elif terminal == "aborted" and not pending and str(reason or "").startswith("interrupted:"):
        # The startup sweep closed an attempt a restart killed before any reservation (COINU,
        # 2026-09-17). Nothing was reserved or signed, so there is nothing to review.
        status = "failed_before_submission"
    elif terminal in ("aborted", "error", "failed"):
        status = "failed_before_submission" if pending.get("status") == "failed" else "needs_review"
    elif terminal == "filled":
        status, reason = "needs_review", "The attempt reports a fill, but its matching entry fill is absent."
    elif terminal == "shadow" and chosen.get("delivery_lane") == "j7_shadow" and not pending:
        status = "shadow"
        reason = "Pre-entry checks passed; reservation and quote checks not run. Fast lanes own execution."
    elif chosen or pending:
        status = "evaluating" if not terminal else "needs_review"
    elif source.get("real_decision") in ("not_attempted", "not_evaluated", "disabled", "hint_only"):
        status = "not_evaluated"
    elif source.get("real_decision"):
        status, reason = "needs_review", reason or "A decision exists without its attributed attempt."
    else:
        status = "source_received"
    if (status == "refused" and net == "solana"
            and chosen.get("signal_source") == "slot_lane" and not pending):
        # These unsent lane outcomes delegate to its async leg worker or the confirmed watcher.
        # They describe a funding path change, not the source buy's final funded result.
        from sniper.services.memecoin_slot_lane import EXOTIC_PENDING_REASON
        if (terminal == "source_initiation_unknown" and reason == "missing_receipt_proof"
                and chosen.get("delivery_lane") == "slot_lane_processed"):
            # AUTON e665ea25: the receipt-bound watcher filled after this processed observation.
            # Keep the admission refusal, but do not page it as the buy's final result.
            status = "funding_handoff"
            reason = "Fast lane awaits confirmed receipt proof; the confirmed watcher handles this buy."
        elif reason == EXOTIC_PENDING_REASON or str(reason or "") in {
                "trial_unmarked: slot lane carries a trial only SOL-funded",
                "trial_unmarked: slot lane SOL buffer short for the trial clip"}:
            status = "funding_handoff"
            reason = "Fast lane handed this buy to a different funding path; see the resulting entry."
    failed_swap = None
    if status == "refused" and pending.get("status") == "failed" and not signed:
        # A confirmed min-out resets the binding before retry. A later quote gate releases
        # that unsigned retry; it cannot erase the first swap's recorded chain failure.
        confirmations = _object(chosen.get("trace_json")).get("submission_events", [])
        if isinstance(confirmations, list):
            failed_swap = next((row for row in reversed(confirmations)
                                if isinstance(row, dict) and row.get("stage") == "chain_confirmation"
                                and row.get("outcome") == "failed"), None)
        if failed_swap:
            status = "transaction_failed"
            reason = "Swap failed on chain; retry refused: " + str(reason or resolution)
    fill_row = selected_fill or {}
    pos = position or {}
    tx_sig = fill_row.get("tx_sig") or pending.get("tx_sig") or chosen.get("tx_sig")
    if failed_swap:
        tx_sig = failed_swap.get("tx_sig") or tx_sig
    observed = chosen.get("observed_at") or source.get("received_at_normalized")
    if not observed and source.get("clock_domain") == "observed_real_utc":
        observed = source.get("received_at")
    contract = _object(pos.get("funded_contract_json"))
    policy = contract.get("policy") or {}
    # A fill's result time is the CONFIRMED fill (opened_recorded_at). The entry fill row's
    # created_at carries positions.opened_at, the decision start, which precedes the detection on a
    # fast lane: 66154 read "result time unknown (clocks disagree)" for a fill 1.44 s after it.
    completed_at = ((pos.get("opened_recorded_at") or fill_row.get("created_at"))
                    if status == "filled" else chosen.get("completed_at"))
    if status == "no_fill":
        completed_at = pending.get("last_recovery_check_at") if resolution in {
            "expired_unfound_proven", "dropped_blockhash_expired_proven"} else None
    elif status in {"failed_before_submission", "transaction_failed"} and terminal in {
            "", "unknown", "submitted_unknown", "transaction_unresolved", "anomaly"}:
        completed_at = None
    if status in {"filled", "no_fill", "refused", "failed_before_submission", "transaction_failed"}:
        at = completed_at
    else:
        at = (pending.get("last_recovery_check_at") or pending.get("broadcast_at")
              or chosen.get("completed_at") or chosen.get("decision_started_at") or observed)
    handle = chosen.get("source_handle") or source.get("source_handle")
    authority = {"state": "unknown", "network_id": net, "basis": "historical_evidence"}
    if chosen.get("terminal_reason") in ("not_approved", "unsupported_or_unarmed_network"):
        authority["state"] = "not_armed"
    from sniper.persistence.db_memecoin_notifications import notification_delivery_view
    delivery = notification_delivery_view(conn, network_id=net, origin_event_id=origin_event_id,
                                          attempt_id=chosen.get("attempt_id"))
    blocker = _object(chosen.get("gate_inputs_json")).get("duplicate_blocker")
    if not isinstance(blocker, dict):
        blocker = None
    if status == "refused" and blocker:
        if blocker.get("kind") == "reservation":
            reason = "Another entry attempt was in progress at decision time."
        elif blocker.get("kind") == "position":
            reason = "Open funded position at decision time."
    source_uid = chosen.get("source_user_id") or source.get("source_user_id")
    source_identity = m.entry_attempt_source_identity(conn, chosen) if status == "refused" else None
    native_owned = bool(status == "refused" and m.native_owned_secondary_refusal(conn, chosen))
    duplicate_same_source = native_owned or bool(status == "refused" and blocker and source_identity
        and _blocker_source(conn, blocker) == source_identity)
    substages = _object(chosen.get("trace_json")).get("substage_events", [])
    if status == "transaction_failed" and reason == "reverted_min_out":
        reason = "Price moved beyond the signed minimum output; no purchase"
    guard = next((row for row in substages
                  if isinstance(row, dict) and row.get("name") == "slot_guard_result"
                  and row.get("reason") == "slot_window_expired"), None)
    if status == "transaction_failed" and guard:
        reason = (f"Slot window expired: source {guard.get('source_slot')}, "
                  f"allowed through {guard.get('max_slot')}, landed {guard.get('landing_slot')}; "
                  "no tokens bought")
        if guard.get("fee_lamports") is not None:
            reason += f"; fee {guard['fee_lamports'] / 1e9:.9f} SOL"
    # pump.fun 6042: the tokens the spend would have bought vs the signed minimum, from the failed
    # receipt (solana_slot_executor.pump_slippage_failure). Curve tokens carry 6 decimals.
    pump_slippage = next((row for row in reversed(substages)
                          if isinstance(row, dict) and row.get("name") == "slot_pump_slippage"), None)
    if (status == "transaction_failed" and pump_slippage
            and isinstance(pump_slippage.get("min_tokens_atomic"), int)
            and isinstance(pump_slippage.get("available_tokens_atomic"), int)):
        price_limit = next((row for row in reversed(substages) if isinstance(row, dict)
                            and row.get("name") == "slot_price_limit"), {})
        decimals = price_limit.get("base_decimals")
        if decimals is None and not pump_slippage.get("program_id"):
            decimals = 6  # legacy curve-only receipt
        minimum, available = pump_slippage['min_tokens_atomic'], pump_slippage['available_tokens_atomic']
        amounts = f"minimum {minimum} atomic units, available {available}"
        if type(decimals) is int and 0 <= decimals <= 18:
            minimum_m = f"{minimum / 10**(decimals + 6):.2f}"
            available_m = f"{available / 10**(decimals + 6):.2f}"
            if minimum_m != available_m:
                amounts = f"minimum {minimum_m}M tokens, available {available_m}M"
        reason = f"Price moved beyond limit: {amounts}; no purchase"
        try:
            cost = price_limit.get('cost_usdc_atomic')
            references = (('source average', price_limit.get('source_average_price_usd')),
                          ('source post-trade', price_limit.get('source_price_usd')))
            for basis, source_price in references:
                if (type(decimals) is int and 0 <= decimals <= 18 and type(cost) is int and cost > 0
                        and type(available) is int and available > 0
                        and type(minimum) is int and minimum > 0
                        and type(source_price) in (int, float) and math.isfinite(source_price) and source_price > 0):
                    premium = ((cost / 1e6) / (available / 10**decimals) / source_price - 1) * 100
                    # The signed minimum includes every floor, not just the post-trade chase cap.
                    ceiling = ((cost / 1e6) / (minimum / 10**decimals) / source_price - 1) * 100
                    if math.isfinite(premium) and math.isfinite(ceiling):
                        reason += (f'; implied ticket price {premium:+.1f}% over {basis}'
                                   f' (signed limit {ceiling:+.1f}%)')
                        break
        except (OverflowError, ZeroDivisionError):
            pass
        if pump_slippage.get("fee_lamports") is not None:
            reason += f"; fee {pump_slippage['fee_lamports'] / 1e9:.9f} SOL"
    fast_lane_results = []
    if status == "shadow":
        for candidate in m.shadow_fast_lane_results(conn, chosen):
            projected = _entry_view_snapshot(conn, attempt_id=candidate["attempt_id"])
            if projected:
                fast_lane_results.append({k: projected.get(k) for k in
                                          ("attempt_id", "signal_source", "status", "reason")})
    return {
        "origin_event_id": origin_event_id, "event_id": source.get("id"),
        "attempt_id": chosen.get("attempt_id"), "network_id": net, "mint": mint,
        "ticker": (chosen.get("ticker") or source.get("ticker") or pos.get("ticker")
                   or _known_ticker(conn, net, mint)),
        "source_handle": handle, "source_user_id": chosen.get("source_user_id") or source.get("source_user_id"),
        "signal_source": chosen.get("signal_source") or source.get("signal_source"),
        "side": chosen.get("side") or source.get("side"),
        "source_size_usd": source.get("their_size_usd"),
        "source_valuation": _object(source.get("source_valuation_json")),
        "source_block_time": source.get("block_time"),
        "source_market_cap": chosen.get("source_market_cap") if chosen.get("source_market_cap") is not None else source.get("their_market_cap"),
        "source_price": chosen.get("source_price") if chosen.get("source_price") is not None else source.get("their_price"),
        "source_created_at": chosen.get("source_created_at") or source.get("created_at"),
        "socket_received_at": evidence.get("socket_received_at"),
        "source_timestamp_trust": source.get("source_timestamp_trust") or chosen.get("source_timestamp_trust"),
        "chain_timing": _chain_timing(
            conn, origin_event_id=origin_event_id, network_id=net, mint=mint,
            source_user_id=chosen.get("source_user_id") or source.get("source_user_id"),
            observed=observed, source_created_at=chosen.get("source_created_at") or source.get("created_at"),
            trust=source.get("source_timestamp_trust") or chosen.get("source_timestamp_trust")),
        "observed_at": observed, "decision_started_at": chosen.get("decision_started_at"),
        "decision_elapsed_ms": chosen.get("total_ms") if (
            (status == "filled" and terminal == "filled") or
            (status in {"refused", "shadow", "failed_before_submission", "transaction_failed"}
             and terminal in {"refused", "shadow", "failed", "aborted", "rejected", "skipped_chase"})) else None,
        "completed_at": completed_at, "status_updated_at": at,
        "status": status, "label": LABELS[status], "reason": reason or LABELS[status],
        "requested_usd": pending.get("reserved_notional_usd"), "pending_id": pending.get("id"),
        "reservation_status": pending.get("status"),
        "reservation_resolution_reason": pending.get("resolution_reason"),
        "position_id": pos.get("id") or fill_row.get("position_id"), "fill_id": fill_row.get("id"),
        "filled_notional": fill_row.get("notional"), "filled_price": fill_row.get("price"),
        "position_policy_id": policy.get("policy_id") if isinstance(policy, dict) else None,
        "position_is_open": bool(pos["is_open"]) if "is_open" in pos else None,
        "tx_sig": tx_sig, "submitted_at": pending.get("broadcast_at") or chosen.get("transaction_submitted_at"),
        "paper_action": source.get("action"), "authority": authority,
        "notification_delivery": delivery,
        "retry_next_at": retry.get("next_attempt_at") if retry else None,
        "gate_evidence": _gate_evidence(chosen),
        "addon": next((row for row in substages if isinstance(row, dict) and row.get("name") == "addon_context"),
                      None),
        "duplicate_blocker": blocker,
        "duplicate_same_source": duplicate_same_source,
        "native_owned_secondary_refusal": native_owned,
        "slot_guard": guard,
        "pump_slippage": pump_slippage,
        "fast_lane_results": fast_lane_results,
        # Fill timing (2026-10-02): "Landed S+n · why" from what is stored when the view is built; a
        # submitted attempt only, and never raises.
        "fill_timing_line": _fill_timing_line(conn, chosen),
        "attempts": [{k: a.get(k) for k in _ATTEMPT_FIELDS} for a in attempts[:200]],
        "attempts_truncated": len(evidence["attempts"]) > 200,
        "links": operator_links(net, mint, handle=handle, tx_sig=tx_sig,
                                origin_event_id=origin_event_id, position_id=pos.get("id")),
    }


def _fill_timing_line(conn, chosen):
    """Never breaks the alert's view (Gamma LOW1): any failure here reads as resolving."""
    try:
        from sniper.services.memecoin_fill_timing import view_line
        return view_line(conn, chosen)
    except Exception:
        logger.exception("fill timing line failed for attempt %s", (chosen or {}).get("attempt_id"))
        from sniper.services.memecoin_fill_timing import resolving_line
        return resolving_line((chosen or {}).get("network_id"))


def _blocker_source(conn, blocker):
    """Exact retained source tuple; position-only and opaque feed blockers stay independent."""
    try:
        if blocker.get("attempt_id"):
            row = conn.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id=?",
                               (blocker["attempt_id"],)).fetchone()
            if row:
                from sniper.persistence import db_memecoin as m
                return m.entry_attempt_source_identity(conn, dict(row))
    except sqlite3.Error:
        logger.warning("duplicate blocker source unreadable: %s", blocker, exc_info=True)
    return None


def _known_ticker(conn, network_id, mint):
    """The newest ticker any signal carried for this coin, or None.

    The on-chain watcher sees a trader's buy before any feed names the coin, so its attempt has no
    ticker; the board then showed the address fragment "JE3HT7Sb" for PENIS (2026-09-12). Indexed on
    `memecoin_events(mint)`; display only, never a decision input.
    """
    if not mint:
        return None
    try:
        row = conn.execute(
            "SELECT ticker FROM memecoin_events WHERE mint=? AND COALESCE(network_id,'')=? "
            "AND ticker IS NOT NULL AND ticker<>'' ORDER BY id DESC LIMIT 1",
            (mint, str(network_id or ""))).fetchone()
    except Exception:
        return None
    return row[0] if row else None


def recent_entry_views(conn, *, limit=100, chain=None, origin_event_id=None):
    """Return bounded entries with exact identity and current execution evidence."""
    if origin_event_id:
        row = entry_view(conn, origin_event_id=origin_event_id)
        rows = [row] if row else []
    else:
        rows = []
        for identity in m.recent_entry_identities(conn, limit=limit, chain=chain):
            row = entry_view(conn, origin_event_id=identity.get("origin_event_id"),
                             attempt_id=identity.get("attempt_id") if not identity.get("origin_event_id") else None)
            if row:
                rows.append(row)
    if chain:
        from sniper.services.memecoin_chains import resolve_chain, network_ids_for
        canonical = resolve_chain(chain)
        if canonical is None:
            raise ValueError("unknown chain")
        allowed = {m.normalized_coin_network(n, "") for n in network_ids_for(canonical)}
        rows = [r for r in rows if r["network_id"] in allowed]
    return rows

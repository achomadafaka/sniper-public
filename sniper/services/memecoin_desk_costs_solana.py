"""Pure receipt accounting for desk Solana charges; USD uses an explicit reference rate."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, localcontext

from sniper.services.memecoin_desk_universe import b58decode

USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
WSOL = "So11111111111111111111111111111111111111112"
_ID = re.compile(r"[A-Za-z0-9_.:/-]{1,200}\Z")
_SOURCE = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_EXIT_KINDS = frozenset({"bracket_exit", "manual_close", "mirror_exit", "derisk"})


def _uint(value, *, text=False):
    if text and type(value) is str and re.fullmatch(r"0|[1-9][0-9]*", value):
        value = int(value)
    if type(value) is not int or not 0 <= value < 2 ** 64:
        raise ValueError("atomic_integer_invalid")
    return value


def _key(value, size):
    try:
        return type(value) is str and len(value) <= size * 2 and len(b58decode(value)) == size
    except (TypeError, ValueError):
        return False


def _utc(value):
    try:
        if type(value) is not str:
            raise ValueError
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.utcoffset() != timedelta(0):
            raise ValueError
        return stamp
    except (TypeError, ValueError, OverflowError):
        raise ValueError("timestamp_invalid") from None


def _positive(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError("fill_number_invalid")
    return Decimal(str(value))


def _failed(error):
    if error is None:
        return False
    # Reuse the installed Solana enum parser rather than accepting arbitrary truthy JSON as failure.
    from solders.transaction_status import TransactionStatus
    try:
        TransactionStatus.from_json(json.dumps({"slot": 1, "confirmations": None,
            "status": {"Err": error}, "err": error, "confirmationStatus": "confirmed"}, allow_nan=False))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("receipt_error_invalid") from None
    return True


def _token_deltas(meta, pre, post, owner):
    sides = []
    for field in ("preTokenBalances", "postTokenBalances"):
        rows = meta.get(field)
        if type(rows) is not list:
            raise ValueError("token_balances_missing")
        balances = {}
        for row in rows:
            if type(row) is not dict or type(row.get("uiTokenAmount")) is not dict:
                raise ValueError("token_balance_invalid")
            index, mint, authority = row.get("accountIndex"), row.get("mint"), row.get("owner")
            amount, decimals = row["uiTokenAmount"].get("amount"), row["uiTokenAmount"].get("decimals")
            if (type(index) is not int or not 0 <= index < len(pre) or index in balances
                    or not _key(mint, 32) or not _key(authority, 32)
                    or type(amount) is not str or type(decimals) is not int or not 0 <= decimals <= 255
                    or (mint == USDC and decimals != 6) or (mint == WSOL and decimals != 9)):
                raise ValueError("token_balance_invalid")
            balances[index] = (mint, authority, _uint(amount, text=True), decimals)
        sides.append(balances)
    before, after = sides
    deltas, places, created, returned = {}, {}, 0, 0
    for index in before.keys() | after.keys():
        old, new = before.get(index), after.get(index)
        if old is None:
            if pre[index] != 0:
                raise ValueError("token_pre_balance_missing")
            old = (new[0], new[1], 0, new[3])
        if new is None:
            if post[index] != 0:
                raise ValueError("token_post_balance_missing")
            new = (old[0], old[1], 0, old[3])
        if (old[0], old[1], old[3]) != (new[0], new[1], new[3]):
            raise ValueError("token_identity_mismatch")
        mint, authority, _, decimals = old
        if authority != owner:
            continue
        if mint in places and places[mint] != decimals:
            raise ValueError("token_decimals_mismatch")
        places[mint] = decimals
        deltas[mint] = deltas.get(mint, 0) + new[2] - old[2]
        wrapped_old, wrapped_new = (old[2], new[2]) if mint == WSOL else (0, 0)
        if pre[index] < wrapped_old or post[index] < wrapped_new:
            raise ValueError("wrapped_sol_balance_mismatch")
        if index not in before:
            created += post[index] - wrapped_new
        if index not in after:
            returned += pre[index] - wrapped_old
    return deltas, places, created, returned


def _trade_principal(request, fill, deltas, places, fee, created, returned):
    if type(fill) is not dict or fill.get("kind") not in {"entry", *_EXIT_KINDS}:
        raise ValueError("fill_missing_or_unsupported")
    for key in ("tx_sig", "position_id", "network_id"):
        if key in fill and fill[key] != request.get(key):
            raise ValueError("fill_identity_mismatch")
    mint, entry = request["mint"], fill["kind"] == "entry"
    _positive(fill.get("qty"))
    notional = _positive(fill.get("notional"))
    # The fill writer persists atoms / 10**decimals as a float; retain its rounding.
    if mint not in places or fill["qty"] != (
            deltas.get(mint, 0) if entry else -deltas.get(mint, 0)) / 10 ** places[mint]:
        raise ValueError("fill_quantity_mismatch")
    funding = fill.get("funding_asset")
    if funding not in (None, "usdc", "sol") or (funding == "sol" and not entry):
        raise ValueError("funding_asset_unsupported")
    principal = 0
    if funding == "sol":
        principal = _uint(fill.get("funding_spent_atomic"), text=True)
        if principal == 0 or deltas.get(USDC, 0) != 0:
            raise ValueError("native_principal_unproved")
        if fill.get("funding_sol_usd") is not None:
            expected = Decimal(principal) / Decimal(10 ** 9) * _positive(fill["funding_sol_usd"])
            if abs(expected - notional) > Decimal("0.000001"):
                raise ValueError("native_notional_mismatch")
    elif notional * 10 ** 6 != (-deltas.get(USDC, 0) if entry else deltas.get(USDC, 0)):
        raise ValueError("fill_notional_mismatch")
    for column, actual in (("network_fee_lamports", fee), ("rent_lamports", created),
                           ("returned_rent_lamports", returned)):
        if fill.get(column) is not None and _uint(fill[column]) != actual:
            raise ValueError("stored_fee_mismatch")
    if fill.get("priority_fee_lamports") is not None and _uint(fill["priority_fee_lamports"]) > fee:
        raise ValueError("stored_priority_fee_mismatch")
    return principal


def _fx(quote, event, known):
    if quote is None:
        raise ValueError("usd_quote_missing")
    if (type(quote) is not dict or set(quote) != {"price", "observed_at", "known_at", "source"}
            or type(quote["source"]) is not str or not _SOURCE.fullmatch(quote["source"])):
        raise ValueError("usd_quote_invalid")
    rate = _positive(quote["price"])
    observed, available = _utc(quote["observed_at"]), _utc(quote["known_at"])
    if not observed <= event or event - observed > timedelta(seconds=60) or not observed <= available <= known:
        raise ValueError("usd_quote_time_invalid")
    return rate, {"price": float(rate), "observed_at": observed.isoformat(),
                  "known_at": available.isoformat(), "source": quote["source"]}


def price_solana_expense(request, receipt, *, owner, usd_quote, known_at):
    """Exact non-trade SOL atoms, valued at a causal accounting reference rate. No I/O or mutation."""
    try:
        if (type(request) is not dict or request.get("network_id") != "solana"
                or any(type(request.get(k)) is not str or not _ID.fullmatch(request[k])
                       for k in ("cohort_id", "event_id"))
                or not _key(owner, 32) or not _key(request.get("mint"), 32)
                or request["mint"] in (USDC, WSOL) or not _key(request.get("tx_sig"), 64)):
            raise ValueError("expense_identity_invalid")
        _utc(request.get("occurred_at"))
        known = _utc(known_at)
        if type(receipt) is not dict or type(receipt.get("meta")) is not dict:
            raise ValueError("receipt_missing")
        slot, block = _uint(receipt.get("slot")), _uint(receipt.get("blockTime"))
        event = datetime.fromtimestamp(block, timezone.utc)
        if slot == 0 or event > known:
            raise ValueError("receipt_time_invalid")
        transaction, meta = receipt["transaction"], receipt["meta"]
        keys, signatures = transaction["message"]["accountKeys"], transaction["signatures"]
        if (type(keys) is not list or not 1 <= len(keys) <= 256
                or any(type(k) is not dict or not _key(k.get("pubkey"), 32) for k in keys)
                or len({k["pubkey"] for k in keys}) != len(keys)
                or keys[0].get("pubkey") != owner or keys[0].get("signer") is not True
                or type(signatures) is not list or not signatures or signatures[0] != request["tx_sig"]
                or any(not _key(sig, 64) for sig in signatures)):
            raise ValueError("receipt_identity_mismatch")
        pre, post = meta["preBalances"], meta["postBalances"]
        if type(pre) is not list or type(post) is not list or len(pre) != len(keys) or len(post) != len(keys):
            raise ValueError("native_balances_missing")
        for value in (*pre, *post):
            _uint(value)
        fee = _uint(meta["fee"])
        failed = _failed(meta["err"])
        native, wrapped, principal, created, returned = pre[0] - post[0], 0, 0, 0, 0
        if failed:
            if request.get("fill") is not None:
                raise ValueError("failed_receipt_fill_conflict")
            if native != fee or pre[1:] != post[1:]:
                raise ValueError("failed_receipt_balance_mismatch")
            net = fee
        else:
            deltas, places, created, returned = _token_deltas(meta, pre, post, owner)
            with localcontext() as ctx:
                ctx.prec = 100
                principal = _trade_principal(request, request.get("fill"), deltas, places, fee, created, returned)
            wrapped = -deltas.get(WSOL, 0)
            net = native + wrapped - principal
        execution, refund = max(fee, net), max(fee - net, 0)
        fx, execution_usd, refund_usd = None, 0.0, 0.0
        if execution or refund:
            rate, fx = _fx(usd_quote, event, known)
            with localcontext() as ctx:
                ctx.prec = 100
                execution_usd = float(Decimal(execution) / 10 ** 9 * rate)
                refund_usd = float(Decimal(refund) / 10 ** 9 * rate)
            if (not math.isfinite(execution_usd) or not math.isfinite(refund_usd)
                    or (execution and execution_usd == 0) or (refund and refund_usd == 0)):
                raise ValueError("usd_amount_invalid")
        try:
            encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise ValueError("receipt_serialization_invalid") from None
        evidence = {key: request[key] for key in ("cohort_id", "event_id", "network_id", "mint", "tx_sig")}
        evidence.update(receipt_hash=hashlib.sha256(encoded.encode()).hexdigest(), slot=slot,
            event_at=event.isoformat(), known_at=known.isoformat(), receipt_status="failed" if failed else "success",
            native_debit_lamports=native, wsol_debit_lamports=wrapped, trade_principal_lamports=principal,
            network_fee_lamports=fee, created_rent_lamports=created, returned_rent_lamports=returned,
            net_cost_lamports=net, execution_lamports=execution, refund_lamports=refund,
            usd_basis="accounting_reference_rate", usd_quote=fx)
        return {"execution_usd": execution_usd, "refund_usd": refund_usd, "evidence": evidence}
    except (KeyError, IndexError, TypeError, AttributeError, OverflowError, InvalidOperation, RecursionError):
        raise ValueError("expense_evidence_malformed") from None

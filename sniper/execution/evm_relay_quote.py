"""Validated Relay same-chain quotes for Robinhood chain 4663."""
from __future__ import annotations

import json
import hashlib
import os
from functools import lru_cache
from urllib.error import HTTPError

from eth_abi import decode, encode
from eth_utils import keccak


RELAY_QUOTE_URL = "https://api.relay.link/quote/v2"
RELAY_APPROVAL_PROXY = "0xccc88a9d1b4ed6b0eaba998850414b24f1c315be"
RELAY_ROUTER = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
RELAY_APPROVAL_PROXY_CODE_HASH = (
    "0xa3b36a6aa3c2fb588b92336d88b0735b40dea0282c110d2034edf688ca661b5b"
)
RELAY_ROUTER_CODE_HASH = (
    "0xde894e5c12e9513d50613c8fff375ecbf19b5d9a53bec0662e2b9667e3ec8f15"
)
TRANSFER_AND_MULTICALL = "f9e4bab4"
_APPROVE = "095ea7b3"
_CLEANUP_ERC20S = "9bb43718"
# Non-proxy AggregatorGuard.IceCreamSwap: checks the recipient's net balance delta.
# Sourcify exact-match source + chain-owner RPC, 2026-10-06 (TENET regression fixture).
RELAY_OUTPUT_GUARD = "0x603206d6105217dd972e4ab30676a220ca393346"
RELAY_OUTPUT_GUARD_CODE_HASH = "0x5c9dd55aa2b736702f8cc44aef400fca06ea1b708f35f32978bc4ba37ec4d30f"
_TRANSFER_TYPES = ["address[]", "uint256[]", "(address,bool,uint256,bytes)[]",
                   "address", "address", "bytes"]


class RelayQuoteError(RuntimeError):
    def __init__(self, failure_type):
        super().__init__(failure_type)
        self.failure_type = failure_type


@lru_cache(maxsize=8)
def _live_code_hash(rpc_url, address, http=None):
    import time
    from sniper.execution.evm_wallet import _rpc

    for attempt in range(3):
        try:
            result = _rpc(rpc_url, "eth_getCode", [address, "latest"], http=http)["result"]
            break
        except Exception:
            if attempt == 2:
                raise
            time.sleep(0.2 * (2 ** attempt))
    raw = bytes.fromhex(str(result)[2:])
    if not raw:
        raise RelayQuoteError("code_mismatch")
    return "0x" + keccak(raw).hex()


def warm_relay_code_hashes(rpc_url, *, http=None, output_guard=False):
    """Verify fixed contracts; the optional output guard is verified at startup only."""
    expected = {
        RELAY_APPROVAL_PROXY: RELAY_APPROVAL_PROXY_CODE_HASH,
        RELAY_ROUTER: RELAY_ROUTER_CODE_HASH,
    }
    if output_guard:
        expected[RELAY_OUTPUT_GUARD] = RELAY_OUTPUT_GUARD_CODE_HASH
    for address, code_hash in expected.items():
        try:
            actual = str(_live_code_hash(rpc_url, address, http=http)).lower()
        except RelayQuoteError:
            raise
        except Exception as exc:
            raise RelayQuoteError("rpc_transport") from exc
        if actual != code_hash:
            raise RelayQuoteError("code_mismatch")
    return True


def _payload(http, body, timeout=12):
    """One Relay quote request. `timeout` is the caller's budget, not a constant.

    THE 12 s DEFAULT IS LONGER THAN THE LADDER LIVES (E4, 2026-09-23). The entry ladder's whole
    deadline is `MEMECOIN_EVM_LADDER_WAIT_S`, 8 s by default, so a caller that spends this budget
    unbounded can push a rung past the deadline and lose a forward quote that had already
    answered. The default is unchanged for every caller that has its own time to spend; the
    round-trip measurement passes what is actually left.
    """
    from sniper.execution.evm_wallet import _http

    headers = {"content-type": "application/json", "accept": "application/json"}
    key = os.getenv("MEMECOIN_RELAY_API_KEY", "").strip()
    if key:
        headers["x-api-key"] = key
    from sniper.execution.evm_wallet import http_tap
    try:
        # Measurement only: the ladder's provider HTTP tap (host + label + RTT + status, no URL).
        with http_tap("relay:quote", RELAY_QUOTE_URL) as tap:
            try:
                status, payload, _response_headers = (http or _http)(
                    "POST", RELAY_QUOTE_URL, headers=headers, body=json.dumps(body),
                    timeout=timeout,
                )
                tap["outcome"] = "http_%s" % status
            except HTTPError as exc:
                tap["outcome"] = "http_%s" % exc.code
                raise
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode())
        except Exception:
            payload = {}
        status = exc.code
    except TimeoutError as exc:
        raise RelayQuoteError("timeout") from exc
    except Exception as exc:
        raise RelayQuoteError("rpc_transport") from exc
    if int(status) == 429:
        raise RelayQuoteError("rate_limit")
    if int(status) == 400:
        if str(payload.get("errorCode") or "").upper() == "SWAP_IMPACT_TOO_HIGH":
            raise RelayQuoteError("route_capacity")
        text = " ".join(str(payload.get(key) or "") for key in ("message", "errorCode"))
        if "route" in text.lower() or "quote" in text.lower():
            return None
    if int(status) < 200 or int(status) >= 300:
        raise RelayQuoteError("provider_error")
    if not isinstance(payload, dict):
        raise RelayQuoteError("malformed_response")
    return payload


def _one_item(step, step_id):
    if not isinstance(step, dict) or step.get("id") != step_id \
            or step.get("kind") != "transaction":
        raise RelayQuoteError("malformed_response")
    items = step.get("items")
    if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
        raise RelayQuoteError("malformed_response")
    data = items[0].get("data")
    if not isinstance(data, dict):
        raise RelayQuoteError("malformed_response")
    return data


def _same_address(left, right):
    return str(left or "").lower() == str(right or "").lower()


def _validate_cleanup(calls, buy_token, taker):
    for target, allow_failure, value, call_data in calls:
        if allow_failure or int(value) != 0:
            raise RelayQuoteError("malformed_response")
        if not _same_address(target, RELAY_ROUTER) or call_data[:4].hex() != _CLEANUP_ERC20S:
            continue
        try:
            tokens, recipients, _minimums, _metadata = decode(
                ["address[]", "address[]", "uint256[]", "bytes"], call_data[4:]
            )
        except Exception as exc:
            raise RelayQuoteError("malformed_response") from exc
        if any(_same_address(token, buy_token) and _same_address(recipient, taker)
               for token, recipient in zip(tokens, recipients)):
            return
    raise RelayQuoteError("malformed_response")


def _decode_transaction(data, sell_token, buy_token, amount, taker):
    raw = str(data or "")
    if not raw.startswith("0x" + TRANSFER_AND_MULTICALL):
        raise RelayQuoteError("malformed_response")
    try:
        args = decode(_TRANSFER_TYPES, bytes.fromhex(raw[10:]))
        tokens, amounts, calls, refund_to, nft_recipient, _metadata = args
    except Exception as exc:
        raise RelayQuoteError("malformed_response") from exc
    if (len(tokens) != 1 or len(amounts) != 1
            or not _same_address(tokens[0], sell_token) or int(amounts[0]) != amount
            or not _same_address(refund_to, taker) or not _same_address(nft_recipient, taker)
            or not calls):
        raise RelayQuoteError("malformed_response")
    _validate_cleanup(calls, buy_token, taker)
    return args


def bind_relay_output_minimum(quote, *, guard_verified):
    """Bind a supported terminal guard to the wallet, without I/O or cache mutation."""
    from sniper.execution.lifi_route import decode_exact, LifiQuoteError
    if guard_verified is not True:
        return None
    try:
        sell, buy, taker, amount = quote['_sniper_relay_request']
        tx = quote['transaction']
        if (not _same_address(tx['to'], RELAY_APPROVAL_PROXY)
                or tx.get('value') not in (0, '0', '0x0')
                or int(quote['sellAmount']) != amount):
            return None
        args = list(_decode_transaction(tx['data'], sell, buy, amount, taker))
        raw = bytes.fromhex(tx['data'][10:])
        # Relay appends its request ID outside the ABI payload. Preserve that exact trailer.
        request_id = bytes.fromhex(str(quote['route'].get('request_id') or '')[2:])
        trailer = request_id if len(request_id) == 32 and raw.endswith(request_id) else b''
        if raw != encode(_TRANSFER_TYPES, args) + trailer:
            return None
        calls = list(args[2])
        if len(calls) < 2 or any(fail or value for _, fail, value, _ in calls):
            return None
        target, _, _, data = calls[-2]
        cleanup = calls[-1]
        if (target != RELAY_OUTPUT_GUARD or data[:4].hex() != '3f0bde25' or len(data) < 139
                or '0x' + data[119:139].hex() != buy
                or '0x' + data[79:99].hex() not in (RELAY_ROUTER, taker)
                or cleanup[0] != RELAY_ROUTER or cleanup[3][:4].hex() != _CLEANUP_ERC20S):
            return None
        tokens, recipients, amounts, _ = decode_exact(
            ['address[]', 'address[]', 'uint256[]', 'bytes'], cleanup[3][4:])
        if tokens != (buy,) or recipients != (taker,) or amounts != (0,):
            return None
        minimum = int(quote['minBuyAmount'])
        if minimum <= 0:
            return None
        minimum = max(minimum, int.from_bytes(data[42:58], 'big'))
        if not minimum <= int(quote['buyAmount']) or minimum >= 2**128:
            return None
        # executeSwap receives this same recipient field through msg.data[26:].
        data = data[:42] + minimum.to_bytes(16, 'big') + data[58:79] + bytes.fromhex(taker[2:]) + data[99:]
        calls[-2] = (target, False, 0, data)
        args[2] = calls
        calldata = '0x' + TRANSFER_AND_MULTICALL + (encode(_TRANSFER_TYPES, args) + trailer).hex()
        return {**quote, 'minBuyAmount': str(minimum), 'transaction': {**tx, 'data': calldata},
                '_sniper_relay_output_minimum': (minimum, str(amount),
                                                hashlib.sha256(calldata.encode()).hexdigest())}
    except (KeyError, TypeError, ValueError, OverflowError, RelayQuoteError, LifiQuoteError):
        return None


def relay_output_minimum(quote):
    """Recover our startup-verified proof only while its executable bytes still match."""
    try:
        minimum, amount, digest = quote['_sniper_relay_output_minimum']
        tx = quote['transaction']
        if (_same_address(tx['to'], RELAY_APPROVAL_PROXY) and tx.get('value') in (0, '0', '0x0')
                and str(quote['sellAmount']) == amount and int(quote['minBuyAmount']) == minimum
                and hashlib.sha256(tx['data'].encode()).hexdigest() == digest):
            return minimum
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return None


def dex_relay_quote(chain, sell_token, buy_token, amount_atomic, taker, *, http=None,
                    slippage_bps=None, rpc_url=None, code_hash_fn=None, timeout=12):
    """Return one validated atomic Relay swap, or None when Relay has no route."""
    if str(chain) != "4663":
        return None
    sell, buy, user = str(sell_token).lower(), str(buy_token).lower(), str(taker).lower()
    try:
        amount = int(amount_atomic)
        if slippage_bps is None:
            from sniper.settings import memecoin as cfg
            slippage_bps = int(round(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT * 100))
        slippage_bps = int(slippage_bps)
    except (TypeError, ValueError):
        return None
    if amount <= 0 or not 0 <= slippage_bps < 10_000:
        return None
    body = {
        "user": user, "recipient": user, "refundTo": user,
        "originChainId": 4663, "destinationChainId": 4663,
        "originCurrency": sell, "destinationCurrency": buy,
        "amount": str(amount), "tradeType": "EXACT_INPUT",
        "forceSolverExecution": False, "slippageTolerance": str(slippage_bps),
    }
    payload = _payload(http, body, timeout=timeout)
    if payload is None:
        return None
    steps = payload.get("steps")
    if not isinstance(steps, list) or len(steps) not in {1, 2}:
        raise RelayQuoteError("malformed_response")
    approval = _one_item(steps[0], "approve") if len(steps) == 2 else None
    transaction = _one_item(steps[-1], "swap")
    details = payload.get("details")
    if not isinstance(details, dict) or details.get("operation") != "swap" \
            or not _same_address(details.get("sender"), user) \
            or not _same_address(details.get("recipient"), user):
        raise RelayQuoteError("malformed_response")
    currency_in, currency_out = details.get("currencyIn"), details.get("currencyOut")
    if not isinstance(currency_in, dict) or not isinstance(currency_out, dict):
        raise RelayQuoteError("malformed_response")
    in_currency, out_currency = currency_in.get("currency"), currency_out.get("currency")
    try:
        output = int(currency_out.get("amount"))
        minimum = int(currency_out.get("minimumAmount"))
    except (TypeError, ValueError):
        raise RelayQuoteError("malformed_response")
    if (not isinstance(in_currency, dict) or not isinstance(out_currency, dict)
            or int(in_currency.get("chainId")) != 4663 or int(out_currency.get("chainId")) != 4663
            or not _same_address(in_currency.get("address"), sell)
            or not _same_address(out_currency.get("address"), buy)
            or int(currency_in.get("amount")) != amount or not 0 < minimum <= output):
        raise RelayQuoteError("malformed_response")
    if approval is not None:
        approve_data = str(approval.get("data") or "")
        if (int(approval.get("chainId")) != 4663
                or not _same_address(approval.get("to"), sell)
                or str(approval.get("value")) not in {"0", "0x0"}
                or not approve_data.startswith("0x" + _APPROVE)):
            raise RelayQuoteError("malformed_response")
        try:
            spender, approved_amount = decode(
                ["address", "uint256"], bytes.fromhex(approve_data[10:])
            )
        except Exception as exc:
            raise RelayQuoteError("malformed_response") from exc
        if not _same_address(spender, RELAY_APPROVAL_PROXY) or int(approved_amount) != amount:
            raise RelayQuoteError("malformed_response")
    if (int(transaction.get("chainId")) != 4663
            or not _same_address(transaction.get("from"), user)
            or not _same_address(transaction.get("to"), RELAY_APPROVAL_PROXY)
            or str(transaction.get("value")) not in {"0", "0x0"}):
        raise RelayQuoteError("malformed_response")
    _decode_transaction(transaction.get("data"), sell, buy, amount, user)
    if code_hash_fn is None:
        if not rpc_url:
            from sniper.settings import memecoin as cfg
            rpc_url = cfg.evm_rpc_for_chain("4663")
        if not rpc_url:
            raise RelayQuoteError("rpc_transport")
        warm_relay_code_hashes(rpc_url, http=http)
    else:
        expected = {
            RELAY_APPROVAL_PROXY: RELAY_APPROVAL_PROXY_CODE_HASH,
            RELAY_ROUTER: RELAY_ROUTER_CODE_HASH,
        }
        for address, code_hash in expected.items():
            try:
                actual = str(code_hash_fn(address)).lower()
            except RelayQuoteError:
                raise
            except Exception as exc:
                raise RelayQuoteError("rpc_transport") from exc
            if actual != code_hash:
                raise RelayQuoteError("code_mismatch")
    request_id = steps[-1].get("requestId")
    return {
        "sellAmount": str(amount), "buyAmount": str(output), "minBuyAmount": str(minimum),
        "liquidityAvailable": True,
        "transaction": {
            "to": RELAY_APPROVAL_PROXY, "data": transaction["data"], "value": "0x0",
            "gas": str(transaction.get("gas") or "900000"),
        },
        "issues": {"allowance": {"spender": RELAY_APPROVAL_PROXY}},
        "route": {"protocol": "relay_same_chain", "request_id": request_id},
        "_sniper_relay_request": (sell, buy, user, amount),
    }

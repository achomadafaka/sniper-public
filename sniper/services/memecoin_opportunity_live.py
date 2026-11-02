"""Read-only live-source framing, parsing, recovery, and 4663 route probes."""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace
from typing import Callable, Iterable

from eth_abi import decode, encode
from eth_utils import keccak

from sniper.util.clock import real_utc_now_iso
from sniper.research.memecoin_opportunity.engine import RemoteRoundTripProbe
from sniper.research.memecoin_opportunity.bags_4663 import (
    BAGS_LENS,
    enrich_bags_4663_state,
)
from sniper.research.memecoin_opportunity.pons_4663 import PONS_WETH
from sniper.research.memecoin_opportunity.types import VenueId


_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ANCHOR_EVENT_CPI_PREFIX = bytes.fromhex("e445a52e51cb9a1d")


def _base58_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError("base58 value must be non-empty")
    number = 0
    for character in value:
        try:
            digit = _BASE58_ALPHABET.index(character)
        except ValueError as exc:
            raise ValueError("invalid base58 character") from exc
        number = number * 58 + digit
    zeros = len(value) - len(value.lstrip("1"))
    decoded = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\0" * zeros + decoded


def program_cpi_event_logs(
    transaction: object,
    program_id: str,
) -> tuple[tuple[int, str], ...]:
    """Extract Anchor event-CPI payloads with stable instruction-tree order."""
    if not isinstance(transaction, dict):
        return ()
    meta = transaction.get("meta")
    message = ((transaction.get("transaction") or {}).get("message") or {})
    if not isinstance(meta, dict) or not isinstance(message, dict):
        return ()
    account_keys = list(message.get("accountKeys") or [])
    loaded = meta.get("loadedAddresses") or {}
    account_keys.extend(loaded.get("writable") or [])
    account_keys.extend(loaded.get("readonly") or [])
    account_keys = [
        str(item.get("pubkey")) if isinstance(item, dict) else str(item)
        for item in account_keys
    ]
    found: list[tuple[int, str]] = []
    for group in meta.get("innerInstructions") or []:
        if not isinstance(group, dict):
            continue
        try:
            outer_index = int(group["index"])
        except (KeyError, TypeError, ValueError):
            continue
        for inner_index, instruction in enumerate(group.get("instructions") or []):
            if not isinstance(instruction, dict):
                continue
            instruction_program = instruction.get("programId")
            if instruction_program is None:
                try:
                    instruction_program = account_keys[int(instruction["programIdIndex"])]
                except (KeyError, TypeError, ValueError, IndexError):
                    continue
            if str(instruction_program) != program_id:
                continue
            try:
                raw = _base58_decode(str(instruction.get("data") or ""))
            except ValueError:
                continue
            if len(raw) < 16 or raw[:8] != _ANCHOR_EVENT_CPI_PREFIX:
                continue
            event_index = (outer_index << 16) | inner_index
            encoded = base64.b64encode(raw[8:]).decode("ascii")
            found.append((event_index, f"Program data: {encoded}"))
    return tuple(sorted(found))


def solana_program_subscribe_frame(program_id: str, *, request_id: int) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "logsSubscribe",
            "params": [{"mentions": [program_id]}, {"commitment": "confirmed"}],
        },
        separators=(",", ":"),
    )


def evm_logs_subscribe_frame(topic0s: Iterable[str], *, request_id: int) -> str:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "eth_subscribe",
            "params": ["logs", {"topics": [list(topic0s)]}],
        },
        separators=(",", ":"),
    )


_INVOKE = re.compile(r"^Program (\S+) invoke \[")
_DONE = re.compile(r"^Program (\S+) (?:success|failed:)")


def program_data_logs(logs: Iterable[str], program_id: str) -> tuple[tuple[int, str], ...]:
    stack: list[str] = []
    found = []
    for index, line in enumerate(logs):
        invoke = _INVOKE.match(line)
        if invoke:
            stack.append(invoke.group(1))
            continue
        if line.startswith("Program data: ") and stack and stack[-1] == program_id:
            found.append((index, line))
            continue
        done = _DONE.match(line)
        if done and stack:
            if stack[-1] == done.group(1):
                stack.pop()
            elif done.group(1) in stack:
                stack = stack[: stack.index(done.group(1))]
    return tuple(found)


def parse_solana_log_notification(message: object) -> dict[str, object] | None:
    if not isinstance(message, dict):
        return None
    params = message.get("params") or {}
    result = params.get("result") or {}
    context = result.get("context") or {}
    value = result.get("value") or {}
    if not value.get("signature") or context.get("slot") is None:
        return None
    return {
        "subscription": params.get("subscription"),
        "slot": int(context["slot"]),
        "signature": str(value["signature"]),
        "err": value.get("err"),
        "logs": list(value.get("logs") or []),
    }


def parse_evm_log_notification(message: object) -> dict[str, object] | None:
    if not isinstance(message, dict):
        return None
    log = (message.get("params") or {}).get("result")
    if not isinstance(log, dict) or not log.get("transactionHash"):
        return None
    try:
        return {
            "block_or_slot": int(log["blockNumber"], 16),
            "transaction_index": int(log["transactionIndex"], 16),
            "event_index": int(log["logIndex"], 16),
            "block_hash": str(log["blockHash"]).lower(),
            "transaction_id": str(log["transactionHash"]).lower(),
            "confirmation": "orphaned" if log.get("removed") else "tentative",
            "log": log,
        }
    except (KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class SolanaRecoveryResult:
    signatures: tuple[str, ...]
    complete: bool
    resume_cursor: str | None


def bounded_solana_recovery(
    fetch_page: Callable[[str | None], tuple[list[dict[str, object]], str | None]],
    *,
    previous_durable_signature: str | None,
    max_pages: int,
    start_before: str | None = None,
    not_before_timestamp_s: int | None = None,
) -> SolanaRecoveryResult:
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    before = start_before
    signatures: list[str] = []
    seen = set()
    for _ in range(max_pages):
        rows, next_before = fetch_page(before)
        for row in rows:
            signature = str(row.get("signature") or "")
            if not signature:
                continue
            if signature == previous_durable_signature:
                return SolanaRecoveryResult(tuple(signatures), True, None)
            block_time = row.get("blockTime")
            if (
                previous_durable_signature is None
                and not_before_timestamp_s is not None
                and isinstance(block_time, (int, float))
                and block_time < not_before_timestamp_s
            ):
                return SolanaRecoveryResult(tuple(signatures), True, None)
            if signature not in seen:
                signatures.append(signature)
                seen.add(signature)
        if not rows or next_before is None:
            return SolanaRecoveryResult(tuple(signatures), previous_durable_signature is None, None)
        before = next_before
    return SolanaRecoveryResult(tuple(signatures), False, before)


def json_rpc_http(
    url: str,
    method: str,
    params: list[object],
    *,
    timeout_s: float = 10.0,
    retry_rate_limits: bool = True,
):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Sniper-Opportunity-Radar/1",
        },
        method="POST",
    )
    payload = None
    attempts = 3 if retry_rate_limits else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                payload = json.loads(response.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == attempts - 1:
                raise
            time.sleep(0.25 * (2**attempt))
    assert payload is not None
    if payload.get("error"):
        raise RuntimeError(f"JSON-RPC {method} failed: {payload['error']}")
    return payload.get("result")


def json_rpc_batch_http(
    url: str,
    calls: list[tuple[str, list[object]]],
    *,
    timeout_s: float = 15.0,
    retry_rate_limits: bool = True,
) -> list[object]:
    """Execute one bounded JSON-RPC batch and return results in call order."""
    if not calls:
        return []
    body = json.dumps(
        [
            {"jsonrpc": "2.0", "id": index, "method": method, "params": params}
            for index, (method, params) in enumerate(calls, 1)
        ]
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "Sniper-Opportunity-Radar/1",
        },
        method="POST",
    )
    payload = None
    attempts = 3 if retry_rate_limits else 1
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                payload = json.loads(response.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code != 429 or attempt == attempts - 1:
                raise
            time.sleep(0.25 * (2**attempt))
    if not isinstance(payload, list):
        raise RuntimeError("JSON-RPC batch returned malformed payload")
    by_id = {item.get("id"): item for item in payload if isinstance(item, dict)}
    results = []
    for index in range(1, len(calls) + 1):
        item = by_id.get(index)
        error = item.get("error") if item is not None else None
        if isinstance(error, dict):
            message = str(error.get("message") or "")
            if error.get("code") == 429 or "rate limit" in message.lower():
                raise urllib.error.HTTPError(
                    url,
                    429,
                    message or "JSON-RPC batch item rate limited",
                    None,
                    None,
                )
        if item is None or error:
            raise RuntimeError(f"JSON-RPC batch item {index} failed")
        results.append(item.get("result"))
    return results


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def read_bags_4663_token_state(
    rpc_call,
    token_id: str,
    *,
    block_tag: str = "latest",
) -> dict[str, object]:
    """Read the exact BagsLens TokenState tuple at one EVM block."""
    data = "0x" + (
        _selector("getTokenState(address)") + encode(["address"], [token_id])
    ).hex()
    raw = rpc_call("eth_call", [{"to": BAGS_LENS, "data": data}, block_tag])
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise ValueError("invalid BagsLens eth_call result")
    values = decode(
        ["bool", "bool", "address", "address", "bytes32"] + ["uint256"] * 8,
        bytes.fromhex(raw[2:]),
    )
    (
        exists,
        migrated,
        curve,
        fee_share,
        pool_id,
        threshold_quote,
        real_quote,
        real_token,
        virtual_token,
        virtual_quote,
        price,
        progress_pct,
        total_raised,
    ) = values
    progress_bps = max(0, min(10_000, int(progress_pct) * 100))
    return {
        "exists": bool(exists),
        "migrated": bool(migrated),
        "curve": str(curve).lower(),
        "fee_share": str(fee_share).lower(),
        "pool_id": "0x" + bytes(pool_id).hex(),
        "threshold_quote": int(threshold_quote),
        "real_quote_reserves": int(real_quote),
        "real_token_reserves": int(real_token),
        "virtual_token_reserves": int(virtual_token),
        "virtual_quote_reserves": int(virtual_quote),
        "price_quote_per_token": int(price),
        "progress_bps": progress_bps,
        "total_raised": int(total_raised),
    }


def make_bags_4663_state_enricher(rpc_call):
    """Attach exact block-scoped BagsLens state before canonical persistence."""

    def enrich(envelope, decoded):
        if envelope.venue_id is not VenueId.BAGS_ROBINHOOD_4663 or not decoded.token_id:
            return decoded
        try:
            lens_state = read_bags_4663_token_state(
                rpc_call,
                decoded.token_id,
                block_tag=hex(envelope.block_or_slot),
            )
        except Exception as exc:
            # The event itself remains exact. A transient read-only Lens quote
            # failure makes only this launch state ineligible; it is not a
            # missing chain event and must not open a venue-wide source gap.
            return replace(
                decoded,
                facts={
                    **dict(decoded.facts),
                    "state_enrichment_status": "unavailable",
                    "state_enrichment_error": type(exc).__name__,
                },
            )
        return enrich_bags_4663_state(decoded, lens_state)

    return enrich


def make_bags_4663_remote_probe(rpc_call):
    """Build an exact read-only curve quoteBuy -> acquired-quantity quoteSell probe."""

    def probe(state, local_quote):
        if state.venue_id is not VenueId.BAGS_ROBINHOOD_4663 or not state.curve_id:
            return RemoteRoundTripProbe(
                False, False, "unsupported_venue", 0, "REMOTE_SIMULATION_UNAVAILABLE"
            )
        started = time.perf_counter()
        try:
            block_identity = str(rpc_call("eth_blockNumber", []))
            buy_data = "0x" + (
                _selector("quoteBuy(uint256)")
                + encode(["uint256"], [local_quote.quote_in_atomic])
            ).hex()
            buy_raw = rpc_call(
                "eth_call", [{"to": state.curve_id, "data": buy_data}, block_identity]
            )
            tokens_out, _fee, _net, _used, _refund = decode(
                ["uint256"] * 5, bytes.fromhex(str(buy_raw)[2:])
            )
            sell_data = "0x" + (
                _selector("quoteSell(uint256)") + encode(["uint256"], [tokens_out])
            ).hex()
            sell_raw = rpc_call(
                "eth_call", [{"to": state.curve_id, "data": sell_data}, block_identity]
            )
            quote_to_seller, _sell_fee, _gross = decode(
                ["uint256"] * 3, bytes.fromhex(str(sell_raw)[2:])
            )
            latency = int((time.perf_counter() - started) * 1000)
            return RemoteRoundTripProbe(
                int(tokens_out) > 0,
                int(quote_to_seller) > 0,
                "bags_curve_eth_call_v2",
                latency,
                quote_in_atomic=int(local_quote.quote_in_atomic),
                quote_mint=getattr(state, "quote_mint", None),
                tokens_out_atomic=int(tokens_out),
                immediate_exit_quote_atomic=int(quote_to_seller),
                round_trip_return_bps=(
                    (int(quote_to_seller) - int(local_quote.quote_in_atomic)) * 10_000
                    // int(local_quote.quote_in_atomic)
                ),
                launch_id=state.launch_id,
                venue_id=state.venue_id.value,
                token_id=state.token_id,
                completed_at=real_utc_now_iso(),
                block_identity=block_identity,
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return RemoteRoundTripProbe(
                False,
                False,
                "bags_curve_eth_call_v2",
                latency,
                type(exc).__name__,
            )

    return probe


class _Bags4663QuoteProvider:
    def __init__(self, rpc_call) -> None:
        self.rpc_call = rpc_call
        self._round_trip = make_bags_4663_remote_probe(rpc_call)

    def round_trip(self, state, quote) -> RemoteRoundTripProbe:
        result = self._round_trip(state, quote)
        if result.entry_quote_ok:
            binding = {
                "venue_id": VenueId.BAGS_ROBINHOOD_4663.value,
                "curve_id": str(state.curve_id).lower(),
                "quote_mint": PONS_WETH,
                "token_id": str(state.token_id).lower(),
                "block_identity": result.block_identity,
                "state_fingerprint": getattr(quote, "state_fingerprint", None),
            }
            return replace(
                result,
                quote_mint=PONS_WETH,
                state_fingerprint=getattr(quote, "state_fingerprint", None),
                route_binding=binding,
            )
        return result

    def _decimals(self, token_id: str, block_identity: str) -> int:
        raw = self.rpc_call(
            "eth_call",
            [{"to": token_id, "data": "0x" + _selector("decimals()").hex()}, block_identity],
        )
        value = int(decode(["uint8"], bytes.fromhex(str(raw)[2:]))[0])
        if not 0 <= value <= 30:
            raise ValueError("BAGS_TOKEN_DECIMALS_INVALID")
        return value

    def entry(
        self,
        state,
        quote_in_atomic: int,
        state_fingerprint: str,
    ) -> dict[str, object]:
        if state.venue_id is not VenueId.BAGS_ROBINHOOD_4663 or not state.curve_id:
            raise ValueError("BAGS_CURVE_IDENTITY_INVALID")
        started_at = real_utc_now_iso()
        block_identity = str(self.rpc_call("eth_blockNumber", []))
        decimals = self._decimals(state.token_id, block_identity)
        data = "0x" + (
            _selector("quoteBuy(uint256)")
            + encode(["uint256"], [int(quote_in_atomic)])
        ).hex()
        raw = self.rpc_call(
            "eth_call", [{"to": state.curve_id, "data": data}, block_identity]
        )
        amount_out = int(decode(["uint256"] * 5, bytes.fromhex(str(raw)[2:]))[0])
        if amount_out <= 0:
            raise ValueError("BAGS_QUOTE_ZERO_OUT")
        binding = {
            "venue_id": state.venue_id.value,
            "curve_id": str(state.curve_id).lower(),
            "quote_mint": PONS_WETH,
            "token_id": str(state.token_id).lower(),
            "block_identity": block_identity,
            "state_fingerprint": state_fingerprint,
        }
        completed_at = real_utc_now_iso()
        return {
            "quote_provider": "bags_curve_eth_call_v2",
            "quote_in_asset": PONS_WETH,
            "quote_in_atomic": int(quote_in_atomic),
            "quote_in_decimals": 18,
            "quote_out_asset": state.token_id,
            "quote_out_decimals": decimals,
            "expected_out_atomic": amount_out,
            "floor_out_atomic": amount_out * 9_700 // 10_000,
            "slippage_bps": 300,
            "route_binding": binding,
            "quote_payload": {
                "amount_in": int(quote_in_atomic),
                "amount_out": amount_out,
                "block_identity": block_identity,
            },
            "quote_started_at": started_at,
            "quote_completed_at": completed_at,
        }

    def exit(
        self,
        *,
        token_id: str,
        quantity_atomic: int,
        token_decimals: int,
        quote_decimals: int,
        route_binding,
    ) -> dict[str, object]:
        binding = dict(route_binding or {})
        if (
            binding.get("venue_id") != VenueId.BAGS_ROBINHOOD_4663.value
            or str(binding.get("token_id") or "").lower() != str(token_id).lower()
            or not binding.get("curve_id")
        ):
            raise ValueError("BAGS_ROUTE_BINDING_INVALID")
        started_at = real_utc_now_iso()
        block_identity = str(self.rpc_call("eth_blockNumber", []))
        data = "0x" + (
            _selector("quoteSell(uint256)")
            + encode(["uint256"], [int(quantity_atomic)])
        ).hex()
        raw = self.rpc_call(
            "eth_call",
            [{"to": binding["curve_id"], "data": data}, block_identity],
        )
        amount_out = int(decode(["uint256"] * 3, bytes.fromhex(str(raw)[2:]))[0])
        if amount_out <= 0:
            raise ValueError("BAGS_QUOTE_ZERO_OUT")
        completed_at = real_utc_now_iso()
        current_binding = {**binding, "block_identity": block_identity}
        return {
            "quote_provider": "bags_curve_eth_call_v2",
            "quote_in_asset": token_id,
            "quote_in_atomic": int(quantity_atomic),
            "quote_in_decimals": int(token_decimals),
            "quote_out_asset": PONS_WETH,
            "quote_out_decimals": int(quote_decimals),
            "expected_out_atomic": amount_out,
            "floor_out_atomic": amount_out * 9_700 // 10_000,
            "slippage_bps": 300,
            "route_binding": current_binding,
            "quote_payload": {
                "amount_in": int(quantity_atomic),
                "amount_out": amount_out,
                "block_identity": block_identity,
            },
            "quote_started_at": started_at,
            "quote_completed_at": completed_at,
        }


def make_bags_4663_quote_provider(rpc_call):
    """Create the Bags curve quote provider for scoring and paper results."""
    return _Bags4663QuoteProvider(rpc_call)


class _Pons4663QuoteProvider:
    def __init__(self, rpc_call, quoter: str) -> None:
        self.rpc_call = rpc_call
        self.quoter = str(quoter).lower()

    @staticmethod
    def _blocked(state, code: str, latency_ms: int = 0) -> RemoteRoundTripProbe:
        return RemoteRoundTripProbe(
            False,
            False,
            "pons_v3_quoter_v1",
            latency_ms,
            code,
            launch_id=getattr(state, "launch_id", None),
            venue_id=getattr(getattr(state, "venue_id", None), "value", None),
            token_id=getattr(state, "token_id", None),
        )

    def _amount_out(
        self,
        token_in: str,
        token_out: str,
        amount_in: int,
        fee: int,
        block_identity: str,
    ) -> int:
        data = "0x" + (
            _selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
            + encode(
                ["address", "address", "uint256", "uint24", "uint160"],
                [token_in, token_out, int(amount_in), int(fee), 0],
            )
        ).hex()
        raw = self.rpc_call(
            "eth_call",
            [{"to": self.quoter, "data": data}, block_identity],
        )
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise ValueError("PONS_QUOTER_MALFORMED")
        values = decode(
            ["uint256", "uint160", "uint32", "uint256"],
            bytes.fromhex(raw[2:]),
        )
        amount_out = int(values[0])
        if amount_out <= 0:
            raise ValueError("PONS_QUOTER_ZERO_OUT")
        return amount_out

    def _pool_fee(self, pool_id: str, block_identity: str) -> int:
        raw = self.rpc_call(
            "eth_call",
            [{"to": pool_id, "data": "0x" + _selector("fee()").hex()}, block_identity],
        )
        fee = int(decode(["uint24"], bytes.fromhex(str(raw)[2:]))[0])
        if not 0 < fee < 1_000_000:
            raise ValueError("PONS_POOL_FEE_INVALID")
        return fee

    def _validated_pool(
        self,
        pool_id: str,
        token_id: str,
        block_identity: str,
    ) -> tuple[int, str]:
        fee = self._pool_fee(pool_id, block_identity)
        factory_raw = self.rpc_call(
            "eth_call",
            [
                {"to": self.quoter, "data": "0x" + _selector("factory()").hex()},
                block_identity,
            ],
        )
        factory_id = str(
            decode(["address"], bytes.fromhex(str(factory_raw)[2:]))[0]
        ).lower()
        pool_data = "0x" + (
            _selector("getPool(address,address,uint24)")
            + encode(["address", "address", "uint24"], [token_id, PONS_WETH, fee])
        ).hex()
        canonical_raw = self.rpc_call(
            "eth_call",
            [{"to": factory_id, "data": pool_data}, block_identity],
        )
        canonical_pool = str(
            decode(["address"], bytes.fromhex(str(canonical_raw)[2:]))[0]
        ).lower()
        if canonical_pool != str(pool_id).lower():
            raise ValueError("PONS_POOL_NOT_CANONICAL")
        return fee, factory_id

    def _token_decimals(self, token_id: str, block_identity: str) -> int:
        raw = self.rpc_call(
            "eth_call",
            [{"to": token_id, "data": "0x" + _selector("decimals()").hex()}, block_identity],
        )
        decimals = int(decode(["uint8"], bytes.fromhex(str(raw)[2:]))[0])
        if not 0 <= decimals <= 30:
            raise ValueError("PONS_TOKEN_DECIMALS_INVALID")
        return decimals

    @staticmethod
    def _route_binding(
        state,
        fee: int,
        factory_id: str,
        block_identity: str,
        state_fingerprint: str | None,
    ):
        return {
            "venue_id": state.venue_id.value,
            "pool_id": str(state.pool_id).lower(),
            "factory_id": factory_id,
            "fee": int(fee),
            "quote_mint": PONS_WETH,
            "token_id": str(state.token_id).lower(),
            "token_is_token0": state.v3_token_is_token0,
            "block_identity": block_identity,
            "state_fingerprint": state_fingerprint,
        }

    def round_trip(self, state, quote) -> RemoteRoundTripProbe:
        if (
            state.venue_id is not VenueId.PONS_V1_ROBINHOOD_4663
            or not state.pool_id
            or str(state.quote_mint or "").lower() != PONS_WETH
        ):
            return self._blocked(state, "PONS_POOL_IDENTITY_INVALID")
        started = time.perf_counter()
        try:
            block_identity = str(self.rpc_call("eth_blockNumber", []))
            fee, factory_id = self._validated_pool(
                state.pool_id, state.token_id, block_identity
            )
            tokens_out = self._amount_out(
                PONS_WETH,
                state.token_id,
                int(quote.quote_in_atomic),
                fee,
                block_identity,
            )
            quote_out = self._amount_out(
                state.token_id,
                PONS_WETH,
                tokens_out,
                fee,
                block_identity,
            )
            latency = int((time.perf_counter() - started) * 1_000)
            state_fingerprint = getattr(quote, "state_fingerprint", None)
            route_binding = self._route_binding(
                state, fee, factory_id, block_identity, state_fingerprint
            )
            return RemoteRoundTripProbe(
                True,
                True,
                "pons_v3_quoter_v1",
                latency,
                quote_in_atomic=int(quote.quote_in_atomic),
                quote_mint=PONS_WETH,
                tokens_out_atomic=tokens_out,
                immediate_exit_quote_atomic=quote_out,
                round_trip_return_bps=(
                    (quote_out - int(quote.quote_in_atomic)) * 10_000
                    // int(quote.quote_in_atomic)
                ),
                launch_id=state.launch_id,
                venue_id=state.venue_id.value,
                token_id=state.token_id,
                state_fingerprint=state_fingerprint,
                completed_at=real_utc_now_iso(),
                block_identity=block_identity,
                route_binding=route_binding,
            )
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1_000)
            return self._blocked(state, str(exc) or type(exc).__name__, latency)

    def entry(
        self,
        state,
        quote_in_atomic: int,
        state_fingerprint: str,
    ) -> dict[str, object]:
        if (
            state.venue_id is not VenueId.PONS_V1_ROBINHOOD_4663
            or not state.pool_id
            or str(state.quote_mint or "").lower() != PONS_WETH
        ):
            raise ValueError("PONS_POOL_IDENTITY_INVALID")
        started_at = real_utc_now_iso()
        block_identity = str(self.rpc_call("eth_blockNumber", []))
        fee, factory_id = self._validated_pool(
            state.pool_id, state.token_id, block_identity
        )
        token_decimals = self._token_decimals(state.token_id, block_identity)
        amount_out = self._amount_out(
            PONS_WETH,
            state.token_id,
            int(quote_in_atomic),
            fee,
            block_identity,
        )
        route_binding = self._route_binding(
            state, fee, factory_id, block_identity, state_fingerprint
        )
        completed_at = real_utc_now_iso()
        return {
            "quote_provider": "pons_v3_quoter_v1",
            "quote_in_asset": PONS_WETH,
            "quote_in_atomic": int(quote_in_atomic),
            "quote_in_decimals": 18,
            "quote_out_asset": state.token_id,
            "quote_out_decimals": token_decimals,
            "expected_out_atomic": amount_out,
            "floor_out_atomic": amount_out * 9_700 // 10_000,
            "slippage_bps": 300,
            "route_binding": route_binding,
            "quote_payload": {
                "amount_in": int(quote_in_atomic),
                "amount_out": amount_out,
                "block_identity": block_identity,
                "fee": fee,
            },
            "quote_started_at": started_at,
            "quote_completed_at": completed_at,
        }

    def exit(
        self,
        *,
        token_id: str,
        quantity_atomic: int,
        token_decimals: int,
        quote_decimals: int,
        route_binding,
    ) -> dict[str, object]:
        binding = dict(route_binding or {})
        if (
            binding.get("venue_id") != VenueId.PONS_V1_ROBINHOOD_4663.value
            or str(binding.get("token_id") or "").lower() != str(token_id).lower()
            or str(binding.get("quote_mint") or "").lower() != PONS_WETH
            or not binding.get("pool_id")
        ):
            raise ValueError("PONS_ROUTE_BINDING_INVALID")
        started_at = real_utc_now_iso()
        block_identity = str(self.rpc_call("eth_blockNumber", []))
        fee, factory_id = self._validated_pool(
            str(binding["pool_id"]), token_id, block_identity
        )
        if fee != int(binding.get("fee") or -1):
            raise ValueError("PONS_POOL_FEE_CHANGED")
        if factory_id != str(binding.get("factory_id") or "").lower():
            raise ValueError("PONS_FACTORY_CHANGED")
        amount_out = self._amount_out(
            token_id,
            PONS_WETH,
            int(quantity_atomic),
            fee,
            block_identity,
        )
        completed_at = real_utc_now_iso()
        current_binding = {
            **binding,
            "block_identity": block_identity,
        }
        return {
            "quote_provider": "pons_v3_quoter_v1",
            "quote_in_asset": token_id,
            "quote_in_atomic": int(quantity_atomic),
            "quote_in_decimals": int(token_decimals),
            "quote_out_asset": PONS_WETH,
            "quote_out_decimals": int(quote_decimals),
            "expected_out_atomic": amount_out,
            "floor_out_atomic": amount_out * 9_700 // 10_000,
            "slippage_bps": 300,
            "route_binding": current_binding,
            "quote_payload": {
                "amount_in": int(quantity_atomic),
                "amount_out": amount_out,
                "block_identity": block_identity,
                "fee": fee,
            },
            "quote_started_at": started_at,
            "quote_completed_at": completed_at,
        }


def make_pons_4663_quote_provider(rpc_call, *, quoter: str):
    """Create the registered-pool Pons V3 quote provider."""
    return _Pons4663QuoteProvider(rpc_call, quoter)

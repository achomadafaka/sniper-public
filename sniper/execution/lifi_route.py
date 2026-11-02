"""Pure LI.FI quote validation for chain 4663."""
from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field

from eth_abi import decode, encode


class LifiQuoteError(ValueError):
    pass


@dataclass(frozen=True)
class DecodedQuote:
    calldata_sha256: str
    decoded_semantics_sha256: str
    fee_atomic: int
    input_atomic: int
    output_min_atomic: int
    path: tuple[str, ...]
    request_sha256: str
    response_sha256: str
    spender: str
    swap_input_atomic: int
    target: str
    tool: str
    custody: str | None = None
    input_recipients: tuple[str, ...] = ()
    transaction: dict | None = field(default=None, repr=False, compare=False)
    output_expected_atomic: int | None = None


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    code: str
    required: int | None = None
    available: int | None = None
    loss_bps: int | None = None


def _fail(message):
    raise LifiQuoteError(message)


def _require(condition, message):
    if not condition:
        _fail(message)


def _canonical_hash(value):
    try:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise LifiQuoteError("quote data is not canonical JSON") from exc
    return hashlib.sha256(raw.encode()).hexdigest()


def _address(value):
    if not isinstance(value, str) or len(value) != 42 or not value.startswith("0x"):
        _fail("invalid address")
    lowered = value.lower()
    if any(char not in "0123456789abcdef" for char in lowered[2:]):
        _fail("invalid address")
    return lowered


def _atomic(value, *, positive=False):
    if isinstance(value, bool):
        _fail("invalid atomic amount")
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise LifiQuoteError("invalid atomic amount") from exc
    if str(number) != str(value) or number < 0 or (positive and number == 0):
        _fail("invalid atomic amount")
    return number


def fee_atomic(input_atomic):
    value = _atomic(input_atomic, positive=True)
    return value * 25 // 10_000


def decode_exact(types, payload):
    try:
        values = decode(types, payload, strict=True)
        if encode(types, values) != bytes(payload):
            _fail("calldata has unused or trailing bytes")
        return values
    except LifiQuoteError:
        raise
    except Exception as exc:
        raise LifiQuoteError("malformed calldata") from exc


def _token(action, key):
    try:
        return _address(action[key]["address"])
    except (KeyError, TypeError):
        _fail("response token is absent")


def _validate_request(request, manifest):
    _require(str(request.get("fromChain")) == "4663", "wrong source chain")
    _require(str(request.get("toChain")) == "4663", "wrong destination chain")
    _require(request.get("allowExchanges") == manifest["allow_exchanges"],
             "wrong exchange authority")
    _require(request.get("allowBridges") == [], "bridges are not disabled")
    _require(str(request.get("fee")) == "0", "integrator fee is not zero")
    _require(request.get("simulation") is True, "LI.FI simulation is disabled")
    custody = _address(request.get("fromAddress"))
    _require(_address(request.get("toAddress")) == custody, "external recipient")
    input_token = _address(request.get("fromToken"))
    output_token = _address(request.get("toToken"))
    input_atomic = _atomic(request.get("fromAmount"), positive=True)
    return custody, input_token, output_token, input_atomic


def _validate_response_envelope(response, request, manifest, custody, input_token,
                                output_token, input_atomic):
    _require(response.get("type") == manifest["top_level"]["type"], "wrong top-level type")
    _require(response.get("tool") == manifest["top_level"]["tool"], "wrong top-level tool")
    _require(not response.get("typedData"), "typed data is forbidden")
    _require(response.get("executionType") != "message", "message execution is forbidden")
    _require(response.get("skipApproval") is not True, "approval bypass is forbidden")
    action = response.get("action")
    _require(isinstance(action, dict), "response action is absent")
    _require(str(action.get("fromChainId")) == "4663", "wrong response source chain")
    _require(str(action.get("toChainId")) == "4663", "wrong response destination chain")
    _require(_address(action.get("fromAddress")) == custody, "wrong response sender")
    _require(_address(action.get("toAddress")) == custody, "wrong response recipient")
    _require(_token(action, "fromToken") == input_token, "wrong response input token")
    _require(_token(action, "toToken") == output_token, "wrong response output token")
    _require(_atomic(action.get("fromAmount"), positive=True) == input_atomic,
             "wrong response input amount")
    tx = response.get("transactionRequest")
    _require(isinstance(tx, dict), "transaction request is absent")
    _require(str(tx.get("chainId")) == "4663", "wrong transaction chain")
    _require(_address(tx.get("from")) == custody, "wrong transaction sender")
    _require(_address(tx.get("to")) == manifest["top_level"]["target"],
             "wrong transaction target")
    value = tx.get("value")
    _require(not isinstance(value, bool) and value in (0, "0", "0x0"),
             "unauthorized native value")
    return tx


def _decode_transaction(tx, request, response, manifest, custody, input_token,
                        output_token, input_atomic):
    data = tx.get("data")
    _require(isinstance(data, str) and data.startswith("0x") and len(data) % 2 == 0,
             "transaction calldata is invalid")
    try:
        raw = bytes.fromhex(data[2:])
    except ValueError as exc:
        raise LifiQuoteError("transaction calldata is invalid") from exc
    top = manifest["top_level"]
    selector = bytes.fromhex(top["selector"][2:])
    _require(raw[:4] == selector, "wrong outer selector")
    _require("0x" + raw[:4].hex() not in manifest["reject"]["selectors"],
             "forbidden outer selector")
    transaction_id, integrator, referrer, receiver, minimum, swaps = decode_exact(
        top["types"], raw[4:],
    )
    _require(len(transaction_id) == 32, "wrong transaction identity")
    _require(integrator == request.get("integrator", ""), "wrong integrator")
    _require(referrer == request.get("referrer", ""), "wrong referrer")
    _require(_address(receiver) == custody, "wrong outer receiver")
    estimate = response.get("estimate")
    _require(isinstance(estimate, dict), "response estimate is absent")
    output_min = _atomic(estimate.get("toAmountMin"), positive=True)
    _require(_atomic(minimum, positive=True) == output_min, "wrong minimum output")
    _require(len(swaps) == 2, "wrong swap count")
    return raw, swaps, output_min


def _validate_fee_step(step, swap, manifest_step, input_token, input_atomic):
    call_to, approve_to, send_token, receive_token, amount, call_data, deposit = swap
    _require(_address(call_to) == manifest_step["target"], "wrong fee target")
    _require(_address(approve_to) == manifest_step["approve_to"], "wrong fee approveTo")
    _require(_address(send_token) == input_token, "wrong fee input token")
    _require(_address(receive_token) == input_token, "wrong fee output token")
    _require(_atomic(amount, positive=True) == input_atomic, "wrong fee input amount")
    _require(deposit is True, "wrong fee deposit flag")
    selector = bytes.fromhex(manifest_step["selector"][2:])
    _require(call_data[:4] == selector, "wrong fee selector")
    fee_token, distributions = decode_exact(manifest_step["types"], call_data[4:])
    expected_fee = fee_atomic(input_atomic)
    _require(_address(fee_token) == input_token, "wrong fee calldata token")
    _require(len(distributions) == 1, "wrong fee distribution count")
    _require(_atomic(distributions[0][1]) == expected_fee, "wrong fee amount")
    action = step.get("action")
    estimate = step.get("estimate")
    _require(isinstance(action, dict) and isinstance(estimate, dict), "fee step is incomplete")
    _require(_token(action, "fromToken") == input_token, "wrong fee action input token")
    _require(_token(action, "toToken") == input_token, "wrong fee action output token")
    _require(_atomic(action.get("fromAmount"), positive=True) == input_atomic,
             "wrong fee action input amount")
    net = input_atomic - expected_fee
    _require(_atomic(estimate.get("toAmount")) == net, "wrong fee output")
    _require(_atomic(estimate.get("toAmountMin")) == net, "wrong fee minimum output")
    return expected_fee, net


def _validate_swap_step(step, swap, manifest_step, input_token, output_token,
                        swap_input, output_min, recipient):
    call_to, approve_to, send_token, receive_token, amount, call_data, deposit = swap
    _require(_address(call_to) == manifest_step["target"], "wrong swap target")
    _require(_address(approve_to) == manifest_step["approve_to"], "wrong swap approveTo")
    _require(_address(send_token) == input_token, "wrong swap input token")
    _require(_address(receive_token) == output_token, "wrong swap output token")
    _require(_atomic(amount, positive=True) == swap_input, "wrong swap input amount")
    _require(deposit is False, "wrong swap deposit flag")
    selector = bytes.fromhex(manifest_step["selector"][2:])
    _require(call_data[:4] == selector, "wrong swap selector")
    _require("0x" + call_data[:4].hex() not in manifest_step.get("reject", []),
             "forbidden swap selector")
    _require(len(call_data) >= 140, "Nordstern calldata is truncated")
    executor = _address("0x" + call_data[6:26].hex())
    nested_min = int.from_bytes(call_data[42:58], "big")
    _require(executor == manifest_step["executor"], "wrong Nordstern executor")
    _require(int.from_bytes(call_data[26:42], "big") == swap_input,
             "wrong Nordstern input amount")
    minimum_delta = output_min - nested_min
    # A nested minimum ABOVE the quoted one is stricter, so it is safe; LI.FI began rounding it up
    # by 1 wei on 2026-09-11 and every 4663 quote read as malformed. Allow the same rounding band
    # on both sides (at least 1 wei on the strict side); a looser nested minimum stays bounded.
    tolerance = output_min // 10**15
    _require(-max(1, tolerance) <= minimum_delta <= tolerance,
             "wrong Nordstern minimum output")
    _require(_address("0x" + call_data[59:79].hex()) == executor,
             "wrong Nordstern token receiver")
    _require(_address("0x" + call_data[79:99].hex()) == recipient,
             "wrong Nordstern output recipient")
    _require(_address("0x" + call_data[99:119].hex()) == input_token,
             "wrong Nordstern input token")
    _require(_address("0x" + call_data[119:139].hex()) == output_token,
             "wrong Nordstern output token")
    action = step.get("action")
    estimate = step.get("estimate")
    _require(isinstance(action, dict) and isinstance(estimate, dict), "swap step is incomplete")
    _require(_token(action, "fromToken") == input_token, "wrong swap action input token")
    _require(_token(action, "toToken") == output_token, "wrong swap action output token")
    _require(_atomic(action.get("fromAmount"), positive=True) == swap_input,
             "wrong swap action input amount")
    _require(_atomic(estimate.get("toAmountMin"), positive=True) == output_min,
             "wrong swap minimum output")
    return executor, nested_min


def _validate_fee_cost(response, input_token, expected_fee):
    costs = response["estimate"].get("feeCosts")
    _require(isinstance(costs, list) and len(costs) == 1, "wrong provider fee count")
    cost = costs[0]
    _require(_atomic(cost.get("amount")) == expected_fee, "wrong provider fee amount")
    _require(str(cost.get("percentage")) == "0.0025", "wrong provider fee rate")
    _require(_token(cost, "token") == input_token, "wrong provider fee token")


def _validate_gas_limit(response, tx):
    value = tx.get("gasLimit")
    if value is None:
        return None
    _require(not isinstance(value, bool), "wrong gas limit")
    try:
        gas_limit = int(value, 16) if isinstance(value, str) and value.startswith("0x") \
            else int(value)
    except (TypeError, ValueError) as exc:
        raise LifiQuoteError("wrong gas limit") from exc
    _require(gas_limit > 0, "wrong gas limit")
    costs = response["estimate"].get("gasCosts")
    _require(isinstance(costs, list) and len(costs) == 1, "wrong gas cost count")
    _require(costs[0].get("type") == "SEND", "wrong gas cost type")
    _require(_atomic(costs[0].get("limit"), positive=True) == gas_limit,
             "wrong gas limit")
    return gas_limit


def decode_quote(response, request, manifest, *, swap_validator=None):
    try:
        custody, input_token, output_token, input_atomic = _validate_request(request, manifest)
        tx = _validate_response_envelope(
            response, request, manifest, custody, input_token, output_token, input_atomic,
        )
        raw, swaps, output_min = _decode_transaction(
            tx, request, response, manifest, custody, input_token, output_token, input_atomic,
        )
        steps = response.get("includedSteps")
        _require(isinstance(steps, list) and len(steps) == 2, "wrong included step count")
        expected_sequence = [(item["tool"], item["type"]) for item in manifest["steps"]]
        _require([(item.get("tool"), item.get("type")) for item in steps] == expected_sequence,
                 "wrong included step sequence")
        expected_fee, swap_input = _validate_fee_step(
            steps[0], swaps[0], manifest["steps"][0], input_token, input_atomic,
        )
        executor, nested_min = (swap_validator or _validate_swap_step)(
            steps[1], swaps[1], manifest["steps"][1], input_token, output_token,
            swap_input, output_min, manifest["top_level"]["target"],
        )
        _validate_fee_cost(response, input_token, expected_fee)
        gas_limit = _validate_gas_limit(response, tx)
        output_expected = _atomic(response["estimate"].get("toAmount"), positive=True)
        _require(output_expected >= output_min, "expected output below minimum")
    except (KeyError, TypeError, IndexError) as exc:
        raise LifiQuoteError("quote response is incomplete") from exc
    semantics = {
        "chain_id": "4663",
        "custody": custody,
        "fee_atomic": str(expected_fee),
        "input_atomic": str(input_atomic),
        "nordstern_executor": executor,
        "nordstern_minimum_atomic": str(nested_min),
        "output_min_atomic": str(output_min),
        "path": [input_token, input_token, output_token],
        "spender": manifest["top_level"]["spender"],
        "swap_input_atomic": str(swap_input),
        "target": manifest["top_level"]["target"],
        "tool": manifest["top_level"]["tool"],
    }
    transaction = {"to": tx["to"], "data": tx["data"], "value": tx["value"]}
    if gas_limit is not None:
        transaction["gasLimit"] = gas_limit
    return DecodedQuote(
        calldata_sha256=hashlib.sha256(raw).hexdigest(),
        decoded_semantics_sha256=_canonical_hash(semantics),
        fee_atomic=expected_fee,
        input_atomic=input_atomic,
        output_min_atomic=output_min,
        output_expected_atomic=output_expected,
        path=(input_token, input_token, output_token),
        request_sha256=_canonical_hash(request),
        response_sha256=_canonical_hash(response),
        spender=manifest["top_level"]["spender"],
        swap_input_atomic=swap_input,
        target=manifest["top_level"]["target"],
        tool=manifest["top_level"]["tool"],
        custody=custody,
        input_recipients=(manifest["top_level"]["spender"],),
        transaction=transaction,
    )


def economic_roundtrip(input_atomic, buy, sell, cap_bps):
    amount = _atomic(input_atomic, positive=True)
    cap = _atomic(cap_bps)
    if cap > 10_000:
        return GateResult(False, "invalid_cap")
    if buy.input_atomic != amount or sell.input_atomic != buy.output_min_atomic:
        return GateResult(False, "atomic_continuity")
    proceeds = sell.output_min_atomic
    loss = max(0, amount - proceeds)
    loss_bps = (loss * 10_000 + amount - 1) // amount
    allowed = proceeds * 10_000 >= amount * (10_000 - cap)
    return GateResult(allowed, "ok" if allowed else "roundtrip_cap", loss_bps=loss_bps)


class QuotaGuard:
    entry_request_cost = 4
    canary_request_cost = 7

    def __init__(self, *, remaining, max_in_flight=100):
        self.remaining = _atomic(remaining)
        self.limit = None
        self.reset = None
        self.max_in_flight = _atomic(max_in_flight, positive=True)
        self.in_flight = 0
        self.entry_disabled = False
        self._exit_waiters = 0
        self._condition = threading.Condition()

    def observe_response(self, quota):
        """Apply one request cost and accept only non-stale provider quota state."""
        with self._condition:
            local_remaining = max(0, self.remaining - 1)
            limit = (quota or {}).get("limit")
            remaining = (quota or {}).get("remaining")
            reset = (quota or {}).get("reset")
            if all(isinstance(value, int) and value >= 0
                   for value in (limit, remaining, reset)):
                if self.reset is None or reset > self.reset:
                    self.remaining = remaining
                    self.reset = reset
                elif reset == self.reset:
                    self.remaining = min(local_remaining, remaining)
                else:
                    self.remaining = local_remaining
                self.limit = limit
            else:
                self.remaining = local_remaining

    def disable_entries(self):
        with self._condition:
            self.entry_disabled = True

    def enable_entries(self):
        with self._condition:
            self.entry_disabled = False

    @staticmethod
    def exit_reserve(positions):
        count = _atomic(positions)
        return count * (7_200 + 2 * 3)

    def _admit(self, required, in_flight):
        available = self.remaining - _atomic(in_flight)
        return GateResult(
            available >= required,
            "ok" if available >= required else "insufficient_quota",
            required=required,
            available=available,
        )

    def admit_canary(self, *, in_flight=0):
        return self._admit(self.canary_request_cost, in_flight)

    def admit_entry(self, slots_used, in_flight=0):
        if self.entry_disabled:
            return GateResult(False, "entry_disabled", available=self.remaining)
        required = self.exit_reserve(_atomic(slots_used) + 1) + self.entry_request_cost
        return self._admit(required, in_flight)

    @contextmanager
    def reserve_request(self, role):
        if role not in {"entry", "exit"}:
            raise ValueError("LI.FI request role is invalid")
        with self._condition:
            if role == "exit":
                self._exit_waiters += 1
            try:
                self._condition.wait_for(
                    lambda: self.in_flight < self.max_in_flight
                    and (role == "exit" or self._exit_waiters == 0)
                )
                self.in_flight += 1
            finally:
                if role == "exit":
                    self._exit_waiters -= 1
        try:
            yield
        finally:
            with self._condition:
                self.in_flight -= 1
                self._condition.notify_all()

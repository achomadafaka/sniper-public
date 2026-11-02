"""Jupiter v6 `route_v2` arguments, decoded from the program's own Anchor IDL (rabbit route decoders,
design 2026-10-16 rev 2).

The IDL (jupiter_route_idl.json beside this module) was read from the program's IDL account
C88XWfp26heEmDkmfSzeXP7Fd7GQJ2j9dDTUsyiZbUTa on 2026-10-16 (decompressed sha256 below). It names the
route plan's swap legs and amounts, not each leg's accounts: the coin is `destination_mint`
(instruction account 4) and the owner's coin account `user_destination_token_account` (account 2).

FAIL CLOSED, as dflow_swap: a file that does not hash to IDL_SHA256 disables the decoder, and so does
an instruction whose arguments do not consume its data exactly. A Jupiter upgrade that moves the
layout reads as None; callers keep today's path.
"""
from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path

JUPITER_PROGRAM = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
IDL_PATH = Path(__file__).with_name("jupiter_route_idl.json")
IDL_SHA256 = "82a3907455712fa9c2104d1b8fb8a2649cc4d96789d4c16a572ed343981b9f4e"
ROUTE_V2 = bytes.fromhex("bb64facc31c4af14")
# route_v2 accounts: user_transfer_authority, user_source_token_account, user_destination_token_account,
# source_mint, destination_mint, ...
DESTINATION_ACCOUNT, DESTINATION_MINT = 2, 4
MIN_ACCOUNTS = 10
MAX_DATA = 1232
_HEAD = 8 + 8 + 8 + 2 + 2 + 2           # discriminator, in, quoted out, slippage, platform fee, positive


@functools.cache
def _types():
    try:
        raw = IDL_PATH.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != IDL_SHA256:
        return None
    return {t["name"]: t["type"] for t in json.loads(raw)["types"]}


def idl_ok():
    return _types() is not None


PUMP_AMM_STEPS = {"PumpSwapBuyV3": "buy_exact_quote_in", "PumpSwapBuyV4": "buy_exact_quote_in_v2"}
BPS_TOTAL = 10_000


def decode_route_v2(data):
    """{"in_amount", "quoted_out_amount", "slippage_bps", "platform_fee_bps", "steps": ((variant, bps,
    input_index, output_index), ...)} for a route_v2 whose arguments consume `data` exactly; None
    otherwise."""
    from sniper.execution.dflow_swap import _Short, _read
    types = _types()
    if (types is None or not isinstance(data, (bytes, bytearray)) or not _HEAD < len(data) <= MAX_DATA
            or bytes(data[:8]) != ROUTE_V2):
        return None
    data = bytes(data)
    try:
        in_amount, at = _read(types, "u64", data, 8)
        quoted, at = _read(types, "u64", data, at)
        slippage, at = _read(types, "u16", data, at)
        platform, at = _read(types, "u16", data, at)
        _positive, at = _read(types, "u16", data, at)
        plan, at = _read(types, {"vec": {"defined": {"name": "RoutePlanStepV2"}}}, data, at)
    except _Short:
        return None
    if at != len(data) or not plan:
        return None
    return {"in_amount": in_amount, "quoted_out_amount": quoted, "slippage_bps": slippage,
            "platform_fee_bps": platform,
            "steps": tuple((s["swap"][0], s["bps"], s["input_index"], s["output_index"]) for s in plan)}


def pump_steps(decoded):
    """Every PumpSwap buy step of the route (any version)."""
    return [step for step in (decoded or {}).get("steps") or () if step[0].startswith("PumpSwapBuy")]


def pump_share(decoded):
    """(numerator over BPS_TOTAL**2 of the platform-fee-net input that reaches PumpSwap as SOL, the pump
    step's variant), or (None, reason). The route names token indexes: the PumpSwap step's input
    index k is SOL (its pool is checked canonical-to-WSOL by the handler); the SOL at k is what the
    steps from the input token (index 0) into k produce; a step from 0 straight into another token
    (the coin) is not counted. Refused: per-input-index bps that do not each sum to BPS_TOTAL, not
    exactly one PumpSwap step, a variant other than PUMP_AMM_STEPS, or SOL at k made from anything
    but the input token (a three-hop route)."""
    steps = (decoded or {}).get("steps") or ()
    sums = {}
    for _variant, bps, source, _out in steps:
        sums[source] = sums.get(source, 0) + bps
    if not steps or any(total != BPS_TOTAL for total in sums.values()):
        return None, "bps_sum"
    pumps = pump_steps(decoded)
    if len(pumps) != 1:
        return None, "pump_count"
    variant, pump_bps, k, _final = pumps[0]
    if variant not in PUMP_AMM_STEPS:
        return None, "pump_step_unmodelled"
    into = [step for step in steps if step[3] == k]
    if k == 0 or not into or any(source != 0 for _v, _b, source, _o in into):
        return None, "sol_not_from_input"
    return sum(bps for _v, bps, _s, _o in into) * pump_bps, None

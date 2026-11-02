"""DFlow swap_orchestrator `swap*` arguments, decoded from the program's own Anchor IDL.

The IDL (dflow_swap_orchestrator_idl.json beside this module) was read from the program's IDL
account Cp2dCjxCWdktak2JiSrh87X6sz31EnDVKoTGtsHJvhYq on 2026-10-15 (decompressed sha256 below).
It names every Action and its fields, not the remaining-account layout of each leg, so a decode
yields the route's actions and amounts, never a pool or a mint.

FAIL CLOSED. A file that does not hash to IDL_SHA256 disables the decoder (every call returns
None), and so does any instruction whose arguments do not consume its data exactly. When DFlow
upgrades the program and the Action layout moves (it did between 2026-10-13 and 2026-10-15), the
decoder answers None and every caller keeps today's path; it never reads a moved layout.
"""
from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path

DFLOW_PROGRAM = "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH"
IDL_PATH = Path(__file__).with_name("dflow_swap_orchestrator_idl.json")
IDL_SHA256 = "54995c793d5134e8e57a4be165b1bb9f03dc70ea80caeae20cd2ab65786d5c89"
MAX_DATA = 1232
MAX_VEC = 64
MAX_ACTIONS = 32

# Actions that move no tokens through a venue: bookkeeping, fees, sponsorship, account setup.
NON_SWAP_ACTIONS = frozenset({
    "TransferFee", "TransferFeeWithMint", "TransferFeeV2", "TransferFeeWithMintV2", "RecordId", "RecordId2",
    "IncludeAccount", "SetMinimumLegOutputs", "SetMinimumLegPrices", "SetSponsor",
    "SetSponsorIntermediateCloseAuthority", "WrapSol", "UnwrapSol", "InitAtaIdempotent",
    "SetMaxUnderconsumptionBps", "TransferHookAccounts"})
# The final leg into the coin, by venue. A route whose last swap is anything else is not a buy we read.
BUY_LEGS = {"PumpFunBuy": "pump_curve", "PumpFunBuyV2": "pump_curve", "PumpFunBuyV3": "pump_curve",
            "PumpFunAmmBuy": "pump_amm", "PumpFunAmmBuyV2": "pump_amm",
            "MeteoraDammV2Swap": "meteora_damm_v2", "RaydiumCpSwap": "raydium_cpmm",
            "RaydiumLaunchlabSwap": "raydium_launchlab", "MeteoraDbcSwap": "meteora_dbc"}
_PRIMITIVE = {"u8": 1, "i8": 1, "u16": 2, "i16": 2, "u32": 4, "i32": 4, "u64": 8, "i64": 8,
              "u128": 16, "i128": 16, "bool": 1}


class _Short(ValueError):
    pass


@functools.cache
def _idl():
    """(types by name, swap instructions by discriminator) or None when the file is missing,
    unreadable or not the pinned IDL."""
    try:
        raw = IDL_PATH.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != IDL_SHA256:
        return None
    idl = json.loads(raw)
    types = {t["name"]: t["type"] for t in idl["types"]}
    swaps = {}
    for ix in idl["instructions"]:
        args = ix.get("args") or []
        if len(args) != 1 or not isinstance(args[0]["type"], dict) or "defined" not in args[0]["type"]:
            continue
        name = args[0]["type"]["defined"]["name"]
        if any(f["name"] == "actions" for f in types.get(name, {}).get("fields", ())):
            swaps[bytes(ix["discriminator"])] = (ix["name"], name)
    return types, swaps


def idl_ok():
    return _idl() is not None


def _read(types, ty, data, offset, depth=0):
    if depth > 8:
        raise _Short("nested too deep")
    if isinstance(ty, str):
        if ty == "pubkey":
            end = offset + 32
            if end > len(data):
                raise _Short(ty)
            return data[offset:end], end
        size = _PRIMITIVE.get(ty)
        if size is None:
            raise _Short("type " + ty)
        end = offset + size
        if end > len(data):
            raise _Short(ty)
        value = int.from_bytes(data[offset:end], "little", signed=ty.startswith("i"))
        if ty == "bool" and value not in (0, 1):
            raise _Short("bool")
        return value, end
    if "vec" in ty:
        count, offset = _read(types, "u32", data, offset)
        if count > MAX_VEC:
            raise _Short("vec too long")
        out = []
        for _ in range(count):
            value, offset = _read(types, ty["vec"], data, offset, depth + 1)
            out.append(value)
        return out, offset
    if "option" in ty:
        flag, offset = _read(types, "u8", data, offset)
        if flag not in (0, 1):
            raise _Short("option flag")
        return (_read(types, ty["option"], data, offset, depth + 1) if flag else (None, offset))
    if "array" in ty:
        inner, count = ty["array"]
        if inner == "u8":
            end = offset + count
            if end > len(data):
                raise _Short("array")
            return data[offset:end], end
        out = []
        for _ in range(count):
            value, offset = _read(types, inner, data, offset, depth + 1)
            out.append(value)
        return out, offset
    if "defined" in ty:
        definition = types.get(ty["defined"]["name"])
        if definition is None:
            raise _Short("undefined type")
        if definition["kind"] == "struct":
            out = {}
            for f in definition.get("fields", ()):
                out[f["name"]], offset = _read(types, f["type"], data, offset, depth + 1)
            return out, offset
        index, offset = _read(types, "u8", data, offset)
        variants = definition.get("variants", ())
        if index >= len(variants):
            raise _Short("unknown variant")
        variant, values = variants[index], []
        for f in variant.get("fields") or ():
            value, offset = _read(types, f["type"] if isinstance(f, dict) and "type" in f else f,
                                  data, offset, depth + 1)
            values.append(value)
        return (variant["name"], values), offset
    raise _Short("type shape")


def decode_swap(data):
    """{"instruction", "actions" (names, in order), "quoted_out_amount", "slippage_bps"} for a DFlow
    swap* instruction whose arguments consume `data` exactly; None otherwise (fail closed)."""
    loaded = _idl()
    if loaded is None or not isinstance(data, (bytes, bytearray)) or not 8 < len(data) <= MAX_DATA:
        return None
    types, swaps = loaded
    found = swaps.get(bytes(data[:8]))
    if found is None:
        return None
    instruction, params_type = found
    try:
        params, end = _read(types, {"defined": {"name": params_type}}, bytes(data), 8)
    except _Short:
        return None
    actions = params.get("actions") or []
    if end != len(data) or not 0 < len(actions) <= MAX_ACTIONS:
        return None
    return {"instruction": instruction, "actions": tuple(name for name, _ in actions),
            "action_values": tuple(actions), "quoted_out_amount": params.get("quoted_out_amount"),
            "slippage_bps": params.get("slippage_bps")}


def buy_leg(decoded):
    """The venue of the route's final leg into the coin, or None: a route that sells anything, whose
    last swap is not a known buy leg, or that buys through two such legs (a split) is not read."""
    if not decoded:
        return None
    actions = decoded["actions"]
    if any("Sell" in name for name in actions):
        return None
    swaps = [name for name in actions if name not in NON_SWAP_ACTIONS]
    if not swaps or swaps[-1] not in BUY_LEGS or sum(name in BUY_LEGS for name in swaps) != 1:
        return None
    return BUY_LEGS[swaps[-1]]

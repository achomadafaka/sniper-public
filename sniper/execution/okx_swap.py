"""OKX DEX Router (proVF4) `swap_tob` arguments, decoded from the program's own Anchor IDL.

The IDL (okx_dex_router_idl.json beside this module) was read from the program's IDL account
8wXL8gQduvMr6pmzhJnbUqsnnegJnmnPiZVPzehLjoeT at slot 457911741 on 2026-10-17 (decompressed sha256
below). swap_tob carries SwapArgs {order_id, amount_in, expect_amount_out, slippage,
routes: Vec<Route {dex, weight, index}>} then commission_info u32, platform_fee_rate u16, trim_rate u8.

FAIL CLOSED, as dflow_swap: a file that does not hash to IDL_SHA256, an unknown Dex variant, a field
type the reader does not know, or arguments that do not consume the data exactly all decode to None.

ROUTES. The flat route list is grouped into hops by weight: a hop ends when its weights reach 10000.
On the 49 armed Fomo buys of the 7 days to 2026-10-17 every route was USDC -> SOL (hop 1) -> coin
(hop 2, PumpSwap alone or split with Meteora DLMM / Raydium CLMM legs of the same coin). A hop-1
route with index 1 delivered SOL; one with index 2 swapped USDC straight into the coin (the token
flow of 3jbViXnX proves it), so the weights alone overstate the SOL that reaches PumpSwap.
"""
from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path

OKX_PROGRAM = "proVF4pMXVaYqmy4NjniPh4pqKNfMmsihgd4wdkCX3u"
IDL_PATH = Path(__file__).with_name("okx_dex_router_idl.json")
IDL_SHA256 = "c1f85197a5d96dd43fc2a1b981126cfe04c3eb6d2d68b3731129a259e45d54c9"
SWAP_TOB = bytes.fromhex("aa2955b184501f35")
PUMP_AMM_DEX = "PumpfunammBuy2"
WEIGHT_TOTAL = 10_000
SOL_HOP_INDEX = 1
MAX_DATA = 1232


@functools.cache
def _types():
    """The IDL's types by name, or None when the file is missing, unreadable or not the pinned IDL."""
    try:
        raw = IDL_PATH.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(raw).hexdigest() != IDL_SHA256:
        return None
    idl = json.loads(raw)
    ix = next((i for i in idl["instructions"] if bytes(i["discriminator"]) == SWAP_TOB), None)
    if ix is None or ix["name"] != "swap_tob":
        return None
    return {t["name"]: t["type"] for t in idl["types"]}, [(a["name"], a["type"]) for a in ix["args"]]


def idl_ok():
    return _types() is not None


def decode_swap_tob(data):
    """{"amount_in", "expect_amount_out", "slippage", "routes": ((dex, weight, index), ...),
    "commission_info", "platform_fee_rate", "trim_rate"} for a swap_tob whose arguments consume
    `data` exactly; None otherwise (fail closed)."""
    from sniper.execution.dflow_swap import _read, _Short
    loaded = _types()
    if loaded is None or not isinstance(data, (bytes, bytearray)) or not 8 < len(data) <= MAX_DATA:
        return None
    if bytes(data[:8]) != SWAP_TOB:
        return None
    types, args = loaded
    out, offset = {}, 8
    try:
        for name, ty in args:
            out[name], offset = _read(types, ty, bytes(data), offset)
    except _Short:
        return None
    if offset != len(data):
        return None
    swap = out["args"]
    routes = tuple((dex if isinstance(dex, str) else dex[0], weight, index)
                   for dex, weight, index in ((r["dex"], r["weight"], r["index"]) for r in swap["routes"]))
    return {"amount_in": swap["amount_in"], "expect_amount_out": swap["expect_amount_out"],
            "slippage": swap["slippage"], "routes": routes, "commission_info": out["commission_info"],
            "platform_fee_rate": out["platform_fee_rate"], "trim_rate": out["trim_rate"]}


def hops(routes):
    """The routes grouped into hops (each hop's weights reach WEIGHT_TOTAL), or None when they do not
    group exactly."""
    out, current, total = [], [], 0
    for route in routes:
        current.append(route)
        total += route[1]
        if total == WEIGHT_TOTAL:
            out.append(tuple(current))
            current, total = [], 0
        elif total > WEIGHT_TOTAL:
            return None
    return tuple(out) if out and not current else None


def pump_weight(decoded):
    """The PumpSwap leg's share (of WEIGHT_TOTAL) of the SOL that hop 1 delivers, or None: exactly two
    hops, every hop-1 route at index 1 (it delivers SOL), and exactly one PumpSwap leg, in hop 2."""
    grouped = hops((decoded or {}).get("routes") or ())
    if grouped is None or len(grouped) != 2:
        return None
    first, second = grouped
    if any(index != SOL_HOP_INDEX or dex == PUMP_AMM_DEX for dex, _w, index in first):
        return None
    legs = [weight for dex, weight, _i in second if dex == PUMP_AMM_DEX]
    return legs[0] if len(legs) == 1 and legs[0] > 0 else None

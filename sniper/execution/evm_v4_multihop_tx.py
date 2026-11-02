"""B's transaction builder: ONE atomic V4 multi-hop swap (spec 2026-08-30 Amendment 5, gate 1).

`evm_v4_multihop` decides a route; this builds the transaction for it. Both halves — entry AND
exit — are here deliberately: a four-leg entry with an unbuilt two-leg exit is the largest
possible version of "can buy where we cannot sell", which is the one thing never accepted.

WHY ONE COMMAND. V4's `SWAP_EXACT_IN` action takes a `PathKey[]`, so a two-hop route is a SINGLE
`V4_SWAP` with a single settlement. That makes two of the requirements structural rather than
bolted on:

  * ATOMIC. There is no window in which the router holds the bridge currency on our behalf. A
    two-command build could half-execute and strand us in USDG, which is neither our bankroll nor
    a coin we chose — an unexitable-bag shape.
  * ONE END-TO-END MINIMUM. `ExactInputParams.amountOutMinimum` bounds the FINAL output. Per-leg
    minimums can each pass while the compounded result is bad, because the legs multiply. The
    invariant the money cares about is coin-out for cash-in.

ABI SHAPE, since getting it wrong reverts rather than errors: `ExactInputParams` is
`(Currency currencyIn, PathKey[] path, uint128 amountIn, uint128 amountOutMinimum)` and each
`PathKey` is `(Currency intermediateCurrency, uint24 fee, int24 tickSpacing, IHooks hooks,
bytes hookData)`. Both structs contain dynamic members, so both are offset-encoded — the same
trap the single-hop encoder documents, where omitting the leading offset makes the decoder read
a currency as an offset.

`PathKey.intermediateCurrency` is the OUTPUT of its hop, not the input. So cash -> bridge -> coin
encodes `currencyIn = cash` with `path = [bridge, coin]`.
"""
from __future__ import annotations

import logging

from sniper.execution.evm_v4_quote import (
    V4, _NATIVE, _SEL_WETH_WITHDRAW, _ur_execute_data, _w, _wa, _permit2_input,
    _ACT_SETTLE_ALL, _ACT_TAKE, _ACT_TAKE_ALL, _ACT_SWAP_IN_SINGLE,
    _ADDRESS_THIS, _CONTRACT_BALANCE, _MSG_SENDER,
    _CMD_V4_SWAP, _CMD_PERMIT2_PERMIT, _CMD_WRAP_ETH, _fee_is_sane,
)
from sniper.services.memecoin_evm_gas_topup import WETH_CALL_GAS_LIMIT

logger = logging.getLogger(__name__)

# V4Router action for a MULTI-HOP exact-input swap. 0x06 is the single-pool variant.
_ACT_SWAP_EXACT_IN = 0x07
_U128_MAX = 1 << 128


def _fits(*values):
    return all(isinstance(v, int) and 0 <= v < _U128_MAX for v in values)


def _path_key(currency_out, fee, tick_spacing, hooks):
    """One PathKey, offset-encoded because `hookData` is dynamic.

    Layout: intermediateCurrency, fee, tickSpacing, hooks, offset(hookData)=0xa0, len(hookData)=0.
    """
    ts = int(tick_spacing) % (1 << 256)
    return (_wa(currency_out) + _w(int(fee)) + format(ts, "064x") + _wa(hooks)
            + _w(0xA0) + _w(0))


def _encode_exact_input(currency_in, path, amount_in, min_out, *, chain):
    """abi.encode(ExactInputParams) with the leading struct offset, in the layout of `chain`'s
    deployed Universal Router.

    `path` is a list of (currency_out, fee, tick_spacing, hooks). `chain` is required: the struct
    layout belongs to the router, and evm_v4_quote._EXACT_INPUT_FOUR_FIELD_CHAINS is the one place
    that says which chains take which (the single-hop path encoder reads the same set).
    """
    # FIVE head fields on 4663, 56 and 5042, and the third is easy to miss:
    #     struct ExactInputParams {
    #         Currency  currencyIn;
    #         PathKey[] path;
    #         uint256[] minHopPriceX36;    <-- omitted in the first version of this encoder
    #         uint128   amountIn;
    #         uint128   amountOutMinimum;
    #     }
    # Omitting it shifts amountIn and amountOutMinimum into the array offsets and the router
    # reverts with EMPTY revert data -- no Error(string), nothing to read at the node. Verified by
    # read-only eth_call on 2026-08-30 (4663): the 4-field encoding REVERTS and the 5-field encoding
    # is OK, same pool, same clip, same minimum.
    #
    # FOUR FIELDS ON ETHEREUM (1) AND BASE (8453) (2026-10-01, Gamma LOW 1 on 1192ad309). Their
    # routers run upstream v4-periphery: (currencyIn, PathKey[] path, amountIn, amountOutMinimum).
    # Fed the five-field form they read amountIn from the slot holding the array offset and revert
    # V4TooLittleReceived -- what OPH 67763 hit on Base through the single-hop path encoder.
    #
    # The PathKey[] encoding itself was proven correct BEFORE the 08-30 fix by calling the read-only
    # V4Quoter's quoteExactInput with the same array (path length 1 and 2 both returned real
    # amounts), which isolated the defect to this wrapper rather than the path.
    #
    # minHopPriceX36 is encoded EMPTY: no per-hop price bound, so the end-to-end
    # amountOutMinimum remains the only bound, which is the property this module exists to keep.
    from sniper.execution import evm_v4_quote as _q
    four = str(chain) in _q._EXACT_INPUT_FOUR_FIELD_CHAINS
    n = len(path)
    path_offset = (4 if four else 5) * 32
    bodies = [_path_key(*p) for p in path]
    offs = ""
    cursor = 32 * n
    for b in bodies:
        offs += _w(cursor)
        cursor += len(b) // 2
    arr_path = _w(n) + offs + "".join(bodies)
    if four:
        return _w(0x20) + _wa(currency_in) + _w(path_offset) + _w(amount_in) + _w(min_out) + arr_path
    minhop_offset = path_offset + len(arr_path) // 2
    head = (_wa(currency_in) + _w(path_offset) + _w(minhop_offset)
            + _w(amount_in) + _w(min_out))
    return _w(0x20) + head + arr_path + _w(0)


def _v4_multihop_swap_input(currency_in, currency_out, path, amount_in, min_out, *,
                            take_all_to_sender=True, chain):
    """abi.encode(bytes actions, bytes[] params) for ONE multi-hop V4_SWAP command."""
    if not _fits(int(amount_in), int(min_out)):
        raise ValueError("V4 exact-input amounts must fit uint128")
    take_action = _ACT_TAKE_ALL if take_all_to_sender else _ACT_TAKE
    actions = bytes([_ACT_SWAP_EXACT_IN, _ACT_SETTLE_ALL, take_action])
    p0 = _encode_exact_input(currency_in, path, int(amount_in), int(min_out), chain=chain)
    p1 = _wa(currency_in) + _w(int(amount_in))       # SETTLE_ALL(currencyIn, amountIn)
    if take_all_to_sender:
        p2 = _wa(currency_out) + _w(int(min_out))
    else:
        p2 = _wa(currency_out) + _wa(_ADDRESS_THIS) + _w(0)
    params = [p0, p1, p2]
    head = _w(0x40) + _w(0x40 + 32 + ((len(actions) + 31) // 32) * 32)
    actions_enc = _w(len(actions)) + (actions.hex() + "0" * 64)[:64]
    offs, tails = "", ""
    off = 32 * len(params)
    for p in params:
        blob = _w(len(p) // 2) + p
        offs += _w(off)
        off += len(blob) // 2
        tails += blob
    return head + actions_enc + _w(len(params)) + offs + tails, actions


def _legs_from_route(route, cash, mint):
    """(bridge, [(out, fee, ts, hooks), ...]) for cash -> bridge -> mint, or None.

    Fee-screens EVERY leg. The bridge's dynamic-fee exclusion is enforced in the decision layer;
    this is the second line, because a route reaching here with an unsane fee is a wiring bug.
    """
    # AN ORDERED `legs_path` WINS WHEN PRESENT. A route stored before this field existed has only
    # the pair, and it must still build -- every open position predates it. The pair path below is
    # therefore the compatibility branch, not a fallback for a malformed new route.
    try:
        legs_path = route.get("legs_path")
    except AttributeError:
        return None
    if legs_path:
        try:
            legs = [(str(leg["currency"]), leg["key"]["fee"], leg["key"]["tick_spacing"],
                     leg["key"]["hooks"]) for leg in legs_path]
        except (KeyError, TypeError):
            return None
        if len(legs) < 2:
            return None
        for _currency, fee, _ts, _hooks in legs:
            if not _fee_is_sane(fee):
                return None
        if str(legs[-1][0]).lower() != str(mint).lower():
            return None                     # the last leg must output the coin we are trading
        return legs[0][0], legs
    try:
        bridge = route["bridge"]
        bk = route["bridge_key"]
        ck = route["coin_key"]
    except (KeyError, TypeError):
        return None
    for key in (bk, ck):
        if not key or not _fee_is_sane(key.get("fee")):
            return None
    return bridge, [
        (bridge, bk["fee"], bk["tick_spacing"], bk["hooks"]),
        (mint, ck["fee"], ck["tick_spacing"], ck["hooks"]),
    ]


def native_multihop_buy_tx(chain, mint, path, amount_wei, min_out, deadline):
    """The unsigned UR [V4_SWAP] native -> bridge -> coin buy paid by value from the native float: the
    4663 fast lane's routes R2. `path` = [(bridge, fee, tick_spacing, hooks), (mint, fee, tick_spacing,
    hooks)]. The bytes are build_multihop_entry's native case without its WETH-unwrap pre-transaction
    (the lane holds native): one SWAP_EXACT_IN, SETTLE_ALL native, TAKE_ALL coin, one end-to-end minimum.
    Raises ValueError for a path that does not end in `mint`, an unsane fee or a bad amount."""
    if not path or str(path[-1][0]).lower() != str(mint).lower():
        raise ValueError("the path must end in the coin")
    if not all(_fee_is_sane(p[1]) for p in path):
        raise ValueError("unsane fee on a leg")
    if not (0 < int(amount_wei) < _U128_MAX and 0 < int(min_out) < _U128_MAX):
        raise ValueError("amount and min_out must be positive uint128")
    conf = V4[str(chain)]
    swap_input, _actions = _v4_multihop_swap_input(_NATIVE, mint, path, int(amount_wei), int(min_out),
                                                   chain=chain)
    return {"to": conf["universal_router"], "value": hex(int(amount_wei)), "gas": _multihop_gas(len(path)),
            "data": _ur_execute_data(bytes([_CMD_V4_SWAP]), [swap_input], int(deadline))}


def build_multihop_entry(chain, cash_token, mint, route, amount_in, min_out, *, taker,
                         acct=None, now_fn=None, deadline_s=600):
    """cash -> bridge -> coin in ONE V4_SWAP. Returns a dict, or None (fail-closed)."""
    conf = V4.get(str(chain))
    if not conf or not _fits(int(amount_in), int(min_out)):
        return None
    legs = _legs_from_route(route, cash_token, mint)
    if legs is None:
        return None
    bridge, path = legs
    swap_input, actions = _v4_multihop_swap_input(cash_token, mint, path, amount_in, min_out, chain=chain)
    is_native = str(cash_token).lower() == _NATIVE
    commands = bytes([_CMD_V4_SWAP])
    inputs = [swap_input]
    pre = []
    if is_native:
        from sniper.execution.evm_wallet import CHAIN_QUOTE
        weth = (CHAIN_QUOTE.get(str(chain)) or {}).get("token")
        if not weth:
            return None
        pre = [{"to": weth, "data": "0x" + _SEL_WETH_WITHDRAW + _w(int(amount_in)),
                "value": "0x0", "gas": str(WETH_CALL_GAS_LIMIT)}]
    else:
        # paying an ERC-20 needs a permit2, and the executor must be told to approve it
        if acct is None:
            return None
        permit = _permit2_input(chain, acct, cash_token, conf["universal_router"],
                                int(amount_in), route.get("rpc_url"), now_fn=now_fn)
        if permit is None:
            return None
        commands = bytes([_CMD_PERMIT2_PERMIT, _CMD_V4_SWAP])
        inputs = [permit, swap_input]
    if now_fn is None:
        from sniper.util.clock import real_utc_now
        now_fn = real_utc_now
    from sniper.settings.memecoin import memecoin_evm_swap_deadline_s
    data = _ur_execute_data(commands, inputs,
                            int(now_fn()) + memecoin_evm_swap_deadline_s(chain, deadline_s))
    return {
        "commands": commands, "actions": actions, "swap_input": swap_input,
        "currency_in": cash_token, "path_currencies": [p[0] for p in path],
        "path_len": len(path), "per_leg_minimums": False,
        "bridge": bridge,
        "to": conf["universal_router"], "data": data,
        "value": hex(int(amount_in)) if is_native else "0x0",
        "legs_n": len(path),
        "pre_transactions": pre,
    }


def build_multihop_exit(chain, mint, cash_token, route, amount_in, min_out, *, taker,
                        acct=None, now_fn=None, deadline_s=600):
    """coin -> bridge -> cash in ONE V4_SWAP. Built HERE, not in a follow-up.

    Selling an ERC-20 always needs a permit2 signature, so a missing signer refuses rather than
    falling back to something that would revert.
    """
    conf = V4.get(str(chain))
    if not conf or not _fits(int(amount_in), int(min_out)):
        return None
    if acct is None:
        return None
    legs = _legs_from_route(route, cash_token, mint)
    if legs is None:
        return None
    bridge, entry_path = legs
    # THE EXIT IS THE ENTRY PATH REVERSED. Each PathKey names the OUTPUT of its hop, so reversing
    # shifts the currencies by one: hop i sells into the currency hop i-1 bought, and the last hop
    # lands in cash. The pool KEYS keep their own order reversed. Selling a coin out through a
    # different path than it came in is how a position exits through a trap pool it avoided on
    # entry, so the stored route is replayed rather than searched again.
    n = len(entry_path)
    path = [(entry_path[i - 1][0] if i else cash_token,
             entry_path[i][1], entry_path[i][2], entry_path[i][3])
            for i in range(n - 1, -1, -1)]
    wrap_native = str(cash_token).lower() == _NATIVE
    swap_input, actions = _v4_multihop_swap_input(
        mint, cash_token, path, amount_in, min_out, take_all_to_sender=not wrap_native, chain=chain)
    permit = _permit2_input(chain, acct, mint, conf["universal_router"], int(amount_in),
                            route.get("rpc_url"), now_fn=now_fn)
    if permit is None:
        return None
    if now_fn is None:
        from sniper.util.clock import real_utc_now
        now_fn = real_utc_now
    commands = bytes([_CMD_PERMIT2_PERMIT, _CMD_V4_SWAP])
    inputs = [permit, swap_input]
    if wrap_native:
        commands += bytes([_CMD_WRAP_ETH])
        inputs.append(_wa(_MSG_SENDER) + _w(_CONTRACT_BALANCE))
    from sniper.settings.memecoin import memecoin_evm_swap_deadline_s
    data = _ur_execute_data(commands, inputs,
                            int(now_fn()) + memecoin_evm_swap_deadline_s(chain, deadline_s))
    return {
        "commands": commands, "actions": actions, "swap_input": swap_input,
        "currency_in": mint, "path_currencies": [p[0] for p in path],
        "path_len": len(path), "per_leg_minimums": False,
        "bridge": bridge,
        "to": conf["universal_router"], "data": data, "value": "0x0",
        "legs_n": len(path),
        "pre_transactions": [],
    }


def as_zerox_quote(chain, cash_token, mint, route, amount_in, min_out, *, out_amount, taker,
                   acct=None, now_fn=None):
    """The 0x-shaped dict the executor and the premium gate consume.

    THE BRIDGE CURRENCY MUST NOT APPEAR. The premium gate prices USD per bought token for the
    WHOLE clip from `sellAmount`/`buyAmount`; surfacing the intermediate would let a comparison be
    made against the wrong asset. That is the 2026-07-30 unit defect, which produced $3.47M of
    fabricated PnL with ONE non-USD unit — a two-hop route has two. Cash in, coin out, nothing
    between.
    """
    built = build_multihop_entry(chain, cash_token, mint, route, amount_in, min_out,
                                 taker=taker, acct=acct, now_fn=now_fn)
    if built is None:
        return None
    conf = V4.get(str(chain))
    return {
        "sellAmount": str(int(amount_in)),
        "buyAmount": str(int(out_amount)),
        "minBuyAmount": str(int(min_out)),
        "liquidityAvailable": True,
        "pre_transactions": built["pre_transactions"],
        "transaction": {"to": built["to"], "data": built["data"],
                        "value": built["value"], "gas": _multihop_gas(built.get("legs_n", 2))},
        "issues": {"allowance": (None if built["value"] != "0x0"
                                 else {"spender": conf["permit2"]})},
        # `legs` KEEPS ITS MEANING as the two-hop count the executor and its tests read;
        # `legs_path` is the ordered route the exit replays in reverse.
        "route": {"protocol": "uniswap_v4_multihop", "legs": 2,
                  "pool_fee": route["coin_key"]["fee"], "bridge": route["bridge"],
                  "bridge_key": route["bridge_key"], "coin_key": route["coin_key"],
                  "legs_path": route.get("legs_path")},
    }


def _multihop_gas(legs_n=2):
    """Gas for a V4 multihop swap, as a decimal string.

    Was the literal "900000". FROGE 50786 used 899,687 of it and the trailing WRAP_ETH got 17,432
    gas and ran OUT: this chain's WETH is a proxy, so deposit() delegatecalls and costs far more
    than a standard WETH9. The swap legs all succeeded; only the wrap failed, and the position was
    un-exitable for 34 minutes across five attempts because of one constant. Replaying the exact
    reverted calldata: 900,000 reverts, 1,200,000 succeeds.
    """
    from sniper.settings import memecoin as _cfg
    two_leg = int(getattr(_cfg, "MEMECOIN_EVM_V4_MULTIHOP_GAS", 1_500_000))
    per_leg = int(getattr(_cfg, "MEMECOIN_EVM_V4_MULTIHOP_GAS_PER_LEG", 0) or 0)
    # THE TWO-LEG VALUE IS UNCHANGED AND STILL OPERATOR-TUNABLE. Extra legs add headroom on top of
    # it rather than replacing it, so retuning the measured limit keeps working for every length.
    extra = max(0, int(legs_n) - 2)
    return str(two_leg + extra * per_leg)


def as_zerox_exit_quote(chain, cash_token, mint, route, amount_in, min_out, *, out_amount,
                        taker, acct=None, now_fn=None):
    """Return an exit quote with WETH output after the router wraps native cash."""
    built = build_multihop_exit(
        chain, mint, cash_token, route, amount_in, min_out,
        taker=taker, acct=acct, now_fn=now_fn)
    if built is None:
        return None
    conf = V4.get(str(chain))
    return {
        "sellAmount": str(int(amount_in)),
        "buyAmount": str(int(out_amount)),
        "minBuyAmount": str(int(min_out)),
        "liquidityAvailable": True,
        "pre_transactions": [],
        "transaction": {"to": built["to"], "data": built["data"],
                        "value": built["value"], "gas": _multihop_gas(built.get("legs_n", 2))},
        "issues": {"allowance": {"spender": conf["permit2"]}},
        # `legs` KEEPS ITS MEANING as the two-hop count the executor and its tests read;
        # `legs_path` is the ordered route the exit replays in reverse.
        "route": {"protocol": "uniswap_v4_multihop", "legs": 2,
                  "pool_fee": route["coin_key"]["fee"], "bridge": route["bridge"],
                  "bridge_key": route["bridge_key"], "coin_key": route["coin_key"],
                  "legs_path": route.get("legs_path")},
    }

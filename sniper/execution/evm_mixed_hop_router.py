"""Atomic PancakeSwap V3 -> V2 mixed hops on chain 56, through the SmartRouter.

WHY THIS EXISTS. 8 of 13 chain-56 `no_route` signals in the 8.8 days to 2026-09-22 are Pancake V2
pairs quoted in a NON-CONNECTOR base token (BEM, MCDon, BNCB, oANTHROPIC). Their pools were fine —
$9.9k-$22.4k, $100 round trip 1.4-2.5%. The V2-ONLY path through the coin's own pair token is
already live (`pair_quote_tokens`, and `MEMECOIN_EVM_V2_PAIR_DISCOVERY_CHAINS` defaults to "56"),
and those signals refused anyway, so the base token has no V2 route back to a connector. The
missing leg is a V3 BRIDGE: quote token -> base token on V3, then base token -> coin on V2.

ONE TRANSACTION, NEVER TWO. Both legs go in a single `multicall`. Two sequential swaps would mean
a fill on the first and a failure on the second leaves an unhedged position in a coin the ladder
refused outright — an entry without its exit. The exit is built by the SAME builder for the same
reason: a position opened on a mixed hop can only be closed on one, and it is confirmed BEFORE the
entry is signed.

THE ROUTER, verified on chain 2026-09-22 against bsc-rpc.publicnode.com, never from documentation:
`0x13f4EA83D0bd40E75C8222255bc855a974568Dd4`, 24,316 bytes of code, whose `deployer()` and
`factory()` are byte-identical to our already-funded PCS V3 router's and whose `WETH9()` equals
V2 Router02's `WETH()` and our configured WBNB connector.

SELECTOR NOTE (2026-09-22; the V3 leg moved to the path form `exactInput` 0xb858183f on 2026-09-25,
also present in the deployed bytecode, because the bridge is often two pools). The SmartRouter dispatches `0x04e45aaf` — the
SwapRouter02 `exactInputSingle` with NO deadline field, which is the calldata
`evm_dex_quote._encode_exact_input_single` already builds and already funds. It does NOT dispatch
`0x414bf389`, the deadline variant our standalone PCS V3 router uses. The deadline lives on
`multicall(uint256,bytes[])` instead. So the V3 leg needs no new encoder.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

# multicall(uint256 deadline, bytes[] data) — verified present in the deployed bytecode.
MULTICALL_DEADLINE = "5ae401dc"
# swapExactTokensForTokens(uint256 amountIn, uint256 amountOutMin, address[] path, address to)
# — verified present. NOTE: no deadline parameter; the multicall carries it.
SWAP_EXACT_TOKENS_FOR_TOKENS = "472b43f3"
# sweepToken(address,uint256,address), used only to isolate simulated proceeds.
SWEEP_TOKEN = "df2ab5bb"

# THE SECOND LEG SPENDS THE ROUTER'S WHOLE BALANCE (verified 2026-09-25, read-only eth_call on 56).
# SwapRouter02-family routers read amountIn == 0 as "pay from my own balance". An EXPLICIT amount on
# the second leg is paid from msg.sender instead, who holds none of the bridged token, and the
# multicall reverts `STF` -- measured on BOTH legs for 螃蟹 via oANTHROPIC. The first design here
# spent `bridged_min` on leg 2 and would have reverted every entry and every exit. With the
# sentinel the whole bridged amount is spent, so no dust is left in the router either.
CONTRACT_BALANCE = 0

SMART_ROUTERS = {
    "56": "0x13f4ea83d0bd40e75c8222255bc855a974568dd4",
}


def smart_router_for(chain):
    """The verified SmartRouter for `chain`, or None. Never guesses an address."""
    return SMART_ROUTERS.get(str(chain))


def _w_uint(x):
    return format(int(x), "064x")


def _w_addr(a):
    return str(a).lower().replace("0x", "").rjust(64, "0")


def encode_v2_leg(amount_in, amount_out_min, path, recipient):
    """`swapExactTokensForTokens` calldata. `path` is a list of token addresses.

    The array is DYNAMIC, so the head holds an offset to it and the tail holds length + items.
    """
    tokens = [str(t) for t in path]
    if len(tokens) < 2:
        raise ValueError("a V2 leg needs at least two tokens")
    head = (_w_uint(amount_in) + _w_uint(amount_out_min)
            + _w_uint(4 * 32)                      # offset to `path`, past the four head words
            + _w_addr(recipient))
    tail = _w_uint(len(tokens)) + "".join(_w_addr(t) for t in tokens)
    return "0x" + SWAP_EXACT_TOKENS_FOR_TOKENS + head + tail


def encode_multicall(deadline, calls):
    """`multicall(uint256,bytes[])` over already-encoded leg calldata."""
    if not calls:
        raise ValueError("a multicall needs at least one call")
    payloads = [bytes.fromhex(str(c)[2:] if str(c).startswith("0x") else str(c)) for c in calls]
    head = _w_uint(int(deadline)) + _w_uint(2 * 32)      # offset to the bytes[] array
    body = _w_uint(len(payloads))
    # Each element is itself dynamic: an offset table, then length-prefixed padded payloads.
    offsets, blobs, cursor = [], [], len(payloads) * 32
    for payload in payloads:
        offsets.append(_w_uint(cursor))
        padded = payload + b"\x00" * ((32 - len(payload) % 32) % 32)
        blobs.append(_w_uint(len(payload)) + padded.hex())
        cursor += 32 + len(padded)
    return "0x" + MULTICALL_DEADLINE + head + body + "".join(offsets) + "".join(blobs)


def _v3_leg_path(first, last, v3_fee=None, v3_tokens=None, v3_fees=None):
    """(tokens, fees) of the V3 leg, single pool (`v3_fee`) or a path (`v3_tokens`/`v3_fees`).

    The bridge is often TWO V3 pools: chain 56's quote token is WBNB, and oANTHROPIC has no WBNB
    pool, only USDT (WBNB -> USDT -> oANTHROPIC)."""
    if v3_tokens is None:
        if v3_fee is None:
            raise ValueError("a V3 leg needs a fee or a path")
        tokens, fees = [first, last], [int(v3_fee)]
    else:
        tokens, fees = [str(t) for t in v3_tokens], [int(f) for f in v3_fees]
    if len(tokens) != len(fees) + 1 or len(tokens) < 2:
        raise ValueError("V3 path token/fee arity mismatch")
    if tokens[0].lower() != str(first).lower() or tokens[-1].lower() != str(last).lower():
        raise ValueError("V3 path does not join the legs")
    return tokens, fees


def _v3_exact_input(tokens, fees, recipient, amount_in, amount_out_min):
    from sniper.execution.evm_dex_quote import _encode_exact_input, _encode_path
    return _encode_exact_input(_encode_path(tokens, fees), recipient, int(amount_in),
                               int(amount_out_min))


def build_entry_calldata(*, quote_token, base_token, coin, amount_in, bridged_min, coin_min,
                         recipient, deadline, router, v3_fee=None, v3_tokens=None, v3_fees=None):
    """One transaction: quote -> base on V3 INTO THE ROUTER, then base -> coin on V2 spending the
    router's whole balance (CONTRACT_BALANCE). `bridged_min` floors leg 1; `coin_min` floors the
    whole trade."""
    tokens, fees = _v3_leg_path(quote_token, base_token, v3_fee, v3_tokens, v3_fees)
    leg_v3 = _v3_exact_input(tokens, fees, router, amount_in, bridged_min)
    leg_v2 = encode_v2_leg(CONTRACT_BALANCE, int(coin_min), [base_token, coin], recipient)
    return encode_multicall(deadline, [leg_v3, leg_v2])


def build_exit_calldata(*, coin, base_token, quote_token, amount_in, bridged_min, quote_min,
                        recipient, deadline, router, v3_fee=None, v3_tokens=None, v3_fees=None,
                        simulation_only_sweep=False):
    """The mirror: coin -> base on V2 into the router, then base -> quote on V3 spending the
    router's whole balance. Same router as the entry: a position opened on a mixed hop has no
    single-venue exit, which is why the bridge exists."""
    tokens, fees = _v3_leg_path(base_token, quote_token, v3_fee, v3_tokens, v3_fees)
    leg_v2 = encode_v2_leg(int(amount_in), int(bridged_min), [coin, base_token], router)
    leg_v3 = _v3_exact_input(tokens, fees, recipient, CONTRACT_BALANCE, quote_min)
    calls = [leg_v2, leg_v3]
    if simulation_only_sweep:
        # CONTRACT_BALANCE would otherwise count public-router inventory as our proceeds.
        # Clear it inside this same eth_call; never add this transfer to a signed exit.
        sweep = "0x" + SWEEP_TOKEN + _w_addr(base_token) + _w_uint(0) + _w_addr(recipient)
        calls.insert(0, sweep)
    return encode_multicall(deadline, calls)


def simulate_exit(*, chain, coin, base_token, quote_token, amount_in, taker, rpc_url,
                  http=None, deadline, bridged_min=1, quote_min=1, call_fn=None,
                  fund_overrides_fn=None, v3_fee=None, v3_tokens=None, v3_fees=None,
                  safety_deadline=None, result_out=None):
    """Can the WHOLE would-be balance be sold back through the same router? (ok, reason).

    Run BEFORE any entry is signed, with the coin balance and the router allowance funded by
    `eth_call` state override: we do not hold the coin yet, so an un-overridden sell reverts on
    every token and would read as unsellable (the 2026-07-14 regression that refused 27 of 28 real
    entries). The minimums default to 1 wei: this asks "does the path EXECUTE", not "is the price
    good". The price question is the quote's.
    """
    if result_out is not None:
        result_out.clear()
    raw_result = {}
    router = smart_router_for(chain)
    if router is None:
        return False, "no_verified_smart_router"
    try:
        data = build_exit_calldata(
            coin=coin, base_token=base_token, quote_token=quote_token, v3_fee=v3_fee,
            v3_tokens=v3_tokens, v3_fees=v3_fees, amount_in=int(amount_in),
            bridged_min=int(bridged_min), quote_min=int(quote_min), recipient=taker,
            deadline=deadline, router=router, simulation_only_sweep=result_out is not None)
    except (TypeError, ValueError) as exc:
        return False, "exit_calldata_unbuildable:%s" % str(exc)[:60]

    live_call = call_fn is None
    if live_call:
        from sniper.execution.evm_real_seams import _rpc_eth_call_result

        def call_fn(tx, overrides):
            return _rpc_eth_call_result(tx, taker, rpc_url, http=http, overrides=overrides,
                                        **({"result_out": raw_result} if result_out is not None else {}))

    overrides = None
    if fund_overrides_fn is not None:
        try:
            if safety_deadline is not None and time.monotonic() >= safety_deadline:
                return False, "exit_overrides_unavailable:timeout"
            kwargs = {"deadline": safety_deadline} if safety_deadline is not None else {}
            overrides = fund_overrides_fn(coin, router, int(amount_in), **kwargs)
        except Exception as exc:
            logger.exception("mixed-hop exit override build failed for %s", coin)
            kind = "timeout" if isinstance(exc, TimeoutError) else getattr(exc, "failure_type", None)
            return False, "exit_overrides_unavailable" + (":" + kind if kind else "")
        if overrides is None:
            return False, "exit_overrides_undiscoverable"
    elif live_call:
        # NO BUILDER, NO SIMULATION, AND SAY SO (E7, 2026-09-23). The builder is token safety's
        # `_fund_overrides`, exposed as `token_safety_fn.fund_overrides` and handed to the ladder.
        return False, "exit_overrides_unsupported"

    tx = {"to": router, "data": data, "value": 0}
    try:
        if safety_deadline is not None and time.monotonic() >= safety_deadline:
            return False, "exit_sim_error:timeout"
        status, _kind, detail = call_fn(tx, overrides)
    except Exception as exc:
        logger.exception("mixed-hop exit simulation failed for %s", coin)
        kind = "timeout" if isinstance(exc, TimeoutError) else getattr(exc, "failure_type", None)
        return False, "exit_sim_error" + (":" + kind if kind else "")
    if status is True:
        if result_out is not None:
            amount = _exit_proceeds(raw_result.get("result"))
            if amount is not None:
                result_out["amount_out_atomic"] = amount
        return True, "exit_simulated"
    if status is None:
        # TRANSPORT, not the token. Fail closed, but say so: a 429 is not an unsellable coin.
        return False, "exit_sim_unknown" + (":" + _kind if _kind else "")
    return False, "exit_sim_reverted:%s" % (" ".join(str(detail or "").split())[:60] or "no_reason")


def _exit_proceeds(raw):
    """Exact isolated SmartRouter multicall return; malformed data is unknown.

    The simulation-only sweep returns empty bytes; both swaps return one uint256.
    Old two-leg calls include unowned router inventory and cannot supply a price proof.
    """
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) != 642:
        return None
    body = raw[2:].lower()
    if any(c not in "0123456789abcdef" for c in body):
        return None
    words = [int(body[i:i + 64], 16) for i in range(0, 640, 64)]
    if (words[:7] != [32, 3, 96, 128, 192, 0, 32] or words[8] != 32
            or words[7] <= 0 or words[9] <= 0):
        return None
    return words[9]


def plan_mixed_entry(*, chain, quote_token, base_token, coin, amount_in, bridged_min,
                     coin_min, taker, rpc_url, deadline, expected_coin_out, http=None,
                     exit_sim_fn=None, enabled=None, fund_overrides_fn=None, v3_fee=None,
                     v3_tokens=None, v3_fees=None, exit_v3_tokens=None, exit_v3_fees=None,
                     safety_deadline=None, exit_result_out=None):
    """Everything an executor needs to sign a mixed-hop entry, or the reason there is none.

    THE EXIT IS CHECKED HERE, NOT BY THE CALLER: without a simulated whole-balance exit there is no
    plan and nothing to sign. The exit path defaults to the entry's V3 path reversed.
    """
    from sniper.settings import memecoin as cfg
    on = (bool(getattr(cfg, "MEMECOIN_EVM_MIXED_HOP_EXECUTION_ENABLED", False))
          if enabled is None else bool(enabled))
    if not on:
        return None, "mixed_hop_execution_disabled"
    router = smart_router_for(chain)
    if router is None:
        return None, "no_verified_smart_router"
    if int(expected_coin_out) <= 0:
        return None, "no_expected_output"
    try:
        tokens, fees = _v3_leg_path(quote_token, base_token, v3_fee, v3_tokens, v3_fees)
    except ValueError as exc:
        return None, "entry_calldata_unbuildable:%s" % str(exc)[:60]
    if exit_v3_tokens is None:
        exit_v3_tokens, exit_v3_fees = list(reversed(tokens)), list(reversed(fees))

    sim = exit_sim_fn or simulate_exit
    ok, reason = sim(chain=chain, coin=coin, base_token=base_token, quote_token=quote_token,
                     v3_tokens=exit_v3_tokens, v3_fees=exit_v3_fees,
                     amount_in=int(expected_coin_out), taker=taker, rpc_url=rpc_url, http=http,
                     deadline=deadline, fund_overrides_fn=fund_overrides_fn,
                     **({"result_out": exit_result_out} if exit_result_out is not None else {}),
                     **({"safety_deadline": safety_deadline} if safety_deadline is not None else {}))
    if not ok:
        return None, "exit_unproven:%s" % reason

    try:
        data = build_entry_calldata(
            quote_token=quote_token, base_token=base_token, coin=coin, v3_tokens=tokens,
            v3_fees=fees, amount_in=int(amount_in), bridged_min=int(bridged_min),
            coin_min=int(coin_min), recipient=taker, deadline=deadline, router=router)
    except (TypeError, ValueError) as exc:
        return None, "entry_calldata_unbuildable:%s" % str(exc)[:60]
    return {
        "router": router,
        "to": router,
        "data": data,
        "value": 0,
        # The SmartRouter is a NEW spender: the quote token's allowance to it is not the one the
        # single-venue routers hold. The executor approves this address, never assumes it.
        "approve_token": str(quote_token).lower(),
        "approve_spender": router,
        "approve_amount": int(amount_in),
        "expected_coin_out": int(expected_coin_out),
        "coin_min": int(coin_min),
        "base_token": str(base_token).lower(),
        "exit_v3_tokens": [str(t).lower() for t in exit_v3_tokens],
        "exit_v3_fees": [int(f) for f in exit_v3_fees],
        "exit_proven": True,
        "exit_reason": reason,
    }, "planned"


# --- the ladder rung ------------------------------------------------------------------------------

PROTOCOL = "pcs_mixed_v3_v2"
_BPS = 10_000


def _amount(quote, key="buyAmount"):
    try:
        return int((quote or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _v3_route(quote, first=None, last=None):
    """(tokens, fees) of a single- or multi-pool PancakeSwap V3 quote, or None."""
    route = (quote or {}).get("route") or {}
    if not isinstance(route, dict):
        return None
    tokens, fees = route.get("tokens"), route.get("fees")
    if route.get("protocol") != "pancake_v3" or not tokens or not fees:
        return None
    if len(tokens) != len(fees) + 1:
        return None
    try:
        tokens, fees = [str(t).lower() for t in tokens], [int(f) for f in fees]
        if (any(len(t) != 42 or not t.startswith("0x")
                or any(c not in "0123456789abcdef" for c in t[2:]) for t in tokens)
                or any(f <= 0 or f > 1_000_000 for f in fees)):
            return None
    except (TypeError, ValueError):
        return None
    if ((first is not None and tokens[0] != str(first).lower())
            or (last is not None and tokens[-1] != str(last).lower())):
        return None
    return tokens, fees


def _v2_direct(v2_quote_fn, chain, sell, buy, amount, taker, *, http, slippage_bps, tax_buffer_bps):
    """A V2 quote on the ONE pair sell/buy only: the SmartRouter leg names exactly that pair."""
    q = v2_quote_fn(chain, sell, buy, int(amount), taker, http=http, slippage_bps=slippage_bps,
                    tax_buffer_bps=tax_buffer_bps, connector_tokens=(),
                    pair_tokens_fn=lambda *_a, **_k: ())
    tokens = [str(t).lower() for t in ((q or {}).get("route") or {}).get("tokens") or ()]
    if q is None or tokens != [str(sell).lower(), str(buy).lower()]:
        return None
    return q


def _bases(chain, coin, quote_token, pair_tokens_fn, cfg):
    connectors = {str(c).lower() for c in
                  ((cfg.memecoin_evm_v2_exit_config(chain) or {}).get("connectors") or ())}
    out = []
    for base in (pair_tokens_fn(chain, coin) or ()):
        base = str(base).lower()
        if (len(base) != 42 or not base.startswith("0x")
                or any(c not in "0123456789abcdef" for c in base[2:])
                or base in connectors or base in (str(quote_token).lower(), str(coin).lower())
                or base in out):
            continue            # a connector base needs no bridge: the single-venue rungs cover it
        out.append(base)
        if len(out) == 2:
            break
    return out


def _route_gas():
    """MEMECOIN_EVM_DEFAULT_ROUTE_GAS: this builder's transaction carries its own limit. A knob at 0
    or below would sign gas 0 ("0" is truthy to the signer): the default applies instead."""
    from sniper.settings import memecoin as _cfg
    return int(_cfg.MEMECOIN_EVM_DEFAULT_ROUTE_GAS) if int(_cfg.MEMECOIN_EVM_DEFAULT_ROUTE_GAS) > 0 \
        else 900_000


def mixed_entry_quote(chain, quote_token, coin, amount_in, taker, *, rpc_url, fund_overrides_fn,
                      http=None, slippage_bps=None, v3_quote_fn=None, v2_quote_fn=None,
                      pair_tokens_fn=None, now_fn=None, deadline_s=300.0, cfg=None,
                      exit_sim_fn=None, safety_deadline=None):
    """(quote, None) shaped for the shared EVM executor, or (None, reason).

    The quote carries its own measured `route.round_trip_pct` (the same two pools, back out at the
    whole bought amount) and exists only after a whole-balance SmartRouter exit simulated clean.
    """
    import time as _time
    if cfg is None:
        from sniper.settings import memecoin as cfg
    if v3_quote_fn is None:
        from sniper.execution.evm_dex_quote import dex_v3_quote as v3_quote_fn
    if v2_quote_fn is None:
        from sniper.execution.evm_v2_exit_quote import dex_v2_exit_quote as v2_quote_fn
    if pair_tokens_fn is None:
        from sniper.execution.evm_v2_exit_quote import pair_quote_tokens as pair_tokens_fn
    router = smart_router_for(chain)
    if router is None:
        return None, "no_verified_smart_router"
    s_bps = int(slippage_bps if slippage_bps is not None
                else round(float(cfg.MEMECOIN_ENTRY_SLIPPAGE_PCT) * 100))
    bases = _bases(chain, coin, quote_token, pair_tokens_fn, cfg)
    if not bases:
        return None, "no_pair_token_discovered"
    candidates = []
    from sniper.settings.memecoin import memecoin_evm_swap_deadline_s
    deadline = int((now_fn or _time.time)()) + int(memecoin_evm_swap_deadline_s(chain, deadline_s))
    reason = "no_v3_bridge_or_v2_leg"
    tax_bps = int(round(float(getattr(cfg, "MEMECOIN_EVM_ROUNDTRIP_TAX_ALLOWANCE_PCT", 4.0)) * 100))
    for base in bases:
        if safety_deadline is not None and time.monotonic() >= safety_deadline:
            break
        leg1 = v3_quote_fn(chain, quote_token, base, int(amount_in), taker, http=http,
                           slippage_bps=s_bps)
        path = _v3_route(leg1, quote_token, base)
        bridged, bridged_min = _amount(leg1), _amount(leg1, "minBuyAmount")
        if path is None or bridged <= 0 or bridged_min <= 0:
            continue
        # Discovery supplies a contract, not a tax certificate. Model the configured measurement
        # allowance on the bridge transfer; the atomic final minimum protects the actual fill.
        leg2 = _v2_direct(v2_quote_fn, chain, base, coin, bridged, taker, http=http,
                          slippage_bps=s_bps, tax_buffer_bps=tax_bps)
        coin_out = _amount(leg2)
        if coin_out <= 0:
            continue
        reason = "round_trip_unmeasured:mixed_v3_v2"
        if safety_deadline is not None and time.monotonic() >= safety_deadline:
            break
        # Each leg can revert at its own floor; the final floor gets one route-wide allowance.
        coin_min = coin_out * (_BPS - s_bps) // _BPS
        if coin_min <= 0:
            continue
        # The existing whole-balance simulation executes BOTH reverse legs, including actual
        # transfer taxes. Its uint256 return replaces two reverse quotes and two guessed tax
        # deductions. Mirror the verified entry pools; a different reverse route is not measured.
        exit_path = (list(reversed(path[0])), list(reversed(path[1])))
        proceeds = {}
        plan, reason = plan_mixed_entry(
            chain=chain, quote_token=quote_token, base_token=base, coin=coin,
            amount_in=int(amount_in), bridged_min=bridged_min, coin_min=coin_min,
            taker=taker, rpc_url=rpc_url, deadline=deadline, expected_coin_out=coin_out,
            http=http, enabled=True, safety_deadline=safety_deadline,
            exit_sim_fn=exit_sim_fn, fund_overrides_fn=fund_overrides_fn,
            v3_tokens=path[0], v3_fees=path[1],
            exit_v3_tokens=exit_path[0], exit_v3_fees=exit_path[1], exit_result_out=proceeds)
        if plan is not None:
            back = proceeds.get("amount_out_atomic")
            if not isinstance(back, int) or isinstance(back, bool) or back <= 0:
                reason = "round_trip_unmeasured:mixed_v3_v2"
                continue
            candidates.append({"base": base, "path": path, "coin_out": coin_out,
                               "coin_min": coin_min, "exit_path": exit_path, "back": back,
                               "plan": plan})
    if not candidates:
        return None, reason
    best = max(candidates, key=lambda c: c["back"])
    base, coin_out, coin_min = best["base"], best["coin_out"], best["coin_min"]
    exit_path, plan = best["exit_path"], best["plan"]
    # Same input budget, conservative forward output, actual whole-balance simulated sell-back.
    # The shared ladder's admission budget is unchanged.
    round_trip_pct = 100.0 * (float(best["back"]) / float(amount_in) - 1.0)
    return {
        "sellAmount": str(int(amount_in)),
        "buyAmount": str(coin_out),
        "minBuyAmount": str(coin_min),
        "liquidityAvailable": True,
        "transaction": {"to": router, "data": plan["data"], "value": "0x0", "gas": str(_route_gas())},
        "issues": {"allowance": {"spender": router}},
        "route": {"protocol": PROTOCOL, "router": router, "base_token": base,
                  "v3_tokens": best["path"][0], "v3_fees": best["path"][1],
                  "exit_v3_tokens": exit_path[0], "exit_v3_fees": exit_path[1],
                  "round_trip_pct": round_trip_pct, "exit_proof": plan["exit_reason"],
                  "round_trip_back_atomic": str(best["back"]),
                  "round_trip_measured_by": "whole_balance_exit_simulation"},
    }, None


def mixed_exit_quote(chain, coin, quote_token, amount_in, taker, *, route=None, http=None,
                     slippage_bps=None, v3_quote_fn=None, v2_quote_fn=None, pair_tokens_fn=None,
                     now_fn=None, deadline_s=300.0, cfg=None):
    """(quote, None) selling `amount_in` of `coin` through the SmartRouter, or (None, reason).

    The base token comes from the position's stored route when there is one, else from discovery.
    Never refused on cost: the alternative to an expensive exit is a bag we cannot sell at all.
    """
    import time as _time
    if cfg is None:
        from sniper.settings import memecoin as cfg
    if v3_quote_fn is None:
        from sniper.execution.evm_dex_quote import dex_v3_quote as v3_quote_fn
    if v2_quote_fn is None:
        from sniper.execution.evm_v2_exit_quote import dex_v2_exit_quote as v2_quote_fn
    if pair_tokens_fn is None:
        from sniper.execution.evm_v2_exit_quote import pair_quote_tokens as pair_tokens_fn
    router = smart_router_for(chain)
    if router is None:
        return None, "no_verified_smart_router"
    s_bps = int(slippage_bps if slippage_bps is not None
                else round(float(cfg.MEMECOIN_EXIT_SLIPPAGE_LADDER[-1]) * 100))
    stored = str((route or {}).get("base_token") or "").lower()
    bases = [stored] if stored else _bases(chain, coin, quote_token, pair_tokens_fn, cfg)
    best = None
    for base in bases:
        # tax_buffer_bps=None keeps the exit's max-tax floor on the coin leg's minimum.
        leg1 = _v2_direct(v2_quote_fn, chain, coin, base, amount_in, taker, http=http,
                          slippage_bps=s_bps, tax_buffer_bps=None)
        bridged, bridged_min = _amount(leg1), _amount(leg1, "minBuyAmount")
        if bridged <= 0 or bridged_min <= 0:
            continue
        leg2 = v3_quote_fn(chain, base, quote_token, bridged, taker, http=http, slippage_bps=s_bps)
        path = _v3_route(leg2, base, quote_token)
        out = _amount(leg2)
        if path is not None and out > 0 and (best is None or out > best["out"]):
            best = {"base": base, "path": path, "bridged": bridged, "bridged_min": bridged_min,
                    "out": out, "v2_quote": leg1}
    if best is None:
        return None, "no_mixed_exit_route"
    quote_min = best["out"] * (_BPS - s_bps) // _BPS
    from sniper.settings.memecoin import memecoin_evm_swap_deadline_s
    deadline = int((now_fn or _time.time)()) + int(memecoin_evm_swap_deadline_s(chain, deadline_s))
    try:
        data = build_exit_calldata(
            coin=coin, base_token=best["base"], quote_token=quote_token,
            v3_tokens=best["path"][0], v3_fees=best["path"][1], amount_in=int(amount_in),
            bridged_min=best["bridged_min"], quote_min=max(1, quote_min), recipient=taker,
            deadline=deadline, router=router)
    except (TypeError, ValueError) as exc:
        return None, "exit_calldata_unbuildable:%s" % str(exc)[:60]
    return {
        "sellAmount": str(int(amount_in)),
        "buyAmount": str(best["out"]),
        "minBuyAmount": str(max(1, quote_min)),
        "liquidityAvailable": True,
        "transaction": {"to": router, "data": data, "value": "0x0", "gas": str(_route_gas())},
        "issues": {"allowance": {"spender": router}},
        "route": {"protocol": PROTOCOL, "router": router, "base_token": best["base"],
                  "exit_v3_tokens": best["path"][0], "exit_v3_fees": best["path"][1]},
        # Preview-only valuation. Keep the actual first-leg builder's metadata, under a distinct
        # marker: top-level _fot_exit would make the V2 floor patcher treat this multicall as V2.
        "_mixed_exit": {"chain": str(chain), "coin": str(coin).lower(),
                        "quote_token": str(quote_token).lower(), "taker": str(taker).lower(),
                        "v2_quote": best["v2_quote"]},
    }, None


def exit_value_amount(quote, *, taker, http=None, value_tax_bps=None, v3_quote_fn=None):
    """Preview value through the selected mixed route, with the existing V2 tax allowance.

    The signed quote keeps its max-tax floors. A missing/broken binding or value read returns
    unknown, so the preview retains that conservative quote. No discovery or alternate path.
    """
    from sniper.execution.evm_v2_exit_quote import exit_value_amount as v2_exit_value
    from sniper.execution.evm_wallet import CHAIN_QUOTE
    from sniper.settings import memecoin as cfg

    def uint(value):
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError("not an atomic integer")
        value = int(value)
        if not 0 <= value < 2**256:
            raise ValueError("outside uint256")
        return value

    try:
        marker, route = quote["_mixed_exit"], quote["route"]
        chain = marker["chain"]
        coin, base, asset = marker["coin"], route["base_token"], marker["quote_token"]
        leg = marker["v2_quote"]
        fot = leg["_fot_exit"]
        config = cfg.memecoin_evm_v2_exit_config(chain) or {}
        v3_deployment = cfg.memecoin_evm_v3_addresses(chain)
        amount, cut = uint(quote["sellAmount"]), uint(fot["cut_bps"])
        fees = route["exit_v3_fees"]
        path = _v3_route({"route": {"protocol": "pancake_v3",
            "tokens": route["exit_v3_tokens"], "fees": fees}}, base, asset)
        if (chain != "56" or asset != CHAIN_QUOTE[chain]["token"].lower()
                or route["protocol"] != PROTOCOL or route["router"] != smart_router_for(chain)
                or quote["transaction"]["to"].lower() != smart_router_for(chain)
                or marker["taker"] != str(taker).lower()
                or amount <= 0 or uint(leg["sellAmount"]) != amount or not 0 < cut < _BPS
                or fot["chain"] != chain or fot["path"] != [coin, base]
                or fot["router"] != str(config.get("router") or "").lower()
                or (leg.get("route") or {}).get("protocol") != "uniswap_v2"
                or (leg.get("route") or {}).get("tokens") != [coin, base]
                or not path or any(not 0 < uint(fee) <= 1_000_000 for fee in fees)
                or not v3_deployment or v3_deployment.get("protocol") != "pancake_v3"):
            return None
        if value_tax_bps is None:
            allowance = cfg.MEMECOIN_EVM_EXIT_VALUE_TAX_PCT
            if isinstance(allowance, bool):
                return None
            value_tax_bps = round(float(allowance) * 100)
        value_tax_bps = uint(value_tax_bps)
        if value_tax_bps >= cut:
            return None                     # rollback: no additional reads
        bridged = v2_exit_value(leg, http=http, value_tax_bps=value_tax_bps)
        if bridged is None or uint(bridged) <= 0:
            return None
        if v3_quote_fn is None:
            from sniper.execution.evm_dex_quote import dex_v3_quote as v3_quote_fn
        result = v3_quote_fn(chain, base, asset, bridged, taker, http=http,
            slippage_bps=0, cfg_addr=v3_deployment, extra_paths=[path], paths_only=True)
        if (not isinstance(result, dict) or _v3_route(result, base, asset) != path
                or uint(result["sellAmount"]) != bridged):
            return None
        value = uint(result["buyAmount"])
        return value if value > 0 else None
    except Exception:
        # Optional valuation must not discard a usable exit on timeout, rejection or malformed RPC.
        return None

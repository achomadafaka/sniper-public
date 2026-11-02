"""The desk's native pump.fun curve rung (operator 2026-10-23: "Everything we have built that can be of
help in execution needs to be on the desk"; Alpha GO on option a).

WHY. A desk ENTER on a fresh pump.fun curve aborted "no quote (no_route: jupiter 400 NO_ROUTES_FOUND;
raydium ROUTE_NOT_FOUND)" (desk:U:aaf1335c819fc52ea8d08fd2, 2026-10-23 07:49:53Z): Jupiter does not
index a curve for minutes after launch, and the desk's ordinary entry had no third rung. The slot
lane has bought curves natively for weeks; this module gives the desk the same buy.

WHEN. Only a DESK entry (it carries desk_price_cap), only after Jupiter AND the Raydium fallback both
answered a typed `no_route`, never on the oversize retry. A rate limit, timeout or any other miss
keeps today's abort: unknown is not "no route".

WHAT. One getMultipleAccounts (memecoin_rabbit_signal.read_curve: bonding curve, mint, curve
FeeConfig) refuses a complete curve, a Mayhem or unmodelled-fee curve, a curve quoted in another
token and an unknown token program. Then the USDC -> SOL leg (pump_curve_buy.prefetch_usdc_sol_leg,
the slot lane's leg), a blockhash, and one transaction: compute budget, the leg, the coin ATA and
buy_exact_sol_in spending the leg's GUARANTEED minimum lamports. The signed token floor is the desk
verdict cap on the WHOLE USDC ticket (source_price_floor_atomic at ref x (1 + cap/100)), so the
desk's own cap holds on chain whatever the copy-premium knobs say; a floor above the curve's own
expected output refuses before signing. The caller signs it through slot_execute_fn, which applies
the unconditional entry fee clamp and the write-ahead identity, on the same reservation.
"""
from __future__ import annotations

import time

from sniper.execution import pump_curve_buy as P

REASON = "desk_native_curve"

# THE SLOT LANE'S WARM LEG (Alpha 2026-10-23, item 2). The slot lane's curve builder keeps USDC -> SOL
# legs warm on its prefetch thread, and refresh_builder_legs adds MEMECOIN_DESK_CLIP_USD to its sizes;
# the rung takes a matching leg (inside [90%, 100%] of the reservation, younger than the builder's
# max leg age) instead of two Jupiter calls on the miss path. Written ONCE when the lane starts, read
# by the entry thread; the builder publishes each leg as a new immutable tuple, so no lock is needed.
_SHARED = {"curve_builder": None}


def share_curve_builder(builder):
    _SHARED["curve_builder"] = builder


def warm_leg(usd_amount):
    """The slot lane's fresh leg for this clip, or None (no lane, no matching size, or stale)."""
    builder = _SHARED["curve_builder"]
    if builder is None:
        return None
    try:
        return P.matching_cash_leg(builder, usd_amount)
    except Exception:
        return None




def desk_no_route(quote, miss, desk_price_cap, max_accounts):
    """True when the rung may run: a desk entry, an empty quote, a typed no_route miss, first attempt."""
    return (not quote and desk_price_cap is not None and not max_accounts
            and isinstance(miss, dict) and miss.get("code") == "no_route")


MAYHEM_BYTE = 81          # BondingCurve.is_mayhem_mode (Lambda, chain-verified on 202 mints 2026-10-23)


def is_mayhem(curve_account):
    """True / False from the curve account as read, None when it cannot be told."""
    import base64
    try:
        raw = base64.b64decode(((curve_account or {}).get("data") or [None])[0] or "")
    except (ValueError, TypeError):
        return None
    return None if len(raw) <= MAYHEM_BYTE else raw[MAYHEM_BYTE] != 0


def build_desk_curve_message(state, leg, *, mint, user, fee_config, ref_price_usd, cap_pct, blockhash,
                             compute_unit_limit=None, priority_fee_microlamports=None, tip=None):
    """(unsigned MessageV0, amounts) for the leg + curve buy on `state` (a read_curve result). Raises
    ValueError when the curve cannot be bought at the desk cap or the transaction does not fit."""
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.null_signer import NullSigner
    from solders.transaction import VersionedTransaction
    if not isinstance(state, dict) or "reason" in state or state.get("complete") is not False:
        raise ValueError("curve state unusable")
    if not fee_config or len(fee_config) != 3:
        raise ValueError("no_protocol_fee_recipient")
    token_program = state["token_program"]
    if token_program not in (P.TOKEN_PROGRAM, P.TOKEN_2022_PROGRAM):
        raise ValueError("unknown token program %r" % token_program)
    spend = int(leg["min_out_lamports"])
    expected = P.expected_tokens_out(spend, state)
    floor = P.source_price_floor_atomic(leg["usdc_atomic"], ref_price_usd, cap_pct, P.PUMP_TOKEN_DECIMALS)
    if expected <= 0 or floor > expected:
        raise ValueError("desk cap refuses the curve price: floor %d over expected %d" % (floor, expected))
    from sniper.settings.memecoin import MEMECOIN_DESK_CAP_MIN_HEADROOM_PCT as headroom
    # Same no-headroom-no-send rule as the Jupiter rung (2026-10-23, T/ACC); the floor stays at the cap.
    if headroom > 0 and P.source_price_floor_atomic(
            leg["usdc_atomic"], ref_price_usd, cap_pct - headroom, P.PUMP_TOKEN_DECIMALS) > expected:
        raise ValueError("desk cap headroom under %.2f%%: expected %d" % (headroom, expected))
    user_pk, mint_pk, tp_pk = P._pk(user), P._pk(mint), P._pk(token_program)
    create_ata = Instruction(P._pk(P.ATA_PROGRAM), bytes([1]), [      # 1 = CreateIdempotent
        AccountMeta(user_pk, True, True), AccountMeta(P.ata(user_pk, mint_pk, tp_pk), False, True),
        AccountMeta(user_pk, False, False), AccountMeta(mint_pk, False, False),
        AccountMeta(P._pk(P.SYSTEM_PROGRAM), False, False), AccountMeta(tp_pk, False, False)])
    accounts = P.curve_buy_accounts(user=user_pk, mint=mint_pk, token_program=tp_pk, creator=state["creator"],
                                    fee_recipient=fee_config[0], buyback_fee_recipient=fee_config[2])
    buy = Instruction(P._pk(P.PUMP_PROGRAM), P.buy_exact_sol_in_data(spend, floor),
                      [AccountMeta(k, s, w) for k, s, w in accounts])
    cu = int(compute_unit_limit or P.DEFAULT_COMPUTE_UNIT_LIMIT)
    price = int(P.DEFAULT_PRIORITY_FEE_MICROLAMPORTS if priority_fee_microlamports is None
                else priority_fee_microlamports)
    ixs = [set_compute_unit_limit(cu), set_compute_unit_price(price), *leg["ixs"], create_ata, buy]
    if tip:                                   # (tip account, lamports): the desk's Sender tip, appended last
        from sniper.execution.solana_sender_ab import tip_instructions
        ixs += tip_instructions(user, tip)
    message = MessageV0.try_compile(user_pk, ixs, leg["tables"], Hash.from_string(blockhash))
    size = len(bytes(VersionedTransaction(message, [NullSigner(user_pk)])))
    if size > P.SOLANA_PACKET_LIMIT:
        raise ValueError("transaction is %d bytes, over the %d-byte packet" % (size, P.SOLANA_PACKET_LIMIT))
    return message, {"bytes": size, "spend_lamports": spend, "expected_tokens_out": expected,
                     "min_tokens_out": floor, "usdc_atomic": int(leg["usdc_atomic"]),
                     "leg_out_lamports": int(leg.get("out_lamports") or spend),
                     "read_slot": state.get("slot")}


def post_buy_sell_state(mint, state, curve_account, info, fee_config, creator_fee_configurable):
    """The facts native_curve_sell(state=..., simulate=False) needs, for the curve AFTER our own buy: virtual and
    real SOL + the net our buy_exact_sol_in puts in (curve_buy_net, exact on 19/20 landed buys), virtual and real
    tokens - the tokens it is expected to take. A sell priced on the PRE-buy state can need more SOL than the
    curve holds before we put ours in (Gamma: gross <= real_sol), so the probe must see the post-buy curve.
    Pure. Any unknown field is passed as None and native_curve_sell refuses it (fail closed: no native entry)."""
    import base64
    import struct
    from sniper.execution.solana_native_entry_quote import accounts_for
    try:
        raw = base64.b64decode((curve_account or {}).get("data", [""])[0] or "")
    except (TypeError, ValueError, IndexError):
        raw = b""
    net = P.curve_buy_net(info["spend_lamports"], state)
    tokens = int(info["expected_tokens_out"])
    config = fee_config if fee_config and len(fee_config) == 3 else (None, None, None)
    return {"mint": mint, "curve": accounts_for(mint)["curve"], "curve_layout_size": len(raw) or None,
            "virtual_sol_reserves": int(state["virtual_sol_reserves"]) + net,
            "real_sol_reserves": int(state["real_sol_reserves"]) + net,
            "virtual_token_reserves": int(state["virtual_token_reserves"]) - tokens,
            "real_token_reserves": int(state["real_token_reserves"]) - tokens,
            "creator": state.get("creator"), "token_program": state.get("token_program"),
            "complete": state.get("complete"), "mayhem_mode": state.get("mayhem_mode"),
            "is_cashback_coin": state.get("is_cashback_coin"), "is_holder_reward": state.get("is_holder_reward"),
            "quote_mint": state.get("quote_mint"),
            "creator_fee_bps": struct.unpack_from("<Q", raw, 115)[0] if len(raw) >= 123 else None,
            "creator_fee_configurable": creator_fee_configurable,
            "fee_basis_points": state.get("fee_basis_points"),
            "creator_fee_basis_points": state.get("creator_fee_basis_points"),
            "fee_recipient": config[0], "buyback_fee_recipient": config[2]}


def desk_curve_entry(mint, usd_amount, desk_price_cap, *, user, rpc, jup_call, fee_config_fn,
                     alt_cache=None, clock=time.perf_counter, tip=None):
    """(message, validity) or (None, reason). `rpc(method, params)` returns the JSON-RPC result;
    `fee_config_fn()` the Global (fee recipient, Mayhem recipient, buyback) or None. Every read and
    build step is timed into validity['native_curve_ms']."""
    from sniper.services.memecoin_rabbit_signal import read_curve
    t0 = clock()
    timing = {}
    from sniper.execution.solana_native_entry_quote import accounts_for
    raw = {}
    try:
        state = read_curve(mint, rpc, commitment="confirmed", raw_out=raw)
    except Exception as exc:                              # transport: the rung refuses, never guesses
        return None, "%s_read_failed: %s" % (REASON, type(exc).__name__)
    timing["curve_read_ms"] = (clock() - t0) * 1000
    if "reason" in state:
        return None, "%s_%s" % (REASON, state["reason"])
    # MAYHEM HELD (Alpha/Gamma 2026-10-23): pump_fees now prices a Mayhem curve's BUY, but a sell is paid
    # from real SOL, which the Mayhem agent's buys barely add to. Until the native sell refuses gross
    # proceeds above real_sol_reserves of the same state, the desk never enters one natively. Unknown refuses.
    # A holder-reward curve (BondingCurve V3 flag, Lambda 2026-10-23) has a fee flow no one here has
    # modelled; a cashback coin keeps today's 125 bps total, so only holder-reward refuses.
    if state.get("is_holder_reward") is True:
        return None, REASON + "_holder_reward_unmodelled"
    mayhem = is_mayhem(raw.get(accounts_for(mint)["curve"]))
    if mayhem is not False:
        return None, "%s: mayhem until sell feasibility" % REASON if mayhem else REASON + "_mayhem_unknown"
    fee_config = fee_config_fn()        # own 300 s reader; the shared builder's copy carries no age (Codex P2)
    if not fee_config:
        return None, REASON + "_no_fee_config"
    t1 = clock()
    leg = warm_leg(usd_amount)
    timing["leg_source"] = "warm" if leg is not None else "cold"
    if leg is None:
        leg = P.prefetch_usdc_sol_leg(P.clip_atomic(usd_amount), P.DEFAULT_LEG_SLIPPAGE_BPS, user=user,
                                      jup_call=jup_call, rpc=rpc, alt_cache=alt_cache)
    timing["leg_ms"] = (clock() - t1) * 1000
    if leg is None:
        return None, REASON + "_leg_unavailable"
    t2 = clock()
    try:
        latest = (rpc("getLatestBlockhash", [{"commitment": "confirmed"}]) or {}).get("value") or {}
    except Exception as exc:
        return None, "%s_blockhash_failed: %s" % (REASON, type(exc).__name__)
    timing["blockhash_ms"] = (clock() - t2) * 1000
    if not latest.get("blockhash"):
        return None, REASON + "_no_blockhash"
    try:
        message, info = build_desk_curve_message(
            state, leg, mint=mint, user=user, fee_config=fee_config,
            ref_price_usd=float(desk_price_cap["ref_price_usd"]), cap_pct=float(desk_price_cap["cap_pct"]),
            blockhash=latest["blockhash"], tip=tip)
    except (ValueError, KeyError, TypeError) as exc:
        return None, "%s_build: %s" % (REASON, str(exc)[:100])
    timing["total_ms"] = (clock() - t0) * 1000
    validity = {"blockhash": latest["blockhash"], "last_valid_height": latest.get("lastValidBlockHeight"),
                "sender_tip_lamports": int(tip[1]) if tip else 0,
                "target_mint": mint, "route_target": P.curve_route_target(mint, leg),
                "native_curve": info, "native_curve_ms": timing,
                # the curve as read + Global facts, for the post-buy native exit probe (never sent anywhere)
                "_sell_inputs": {"state": state, "curve_account": raw.get(accounts_for(mint)["curve"]),
                                 "fee_config": fee_config},
                "entry_slippage": {"venue": "pump_curve", "program": P.PUMP_PROGRAM, "mint": mint,
                                   "pool": P.curve_route_target(mint), "quote_mint": P.WSOL_MINT,
                                   "base_token_program": state["token_program"],
                                   "spend_atomic": info["spend_lamports"],
                                   "min_tokens_atomic": info["min_tokens_out"]}}
    return message, validity

"""Native pump.fun `sell_v2` exit for curves quoted in something Jupiter cannot build a sell for.

WHY (fries, position 55293, 2026-09-09). pump.fun now launches bonding curves quoted in tokenized
stocks (fries: McDonald's xStock, MCDx). Jupiter QUOTES the sell and then fails to BUILD it -- its
pump.fun adapter dies at an ATA create for the quote asset, `InstructionError[2, IllegalOwner]`,
under plain /swap, useSharedAccounts=false and a single hop alike. 23 of the last 25 transactions
on that curve were other wallets failing the same way; the two that sold drove the pump.fun
program directly with `sell_v2`. So does this module.

WHAT IT BUILDS. One atomic transaction:
    compute budget -> pump.fun sell_v2 (base -> quote asset, into the wallet's quote ATA)
                   -> Jupiter route (quote asset -> USDC, from /swap-instructions, with its ALTs)
Settling in USDC keeps every downstream invariant untouched: the exit intent binds
`otherAmountThreshold` as a 6-decimal USD floor, proceeds are read as the wallet's USDC delta, and
the tx is one signed identity. The tail is sized to the sell's MINIMUM quote-asset output, so a
better-than-minimum fill leaves quote-asset dust rather than an underfunded tail.

WHAT IT PROVES BEFORE ANYONE SIGNS. The assembled transaction is simulated on the funded RPC
(`sigVerify: false`) and any error returns None -- the executor then treats the rung as unquoted,
exactly as a Jupiter build failure does. A build that has not simulated clean never reaches
`sign_swap_tx`.

WHERE THE ACCOUNTS COME FROM. Every account is derived, none is copied from a template:
    global                      PDA ["global"]                          pump program
    bonding_curve               PDA ["bonding-curve", base_mint]        pump program
    creator                     bonding curve bytes [49:81]
    creator_vault               PDA ["creator-vault", creator]          pump program
    sharing_config              PDA ["sharing-config", base_mint]       FEE program (not pump: the
                                on-chain IDL derives it under pump and the program answers custom
                                error 2006 to that; brute-forced against a live sell on 09-09)
    fee_config                  PDA ["fee_config", FEE_CONFIG_SEED]     FEE program
    user_volume_accumulator     PDA ["user_volume_accumulator", user]   pump program
    event_authority             PDA ["__event_authority"]               pump program
    fee_recipient / buyback     global account bytes [41:73] / [933:965]
    quote_mint                  the bonding curve's token account that is not the base mint
    token programs              each mint's owner (classic or Token-2022)
    every associated account    ATA(owner, that mint's token program, mint)
Verified 2026-09-09 by simulating the full fries bag (5,707,019.249939 tokens): 145,702 CUs, three
Token-2022 TransferChecked, program success.
"""
from __future__ import annotations

import base64
import json
import logging
import struct

logger = logging.getLogger(__name__)

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
FEE_PROGRAM = "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SELL_V2_DISCRIMINATOR = bytes.fromhex("5df6823ce7e940b2")
# The second seed of the fee_config PDA, verbatim from pump.fun's on-chain IDL (sell_v2.fee_config).
FEE_CONFIG_SEED = bytes.fromhex("0156e0f693665acf44db1568bf175baa5189cb97f5d2ff3b655d2bb6fd6d18b0")
VENUE = "pump_native"
DEFAULT_PRIORITY_FEE_MICROLAMPORTS = 200_000
DEFAULT_COMPUTE_UNIT_LIMIT = 600_000


def curve_sell_quote(state, amount_atomic, slippage_pct, *, protocol_bps, creator_bps):
    """Pure normal-SOL curve arithmetic; reserves are one pre-sell state in lamports/atoms.

    Entry readiness must supply its post-buy state. Fees are independently resolved rates;
    mode/account/Global validation belongs to the builder, never inferred from this formula.
    Returns (quote, None) or (None, named refusal), without mutating state.
    """
    from decimal import Decimal

    if type(amount_atomic) is not int or not 0 < amount_atomic < 2**64:
        return None, "curve_amount_invalid"
    if type(slippage_pct) not in (int, float) or not 0 < slippage_pct < 100:
        return None, "curve_slippage_invalid"
    bps = int(Decimal(str(slippage_pct)) * 100)
    if not 0 < bps < 10_000:
        return None, "curve_slippage_invalid"
    if any(type(rate) is not int or not 0 <= rate < 10_000 for rate in (protocol_bps, creator_bps)):
        return None, "curve_fee_unmodelled"
    if not isinstance(state, dict):
        return None, "curve_state_unreadable"
    vs, vt, rs = (state.get(k) for k in (
        "virtual_sol_reserves", "virtual_token_reserves", "real_sol_reserves"))
    if (any(type(value) is not int or not 0 <= value < 2**64 for value in (vs, vt, rs))
            or vs == 0 or vt == 0):
        return None, "curve_state_unreadable"
    gross = vs * amount_atomic // (vt + amount_atomic)
    if gross > rs:
        return None, "curve_real_sol_short"
    protocol = (gross * protocol_bps + 9_999) // 10_000
    creator = (gross * creator_bps + 9_999) // 10_000
    net = gross - protocol - creator
    if net <= 0:
        return None, "curve_net_zero"
    minimum = net * (10_000 - bps) // 10_000
    if minimum <= 0:
        return None, "curve_min_zero"
    return {"gross_out": gross, "protocol_fee": protocol, "creator_fee": creator,
            "expected_out": net, "min_out": minimum, "slippage_bps": bps}, None


def _pk(s):
    from solders.pubkey import Pubkey
    return s if not isinstance(s, str) else Pubkey.from_string(s)


def _pda(seeds, program):
    from solders.pubkey import Pubkey
    return Pubkey.find_program_address(list(seeds), _pk(program))[0]


def ata(owner, mint, token_program):
    """Associated token account under the mint's OWN token program (classic or Token-2022)."""
    return _pda([bytes(_pk(owner)), bytes(_pk(token_program)), bytes(_pk(mint))], ATA_PROGRAM)


def sell_v2_data(amount_atomic, min_quote_out_atomic):
    amount, floor = int(amount_atomic), int(min_quote_out_atomic)
    if amount <= 0 or floor < 0:
        raise ValueError("sell_v2 needs a positive amount and a non-negative floor")
    return SELL_V2_DISCRIMINATOR + struct.pack("<QQ", amount, floor)


def sell_v2_accounts(*, user, base_mint, quote_mint, base_token_program, quote_token_program,
                     creator, fee_recipient, buyback_fee_recipient):
    """The 26 accounts of pump.fun `sell_v2`, in IDL order, as (pubkey, is_signer, is_writable)."""
    user, base, quote = _pk(user), _pk(base_mint), _pk(quote_mint)
    btp, qtp = _pk(base_token_program), _pk(quote_token_program)
    curve = _pda([b"bonding-curve", bytes(base)], PUMP_PROGRAM)
    creator_vault = _pda([b"creator-vault", bytes(_pk(creator))], PUMP_PROGRAM)
    uva = _pda([b"user_volume_accumulator", bytes(user)], PUMP_PROGRAM)
    fee_recipient, buyback = _pk(fee_recipient), _pk(buyback_fee_recipient)
    return [
        (_pda([b"global"], PUMP_PROGRAM), False, False),
        (base, False, False),
        (quote, False, False),
        (btp, False, False),
        (qtp, False, False),
        (_pk(ATA_PROGRAM), False, False),
        (fee_recipient, False, True),
        (ata(fee_recipient, quote, qtp), False, True),
        (buyback, False, True),
        (ata(buyback, quote, qtp), False, True),
        (curve, False, True),
        (ata(curve, base, btp), False, True),
        (ata(curve, quote, qtp), False, True),
        (user, True, True),
        (ata(user, base, btp), False, True),
        (ata(user, quote, qtp), False, True),
        (creator_vault, False, True),
        (ata(creator_vault, quote, qtp), False, True),
        (_pda([b"sharing-config", bytes(base)], FEE_PROGRAM), False, False),
        (uva, False, True),
        (ata(uva, quote, qtp), False, True),
        (_pda([b"fee_config", FEE_CONFIG_SEED], FEE_PROGRAM), False, False),
        (_pk(FEE_PROGRAM), False, False),
        (_pk(SYSTEM_PROGRAM), False, False),
        (_pda([b"__event_authority"], PUMP_PROGRAM), False, False),
        (_pk(PUMP_PROGRAM), False, False),
    ]


def sell_v2_instruction(amount_atomic, min_quote_out_atomic, **accounts):
    from solders.instruction import AccountMeta, Instruction
    metas = [AccountMeta(k, s, w) for k, s, w in sell_v2_accounts(**accounts)]
    return Instruction(_pk(PUMP_PROGRAM), sell_v2_data(amount_atomic, min_quote_out_atomic), metas)


def instruction_from_json(ix):
    """One Jupiter /swap-instructions entry -> solders Instruction."""
    from solders.instruction import AccountMeta, Instruction
    metas = [AccountMeta(_pk(a["pubkey"]), bool(a.get("isSigner")), bool(a.get("isWritable")))
             for a in ix.get("accounts") or ()]
    return Instruction(_pk(ix["programId"]), base64.b64decode(ix["data"]), metas)


def lookup_table_from_account(key, data_b64):
    from solders.address_lookup_table_account import AddressLookupTable, AddressLookupTableAccount
    table = AddressLookupTable.deserialize(base64.b64decode(data_b64))
    return AddressLookupTableAccount(_pk(key), list(table.addresses))


# --- discovery ---------------------------------------------------------------------------------

def _rpc_result(rpc, method, params):
    out = rpc(method, params)
    if isinstance(out, dict) and "result" in out:
        out = out["result"]
    return out


def discover_curve(base_mint, *, rpc):
    """Everything sell_v2 needs that lives on chain. `rpc(method, params)` returns the JSON-RPC
    result (or the whole envelope). Raises on any missing piece: a guess here would be an account
    list the program rejects, and the simulation would catch it, but naming the gap is cheaper."""
    base = _pk(base_mint)
    curve = _pda([b"bonding-curve", bytes(base)], PUMP_PROGRAM)
    # Finalized can miss a curve that the confirmed source receipt already bought.
    curve_acc = (_rpc_result(rpc, "getAccountInfo", [str(curve), {"encoding": "base64", "commitment": "confirmed"}]) or {}).get("value")
    if not curve_acc:
        raise RuntimeError("no bonding curve account for %s" % base_mint)
    craw = base64.b64decode(curve_acc["data"][0])
    if len(craw) < 81:
        raise RuntimeError("bonding curve account too short (%d bytes)" % len(craw))
    from solders.pubkey import Pubkey
    creator = Pubkey.from_bytes(craw[49:81])
    complete = bool(craw[48])
    global_acc = (_rpc_result(rpc, "getAccountInfo",
                              [str(_pda([b"global"], PUMP_PROGRAM)), {"encoding": "base64", "commitment": "confirmed"}]) or {}).get("value")
    graw = base64.b64decode(global_acc["data"][0]) if global_acc else b""
    if len(graw) < 965:
        raise RuntimeError("pump global account too short (%d bytes)" % len(graw))
    fee_recipient, buyback = Pubkey.from_bytes(graw[41:73]), Pubkey.from_bytes(graw[933:965])
    quote_mint = None
    for program in (TOKEN_2022_PROGRAM, TOKEN_PROGRAM):
        rows = (_rpc_result(rpc, "getTokenAccountsByOwner",
                            [str(curve), {"programId": program}, {"encoding": "jsonParsed", "commitment": "confirmed"}]) or {}).get("value") or []
        for row in rows:
            mint = ((row.get("account") or {}).get("data") or {}).get("parsed", {}).get("info", {}).get("mint")
            if mint and mint != str(base):
                quote_mint = mint
        if quote_mint:
            break
    if not quote_mint:
        raise RuntimeError("curve %s holds no quote-asset account" % str(curve))
    mints = (_rpc_result(rpc, "getMultipleAccounts", [[str(base), quote_mint], {"encoding": "base64", "commitment": "confirmed"}]) or {}).get("value") or []
    owners = [(m or {}).get("owner") for m in mints]
    if len(owners) != 2 or not all(owners):
        raise RuntimeError("could not read mint owners for %s / %s" % (base_mint, quote_mint))
    return {
        "curve": str(curve), "creator": str(creator), "complete": complete,
        "quote_mint": quote_mint, "base_token_program": owners[0], "quote_token_program": owners[1],
        "fee_recipient": str(fee_recipient), "buyback_fee_recipient": str(buyback),
    }


# --- the composite exit -----------------------------------------------------------------------

def _tail_shapes(tail_max_accounts):
    """The tail quotes to try, in order, until the assembled transaction fits a packet.

    ONLY (HNZR…pump, 2026-09-22, ref e4952cc0). The curve pays HIMS (a tokenized stock). Jupiter's
    best HIMS -> USDC tail plus the 26-account sell_v2 assembled at 1,392 bytes (1,301 on a rebuild
    minutes later) against the 1,232-byte packet, so the entry was refused as an unsellable trap.
    The same tail capped at 20 accounts assembled at 1,029 bytes; onlyDirectRoutes gave 1,029 too;
    a cap of 12 did not quote. The tail is sized to the sell's minimum output, so a capped route
    costs little price and the packet limit is a hard constraint. Jupiter's own best tail goes
    first, so a curve that already fits pays no extra call.
    """
    shapes = [""]
    cap = int(tail_max_accounts or 0)
    if cap > 0:
        shapes.append("&maxAccounts=%d" % cap)
    shapes.append("&onlyDirectRoutes=true")
    return shapes


def build_native_exit(mint, amount_atomic, slippage_pct, *, user, rpc, jup_call,
                      priority_fee_microlamports=None, compute_unit_limit=None, simulate=True,
                      packet_limit=None, tail_max_accounts=None):
    """A quote-shaped dict the exit executor can bind and sign, or None.

    `jup_call(method, path, body=None)` -> (status, json) is the seam's Jupiter caller.
    `rpc(method, params)` -> JSON-RPC result. Both are injected so tests never touch the network.
    The tail is retried in the shapes of `_tail_shapes` until the transaction fits `packet_limit`
    (default `SOLANA_PACKET_LIMIT`); when no shape fits, the smallest build is returned so the
    caller measures a real `too_big`, exactly as before.
    """
    amount = int(amount_atomic)
    if amount <= 0:
        return None
    slippage_bps = max(1, int(round(float(slippage_pct) * 100)))
    curve = discover_curve(mint, rpc=rpc)
    quote_mint = curve["quote_mint"]
    # 1. the curve leg: Jupiter prices it fine (only its BUILD is broken); its threshold is the
    #    signed floor for sell_v2 and the size of the tail.
    status, leg1 = jup_call("GET", "/quote?inputMint=%s&outputMint=%s&amount=%d&slippageBps=%d&onlyDirectRoutes=true"
                            % (mint, quote_mint, amount, slippage_bps))
    if status != 200 or not isinstance(leg1, dict) or not leg1.get("outAmount"):
        status, leg1 = jup_call("GET", "/quote?inputMint=%s&outputMint=%s&amount=%d&slippageBps=%d"
                                % (mint, quote_mint, amount, slippage_bps))
    if status != 200 or not isinstance(leg1, dict):
        logger.warning("pump native exit: no curve-leg quote for %s -> %s (%s)", mint, quote_mint, status)
        return None
    try:
        min_quote_out = int(leg1["otherAmountThreshold"])
        expected_quote_out = int(leg1["outAmount"])
    except (KeyError, TypeError, ValueError):
        return None
    if min_quote_out <= 0:
        return None
    sell_ix = sell_v2_instruction(
        amount, min_quote_out, user=user, base_mint=mint, quote_mint=quote_mint,
        base_token_program=curve["base_token_program"], quote_token_program=curve["quote_token_program"],
        creator=curve["creator"], fee_recipient=curve["fee_recipient"],
        buyback_fee_recipient=curve["buyback_fee_recipient"])
    return _composite_exit(
        mint, amount, slippage_bps, user=user, rpc=rpc, jup_call=jup_call, curve=curve,
        quote_mint=quote_mint, min_quote_out=min_quote_out, expected_quote_out=expected_quote_out,
        sell_ix=sell_ix, priority_fee_microlamports=priority_fee_microlamports,
        compute_unit_limit=compute_unit_limit, simulate=simulate, packet_limit=packet_limit,
        tail_max_accounts=tail_max_accounts, price_impact_pct=leg1.get("priceImpactPct"))


def _composite_exit(mint, amount, slippage_bps, *, user, rpc, jup_call, curve, quote_mint,
                    min_quote_out, expected_quote_out, sell_ix, priority_fee_microlamports,
                    compute_unit_limit, simulate, packet_limit, tail_max_accounts,
                    price_impact_pct=None, strict_sol=False):
    """Shared sell-before-tail assembly; SOL requires a fitting packet and a bound ExactIn tail."""
    from solders.hash import Hash
    from solders.message import MessageV0
    from solders.transaction import VersionedTransaction
    from solders.null_signer import NullSigner
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price

    if packet_limit is None:
        from sniper.execution.submission_wiring import SOLANA_PACKET_LIMIT
        packet_limit = SOLANA_PACKET_LIMIT
    if tail_max_accounts is None:
        from sniper.settings.memecoin import MEMECOIN_PUMP_NATIVE_EXIT_TAIL_MAX_ACCOUNTS
        tail_max_accounts = MEMECOIN_PUMP_NATIVE_EXIT_TAIL_MAX_ACCOUNTS
    head = [set_compute_unit_limit(int(compute_unit_limit or DEFAULT_COMPUTE_UNIT_LIMIT)),
            set_compute_unit_price(int(priority_fee_microlamports or DEFAULT_PRIORITY_FEE_MICROLAMPORTS)),
            sell_ix]
    bh = (_rpc_result(rpc, "getLatestBlockhash", [{"commitment": "confirmed"}]) or {}).get("value") or {}
    if not bh.get("blockhash"):
        return None
    from sniper.execution.entry_attribution import public_quote

    def _assemble(shape):
        # 2. the tail: quote asset -> USDC, sized to the MINIMUM the curve leg guarantees.
        status, tail = jup_call("GET", "/quote?inputMint=%s&outputMint=%s&amount=%d&slippageBps=%d%s"
                                % (quote_mint, USDC_MINT, min_quote_out, slippage_bps, shape))
        if strict_sol and (status in (401, 403, 429) or status >= 500):
            raise ValueError("curve_tail_provider_%s" % status)
        if status != 200 or not isinstance(tail, dict) or not tail.get("outAmount"):
            logger.warning("pump native exit: no tail quote %s -> USDC (%s) shape=%r", quote_mint, status, shape)
            return None
        if strict_sol:
            try:
                valid = (tail.get("inputMint") == quote_mint and tail.get("outputMint") == USDC_MINT
                         and tail.get("swapMode") == "ExactIn"
                         and _tail_atoms(tail["inAmount"]) == min_quote_out
                         and 0 < _tail_atoms(tail["otherAmountThreshold"]) <= _tail_atoms(tail["outAmount"]))
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                logger.warning("pump native SOL exit: curve_tail_invalid mint=%s", mint)
                return None
        status, built = jup_call("POST", "/swap-instructions", body=json.dumps({
            "quoteResponse": public_quote(tail), "userPublicKey": str(user),
            "wrapAndUnwrapSol": True, "dynamicComputeUnitLimit": False,
            "computeUnitPriceMicroLamports": int(priority_fee_microlamports or DEFAULT_PRIORITY_FEE_MICROLAMPORTS),
        }))
        if strict_sol and (status in (401, 403, 429) or status >= 500):
            raise ValueError("curve_tail_provider_%s" % status)
        if status != 200 or not isinstance(built, dict) or not built.get("swapInstruction"):
            logger.warning("pump native exit: tail /swap-instructions failed (%s) shape=%r", status, shape)
            return None
        # 3. assemble
        ixs = list(head)
        ixs += [instruction_from_json(i) for i in built.get("setupInstructions") or ()]
        ixs.append(instruction_from_json(built["swapInstruction"]))
        if built.get("cleanupInstruction"):
            ixs.append(instruction_from_json(built["cleanupInstruction"]))
        tables = []
        for key in built.get("addressLookupTableAddresses") or ():
            acc = (_rpc_result(rpc, "getAccountInfo", [key, {"encoding": "base64", "commitment": "confirmed"}]) or {}).get("value")
            if not acc:
                logger.warning("pump native exit: lookup table %s unreadable", key)
                return None
            tables.append(lookup_table_from_account(key, acc["data"][0]))
        message = MessageV0.try_compile(_pk(user), ixs, tables, Hash.from_string(bh["blockhash"]))
        unsigned = VersionedTransaction(message, [NullSigner(_pk(user))])
        raw = bytes(unsigned)
        return {"tail": tail, "tx_b64": base64.b64encode(raw).decode(), "bytes": len(raw),
                "shape": shape or "best"}

    best = None
    for shape in _tail_shapes(tail_max_accounts):
        candidate = _assemble(shape)
        if candidate is None:
            continue
        if best is None or candidate["bytes"] < best["bytes"]:
            best = candidate
        if candidate["bytes"] <= int(packet_limit):
            break
        logger.info("pump native exit: %s tail assembled %d bytes over the %d-byte packet for %s",
                    candidate["shape"], candidate["bytes"], packet_limit, mint)
    if best is None:
        return None
    if strict_sol and best["bytes"] > int(packet_limit):
        logger.warning("pump native SOL exit: curve_packet_oversize mint=%s bytes=%d", mint, best["bytes"])
        return None
    tail, tx_b64 = best["tail"], best["tx_b64"]
    # 4. prove it before anyone signs it
    if simulate:
        sim = _rpc_result(rpc, "simulateTransaction", [tx_b64, {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
            "commitment": "confirmed"}]) or {}
        value = sim.get("value") if isinstance(sim, dict) else None
        if (not isinstance(value, dict) or value.get("err") is not None
                or (strict_sol and "err" not in value)):
            logs = (value or {}).get("logs") or []
            logger.warning("pump native exit: simulation refused %s: err=%s logs=%s", mint,
                           (value or {}).get("err"), [l for l in logs if "failed" in l][-2:])
            if "'Custom': 6003" in str((value or {}).get("err")):
                # TooLittleSolReceived (73882, n=2): the curve paid under the floor we signed. Name the
                # floor (Jupiter's curve-leg threshold), its expected out, the rung and the program's
                # own lines, so the next one tells a fee Jupiter omits from a price that moved.
                logger.warning("pump native exit: 6003 %s floor=%d expected=%d slippage_bps=%d amount=%d "
                               "quote_mint=%s program_logs=%s", mint, min_quote_out, expected_quote_out,
                               slippage_bps, amount, quote_mint,
                               [str(l)[:160] for l in logs if str(l).startswith("Program log:")][-6:])
            return None
    route_plan = [{"swapInfo": {"label": "Pump.fun (native SOL sell)" if strict_sol else "Pump.fun (native sell_v2)", "ammKey": curve["curve"],
                                "inputMint": mint, "outputMint": quote_mint,
                                "inAmount": str(amount), "outAmount": str(expected_quote_out)}}]
    route_plan += list(tail.get("routePlan") or ())
    return {
        "_venue": VENUE,
        "inputMint": mint, "inAmount": str(amount),
        "outputMint": USDC_MINT, "outAmount": str(tail["outAmount"]),
        "otherAmountThreshold": str(tail["otherAmountThreshold"]),
        "slippageBps": slippage_bps, "priceImpactPct": price_impact_pct,
        "routePlan": route_plan,
        "_curve_leg": {**({"kind": "sol"} if strict_sol else {}),
                       "quote_mint": quote_mint, "expected_out": expected_quote_out,
                       "min_out": min_quote_out, "curve": curve["curve"]},
        "_native_swap": {**({"simulated": simulate} if strict_sol else {}),
                         "swapTransaction": tx_b64,
                         "lastValidBlockHeight": bh.get("lastValidBlockHeight"),
                         "bytes": best["bytes"], "tail_shape": best["shape"]},
    }


def _tail_atoms(value):
    if type(value) is int and 0 <= value < 2**64:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and 0 <= int(value) < 2**64:
        return int(value)
    raise ValueError("curve_tail_invalid")


def _sol_curve_snapshot(mint, rpc):
    """One confirmed snapshot binds reserves, mint owner, FeeConfig and the Global override gate."""
    from sniper.execution import pump_fees as PF
    from solders.pubkey import Pubkey

    curve = str(_pda([b"bonding-curve", bytes(_pk(mint))], PUMP_PROGRAM))
    keys = [curve, mint, str(_pda([b"fee_config", FEE_CONFIG_SEED], FEE_PROGRAM)),
            str(_pda([b"global"], PUMP_PROGRAM))]
    result = _rpc_result(rpc, "getMultipleAccounts", [keys, {"encoding": "base64", "commitment": "confirmed"}])
    values = result.get("value") if isinstance(result, dict) else None
    if not isinstance(values, list) or len(values) != 4:
        raise ValueError("curve_snapshot_unreadable")
    owners = (PUMP_PROGRAM, None, FEE_PROGRAM, PUMP_PROGRAM)
    decoded = []
    for acc, owner in zip(values, owners):
        if not isinstance(acc, dict) or (owner and acc.get("owner") != owner):
            raise ValueError("curve_account_owner")
        data = acc.get("data")
        if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
            raise ValueError("curve_account_encoding")
        decoded.append(base64.b64decode(data[0], validate=True))
    raw, mint_raw, _fee_raw, global_raw = decoded
    token_program = values[1].get("owner")
    if token_program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) or len(mint_raw) < 82 or mint_raw[45] != 1:
        raise ValueError("curve_mint_unreadable")
    if (len(raw) != 166 or raw[:8] != bytes.fromhex("17b7f83760d8ac60")
            or any(raw[i] not in (0, 1) for i in (48, 81, 82, 123, 124))):
        raise ValueError("curve_layout_unsupported")
    if (len(global_raw) < 1046 or global_raw[:8] != bytes.fromhex("a7e8e8b1c86c727f")
            or global_raw[8] != 1 or global_raw[1045] not in (0, 1)):
        raise ValueError("curve_global_unreadable")
    config = PF.parse_fee_config(values[2]["data"][0])
    if (len(config["tiers"]) > 1
            or (config["tiers"] and config["tiers"][0][0] != 0)):
        raise ValueError("curve_fee_unmodelled")
    fees = PF.curve_fee_bps(config, values[0]["data"][0])
    selected = config["tiers"][0][1] if config["tiers"] else config["flat"]
    if fees is None or selected[0] != 0:
        raise ValueError("curve_fee_unmodelled")
    vt, vs, _rt, rs = struct.unpack_from("<4Q", raw, 8)
    return {"mint": mint, "curve": curve, "virtual_sol_reserves": vs,
            "virtual_token_reserves": vt, "real_sol_reserves": rs,
            "creator": str(Pubkey.from_bytes(raw[49:81])), "token_program": token_program,
            "complete": bool(raw[48]), "mayhem_mode": bool(raw[81]),
            "is_cashback_coin": bool(raw[82]), "is_holder_reward": bool(raw[124]),
            "quote_mint": str(Pubkey.from_bytes(raw[83:115])), "curve_layout_size": len(raw),
            "creator_fee_bps": struct.unpack_from("<Q", raw, 115)[0],
            "creator_fee_configurable": bool(global_raw[1045]),
            "fee_basis_points": fees[0], "creator_fee_basis_points": fees[1],
            "fee_recipient": str(Pubkey.from_bytes(global_raw[41:73])),
            "buyback_fee_recipient": str(Pubkey.from_bytes(global_raw[933:965]))}


def native_curve_sell(mint, amount_atomic, slippage_pct, *, user, rpc, jup_call,
                      priority_fee_microlamports=None, compute_unit_limit=None, simulate=True,
                      packet_limit=None, tail_max_accounts=None, state=None):
    """Normal SOL curve -> USDC quote, or None with a named refusal in the log.

    Supplied state is an unsimulated ENTRY probe only; executable exits always read one complete
    account snapshot and simulate the whole transaction. No provider or state cache is owned here.
    """
    try:
        from sniper.settings.memecoin import MEMECOIN_EXIT_SLIPPAGE_LADDER
        if (type(simulate) is not bool or (state is not None and simulate)
                or type(slippage_pct) not in (int, float)
                or not MEMECOIN_EXIT_SLIPPAGE_LADDER
                or not MEMECOIN_EXIT_SLIPPAGE_LADDER[0] <= slippage_pct < 100):
            raise ValueError("curve_probe_or_slippage_invalid")
        if type(amount_atomic) is not int or not 0 < amount_atomic < 2**64:
            raise ValueError("curve_amount_invalid")
        facts = _sol_curve_snapshot(mint, rpc) if state is None else state
        if not isinstance(facts, dict):
            raise ValueError("curve_state_unreadable")
        if (facts.get("mint") != mint
                or facts.get("curve") != str(_pda([b"bonding-curve", bytes(_pk(mint))], PUMP_PROGRAM))
                or type(facts.get("curve_layout_size")) is not int
                or facts.get("curve_layout_size") != 166
                or facts.get("quote_mint") != SYSTEM_PROGRAM
                or any(facts.get(k) is not False for k in (
                    "complete", "mayhem_mode", "is_cashback_coin", "is_holder_reward"))
                or facts.get("token_program") not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM)):
            raise ValueError("curve_identity_or_mode_unsupported")
        override, configurable = facts.get("creator_fee_bps"), facts.get("creator_fee_configurable")
        if (type(override) is not int or not 0 <= override < 10_000
                or type(configurable) is not bool or (override and not configurable)):
            raise ValueError("curve_creator_override_unmodelled")
        protocol, creator = facts.get("fee_basis_points"), facts.get("creator_fee_basis_points")
        if any(type(f) is not int or not 0 <= f < 10_000 for f in (protocol, creator)):
            raise ValueError("curve_fee_unmodelled")
        for field in ("creator", "fee_recipient", "buyback_fee_recipient"):
            _pk(facts[field])
        creator = 0 if facts["creator"] == SYSTEM_PROGRAM else (override or creator)
        quote, reason = curve_sell_quote(facts, amount_atomic, slippage_pct,
                                         protocol_bps=protocol, creator_bps=creator)
        if reason:
            raise ValueError(reason)
        from solders.instruction import AccountMeta, Instruction
        base, owner, curve = _pk(mint), _pk(user), _pk(facts["curve"])
        token = _pk(facts["token_program"])
        metas = [
            (_pda([b"global"], PUMP_PROGRAM), False, False),
            (_pk(facts["fee_recipient"]), False, True), (base, False, False), (curve, False, True),
            (ata(curve, base, token), False, True), (ata(owner, base, token), False, True),
            (owner, True, True), (_pk(SYSTEM_PROGRAM), False, False),
            (_pda([b"creator-vault", bytes(_pk(facts["creator"]))], PUMP_PROGRAM), False, True),
            (token, False, False), (_pda([b"__event_authority"], PUMP_PROGRAM), False, False),
            (_pk(PUMP_PROGRAM), False, False),
            (_pda([b"fee_config", FEE_CONFIG_SEED], FEE_PROGRAM), False, False),
            (_pk(FEE_PROGRAM), False, False),
            (_pda([b"bonding-curve-v2", bytes(base)], PUMP_PROGRAM), False, False),
            (_pk(facts["buyback_fee_recipient"]), False, True)]
        sell_ix = Instruction(_pk(PUMP_PROGRAM), bytes.fromhex("33e685a4017f83ad")
                              + struct.pack("<QQ", amount_atomic, quote["min_out"]),
                              [AccountMeta(k, s, w) for k, s, w in metas])
        return _composite_exit(
            mint, amount_atomic, quote["slippage_bps"], user=user, rpc=rpc, jup_call=jup_call,
            curve=facts, quote_mint="So11111111111111111111111111111111111111112",
            min_quote_out=quote["min_out"], expected_quote_out=quote["expected_out"], sell_ix=sell_ix,
            priority_fee_microlamports=priority_fee_microlamports, compute_unit_limit=compute_unit_limit,
            simulate=simulate, packet_limit=packet_limit, tail_max_accounts=tail_max_accounts, strict_sol=True)
    except Exception as exc:
        logger.warning("pump native SOL exit refused mint=%s reason=%s", mint,
                       str(exc) if isinstance(exc, ValueError) else type(exc).__name__)
        return None

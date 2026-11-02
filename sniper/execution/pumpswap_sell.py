"""Direct PumpSwap (pAMM) SELL instruction, for the native exit rung (2026-10-17, Alpha; phase B).

LAYOUT (pinned from five real sells read on chain 2026-10-17, tests/fixtures/memecoin/
pumpswap_sell_instructions.json): the IDL's 21 accounts -- the buy's 23 without the two volume
accumulators -- then PDA ["pool-v2", base_mint] ONLY for a nonzero coin creator, then the buyback fee
recipient (a GlobalConfig.buyback_fee_recipients member) and its quote-mint ATA. 24 accounts on a
migrated pump pool (a real creator), 23 on a default-creator pool: the same rule the buy follows
(memecoin pumpswap_buy, tests/test_pumpswap_buy_layout.py).

DATA: discriminator sha256("global:sell")[:8] = 33e685a4017f83ad, base_amount_in u64,
min_quote_amount_out u64. The instruction helpers are pure; the exit builder below reads and
simulates through injected clients. Nothing here signs or sends."""
from __future__ import annotations

import struct

from sniper.execution.pump_native_exit import (
    ATA_PROGRAM, FEE_PROGRAM, SYSTEM_PROGRAM, _pda, _pk, ata,
)
from sniper.execution.pumpswap_buy import AMM_FEE_CONFIG_SEED, AMM_PROGRAM

SELL_DISCRIMINATOR = bytes.fromhex("33e685a4017f83ad")


def sell_data(base_amount_in, min_quote_amount_out):
    amount, floor = int(base_amount_in), int(min_quote_amount_out)
    if amount <= 0 or floor <= 0:
        raise ValueError("sell needs a positive amount and a positive floor")
    return SELL_DISCRIMINATOR + struct.pack("<QQ", amount, floor)


def pumpswap_sell_accounts(*, user, pool, pool_facts, protocol_fee_recipient,
                           protocol_fee_recipient_token_account, buyback_fee_recipient):
    """pAMM sell: IDL order, optional pool-v2, then the two buyback accounts. (pubkey, signer, writable)."""
    user, pool = _pk(user), _pk(pool)
    base, quote = _pk(pool_facts["base_mint"]), _pk(pool_facts["quote_mint"])
    btp, qtp = _pk(pool_facts["base_token_program"]), _pk(pool_facts["quote_token_program"])
    creator = _pk(pool_facts["coin_creator"])
    vault_auth = _pda([b"creator_vault", bytes(creator)], AMM_PROGRAM)
    accounts = [
        (pool, False, True),
        (user, True, True),
        (_pda([b"global_config"], AMM_PROGRAM), False, False),
        (base, False, False),
        (quote, False, False),
        (ata(user, base, btp), False, True),
        (ata(user, quote, qtp), False, True),
        (_pk(pool_facts["pool_base_token_account"]), False, True),
        (_pk(pool_facts["pool_quote_token_account"]), False, True),
        (_pk(protocol_fee_recipient), False, False),
        (_pk(protocol_fee_recipient_token_account), False, True),
        (btp, False, False),
        (qtp, False, False),
        (_pk(SYSTEM_PROGRAM), False, False),
        (_pk(ATA_PROGRAM), False, False),
        (_pda([b"__event_authority"], AMM_PROGRAM), False, False),
        (_pk(AMM_PROGRAM), False, False),
        (ata(vault_auth, quote, qtp), False, True),
        (vault_auth, False, False),
        (_pda([b"fee_config", AMM_FEE_CONFIG_SEED], FEE_PROGRAM), False, False),
        (_pk(FEE_PROGRAM), False, False),
    ]
    if str(creator) != SYSTEM_PROGRAM:
        accounts.append((_pda([b"pool-v2", bytes(base)], AMM_PROGRAM), False, False))
    return accounts + [
        (_pk(buyback_fee_recipient), False, False),
        (ata(buyback_fee_recipient, quote, qtp), False, True),
    ]


SELL_EVENT_DISCRIMINATOR = bytes.fromhex("3e2f370aa503dc2a")      # sha256("event:SellEvent")[:8]


def _ceil_bps(amount, bps):
    return -(-int(amount) * int(bps) // 10_000)


def expected_quote_out(base_in, base_reserve, quote_reserve, *, virtual_quote=0, lp_bps, protocol_bps,
                       creator_bps):
    """What the seller receives: gross = (quote + virtual) * in // (base + in), capped at the real
    quote reserve, less each fee rounded UP (lp, protocol, creator), as the program does. Pinned on
    five logged SellEvents: four default-creator pools (no virtual quote; two exact to the unit, two
    within 5e-7) and one migrated pump pool, where the virtual quote IS priced (within 5.4e-5 with the
    virtual quote read a few slots later; 9% low without it)."""
    base_in, base_reserve, quote_reserve = int(base_in), int(base_reserve), int(quote_reserve)
    if base_in <= 0 or base_reserve <= 0 or quote_reserve <= 0:
        return 0
    gross = min((quote_reserve + int(virtual_quote)) * base_in // (base_reserve + base_in), quote_reserve)
    return max(0, gross - _ceil_bps(gross, lp_bps) - _ceil_bps(gross, protocol_bps)
               - _ceil_bps(gross, creator_bps))


def sell_floor(base_in, base_reserve, quote_reserve, *, virtual_quote, slippage_pct, fee_bps):
    """min_quote_amount_out: the expected out at a CONSERVATIVE total fee (`fee_bps`, above the pool's
    tier) x (1 - slippage). A floor too high only reverts the sell, refused at simulation."""
    expected = expected_quote_out(base_in, base_reserve, quote_reserve, virtual_quote=virtual_quote,
                                  lp_bps=int(fee_bps), protocol_bps=0, creator_bps=0)
    return int(expected * (100.0 - float(slippage_pct)) / 100.0)


def parse_sell_event(data_b64):
    """The numeric head of a SellEvent ('Program data:' payload), or None when it is not one."""
    import base64
    raw = base64.b64decode(data_b64)
    if raw[:8] != SELL_EVENT_DISCRIMINATOR or len(raw) < 8 + 8 + 13 * 8 + 7 * 32 + 16:
        return None
    names = ("timestamp", "base_amount_in", "min_quote_amount_out", "user_base_token_reserves",
             "user_quote_token_reserves", "pool_base_token_reserves", "pool_quote_token_reserves",
             "quote_amount_out", "lp_fee_basis_points", "lp_fee", "protocol_fee_basis_points",
             "protocol_fee", "quote_amount_out_without_lp_fee", "user_quote_amount_out")
    out = dict(zip(names, struct.unpack_from("<q13Q", raw, 8)))
    out["coin_creator_fee_basis_points"], out["coin_creator_fee"] = struct.unpack_from(
        "<QQ", raw, 8 + 8 + 13 * 8 + 7 * 32)
    return out


# --- the native exit transaction -----------------------------------------------------------------

VENUE = "pumpswap_native"
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _quote_mint_supported(raw, program):
    """No quote-side debit/tax hooks: the tail may spend only this sell's guaranteed credit.

    Token-2022 extended mints pad Mint(82) to165, then Mint type1 and TLV at166.
    See spl-token-2022-interface extension::type_and_tlv_indices / TransferFeeConfig.
    Only metadata, mint-close authority and zero-effective-fee schedules are supported here.
    """
    from sniper.execution.pump_native_exit import TOKEN_PROGRAM, TOKEN_2022_PROGRAM
    if program == TOKEN_PROGRAM:
        return len(raw) == 82
    if program != TOKEN_2022_PROGRAM:
        return False
    if len(raw) == 82:
        return True  # an unextended Token-2022 mint has no account-type byte
    if len(raw) < 166 or len(raw) == 355 or any(raw[82:165]) or raw[165] != 1:
        return False
    offset, seen = 166, set()
    while offset < len(raw):
        if not any(raw[offset:]):
            return True  # unused zero padding, not an extension
        if offset + 4 > len(raw):
            return False
        kind, size = struct.unpack_from('<HH', raw, offset)
        offset += 4
        end = offset + size
        if kind in seen or end > len(raw):
            return False
        seen.add(kind)
        value = raw[offset:end]
        if kind == 1:  # TransferFeeConfig: authorities64 + withheld8 + two (epoch8,max8,bps2)
            if size != 108:
                return False
            for fee_offset in (72, 90):
                _epoch, maximum, bps = struct.unpack_from('<QQH', value, fee_offset)
                # Check BOTH schedules; guessing an epoch could admit a future fee.
                if bps > 10_000 or (bps and maximum):
                    return False
        elif not ((kind == 3 and size == 32) or (kind == 18 and size == 64)
                  or (kind == 19 and size >= 80)):
            return False  # only MintCloseAuthority, MetadataPointer and TokenMetadata
        offset = end
    return True


def read_pool_state(pool, *, rpc):
    """Read a PumpSwap pool, or resolve an entry-bound coin vault to its verified pool.

    The tape's AMM `pool` is a TOKEN VAULT. Its token authority names the AMM account; that
    account must be PumpSwap-owned and name this exact vault. Never use a mint-wide pool guess.
    All snapshots use confirmed commitment and cannot move backwards between reads.
    """
    import base64
    import hashlib
    from solders.pubkey import Pubkey
    from sniper.execution.pump_native_exit import TOKEN_PROGRAM, TOKEN_2022_PROGRAM
    from sniper.execution.pumpswap_buy import (
        buyback_fee_recipient_from_global_config, parse_pool_account,
        protocol_fee_recipient_from_global_config)
    programs = {TOKEN_PROGRAM, TOKEN_2022_PROGRAM}
    global_config = str(_pda([b"global_config"], AMM_PROGRAM))
    def raw(account):
        return base64.b64decode(account["data"][0], validate=True)
    def read(keys, minimum=0):
        result = rpc("getMultipleAccounts", [keys, {"encoding": "base64", "commitment": "confirmed",
                                                   **({"minContextSlot": minimum} if minimum else {})}])
        slot = (result.get("context") or {}).get("slot") if isinstance(result, dict) else None
        values = result.get("value") if isinstance(result, dict) else None
        if (type(slot) is not int or slot <= 0 or slot < minimum or not isinstance(values, list)
                or len(values) != len(keys) or not all(isinstance(a, dict) for a in values)):
            raise ValueError("incomplete pool snapshot")
        return values, slot
    try:
        values, slot = read([str(pool), global_config])
        bound_vault = None
        if values[0].get("owner") in programs:
            # SPL token account: mint [0:32], authority [32:64], amount [64:72].
            vault = raw(values[0])
            if len(vault) < 165 or vault[108] != 1:
                return None
            bound_vault, pool = str(pool), str(Pubkey.from_bytes(vault[32:64]))
            values, slot = read([pool, global_config], slot)
        if any(a.get("owner") != AMM_PROGRAM for a in values):
            return None
        global_raw = raw(values[1])
        if global_raw[:8] != hashlib.sha256(b"account:GlobalConfig").digest()[:8]:
            return None
        facts = parse_pool_account(values[0]["data"][0])
        if (facts["virtual_quote"] < 0 or facts["base_mint"] == facts["quote_mint"]
                or (bound_vault and facts["pool_base_token_account"] != bound_vault)):
            return None
        accounts, slot = read([facts["pool_base_token_account"], facts["pool_quote_token_account"],
                               facts["base_mint"], facts["quote_mint"]], slot)
        reserves = []
        for vault, mint, expected_mint in ((accounts[0], accounts[2], facts["base_mint"]),
                                            (accounts[1], accounts[3], facts["quote_mint"])):
            program = mint.get("owner")
            data, mint_data = raw(vault), raw(mint)
            if (program not in programs or vault.get("owner") != program or len(data) < 165
                    or len(mint_data) < 82 or mint_data[45] != 1 or data[108] != 1
                    or str(Pubkey.from_bytes(data[:32])) != expected_mint
                    or str(Pubkey.from_bytes(data[32:64])) != str(pool)):
                return None
            if ((expected_mint == WSOL_MINT and mint_data[44] != 9)
                    or (expected_mint == USDC_MINT and mint_data[44] != 6)):
                return None
            if expected_mint == facts["quote_mint"] and not _quote_mint_supported(mint_data, program):
                return None
            reserves.append(struct.unpack_from("<Q", data, 64)[0])
        if not all(reserves):
            return None
        facts = dict(facts, base_token_program=accounts[2]["owner"], quote_token_program=accounts[3]["owner"])
        return {"pool": str(pool), "facts": facts, "base_reserve": reserves[0], "quote_reserve": reserves[1],
                "slot": slot, "protocol_fee_recipient": protocol_fee_recipient_from_global_config(values[1]["data"][0]),
                "buyback_fee_recipient": buyback_fee_recipient_from_global_config(values[1]["data"][0])}
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return None


def build_pumpswap_native_exit(mint, amount_atomic, slippage_pct, *, user, pool, rpc, jup_call, fee_bps,
                               priority_fee_microlamports=None, compute_unit_limit=None, simulate=True,
                               packet_limit=None, tail_max_accounts=None):
    """Build one unsigned, simulated exact-input sell settling in USDC, or return None.

    For non-USDC quotes the Jupiter tail spends only the native sell's guaranteed minimum.
    This owns no claim, signature, send, confirmation, or accounting operation.
    """
    import base64
    import json
    import logging
    import math
    import operator
    from solders.compute_budget import set_compute_unit_limit, set_compute_unit_price
    from solders.hash import Hash
    from solders.instruction import AccountMeta, Instruction
    from solders.message import MessageV0
    from solders.null_signer import NullSigner
    from solders.transaction import VersionedTransaction
    from sniper.execution.entry_attribution import public_quote
    from sniper.execution.pump_native_exit import (
        DEFAULT_COMPUTE_UNIT_LIMIT, DEFAULT_PRIORITY_FEE_MICROLAMPORTS, _rpc_result, _tail_shapes,
        instruction_from_json, lookup_table_from_account)
    log = logging.getLogger(__name__)
    try:
        amount, slip, fee = operator.index(amount_atomic), float(slippage_pct), operator.index(fee_bps)
        if (type(amount_atomic) is bool or not 0 < amount < 2**64 or not math.isfinite(slip)
                or not 0 <= slip < 100 or type(fee_bps) is bool or not 0 <= fee < 10_000):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    read = lambda method, params: _rpc_result(rpc, method, params)
    state = read_pool_state(pool, rpc=read)
    if state is None or state["facts"]["base_mint"] != mint:
        return None
    pool, facts = state["pool"], state["facts"]
    quote_mint = facts["quote_mint"]
    slippage_bps = max(1, int(round(slip * 100)))
    expected = expected_quote_out(amount, state["base_reserve"], state["quote_reserve"],
                                  virtual_quote=facts["virtual_quote"], lp_bps=fee, protocol_bps=0,
                                  creator_bps=0)
    floor = expected * (10_000 - slippage_bps) // 10_000
    if floor <= 0:
        return None
    user_pk, quote, qtp = _pk(user), _pk(quote_mint), _pk(facts["quote_token_program"])
    quote_ata = ata(user_pk, quote, qtp)
    metas = [AccountMeta(k, s, w) for k, s, w in pumpswap_sell_accounts(
        user=user_pk, pool=pool, pool_facts=facts, protocol_fee_recipient=state["protocol_fee_recipient"],
        protocol_fee_recipient_token_account=ata(state["protocol_fee_recipient"], quote, qtp),
        buyback_fee_recipient=state["buyback_fee_recipient"])]
    priority = int(DEFAULT_PRIORITY_FEE_MICROLAMPORTS if priority_fee_microlamports is None
                   else priority_fee_microlamports)
    head = [set_compute_unit_limit(int(compute_unit_limit or DEFAULT_COMPUTE_UNIT_LIMIT)),
            set_compute_unit_price(priority),
            Instruction(_pk(ATA_PROGRAM), bytes([1]), [
                AccountMeta(user_pk, True, True), AccountMeta(quote_ata, False, True),
                AccountMeta(user_pk, False, False), AccountMeta(quote, False, False),
                AccountMeta(_pk(SYSTEM_PROGRAM), False, False), AccountMeta(qtp, False, False)]),
            Instruction(_pk(AMM_PROGRAM), sell_data(amount, floor), metas)]
    close = ([Instruction(qtp, bytes([9]), [AccountMeta(quote_ata, False, True),
              AccountMeta(user_pk, False, True), AccountMeta(user_pk, True, False)])]
             if quote_mint == WSOL_MINT else [])
    bh = (read("getLatestBlockhash", [{"commitment": "confirmed", "minContextSlot": state["slot"]}]) or {}).get("value") or {}
    if not bh.get("blockhash") or type(bh.get("lastValidBlockHeight")) is not int or bh["lastValidBlockHeight"] <= 0:
        return None
    if packet_limit is None:
        from sniper.execution.submission_wiring import SOLANA_PACKET_LIMIT
        packet_limit = SOLANA_PACKET_LIMIT
    if tail_max_accounts is None:
        from sniper.settings.memecoin import MEMECOIN_PUMP_NATIVE_EXIT_TAIL_MAX_ACCOUNTS
        tail_max_accounts = MEMECOIN_PUMP_NATIVE_EXIT_TAIL_MAX_ACCOUNTS
    def assemble(shape):
        tables, ixs = [], list(head)
        tail = {"outAmount": str(expected), "otherAmountThreshold": str(floor), "routePlan": []}
        if quote_mint != USDC_MINT:
            status, tail = jup_call("GET", "/quote?inputMint=%s&outputMint=%s&amount=%d&slippageBps=%d%s"
                                    % (quote_mint, USDC_MINT, floor, slippage_bps, shape))
            try:
                for field in ("inAmount", "outAmount", "otherAmountThreshold", "slippageBps"):
                    value = tail[field]
                    if (type(value) is not int and (not isinstance(value, str)
                            or not value.isascii() or not value.isdecimal())):
                        return None
                    if not 0 <= int(value) < 2**64:
                        return None
                if (status != 200 or tail["inputMint"] != quote_mint or tail["outputMint"] != USDC_MINT
                        or int(tail["inAmount"]) != floor or tail.get("swapMode") != "ExactIn"
                        or int(tail["slippageBps"]) != slippage_bps
                        or not 0 < int(tail["otherAmountThreshold"]) <= int(tail["outAmount"])):
                    return None
            except (KeyError, TypeError, ValueError, OverflowError):
                return None
            status, built = jup_call("POST", "/swap-instructions", body=json.dumps({
                "quoteResponse": public_quote(tail), "userPublicKey": str(user),
                "wrapAndUnwrapSol": False, "dynamicComputeUnitLimit": False,
                "computeUnitPriceMicroLamports": priority}))
            if status != 200 or not isinstance(built, dict) or not built.get("swapInstruction"):
                return None
            ixs += [instruction_from_json(i) for i in built.get("setupInstructions") or ()]
            ixs.append(instruction_from_json(built["swapInstruction"]))
            if built.get("cleanupInstruction"):
                ixs.append(instruction_from_json(built["cleanupInstruction"]))
            ixs += close
            for key in built.get("addressLookupTableAddresses") or ():
                acc = (read("getAccountInfo", [key, {"encoding": "base64", "commitment": "confirmed"}]) or {}).get("value")
                if not acc:
                    return None
                tables.append(lookup_table_from_account(key, acc["data"][0]))
        message = MessageV0.try_compile(user_pk, ixs, tables, Hash.from_string(bh["blockhash"]))
        raw = bytes(VersionedTransaction(message, [NullSigner(user_pk)]))
        return {"tail": tail, "tx_b64": base64.b64encode(raw).decode(), "bytes": len(raw), "shape": shape or "best"}
    best = None
    for shape in ([""] if quote_mint == USDC_MINT else _tail_shapes(tail_max_accounts)):
        candidate = assemble(shape)
        if candidate is not None and (best is None or candidate["bytes"] < best["bytes"]):
            best = candidate
        if candidate is not None and candidate["bytes"] <= int(packet_limit):
            break
    if best is None or best["bytes"] > int(packet_limit):
        return None
    if simulate:
        sim = read("simulateTransaction", [best["tx_b64"], {
            "encoding": "base64", "sigVerify": False, "replaceRecentBlockhash": True,
            "commitment": "confirmed", "minContextSlot": state["slot"]}])
        value = sim.get("value") if isinstance(sim, dict) else None
        if not isinstance(value, dict) or "err" not in value or value["err"] is not None:
            log.warning("pumpswap native exit simulation refused mint=%s pool=%s slot=%s err=%s",
                        mint, pool, state["slot"], (value or {}).get("err"))
            return None
    tail = best["tail"]
    return {
        "_venue": VENUE, "inputMint": mint, "inAmount": str(amount), "outputMint": USDC_MINT,
        "outAmount": str(tail["outAmount"]), "otherAmountThreshold": str(tail["otherAmountThreshold"]),
        "slippageBps": slippage_bps, "priceImpactPct": None,
        "routePlan": [{"swapInfo": {"label": "PumpSwap (native sell)", "ammKey": pool, "inputMint": mint,
                                    "outputMint": quote_mint, "inAmount": str(amount),
                                    "outAmount": str(expected)}}] + list(tail.get("routePlan") or ()),
        "_pool_leg": {"pool": pool, "quote_mint": quote_mint, "expected_out": expected, "min_out": floor,
                      "slot": state["slot"], "base_reserve": state["base_reserve"],
                      "quote_reserve": state["quote_reserve"], "virtual_quote": facts["virtual_quote"]},
        "_native_swap": {"swapTransaction": best["tx_b64"], "lastValidBlockHeight": bh["lastValidBlockHeight"],
                         "bytes": best["bytes"], "tail_shape": best["shape"]},
    }

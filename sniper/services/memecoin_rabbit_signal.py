"""RabbitStream as a slot-lane signal: a tracked owner's pump-curve buy, fired BEFORE it executes.

Operator 2026-10-15 ("yes" to both: a pre-execution owner-signed proof for this lane only, and the
source_not_landed exit live). Needs MEMECOIN_SLOT_LANE_RABBIT_SIGNAL (the decoder) AND
MEMECOIN_SLOT_LANE_RABBIT_BUY (the send); either at 0 = the lane exactly as before.

What a pre-execution frame can and cannot prove:
- the owner is a REQUIRED SIGNER by the message header (index < num_required_signatures); a frame
  whose signers include no tracked owner never fires;
- the signature: verified locally (ed25519_local) for a legacy or v0 message; a version-1 message
  (transactionConfig) cannot be re-serialized by solders 0.28, so it rests on the leader's sigverify
  (leader_sigverify_v1, Alpha 2026-10-15): a shred carries only entries the leader recorded;
- never that it executes or lands: source_not_landed is the backstop.

The source's buy is priced from ONE read at signal on the funded read role (never custody, never GT):
the bonding curve, the coin mint and the curve FeeConfig, then the source's own quoted output
(DFlow) or exact SOL spend (direct or the bound FLASHX curve route). That becomes a TradeEvent marked pre-execution everywhere it
lands (proof version, source_evidence, the attempt's delivery lane), encoded as the exact Program-data
line every existing gate and builder already parses. Anything unread, unmodelled (mayhem, a multi-tier
fee config, a quote-token curve, a completed curve) or ambiguous fails closed: no signal, and the
processed feed's path is untouched.

SOURCE_NOT_LANDED (operator 2026-10-15, live): a fire is a bet that the source's buy executes. Each
fire's source signature is watched (LandingWatch) until a processed feed shows it landed, failed
(meta.err: `source_failed`), or the processed head passes its frame slot by LANDING_SLOTS with no
sign of it and getSignatureStatuses answers null twice (`source_not_landed`). Either verdict
releases the fire's unsigned reservation before it signs and exits the open position it bought
(the source-sell mirror's thread, its exit lock and retries). A read error is UNKNOWN, never "not
landed": it is retried after 200 ms, then every second, and after UNKNOWN_AFTER_S the position is
left to its ordinary rails. A null counts only from a read node whose context slot has reached
S + LANDING_SLOTS; a node behind that (or silent about its slot) is a read error. A later successful buy of the same coin by the same owner (a retry
under a new signature) keeps the position.
READ GUARD (Gamma B2, 2026-10-16): a RabbitStream frame is a shred the leader already executed, and the
processed read lands ~28 ms later, so the read can already hold the source's buy; applying the buy to it
again would price the source high and loosen the chase reference and the hard copy cap (fail-open). The
SAME getMultipleAccounts reads the source's own coin account, bound to the frame:
- balance 0 or no account: the source has not executed on that node; the read is pre-trade (applied once);
- balance > 0 on a bound exact-spend buy: the read is the POST state only if un-applying the frame's
  exact net and that balance reproduces it to the lamport; the fill is then the source's own, never
  re-applied. Anything else refuses (`read_after_source_unproven`);
- a DFlow leg names no destination account (its IDL lists six fixed accounts; 7 of 10 landed DFlow
  curve buys credited no account the owner holds), so a DFlow curve read is refused
  (`holder_unbound_dflow`) until a destination can be bound to the frame.
A frame in which any other top-level instruction (an ATA create excepted) names the coin account is
refused (`holder_moved_in_frame`). Residual, named: the venue's own CPI cannot be seen pre-execution.
Every read is logged with its verdict, the balance, read_slot - frame_slot and the reserves, and joined
to the processed copy of the same signature (`read_matrix`): truly included but called pre-trade is the
miss, counted and paged in the log.
Not handled, accepted 2026-10-15: a source buy that lands on a minority fork and rolls back after
S+4 (processed, never confirmed); watch state across a restart (in memory only: a restart leaves
those positions to their ordinary rails).
"""
from __future__ import annotations

import asyncio
import base64
import collections
import logging
import struct
import threading
import time

logger = logging.getLogger(__name__)

PRE_EXECUTION_PROOF_VERSION = "solana-pre-execution-owner-signed-v1"
# The exits a landing verdict takes, by reason (memecoin_positions.exit_reason, the exit audit row).
SOURCE_FAILED = "source_failed"
SOURCE_NOT_LANDED = "source_not_landed"
EXIT_REASONS = (SOURCE_FAILED, SOURCE_NOT_LANDED)
PROVENANCE = "rabbit_pre_execution"
DELIVERY_LANE = "slot_lane_rabbit"
VALUATION_BASIS = "rabbit_pre_execution_quote_x_read_reserves"
SIGNATURE_BASES = ("ed25519_local", "leader_sigverify_v1")
CURVE_LAMPORTS_MIN_SLACK = 0          # account lamports must cover rent + real SOL reserves
ATA_PROGRAM = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
PRE_TRADE, POST_TRADE = "pre_trade", "post_trade"


def is_rabbit(buy):
    return getattr(buy, "proof_provenance", None) == PROVENANCE


# THE PRE-EXECUTION FEEDS (2026-10-17): RabbitStream shreds and Helius preconfirmations share this path,
# one buyer, one proof and one landing watch. Each has its own hot knob and its own breaker.
# (hot knob, (breaker N, breaker WINDOW_MIN, breaker PAUSE_MIN)): full settings names, every one defined
# in sniper/settings/memecoin.py (tests/test_memecoin_inert_settings_knobs.py reads them).
FEED_KINDS = {"rabbit": ("MEMECOIN_SLOT_LANE_RABBIT_BUY",
                         ("MEMECOIN_SLOT_LANE_RABBIT_BREAKER_N", "MEMECOIN_SLOT_LANE_RABBIT_BREAKER_WINDOW_MIN",
                          "MEMECOIN_SLOT_LANE_RABBIT_BREAKER_PAUSE_MIN")),
              "preconf": ("MEMECOIN_SLOT_LANE_PRECONF",
                          ("MEMECOIN_SLOT_LANE_PRECONF_BREAKER_N", "MEMECOIN_SLOT_LANE_PRECONF_BREAKER_WINDOW_MIN",
                           "MEMECOIN_SLOT_LANE_PRECONF_BREAKER_PAUSE_MIN"))}


def feed_kind(feed):
    """'preconf' for a preconf:* feed, else 'rabbit' (the feed every older caller means)."""
    return "preconf" if str(feed or "").startswith("preconf:") else "rabbit"


def owner_signature_basis(update, owner):
    """'ed25519_local' when the owner's own signature verifies over the legacy/v0 message,
    'leader_sigverify_v1' for a version-1 message, None otherwise (not a required signer, a bad
    signature, an unreadable message)."""
    from solders.pubkey import Pubkey
    from solders.signature import Signature
    try:
        info = update.transaction.transaction
        tx, msg = info.transaction, info.transaction.message
        n = msg.header.num_required_signatures
        keys = [bytes(k) for k in msg.account_keys]
        target = bytes(Pubkey.from_string(owner))
        index = next((i for i, key in enumerate(keys[:n]) if key == target), None)
        if index is None or index >= len(tx.signatures):
            return None
        if msg.versioned and msg.HasField("config"):
            return "leader_sigverify_v1"
        message = _message_bytes(msg)
        if message is None:
            return None
        ok = Signature.from_bytes(bytes(tx.signatures[index])).verify(Pubkey.from_bytes(target), message)
        return "ed25519_local" if ok else None
    except Exception:
        return None


def _message_bytes(msg):
    """The exact signed bytes of a legacy or v0 message rebuilt from the geyser fields, or None."""
    from solders.hash import Hash
    from solders.instruction import CompiledInstruction
    from solders.message import Message, MessageHeader, MessageV0, MessageAddressTableLookup, to_bytes_versioned
    from solders.pubkey import Pubkey
    header = MessageHeader(msg.header.num_required_signatures, msg.header.num_readonly_signed_accounts,
                           msg.header.num_readonly_unsigned_accounts)
    keys = [Pubkey.from_bytes(bytes(k)) for k in msg.account_keys]
    ixs = [CompiledInstruction(ix.program_id_index, bytes(ix.data), bytes(ix.accounts)) for ix in msg.instructions]
    blockhash = Hash.from_bytes(bytes(msg.recent_blockhash))
    if msg.versioned:
        lookups = [MessageAddressTableLookup(Pubkey.from_bytes(bytes(t.account_key)), bytes(t.writable_indexes),
                                             bytes(t.readonly_indexes)) for t in msg.address_table_lookups]
        return to_bytes_versioned(MessageV0(header, keys, blockhash, ixs, lookups))
    return bytes(Message.new_with_compiled_instructions(
        header.num_required_signatures, header.num_readonly_signed_accounts,
        header.num_readonly_unsigned_accounts, keys, blockhash, ixs))


def curve_mint_from_refs(refs):
    """The one reference whose bonding-curve PDA is also referenced (DFlow names no leg accounts), or
    None when there is not exactly one."""
    from sniper.execution.pump_native_exit import PUMP_PROGRAM, _pda, _pk
    refs = set(refs)
    found = []
    for ref in refs:
        try:
            if str(_pda([b"bonding-curve", bytes(_pk(ref))], PUMP_PROGRAM)) in refs:
                found.append(ref)
        except Exception:
            continue
    return found[0] if len(found) == 1 else None


def writable_keys(update):
    """The frame's writable account keys (base58): static keys by the message header, plus the
    lookup-table addresses loaded writable. None when the frame cannot be read."""
    import base58
    try:
        info = update.transaction.transaction
        msg, h = info.transaction.message, info.transaction.message.header
        n, static = h.num_required_signatures, list(msg.account_keys)
        out = {base58.b58encode(bytes(k)).decode() for i, k in enumerate(static)
               if (i < n - h.num_readonly_signed_accounts if i < n
                   else i < len(static) - h.num_readonly_unsigned_accounts)}
        if info.HasField("meta"):
            out.update(base58.b58encode(bytes(k)).decode() for k in info.meta.loaded_writable_addresses)
        return frozenset(out)
    except Exception:
        return None


def _ata(owner, mint, token_program):
    from sniper.execution.pump_native_exit import _pda, _pk
    return str(_pda([bytes(_pk(owner)), bytes(_pk(token_program)), bytes(_pk(mint))], ATA_PROGRAM))


def bind_holder(candidate, found, writable):
    """The source's writable coin account, bound to a direct buy or the known FLASHX curve route.
    A DFlow leg names none: refused. Any other top-level
    instruction naming that account, except an ATA create of it, refuses: it could move the coin out
    inside the same transaction, so a zero balance would no longer prove the buy unexecuted."""
    from sniper.execution import pump_curve_buy as curve
    mint, _amount, route = found
    if route == "flashx_curve_v3":
        from sniper.services.memecoin_shyft_prepare import flashx_curve_buy
        flash = flashx_curve_buy(candidate)
        if flash is None:
            return None, "holder_unbound"
        holder, own = flash["holder"], {flash["leg_index"], flash["setup_index"]}
    elif route == "direct":
        legs = [(i, accounts) for i, (program, accounts, data) in enumerate(candidate["instructions"])
                if program == curve.PUMP_PROGRAM and data[:8] == curve.BUY_EXACT_SOL_IN_DISCRIMINATOR]
        if len(legs) != 1 or len(legs[0][1]) < 7:
            return None, "holder_unbound"
        holder, own = legs[0][1][5], {legs[0][0]}
    else:
        return None, "holder_unbound_dflow"
    if writable is None or holder not in writable:
        return None, "holder_unbound"
    for i, (program, accounts, data) in enumerate(candidate["instructions"]):
        if i in own or holder not in accounts:
            continue
        if program == ATA_PROGRAM and data in (b"", b"\x00", b"\x01") and accounts[1:2] == (holder,):
            continue                                    # Create / CreateIdempotent of that very account
        return None, "holder_moved_in_frame"
    return holder, None


def source_buy(candidate, target):
    """(mint, quoted output or exact spend with an optional output floor, route), or None."""
    from sniper.execution import dflow_swap, pump_curve_buy as curve
    from sniper.services.memecoin_shyft_prepare import DFLOW_PROGRAM, flashx_curve_buy
    if not target or target.get("venue") != "pump_curve":
        return None
    flash = flashx_curve_buy(candidate)
    if flash is not None:
        return flash["mint"], {"spend": flash["spend"], "min_tokens_out": flash["min_tokens_out"]}, "flashx_curve_v3"
    hits = []
    for program, accounts, data in candidate["instructions"]:
        if program == DFLOW_PROGRAM:
            decoded = dflow_swap.decode_swap(data)
            out = (decoded or {}).get("quoted_out_amount")
            if dflow_swap.buy_leg(decoded) == "pump_curve" and type(out) is int and out > 0:
                hits.append(("dflow", out, accounts))
        elif program == curve.PUMP_PROGRAM and data[:8] == curve.BUY_EXACT_SOL_IN_DISCRIMINATOR and len(data) == 25:
            hits.append(("direct", int.from_bytes(data[8:16], "little"), accounts))
    if len(hits) != 1:
        return None
    route, amount, accounts = hits[0]
    if route == "direct":
        mint = target.get("mint")
        return (mint, {"spend": amount}, route) if mint and amount > 0 else None
    mint = curve_mint_from_refs(target.get("candidate_accounts") or accounts)
    return (mint, {"quoted_out": amount}, route) if mint else None


def read_curve(mint, rpc, *, commitment="processed", holder=None, owner=None, raw_out=None, allow_mayhem=False):
    """ONE getMultipleAccounts [bonding curve, mint, curve FeeConfig(, the source's coin account)] -> the
    curve's state as read, or {'reason': ...}. With `holder` (bound to the frame by bind_holder) the
    state carries `holder_atomic`: its balance, 0 for no account, checked to be `owner`'s account of
    this mint under the mint's own token program. `rpc(method, params)` returns the JSON-RPC result;
    a transport error raises. A Mayhem curve (byte 81) is refused unless `allow_mayhem`; the state then carries
    `mayhem_mode`, which a buy builder needs to pick the Mayhem fee recipient (the copy lane never opts in)."""
    from sniper.execution import pump_fees as PF
    from sniper.execution.pump_curve_buy import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
    from sniper.execution.pump_native_exit import PUMP_PROGRAM
    from sniper.execution.solana_native_entry_quote import accounts_for
    keys = accounts_for(mint)
    wanted = [keys["curve"], mint, keys["curve_fee_config"]] + ([holder] if holder else [])
    res = rpc("getMultipleAccounts", [wanted, {"encoding": "base64", "commitment": commitment}]) or {}
    values = list(res.get("value") or ())
    if len(values) != len(wanted):
        return {"reason": "read_short"}
    curve, mint_acc, config = values[:3]
    if raw_out is not None:                    # the accounts as read, for the pool tape's seed
        raw_out.update({keys["curve"]: curve, keys["curve_fee_config"]: config})
    if not (isinstance(curve, dict) and curve.get("owner") == PUMP_PROGRAM):
        return {"reason": "no_curve"}
    data = (curve.get("data") or [None])[0]
    raw = base64.b64decode(data or "")
    if len(raw) < 82:
        return {"reason": "curve_unreadable"}
    v_tok, v_sol, r_tok, r_sol, _supply = struct.unpack_from("<5Q", raw, 8)
    if raw[48] != 0:
        return {"reason": "curve_complete"}
    if r_sol <= 0 or int(curve.get("lamports") or 0) < r_sol + CURVE_LAMPORTS_MIN_SLACK:
        return {"reason": "curve_not_sol_quoted"}   # a quote-token curve keeps no SOL reserve
    mayhem = raw[81] != 0
    if mayhem and not allow_mayhem:
        return {"reason": "mayhem_or_fee_unmodelled"}
    try:
        fees = PF.curve_fee_bps(PF.parse_fee_config((config or {}).get("data", [None])[0]), data)
    except (TypeError, ValueError):
        return {"reason": "fee_config_unreadable"}
    if fees is None:
        return {"reason": "mayhem_or_fee_unmodelled"}
    program = (mint_acc or {}).get("owner")
    if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        return {"reason": "mint_unreadable"}
    from solders.pubkey import Pubkey
    v3_known = len(raw) >= 166 and raw[:8] == bytes.fromhex("17b7f83760d8ac60")
    state = {"slot": (res.get("context") or {}).get("slot"), "virtual_token_reserves": v_tok,
             "virtual_sol_reserves": v_sol, "real_token_reserves": r_tok, "real_sol_reserves": r_sol,
             "fee_basis_points": int(fees[0]), "creator_fee_basis_points": int(fees[1]),
             "creator": str(Pubkey.from_bytes(raw[49:81])), "token_program": program,
             # Official BondingCurve IDL 7616681e: require the full V3 prefix before trusting its modes.
             "complete": False, "mayhem_mode": mayhem,
             "is_cashback_coin": bool(raw[82]) if v3_known and raw[82] in (0, 1) else None,
             "is_holder_reward": bool(raw[124]) if v3_known and raw[124] in (0, 1) else None,
             "quote_mint": str(Pubkey.from_bytes(raw[83:115])) if v3_known else None}
    if holder:
        held = _holder_balance(values[3], mint=mint, owner=owner, program=program)
        if held is None:
            return {"reason": "holder_unreadable"}
        state["holder_atomic"] = held
    return state


def _holder_balance(account, *, mint, owner, program):
    """The coin account's balance (0 when it does not exist yet), or None when it is not `owner`'s
    account of `mint` under the mint's token program (SPL layout: mint 0..32, owner 32..64, amount)."""
    if account is None:
        return 0
    from solders.pubkey import Pubkey
    try:
        raw = base64.b64decode((account.get("data") or [None])[0] or "")
        if account.get("owner") != program or len(raw) < 72 or not owner:
            return None
        if raw[:32] != bytes(Pubkey.from_string(mint)) or raw[32:64] != bytes(Pubkey.from_string(owner)):
            return None
        return struct.unpack_from("<Q", raw, 64)[0]
    except Exception:
        return None


def read_verdict(state, amount):
    """(PRE_TRADE | POST_TRADE, fill) or (None, refusal) for a read that carries `holder_atomic`.
    Zero held: the source has not executed on that node, so the read is pre-trade and its buy is
    applied once. Held tokens on a direct spend: the read is the POST state only when un-applying the
    frame's exact net and those tokens reproduces it to the lamport; the fill is then the source's
    own and nothing is applied again. Anything else refuses."""
    held = state.get("holder_atomic")
    if type(held) is not int or held < 0:
        return None, "holder_unread"
    if held == 0:
        fill = source_fill(state, **amount)
        return (PRE_TRADE, fill) if fill is not None else (None, "fill_unmodelled")
    if "spend" not in amount:
        return None, "read_after_source_quoted"
    net = direct_net(amount["spend"], state)
    pre = dict(state, virtual_sol_reserves=int(state["virtual_sol_reserves"]) - net,
               virtual_token_reserves=int(state["virtual_token_reserves"]) + held,
               real_sol_reserves=int(state["real_sol_reserves"]) - net,
               real_token_reserves=int(state["real_token_reserves"]) + held)
    if min(pre["virtual_sol_reserves"], pre["real_sol_reserves"]) <= 0:
        return None, "read_after_source_unproven"
    fill = source_fill(pre, **amount)
    keys = ("virtual_sol_reserves", "virtual_token_reserves", "real_sol_reserves", "real_token_reserves")
    if fill is None or fill["token_amount"] != held or any(fill[k] != int(state[k]) for k in keys):
        return None, "read_after_source_unproven"
    return POST_TRADE, fill


def direct_net(spend, state):
    """The net SOL a buy_exact_sol_in of `spend` puts into the curve (pump's rule, the event's
    sol_amount): pump_curve_buy.curve_buy_net, the one source of that rule."""
    from sniper.execution.pump_curve_buy import curve_buy_net
    return curve_buy_net(spend, state)


def source_fill(state, *, quoted_out=None, spend=None, min_tokens_out=0):
    """The source's estimated net SOL in, tokens out and the curve's reserves after it, or None.
    DFlow: its quoted output, the net that buys exactly that much. Direct: its exact SOL spend, through
    pump's own net and pricing rule (below)."""
    v_sol, v_tok = int(state["virtual_sol_reserves"]), int(state["virtual_token_reserves"])
    r_tok, r_sol = int(state["real_token_reserves"]), int(state["real_sol_reserves"])
    if spend is not None:
        # pump's own rule, exact on 19 of 20 landed buy_exact_sol_in (2026-10-16; the 20th a capped
        # partial fill, refused below): curve_buy_net, priced one lamport under it.
        net = direct_net(spend, state)
        tokens = v_tok * (net - 1) // (v_sol + net - 1) if net > 1 else 0
    elif quoted_out is not None:
        tokens = int(quoted_out)
        if not 0 < tokens < v_tok:
            return None
        net = -(-tokens * v_sol // (v_tok - tokens)) + 1      # ceil, plus the program's one lamport
    else:
        return None
    if tokens <= 0 or net <= 0 or tokens >= r_tok or tokens < min_tokens_out:
        return None
    fee = -(-net * int(state["fee_basis_points"]) // 10_000)
    creator_fee = -(-net * int(state["creator_fee_basis_points"]) // 10_000)
    return {"sol_amount": net, "token_amount": tokens, "fee": fee, "creator_fee": creator_fee,
            "virtual_sol_reserves": v_sol + net, "virtual_token_reserves": v_tok - tokens,
            "real_sol_reserves": r_sol + net, "real_token_reserves": r_tok - tokens}


def synthesized_event(mint, owner, state, fill, *, timestamp):
    """A pump.fun TradeEvent for the source's buy as it is EXPECTED to execute, every field in the
    on-chain order parse_trade_event reads. fee_recipient is the system program: the lane's builder
    then resolves a Global recipient, as for any v3 source. ix_name names the estimate."""
    from sniper.execution.pump_curve_buy import SYSTEM_PROGRAM
    return {"mint": mint, "sol_amount": fill["sol_amount"], "token_amount": fill["token_amount"],
            "is_buy": True, "user": owner, "timestamp": int(timestamp),
            "virtual_sol_reserves": fill["virtual_sol_reserves"],
            "virtual_token_reserves": fill["virtual_token_reserves"],
            "real_sol_reserves": fill["real_sol_reserves"], "real_token_reserves": fill["real_token_reserves"],
            "fee_recipient": SYSTEM_PROGRAM, "fee_basis_points": state["fee_basis_points"], "fee": fill["fee"],
            "creator": state["creator"], "creator_fee_basis_points": state["creator_fee_basis_points"],
            "creator_fee": fill["creator_fee"], "track_volume": False, "total_unclaimed_tokens": 0,
            "total_claimed_tokens": 0, "current_sol_volume": 0, "last_update_timestamp": int(timestamp),
            "ix_name": PROVENANCE, "mayhem_mode": bool(state.get("mayhem_mode", False)), "cashback_fee_basis_points": 0, "cashback": 0,
            "buyback_fee_basis_points": 0, "buyback_fee": 0, "shareholders": [],
            "quote_mint": SYSTEM_PROGRAM, "quote_amount": 0, "virtual_quote_reserves": 0,
            "real_quote_reserves": 0, "holder_rewards_bps": 0, "holder_rewards": 0}


def encode_trade_event(e):
    """The `Program data:` line parse_trade_event reads back field for field."""
    from solders.pubkey import Pubkey
    from sniper.execution.pump_curve_buy import TRADE_EVENT_DISCRIMINATOR
    pk = lambda s: bytes(Pubkey.from_string(s))
    u64 = lambda v: struct.pack("<Q", int(v))
    i64 = lambda v: struct.pack("<q", int(v))
    b = lambda v: b"\x01" if v else b"\x00"
    name = e["ix_name"].encode()
    raw = (TRADE_EVENT_DISCRIMINATOR + pk(e["mint"]) + u64(e["sol_amount"]) + u64(e["token_amount"])
           + b(e["is_buy"]) + pk(e["user"]) + i64(e["timestamp"]) + u64(e["virtual_sol_reserves"])
           + u64(e["virtual_token_reserves"]) + u64(e["real_sol_reserves"]) + u64(e["real_token_reserves"])
           + pk(e["fee_recipient"]) + u64(e["fee_basis_points"]) + u64(e["fee"]) + pk(e["creator"])
           + u64(e["creator_fee_basis_points"]) + u64(e["creator_fee"]) + b(e["track_volume"])
           + u64(e["total_unclaimed_tokens"]) + u64(e["total_claimed_tokens"]) + u64(e["current_sol_volume"])
           + i64(e["last_update_timestamp"]) + struct.pack("<I", len(name)) + name + b(e["mayhem_mode"])
           + u64(e["cashback_fee_basis_points"]) + u64(e["cashback"]) + u64(e["buyback_fee_basis_points"])
           + u64(e["buyback_fee"]) + struct.pack("<I", len(e["shareholders"]))
           + b"".join(pk(a) + struct.pack("<H", s) for a, s in e["shareholders"])
           + pk(e["quote_mint"]) + u64(e["quote_amount"]) + u64(e["virtual_quote_reserves"])
           + u64(e["real_quote_reserves"]) + u64(e["holder_rewards_bps"]) + u64(e["holder_rewards"]))
    return "Program data: " + base64.b64encode(raw).decode()


def pre_execution_proof(candidate, mint, fill, *, source_user_id, signature_basis, route, read_slot):
    feed = candidate.get("feed")
    return dict(schema_version=1, proof_version=PRE_EXECUTION_PROOF_VERSION, network_id="solana",
                **({"feed": feed} if feed else {}),
                tx_hash=candidate["signature"], block_number=candidate["slot"], mint=mint,
                beneficiary=candidate["owner"], payer=candidate["owner"], source_user_id=source_user_id,
                acquired_atomic=str(fill["token_amount"]),
                payment_atomic=str(fill["sol_amount"] + fill["fee"] + fill["creator_fee"]),
                payment_basis="pre_execution_estimate", program_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
                signature_basis=signature_basis, route=route, read_slot=read_slot)


def rabbit_slot_buy(candidate, mint, state, fill, *, source_user_id, handle, signature_basis, route,
                    read_ms, recv_epoch, feed, head=None, head_mono=None, now_mono, read_basis=None):
    """The lane's SlotBuy for a rabbit-led fire: the synthesized event as its trade_event_log, the
    pre-execution proof as its source_initiation, and everything the attempt row records about the
    frame in `rabbit`. Never published as a receipt, never tapped into the sell mirror."""
    from datetime import datetime, timezone
    from sniper.services.memecoin_slot_signal import SlotBuy
    recv_utc = datetime.fromtimestamp(recv_epoch, tz=timezone.utc)
    event = synthesized_event(mint, candidate["owner"], state, fill, timestamp=int(recv_epoch))
    proof = pre_execution_proof(candidate, mint, fill, source_user_id=source_user_id,
                                signature_basis=signature_basis, route=route, read_slot=state.get("slot"))
    return SlotBuy(venue="pump_curve", source_user_id=source_user_id, handle=handle, owner=candidate["owner"],
                   signature=candidate["signature"], slot=candidate["slot"], mint=mint, pool=None,
                   sol_amount=fill["sol_amount"], token_amount=fill["token_amount"],
                   virtual_sol_reserves=fill["virtual_sol_reserves"],
                   virtual_token_reserves=fill["virtual_token_reserves"],
                   real_sol_reserves=fill["real_sol_reserves"], real_token_reserves=fill["real_token_reserves"],
                   token_program=state["token_program"], trade_event_log=encode_trade_event(event),
                   recv_monotonic=candidate["recv_monotonic"], recv_utc=recv_utc.isoformat(),
                   head_at_recv=head, head_at_recv_mono=head_mono, source_initiation=proof,
                   signal_feed=feed, proof_ready_monotonic=now_mono, proof_provenance=PROVENANCE,
                   decoded_monotonic=now_mono, source_authorized=True,
                   rabbit={"signature_basis": signature_basis, "route": route, "read_ms": read_ms,
                           "read_slot": state.get("slot"), "frame_slot": candidate["slot"],
                           "slot_status": "pre_execution", "valuation_basis": VALUATION_BASIS,
                           "generation": candidate.get("generation"), "read_basis": read_basis,
                           "holder_atomic": state.get("holder_atomic"),
                           "read_minus_frame_slot": _slot_delta(state.get("slot"), candidate["slot"])})


def _leg_token_program(candidate, mint, owner, holder):
    """The token program named by the frame's direct or known FLASHX curve leg, only
    when the owner's ATA of the mint under it is the bound holder; else None (no tape read)."""
    from sniper.execution import pump_curve_buy as curve
    from sniper.services.memecoin_shyft_prepare import flashx_curve_buy
    flash = flashx_curve_buy(candidate)
    if flash is not None:
        return (flash["token_program"] if (flash["mint"], flash["holder"]) == (mint, holder)
                and owner == candidate["owner"] else None)
    legs = [accounts for program, accounts, data in candidate["instructions"]
            if program == curve.PUMP_PROGRAM and data[:8] == curve.BUY_EXACT_SOL_IN_DISCRIMINATOR]
    try:
        tp = legs[0][8] if len(legs) == 1 and len(legs[0]) > 8 else None
        return tp if tp in (curve.TOKEN_PROGRAM, curve.TOKEN_2022_PROGRAM) and _ata(owner, mint, tp) == holder else None
    except Exception:
        return None


def _slot_delta(read_slot, frame_slot):
    return read_slot - frame_slot if type(read_slot) is int and type(frame_slot) is int else None


class CurveHandler:
    """The pump-curve route of the owner-frame path (today's rabbit fire, unchanged): a direct
    buy_exact_sol_in, the receipt-backed FLASHX curve buy, or a DFlow leg into the curve, its coin
    account bound to the frame, one curve read, the read guard's verdict and the synthesized TradeEvent.

    THE HANDLER SEAM (agreed with Beta, 2026-10-17; memecoin_rabbit_dflow_amm.DflowAmmHandler is the
    second): `route`; match(candidate, update) -> None (not mine) | (None, reason) (mine, refused) |
    (plan, None); read(plan) -> state, on a worker thread, raising on a short or failed read;
    verdict(plan, state, candidate) -> (fill, None) | (None, reason); build(candidate, plan, state,
    fill, *, source_user_id, handle, signature_basis, feed, read_ms, recv_epoch, head, head_mono,
    now_mono) -> the lane's buy. A handler may return a third element from match, the route its
    refusal counts under (this one does: direct | dflow), and may take bind(buyer)."""

    route = "pump_curve"
    prior_holder_refusal = "read_after_source_unproven"     # the RPC path's label for a held coin

    def bind(self, buyer):
        self.buyer = buyer

    def match(self, candidate, update):
        from sniper.services.memecoin_shyft_prepare import preparation_target
        found = source_buy(candidate, preparation_target(candidate))
        if found is None:
            return None
        holder, why = bind_holder(candidate, found, writable_keys(update))
        if holder is None:
            return None, why, found[2]
        return {"found": found, "holder": holder, "owner": candidate["owner"], "route": found[2],
                "token_program": _leg_token_program(candidate, found[0], candidate["owner"], holder)}, None, found[2]

    def read(self, plan):
        raw = {}
        state = read_curve(plan["found"][0], self.buyer.read, holder=plan["holder"], owner=plan["owner"],
                           raw_out=raw)
        if getattr(self.buyer, "tape", None) is not None:   # only a tape seeds from it (Gamma LOW-2)
            state["_raw"] = raw
        return state

    def tape_read(self, plan, tape, frame_slot, lane_head):
        """The curve at the end of the frame slot's parent, from the pool tape: (state, None) or
        (None, reason). The mint's token program is the frame's own buy leg's (bound to the holder
        ATA in match), so no mint read. holder_atomic is 0 by construction: the state is from a slot
        before the source's, which cannot hold its buy (the read guard's pre-trade case)."""
        from sniper.execution.solana_native_entry_quote import accounts_for
        if not plan.get("token_program"):
            return None, "token_program_unbound"
        keys = accounts_for(plan["found"][0])
        accounts, parent = tape.lookup([keys["curve"], keys["curve_fee_config"]], frame_slot, lane_head=lane_head)
        if accounts is None:
            return None, parent
        values = [accounts[keys["curve"]], {"owner": plan["token_program"]}, accounts[keys["curve_fee_config"]], None]
        state = read_curve(plan["found"][0], lambda method, params: {"context": {"slot": parent}, "value": values},
                           holder=plan["holder"], owner=plan["owner"])
        if "reason" in state:
            return None, "tape_" + state["reason"]
        state["holder_atomic"] = 0
        return state, None

    def tape_keys(self, plan, state=None):
        from sniper.execution.solana_native_entry_quote import accounts_for
        keys = accounts_for(plan["found"][0])
        return (keys["curve"], keys["curve_fee_config"])

    def verdict(self, plan, state, candidate):
        route = plan["route"]
        if "reason" in state:
            self.buyer._route(route, state["reason"])
            return None, state["reason"]
        # FRESHNESS (Gamma LOW-1, Alpha 2026-10-17; the AMM handler's rule): a read more than
        # MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS slots before the frame, or with no context slot,
        # misses the net sells of S-k..S-1: the modelled post price sits above the real one and the
        # cap loosens. Refused before the read guard's held == 0 pre-ness check.
        from sniper.settings import memecoin as cfg
        read_slot, frame_slot = state.get("slot"), candidate.get("slot")
        if (type(read_slot) is not int or type(frame_slot) is not int
                or frame_slot - read_slot > int(cfg.MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS)):
            self.buyer._route(route, "read_stale")
            return None, "read_stale"
        if route == "flashx_curve_v3":
            from sniper.execution.pump_curve_buy import SOL_QUOTE_MINTS
            if (state.get("complete") is not False or state.get("is_cashback_coin") is not False
                    or state.get("is_holder_reward") is not False or state.get("quote_mint") not in SOL_QUOTE_MINTS):
                self.buyer._route(route, "curve_v3_mode_unmodelled")
                return None, "curve_v3_mode_unmodelled"
        verdict, fill = read_verdict(state, plan["found"][1])
        self.buyer._audit(candidate, route, state, verdict or "refused:" + fill)
        if verdict is None:
            return None, fill
        plan["read_basis"] = verdict
        return fill, None

    def build(self, candidate, plan, state, fill, **ctx):
        return rabbit_slot_buy(candidate, plan["found"][0], state, fill, route=plan["route"],
                               read_basis=plan["read_basis"], **ctx)


_UNSET = object()


class RabbitBuyer:
    """The pre-execution fire, owned by the slot-signal event loop (MEMECOIN_SLOT_LANE_RABBIT_SIGNAL and
    MEMECOIN_SLOT_LANE_RABBIT_BUY; MEMECOIN_SLOT_LANE_PRECONF for preconf frames). `offer(candidate,
    update)` runs on the loop and does only memory work (dedupe, the owner's signature, the route
    decode); the one read runs on a worker thread, so the loop keeps reading frames. FIRST ARRIVAL
    WINS: a signature the processed feeds already claimed in `seen` never fires, here or after the read,
    and ONE buyer serves every pre-execution feed, so a signature fires once whichever feed saw it
    first. It never claims `seen`: the processed buy still reaches the lane (which drops it when this
    fire owns the coin), the sell mirror and the receipt journal exactly as before. `handlers` are
    the routes (CurveHandler first); the first whose match answers owns the frame."""

    KEEP_WARM_S = 20.0      # inside pump_curve_buy.POOL_READ_KEEPALIVE_S: a read pays no handshake
    MAX_IN_FLIGHT = 8
    MINE_MAX = 4096

    def __init__(self, *, seen, owners_fn, on_buy, backstop, feed, status, landing, head_fn=lambda: (None, None),
                 clock=time, epoch_fn=None, handlers=None):
        if epoch_fn is None:
            from sniper.util.clock import real_utc_now as epoch_fn
        self.seen, self.owners_fn, self.on_buy, self.feed = seen, owners_fn, on_buy, feed
        self.backstop, self.read, self.landing = backstop, backstop.read, landing
        self.head_fn, self.clock, self.epoch_fn = head_fn, clock, epoch_fn
        self.stats = status
        status.update(candidates=0, held=0, duplicate=0, unsigned=0, unsupported=0, busy=0, read_errors=0,
                      processed_first=0, roster_refused=0, fired=0, fire_errors=0, ping_errors=0,
                      refused={}, read_ms_p50=None, read_ms_p90=None, last_error=None,
                      read_basis={}, read_slot_delta={}, read_matrix={}, read_miss=0, by_route={},
                      by_feed={})
        self.handlers = list(handlers) if handlers is not None else [CurveHandler()]
        for handler in self.handlers:
            if hasattr(handler, "bind"):
                handler.bind(self)
        self._mine = collections.OrderedDict()
        self._tasks = set()
        self._read_ms = collections.deque(maxlen=256)
        self._route_ms = {}
        self._last_read = None
        self._reads = collections.OrderedDict()       # signature -> this read's verdict and reserves
        self._processed = collections.OrderedDict()   # signature -> the processed copy's reserves
        self.tape = None                              # memecoin_rabbit_tape.PoolTape, or None
        self.frames = None                            # FrameLog: each frame's verdict, memory only
        self.frame_logs = {}                          # feed -> its own FrameLog (preconf)

    def _v(self, candidate, verdict, route=None):
        frames = self.frame_logs.get(candidate.get("feed"), self.frames)
        if frames is not None:
            frames.note(candidate["signature"], candidate["owner"], verdict, route=route)

    def _read_note(self, candidate, **read):
        frames = self.frame_logs.get(candidate.get("feed"), self.frames)
        if frames is not None:
            frames.note(candidate["signature"], candidate["owner"], read=read)

    def _count(self, candidate, key):
        by = self.stats["by_feed"].setdefault(candidate.get("feed") or self.feed, {})
        by[key] = by.get(key, 0) + 1

    def offer(self, candidate, update, *, basis=_UNSET):
        """`basis`: the feed's own verified owner-signature basis (preconf verifies the raw bytes),
        None when it did not verify; unset = computed from the frame (rabbit)."""
        candidate.setdefault("feed", self.feed)
        feed = candidate["feed"]
        key = (candidate["signature"], candidate["owner"])
        repeat = key in self._mine                # a repeat frame of an offered signature: counted, never
        if not self.backstop.allowed(feed):       # re-noted (its first offer's verdict stands, Gamma LOW-1)
            self.stats["held"] += 1               # the hot knob off, or the breaker open
            return None if repeat else self._v(candidate, "held")
        if repeat or key in self.seen:
            self.stats["duplicate"] += 1
            return None if repeat else self._v(candidate, "processed_first")
        self._mine[key] = None
        while len(self._mine) > self.MINE_MAX:
            self._mine.popitem(last=False)
        self.stats["candidates"] += 1
        self._count(candidate, "candidates")
        if self.landing.failed(candidate["signature"]):
            self._v(candidate, SOURCE_FAILED)
            return self._refused(SOURCE_FAILED)
        if basis is _UNSET:
            basis = owner_signature_basis(update, candidate["owner"])
        if basis is None:
            self.stats["unsigned"] += 1
            return self._v(candidate, "unsigned")
        for handler in self.handlers:
            answer = handler.match(candidate, update)
            if answer is not None:
                break
        else:
            self.stats["unsupported"] += 1
            return self._v(candidate, "unsupported")
        plan, why = answer[0], answer[1]
        route = (answer[2] if len(answer) > 2 else None) or (plan or {}).get("route") or handler.route
        if plan is None:
            self._route(route, why)
            self._v(candidate, why, route)
            return self._refused(why)
        if len(self._tasks) >= self.MAX_IN_FLIGHT:
            self.stats["busy"] += 1
            return self._v(candidate, "busy", route)
        self._v(candidate, "reading", route)
        task = asyncio.get_running_loop().create_task(self._fire(candidate, handler, plan, route, basis))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _refused(self, reason):
        self.stats["refused"][reason] = self.stats["refused"].get(reason, 0) + 1

    def _route(self, route, key):
        counts = self.stats["by_route"].setdefault(route, {})
        counts[key] = counts.get(key, 0) + 1

    @staticmethod
    def _p(values):
        ordered = sorted(values)
        return (round(ordered[(len(ordered) - 1) // 2], 1), round(ordered[-(-9 * len(ordered) // 10) - 1], 1))

    def _audit(self, candidate, route, state, verdict):
        """Every read, fired or not: its verdict, the coin balance, read_slot - frame_slot and the
        reserves, logged and kept until the processed copy of the same signature joins it."""
        delta = _slot_delta(state.get("slot"), candidate["slot"])
        for table, key in ((self.stats["read_basis"], verdict), (self.stats["read_slot_delta"], str(delta))):
            table[key] = table.get(key, 0) + 1
        self._route(route, verdict)
        rec = dict(route=route, verdict=verdict, held=state.get("holder_atomic"), delta=delta,
                   reserves=(state.get("virtual_sol_reserves"), state.get("virtual_token_reserves")))
        # Its line is the frame line (FrameLog, WARNING): a logger.info here never reached the runner log.
        self._read_note(candidate, read_basis=verdict)
        self._reads[candidate["signature"]] = rec
        while len(self._reads) > self.MINE_MAX:
            self._reads.popitem(last=False)
        self._join(candidate["signature"])

    def observe_processed(self, buy):
        """The processed copy of a curve buy (its event's POST-trade reserves), joined to this
        signature's read when there is one; a PumpSwap buy goes to the handlers that join their own
        fires (DflowAmmHandler.observe_processed). Memory only, after the processed buy was handed on."""
        if getattr(buy, "venue", None) == "pump_amm" and getattr(buy, "signature", None):
            for handler in self.handlers:
                join = getattr(handler, "observe_processed", None)
                if join is not None:
                    try:
                        join(buy.signature, buy.trade_event_log)
                    except Exception:
                        logger.exception("[memecoin] slot lane %s join failed sig=%s",
                                         getattr(handler, "route", "?"), buy.signature)
            return
        if getattr(buy, "venue", None) != "pump_curve" or not getattr(buy, "signature", None):
            return
        try:
            post = (int(buy.virtual_sol_reserves), int(buy.virtual_token_reserves))
            pre = (post[0] - int(buy.sol_amount), post[1] + int(buy.token_amount))
        except (TypeError, ValueError):
            return
        self._processed[buy.signature] = (pre, post)
        while len(self._processed) > self.MINE_MAX:
            self._processed.popitem(last=False)
        self._join(buy.signature)

    def _join(self, signature):
        rec, seen = self._reads.get(signature), self._processed.get(signature)
        if rec is None or seen is None:
            return
        self._reads.pop(signature, None)
        self._processed.pop(signature, None)
        pre, post = seen
        truth = ("included" if rec["reserves"] == post else "excluded" if rec["reserves"] == pre else "other")
        key = truth + "|" + rec["verdict"]
        self.stats["read_matrix"][key] = self.stats["read_matrix"].get(key, 0) + 1
        if truth == "included" and rec["verdict"] == PRE_TRADE:
            self.stats["read_miss"] += 1
            logger.warning("[memecoin] slot lane rabbit read MISS sig=%s route=%s: the read held the source's "
                           "buy but was called pre-trade (held=%s)", signature, rec["route"], rec["held"])

    def _tape_state(self, candidate, handler, plan):
        """(state, state_source): the pool tape's state when it has one, else (None, "rpc" or
        "tape_miss:<reason>"). Memory only, on the loop (MEMECOIN_SLOT_LANE_RABBIT_TAPE, read here)."""
        from sniper.services.memecoin_rabbit_tape import knob_on
        tape = self.tape
        if tape is None or not hasattr(handler, "tape_read") or not knob_on():
            return None, "rpc", None
        generation = tape.generation
        try:
            state, why = handler.tape_read(plan, tape, candidate.get("slot"), self.head_fn()[0])
        except Exception as exc:
            logger.exception("[memecoin] slot lane rabbit tape read failed sig=%s", candidate.get("signature"))
            state, why = None, "error:" + type(exc).__name__
        if state is not None:
            # ADMISSION AS ON THE RPC PATH (Alpha, 2026-10-17): an RPC read of an owner who already held
            # the coin read held > 0 and refused; the tape's state is pre by construction, so it reads
            # 0. An owner our memory shows buying this coin before refuses with the RPC path's label.
            # Residual: holdings from before our watch (or past its bound) are not in that memory.
            mint = plan.get("mint") or (plan.get("found") or (None,))[0]
            landing = getattr(self, "landing", None)
            if mint and landing is not None and hasattr(landing, "has_bought") and landing.has_bought(
                    candidate.get("owner"), mint, other_than=candidate.get("signature")):
                return None, "tape_prior_holder:" + getattr(handler, "prior_holder_refusal", "read_after_source_unproven"), generation
            return state, "tape", generation
        return None, "tape_miss:" + str(why), generation

    def _seed(self, handler, plan, state, generation):
        """An RPC fallback read's accounts into the tape (only the keys its tape read uses)."""
        if self.tape is None or generation is None or not hasattr(handler, "tape_keys"):
            return
        raw = state.get("_raw") or state.get("accounts") or {}
        try:
            for key in handler.tape_keys(plan, state):
                if key in raw:
                    self.tape.seed(key, raw[key], state.get("slot"), generation)
        except Exception:
            logger.exception("[memecoin] slot lane rabbit tape seed failed")

    async def _fire(self, candidate, handler, plan, route, basis):
        feed = candidate["feed"]
        started = self._last_read = self.clock.monotonic()
        state, source, generation = self._tape_state(candidate, handler, plan)
        if source.startswith("tape_prior_holder:"):
            why = source.split(":", 1)[1]
            self._read_note(candidate, read_ms=(self.clock.monotonic() - started) * 1000.0, state_source="tape")
            self._route(route, "state:tape")
            self._route(route, why)
            self._v(candidate, why)
            return self._refused(why)
        if state is None:
            try:
                state = await asyncio.to_thread(handler.read, plan)
            except Exception as exc:              # transport, HTTP or JSON-RPC error: no signal
                self._read_note(candidate, read_ms=(self.clock.monotonic() - started) * 1000.0, state_source=source)
                self.stats["read_errors"] += 1
                self.stats["last_error"] = "read:" + type(exc).__name__
                return self._v(candidate, "read_error")
            # Seeded after this fire's own decision (call_soon runs once _fire yields), never before it.
            asyncio.get_running_loop().call_soon(self._seed, handler, plan, state, generation)
        if self.tape is not None:                 # knob off or no tape: by_route exactly as before
            self._route(route, "state:" + source.split(":")[0])
            if source.startswith("tape_miss:"):
                self._route(route, source)
        read_ms = (self.clock.monotonic() - started) * 1000.0
        self._read_note(candidate, read_ms=read_ms, read_minus_frame=_slot_delta(state.get("slot"), candidate["slot"]),
                        state_source=source, held=state.get("holder_atomic"))
        self._read_ms.append(read_ms)
        self.stats["read_ms_p50"], self.stats["read_ms_p90"] = self._p(self._read_ms)
        route_ms = self._route_ms.setdefault(route, collections.deque(maxlen=256))
        route_ms.append(read_ms)
        p50, p90 = self._p(route_ms)
        self.stats["by_route"].setdefault(route, {}).update(read_ms_p50=p50, read_ms_p90=p90)
        fill, why = handler.verdict(plan, state, candidate)
        if fill is None:
            self._v(candidate, why)
            return self._refused(why)
        if (candidate["signature"], candidate["owner"]) in self.seen:
            self.stats["processed_first"] += 1
            return self._v(candidate, "processed_first_after_read")
        if self.landing.failed(candidate["signature"]):
            self._v(candidate, SOURCE_FAILED)
            return self._refused(SOURCE_FAILED)
        if not self.backstop.allowed(feed):       # switched off during the read
            self.stats["held"] += 1
            return self._v(candidate, "held")
        ident = dict(self.owners_fn()).get(candidate["owner"])
        if not ident:
            self.stats["roster_refused"] += 1
            return self._v(candidate, "roster_refused")
        now = self.clock.monotonic()
        head, head_mono = self.head_fn()
        try:
            buy = handler.build(candidate, plan, state, fill, source_user_id=ident[0],
                                handle=ident[1], signature_basis=basis, read_ms=round(read_ms, 1),
                                recv_epoch=self.epoch_fn() - (now - candidate["recv_monotonic"]),
                                feed=feed, head=head, head_mono=head_mono, now_mono=now)
            proof = getattr(buy, "source_initiation", None)
            if isinstance(proof, dict) and feed and proof.get("feed") != feed:
                # THE FEED NAMES ITS KNOB AT THE PROOF GATE (memecoin_source_initiation): every handler's
                # proof carries it, so a preconf-fed fire needs MEMECOIN_SLOT_LANE_PRECONF, whatever route.
                proof["feed"] = feed
            self.landing.expect(signature=candidate["signature"], owner=candidate["owner"],
                                source_user_id=ident[0], mint=buy.mint, slot=candidate["slot"],
                                frame_mono=candidate["recv_monotonic"], feed=feed)
            self.stats["fired"] += 1
            self._count(candidate, "fired")
            self._v(candidate, "fired")
            self.on_buy(buy)
        except Exception as exc:
            self.stats["fire_errors"] += 1
            self._v(candidate, "fire_error")
            self.stats["last_error"] = "fire:" + type(exc).__name__
            logger.exception("[memecoin] slot lane rabbit fire failed sig=%s feed=%s", candidate["signature"], feed)

    async def keep_warm(self, stop):
        """getHealth on the read connection after KEEP_WARM_S idle, one at a time; never a refusal."""
        from sniper.settings import memecoin as cfg
        while not stop.is_set():
            last = self._last_read
            if (cfg.MEMECOIN_SLOT_LANE_RABBIT_BUY or getattr(cfg, "MEMECOIN_SLOT_LANE_PRECONF", False)) and (
                    last is None or self.clock.monotonic() - last >= self.KEEP_WARM_S):
                self._last_read = self.clock.monotonic()
                try:
                    await asyncio.to_thread(self.read.ping)
                except Exception:
                    self.stats["ping_errors"] += 1
            await asyncio.sleep(1.0)


class FrameLog:
    """ONE LINE PER RABBIT OWNER-SIGNED FRAME (Lambda 2026-10-16, Alpha item 2b): its verdict (the prep
    miss reason or the buyer's outcome, "pending" when neither answered), venue, route and lead over the
    first processed feed, GRACE_S after its receipt. lead_ms = processed - rabbit on the one process
    clock: positive means rabbit first, "none" means no processed copy of the signature within GRACE_S
    (unseen, failed or not landed: unproved which). The event loop only notes in memory; the line is
    written on a worker thread (asyncio.to_thread), never on the loop. The rabbit read's own
    logger.info line never reached the runner log (nothing handles below WARNING): this one is
    logger.warning."""

    GRACE_S = 3.0
    TICK_S = 0.5
    MAX = 4096
    EVIDENCE_MAX = 16
    EVIDENCE_BYTES = 16 * 1024

    def __init__(self, *, feed, arrivals_fn, log=None, clock=time, status=None, label="rabbit", exclude=(),
                 peer=None, source=None):
        """`label` names the line (rabbit | preconf); `exclude`: other pre-execution feeds, never a
        processed copy; `peer`: one of them whose lead over this frame the line also prints
        (peer_lead_ms = peer - this; positive = this frame first); `source`: a preconf
        subscription's includeSource (helius | bam), printed as source= (2026-10-17)."""
        self.feed, self.arrivals_fn, self.clock, self.source = feed, arrivals_fn, clock, source
        self.label, self.exclude, self.peer = label, frozenset(exclude) | ({peer} if peer else set()), peer
        self.log = log or logger.warning
        self.stats = status if status is not None else {}
        self.stats.update(lines=0, dropped=0, write_errors=0, verdicts={})
        self.stats['evidence'] = dict.fromkeys(('retained', 'captured', 'duplicate', 'dropped_capacity',
            'dropped_oversize', 'invalid', 'written', 'encoded_oversize', 'encode_errors'), 0)
        self._recs = collections.OrderedDict()      # (signature, owner) -> rec, receipt order

    def frame(self, signature, owner, slot, mono):
        key = (signature, owner)
        if key in self._recs:
            return
        self._recs[key] = {"slot": slot, "mono": mono, "verdict": "pending", "venue": None, "route": None,
                           "read": {}}
        if len(self._recs) > self.MAX:
            _, old = self._recs.popitem(last=False)
            if 'rejected_evidence' in old:
                self.stats['evidence']['retained'] -= 1
            self.stats["dropped"] += 1

    def rejected(self, candidate):
        """Keep normalized unsupported-candidate diagnostics; no proto, serialization or I/O."""
        stats, rec = self.stats['evidence'], None
        try:
            rec = self._recs.get((candidate['signature'], candidate['owner']))
            if rec is None:
                return False
            if 'evidence_state' in rec:
                stats['duplicate'] += 1
                return False
            if stats['retained'] >= self.EVIDENCE_MAX:
                rec['evidence_state'] = 'capacity'
                stats['dropped_capacity'] += 1
                return False
            keys, instructions = candidate['account_keys'], candidate['instructions']
            if not 0 < len(keys) <= 256 or not 0 < len(instructions) <= 64:
                raise ValueError('candidate bounds')
            keys, signers = tuple(keys), tuple(candidate['signers'])
            writable, version = tuple(candidate['writable']), candidate['version']
            if (len(writable) != len(keys) or len(set(keys)) != len(keys)
                    or not all(type(key) is str and key.isascii() for key in keys)
                    or not all(type(flag) is bool for flag in writable)
                    or not set(signers).issubset(keys) or version not in ('legacy', 'v0', 'v1')):
                raise ValueError('candidate shape')
            size = sum(map(len, keys)) + sum(map(len, signers)) + len(writable) + len(version)
            for program, accounts, data in instructions:
                if type(data) is not bytes or len(data) > 1232:
                    raise ValueError('instruction data')
                size += len(program) + len(data)
                for account in accounts:
                    size += len(account)
                    if size > self.EVIDENCE_BYTES:
                        break
                if size > self.EVIDENCE_BYTES:
                    break
            if size > self.EVIDENCE_BYTES:
                rec['evidence_state'] = 'oversize'
                stats['dropped_oversize'] += 1
                return False
            rec['rejected_evidence'] = (keys, tuple((p, tuple(a), d) for p, a, d in instructions),
                                        signers, writable, version)
            rec['evidence_state'] = 'captured'
            stats['retained'] += 1
            stats['captured'] += 1
            return True
        except Exception:
            stats['invalid'] += 1
            if rec is not None:
                rec['evidence_state'] = 'invalid'
            return False

    def note(self, signature, owner, verdict=None, *, venue=None, route=None, read=None):
        """`read`: the buyer's read fields (read_ms, read_minus_frame, state_source, read_basis, held),
        merged; memory only, on the loop."""
        rec = self._recs.get((signature, owner))
        if rec is None:
            return
        if read:
            rec["read"].update(read)
        for name, value in (("verdict", verdict), ("venue", venue), ("route", route)):
            if value is not None:
                rec[name] = value

    def due(self):
        """Pop every frame past GRACE_S with its processed arrival; memory only (the loop)."""
        now, out = self.clock.monotonic(), []
        while self._recs:
            key, rec = next(iter(self._recs.items()))
            if now - rec["mono"] < self.GRACE_S:
                break
            del self._recs[key]
            if 'rejected_evidence' in rec:
                self.stats['evidence']['retained'] -= 1
            arrivals = tuple(self.arrivals_fn(*key) or ())
            processed = [(mono, feed) for feed, mono in arrivals
                         if feed != self.feed and feed not in self.exclude]
            rec["peer_mono"] = next((mono for feed, mono in arrivals if feed == self.peer), None)
            out.append((key, rec, min(processed) if processed else None))
        return out

    def write(self, due):
        for (signature, owner), rec, first in due:
            try:
                lead = "none" if first is None else "%.1f" % ((first[0] - rec["mono"]) * 1000.0)
                peer = ""
                if self.peer:
                    peer_mono = rec.get("peer_mono")
                    peer = " peer=%s peer_lead_ms=%s" % (self.peer, "none" if peer_mono is None
                                                         else "%.1f" % ((peer_mono - rec["mono"]) * 1000.0))
                evidence = self._evidence_fields(rec)
                self.log("[memecoin] slot lane %s frame sig=%s owner=%s slot=%s verdict=%s venue=%s "
                         "route=%s lead_ms=%s processed=%s%s", self.label, signature, owner, rec["slot"],
                         rec["verdict"], rec["venue"] or "none", rec["route"] or "none", lead,
                         "none" if first is None else first[1], self._read_fields(rec) + peer
                         + (" source=%s" % self.source if self.source else "") + evidence)
                self.stats["lines"] += 1
                if evidence.startswith(' evidence_json='):
                    self.stats['evidence']['written'] += 1
                verdicts = self.stats["verdicts"]
                verdicts[rec["verdict"]] = verdicts.get(rec["verdict"], 0) + 1
            except Exception:
                self.stats["write_errors"] += 1

    def _evidence_fields(self, rec):
        """Compact encoding on the existing writer worker; omissions keep the frame verdict."""
        state = rec.get('evidence_state')
        if state != 'captured':
            return ' evidence_omitted=' + state if state else ''
        try:
            import json
            keys, instructions, signers, writable, version = rec['rejected_evidence']
            indices = {key: i for i, key in enumerate(keys)}
            encoded = json.dumps(dict(basis='diagnostic_normalized_pre_execution', version=version,
                keys=keys, signer_indices=tuple(indices[key] for key in signers), writable=writable,
                instructions=tuple(dict(program=indices[program], accounts=tuple(indices[a] for a in accounts),
                                        data_hex=data.hex()) for program, accounts, data in instructions)),
                separators=(',', ':'), allow_nan=False)
            if len(encoded.encode('utf-8')) > self.EVIDENCE_BYTES:
                self.stats['evidence']['encoded_oversize'] += 1
                return ' evidence_omitted=encoded_oversize'
            return ' evidence_json=' + encoded
        except Exception:
            self.stats['evidence']['encode_errors'] += 1
            return ' evidence_omitted=encode_error'

    @staticmethod
    def _read_fields(rec):
        """The read's fields, "none" when the frame had no read (or the field is unknown), never 0;
        empty with MEMECOIN_SLOT_LANE_RABBIT_FRAME_READ_FIELDS off."""
        from sniper.settings import memecoin as cfg
        if not getattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_FRAME_READ_FIELDS", True):
            return ""
        read = rec.get("read") or {}
        ms = read.get("read_ms")
        out = [("read_ms", "none" if ms is None else "%.1f" % ms)]
        out += [(name, "none" if read.get(name) is None else read[name])
                for name in ("read_minus_frame", "state_source", "read_basis", "held")]
        return "".join(" %s=%s" % pair for pair in out)

    async def run(self, stop):
        while not stop.is_set():
            await asyncio.sleep(self.TICK_S)
            due = self.due()
            if due:
                await asyncio.to_thread(self.write, due)


class RabbitBackstop:
    """What a rabbit-led fire needs besides its frame, built once at lane start: the read client,
    where a source that did not land goes (`exit_offer`, the source-sell mirror), and the breaker.
    The signal loop writes verdicts and asks `allowed(feed)` per signal; the lane thread reads verdicts
    before it signs and reports outcomes; the mirror thread reports each exit's realized PnL.

    HOT KILL (operator 2026-10-15): MEMECOIN_SLOT_LANE_RABBIT_BUY is read per signal from the
    settings module, where the runner's cockpit overlay lands it (memecoin_entry_knobs): one
    attribute read, no I/O; with no override the .env value stands. Off is instant and also clears
    a breaker pause (the manual resume).
    BREAKER: MEMECOIN_SLOT_LANE_RABBIT_BREAKER_N verdicts (source_failed + source_not_landed) within
    _WINDOW_MIN minutes pause every rabbit fire for _PAUSE_MIN minutes, paged once. In memory only:
    a restart clears it. Processed-path buys are never touched.
    PER FEED (2026-10-17): a preconf:* feed has its own hot knob (MEMECOIN_SLOT_LANE_PRECONF) and its
    own breaker (MEMECOIN_SLOT_LANE_PRECONF_BREAKER_*, defaulting to rabbit's), reported under
    status["preconf"]; a preconf trip never pauses rabbit, nor the reverse. The top-level status keys
    stay rabbit's."""

    MAX = 4096

    def __init__(self, *, read, exit_offer, clock=time, alert_fn=None):
        self.read, self.exit_offer, self.clock = read, exit_offer, clock
        if alert_fn is None:
            from sniper.services.memecoin_alerts import send_alert as alert_fn
        self.alert_fn = alert_fn
        self._lock = threading.Lock()
        self._verdicts = collections.OrderedDict()   # source signature -> reason
        self._costs = collections.OrderedDict()      # source signature -> realized USD of its exit
        self._recent = {kind: collections.deque() for kind in FEED_KINDS}   # (mono, reason, signature)
        self._paused_until = dict.fromkeys(FEED_KINDS)
        self._landings = {kind: collections.deque(maxlen=256) for kind in FEED_KINDS}
        self.status = dict(knob=None, land_minus_s=None, paused=False, paused_remaining_s=None, trips=0, window=0,
                           verdicts={SOURCE_FAILED: 0, SOURCE_NOT_LANDED: 0}, sent=0, fills=0,
                           exits_closed=0, realized_usd=0.0, last_trip_at=None, last_resume=None)
        self.status["preconf"] = dict(knob=None, land_minus_s=None, paused=False, paused_remaining_s=None,
                                      trips=0, window=0, verdicts={SOURCE_FAILED: 0, SOURCE_NOT_LANDED: 0},
                                      sent=0, fills=0, last_trip_at=None, last_resume=None)

    def _st(self, kind):
        return self.status if kind == "rabbit" else self.status[kind]

    @staticmethod
    def _knobs(kind="rabbit"):
        from sniper.settings import memecoin as cfg
        names, rabbit = FEED_KINDS[kind][1], FEED_KINDS["rabbit"][1]

        def value(i, cast):
            raw = getattr(cfg, names[i], None)             # unset preconf values fall back to rabbit's
            return cast(getattr(cfg, rabbit[i]) if raw is None else raw)
        return value(0, int), value(1, float) * 60.0, value(2, float) * 60.0

    def _resume(self, why, kind="rabbit"):
        self._paused_until[kind] = None
        self._st(kind).update(paused=False, paused_remaining_s=None, last_resume=why)
        logger.warning("[memecoin] slot lane %s breaker RESUMED (%s)", kind, why)

    def allowed(self, feed=None):
        """True when a fire from `feed` may go out now: its hot knob on and its breaker closed."""
        from sniper.settings import memecoin as cfg
        kind = feed_kind(feed)
        knob = bool(getattr(cfg, FEED_KINDS[kind][0], False))
        with self._lock:
            st = self._st(kind)
            st["knob"] = knob
            if self._paused_until[kind] is not None:
                if not knob:
                    self._resume("knob_off", kind)
                elif self.clock.monotonic() >= self._paused_until[kind]:
                    self._resume("pause_elapsed", kind)
                else:
                    st["paused_remaining_s"] = round(self._paused_until[kind] - self.clock.monotonic(), 1)
                    return False
            return knob

    def verdict(self, rec):
        page = None
        kind = feed_kind(rec.get("feed"))
        with self._lock:
            sig, reason = rec["signature"], rec["reason"]
            st = self._st(kind)
            self._verdicts[sig] = reason
            while len(self._verdicts) > self.MAX:
                self._verdicts.popitem(last=False)
            st["verdicts"][reason] = st["verdicts"].get(reason, 0) + 1
            n, window_s, pause_s = self._knobs(kind)
            now = self.clock.monotonic()
            recent = self._recent[kind]
            recent.append((now, reason, sig))
            while recent and now - recent[0][0] > window_s:
                recent.popleft()
            st["window"] = len(recent)
            if self._paused_until[kind] is None and n > 0 and len(recent) >= n:
                self._paused_until[kind] = now + pause_s
                from sniper.util.clock import real_utc_now_iso
                st.update(paused=True, paused_remaining_s=round(pause_s, 1),
                          trips=st["trips"] + 1, last_trip_at=real_utc_now_iso())
                counts = collections.Counter(r for _t, r, _s in recent)
                closed = [self._costs[s] for _t, _r, s in recent if self._costs.get(s) is not None]
                page = ("%s fires PAUSED %.0f min: %d verdicts in %.0f min (source_failed %d, "
                        "source_not_landed %d, unconfirmed 0: a read error never exits); realized "
                        "$%.2f over %d closed exits, %d not closed yet. Processed-path buys continue. "
                        "In memory: a restart clears the pause. Resume now: set "
                        "%s off, then on."
                        % (kind, pause_s / 60.0, len(recent), window_s / 60.0, counts[SOURCE_FAILED],
                           counts[SOURCE_NOT_LANDED], sum(closed), len(closed),
                           len(recent) - len(closed), FEED_KINDS[kind][0]))
        if page is not None:
            logger.warning("[memecoin] slot lane %s breaker TRIPPED: %s", kind, page)
            try:
                self.alert_fn("SLOT-LANE %s BREAKER" % kind.upper(), page)
            except Exception:
                logger.exception("[memecoin] slot lane %s breaker page failed", kind)
        self.exit_offer(dict(rec))

    def not_landed(self, signature):
        """The verdict on this fire's source signature (SOURCE_FAILED / SOURCE_NOT_LANDED) or None."""
        with self._lock:
            return self._verdicts.get(signature)

    def note_result(self, result, source_slot=None, feed=None):
        """The lane's outcome for one pre-execution fire (after any deferred confirmation), and where
        it landed against the source's frame slot S (landing minus S; 0 = the source's own slot),
        counted under the fire's feed kind."""
        result = result or {}
        status, landing = result.get("status"), result.get("landing_slot")
        kind = feed_kind(feed)
        with self._lock:
            st = self._st(kind)
            if status == "filled":
                st["fills"] += 1
            if status in ("filled", "submitted", "submitted_unknown", "failed") and result.get("sig"):
                st["sent"] += 1
            if type(landing) is int and type(source_slot) is int:
                self._landings[kind].append(landing - source_slot)
                ordered = sorted(self._landings[kind])
                st["land_minus_s"] = {
                    "n": len(ordered), "p50": ordered[(len(ordered) - 1) // 2],
                    "p90": ordered[-(-9 * len(ordered) // 10) - 1],
                    "hist": {str(k): v for k, v in sorted(collections.Counter(ordered).items())}}

    def record_cost(self, signature, realized):
        with self._lock:
            try:
                value = float(realized)
            except (TypeError, ValueError):
                return
            self._costs[signature] = value
            while len(self._costs) > self.MAX:
                self._costs.popitem(last=False)
            self.status["exits_closed"] += 1
            self.status["realized_usd"] = round(self.status["realized_usd"] + value, 2)


class LandingWatch:
    """Each rabbit-led fire's source signature, owned by the slot-signal event loop (see the module
    docstring). `observe` sees every processed owner frame of both feeds; `observe_buy` every
    decoded processed buy; `run` checks the due watches."""

    LANDING_SLOTS = 4          # ~0.9 s at 218 ms slots
    MAX_WAIT_S = 3.0           # due anyway when the processed head is stale
    HEAD_FRESH_S = 1.0
    NULL_RECHECK_S = 0.3       # a null answer is asked once more: the read node may trail the feeds
    ERROR_RETRY_S = (0.2, 1.0)
    UNKNOWN_AFTER_S = 10.0
    TICK_S = 0.05
    SEEN_MAX = 4096

    def __init__(self, *, backstop, head_fn, status, clock=time):
        self.backstop, self.head_fn, self.clock = backstop, head_fn, clock
        self.stats = status
        status.update(watched=0, landed=0, feed_missed=0, replaced=0, unknown=0, check_errors=0, node_behind=0,
                      verdict_errors=0, observe_errors=0, **{SOURCE_FAILED: 0, SOURCE_NOT_LANDED: 0})
        self._seen = collections.OrderedDict()      # signature -> ("ok" | "err", processed mono)
        self._bought = collections.OrderedDict()    # (owner, mint) -> ((signature, slot), ...) processed buys
        self._watch = collections.OrderedDict()     # signature -> fire record
        self._checking = set()
        self._tasks = set()

    def _bound(self, table):
        while len(table) > self.SEEN_MAX:
            table.popitem(last=False)

    def observe(self, update, mono):
        """One processed owner frame (success or meta.err). Memory only."""
        import base58
        try:
            info = update.transaction.transaction
            if len(info.signature) != 64 or not info.HasField("meta"):
                return
            signature = base58.b58encode(info.signature).decode()
            state = "err" if info.meta.HasField("err") else "ok"
        except Exception:
            self.stats["observe_errors"] += 1
            return
        self._seen[signature] = (state, mono)
        self._bound(self._seen)
        if signature in self._watch:
            self._landed(self._watch[signature], state, "processed_feed", mono)

    BOUGHT_PER_COIN = 8

    def observe_buy(self, owner, mint, mono, *, signature=None, slot=None):
        """A decoded processed buy of `owner`, kept with its signature and slot: receipt order is
        not chain order (Codex F3), so `_verdict` reads only the chain's. `mono` is unused."""
        if owner and mint:
            key = (owner, mint)
            entry = (signature, slot if type(slot) is int else None)
            self._bought[key] = (self._bought.pop(key, ()) + (entry,))[-self.BOUGHT_PER_COIN:]
            self._bound(self._bought)

    def has_bought(self, owner, mint, *, other_than=None):
        """True when a processed buy of `mint` by `owner` under another signature is in memory."""
        return any(sig and sig != other_than for sig, _slot in self._bought.get((owner, mint), ()))

    def _replaced(self, rec):
        """True only for a PROVEN later distinct buy of the coin: another signature in a later slot
        than the watched frame. Same slot (a shred carries no transaction index), an unknown slot
        or signature, or an older slot keeps the verdict."""
        return any(sig and sig != rec["signature"] and slot is not None and slot > rec["slot"]
                   for sig, slot in self._bought.get((rec["owner"], rec["mint"]), ()))

    def failed(self, signature):
        return (self._seen.get(signature) or (None,))[0] == "err"

    def expect(self, *, signature, owner, source_user_id, mint, slot, frame_mono, feed=None):
        rec = dict(signature=signature, owner=owner, source_user_id=source_user_id, mint=mint,
                   slot=int(slot), frame_mono=frame_mono, fired_mono=self.clock.monotonic(), feed=feed)
        self.stats["watched"] += 1
        seen = self._seen.get(signature)
        if seen is not None:
            return self._landed(rec, seen[0], "processed_feed", seen[1])
        self._watch[signature] = rec

    def _landed(self, rec, state, basis, mono):
        self._watch.pop(rec["signature"], None)
        if state == "ok":
            self.stats["landed"] += 1
            return
        self._verdict(rec, SOURCE_FAILED, basis, landing_mono=mono)

    def _drop(self, signature, outcome):
        rec = self._watch.pop(signature, None)
        if rec is not None:
            self.stats[outcome] += 1
            if outcome == "unknown":
                logger.warning("[memecoin] slot lane rabbit landing UNKNOWN sig=%s uid=%s mint=%s: "
                               "the read failed for %.0f s; the position keeps its ordinary rails",
                               rec["signature"], rec["source_user_id"], rec["mint"], self.UNKNOWN_AFTER_S)

    def _verdict(self, rec, reason, basis, *, landing_mono=None):
        self._watch.pop(rec["signature"], None)
        # The add-on ring (memecoin_addon_guard): a source tx that failed or never landed was never
        # his buy, whatever this verdict does next (Gamma LOW on 13da10592).
        try:
            from sniper.services.memecoin_addon_guard import RING
            RING.note_tx_failed(rec["signature"])
        except Exception:
            logger.exception("[memecoin] add-on ring drop failed sig=%s", rec.get("signature"))
        now = self.clock.monotonic()
        if self._replaced(rec):
            self.stats["replaced"] += 1       # the owner bought this coin later, under another signature
            return
        self.stats[reason] += 1
        out = dict(rec, reason=reason, basis=basis,
                   lead_ms=(round((landing_mono - rec["frame_mono"]) * 1000.0, 1)
                            if landing_mono is not None else None),
                   age_ms=round((now - rec["frame_mono"]) * 1000.0, 1))
        logger.warning("[memecoin] slot lane rabbit %s sig=%s uid=%s mint=%s basis=%s lead_ms=%s age_ms=%.0f",
                       reason, rec["signature"], rec["source_user_id"], rec["mint"], basis,
                       out["lead_ms"], out["age_ms"])
        try:
            self.backstop.verdict(out)
        except Exception:
            self.stats["verdict_errors"] += 1
            logger.exception("[memecoin] slot lane rabbit verdict failed sig=%s", rec["signature"])

    def _status(self, signature):
        """(the read node's context slot, the status or None)."""
        res = self.backstop.read("getSignatureStatuses", [[signature], {"searchTransactionHistory": False}])
        value = (res or {}).get("value")
        if not isinstance(value, list) or len(value) != 1:
            raise ValueError("getSignatureStatuses: malformed answer")
        slot = ((res or {}).get("context") or {}).get("slot")
        return (slot if type(slot) is int else None), value[0]

    async def _confirm(self, signature, rec):
        nulls = errors = 0
        try:
            while signature in self._watch:
                try:
                    node_slot, status = await asyncio.to_thread(self._status, signature)
                    if status is None and (node_slot is None or node_slot < rec["slot"] + self.LANDING_SLOTS):
                        # A null from a node that has not reached S+4 (or does not say) is no
                        # evidence (Gamma MEDIUM 2026-10-15): the same as a read error.
                        self.stats["node_behind"] += 1
                        raise LookupError("read node behind the frame slot")
                except Exception:
                    errors += 1
                    self.stats["check_errors"] += 1
                    if self.clock.monotonic() - rec["fired_mono"] >= self.UNKNOWN_AFTER_S:
                        return self._drop(signature, "unknown")
                    await asyncio.sleep(self.ERROR_RETRY_S[min(errors, len(self.ERROR_RETRY_S)) - 1])
                    continue
                if signature not in self._watch:
                    return                                  # a feed answered meanwhile
                if status is None:
                    nulls += 1
                    if nulls >= 2:
                        return self._verdict(rec, SOURCE_NOT_LANDED, "signature_status_null")
                    await asyncio.sleep(self.NULL_RECHECK_S)
                    continue
                if status.get("err") is None:
                    return self._drop(signature, "feed_missed")
                return self._verdict(rec, SOURCE_FAILED, "signature_status_err")
        finally:
            self._checking.discard(signature)

    def due(self):
        """The watches whose source should have shown by now, not yet being checked."""
        head, head_mono = self.head_fn()
        now = self.clock.monotonic()
        fresh = type(head) is int and head_mono is not None and now - head_mono <= self.HEAD_FRESH_S
        return [rec for sig, rec in self._watch.items() if sig not in self._checking
                and ((fresh and head >= rec["slot"] + self.LANDING_SLOTS)
                     or now - rec["fired_mono"] >= self.MAX_WAIT_S)]

    async def run(self, stop):
        while not stop.is_set():
            await asyncio.sleep(self.TICK_S)
            for rec in self.due():
                self._checking.add(rec["signature"])
                task = asyncio.get_running_loop().create_task(self._confirm(rec["signature"], rec))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

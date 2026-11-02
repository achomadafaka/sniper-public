"""Desk U safety evidence: token_safety, holders, market_data and sell_probe check rows for one
producer-U ticket, from two research-RPC reads. Pure: the worker does the I/O.

Reads (Gamma/Alpha 2026-10-21, research key only, off every entry path):
  1. getTokenLargestAccounts(mint)                               -> top-20 token accounts
  2. getMultipleAccounts([mint, bonding curve, curve FeeConfig, *top-20])   one context slot
The curve, its fee config and the holder accounts come from the same slot, so the sell probe's
reserves, `complete` flag and fee tier describe one state. The FeeConfig is read on every ticket,
so a mid-run fee change is seen at once (no start-up cache to go stale).

Every row is pass / kill / unknown in the hard-filter grammar; anything unread or unparsable is
unknown with a machine reason, never a pass. The sell probe is curve math: it proves the round-trip
cost on a live curve at that slot, not that a real sell transaction lands (reason "curve_math").
"""
from __future__ import annotations

import base64
import hashlib
import json
import struct
from datetime import datetime

from sniper.execution.pump_curve_buy import curve_buy_net, expected_tokens_out
from sniper.execution.pump_fees import curve_fee_bps, parse_fee_config
from sniper.execution.pump_native_exit import (FEE_CONFIG_SEED, FEE_PROGRAM, PUMP_PROGRAM, TOKEN_2022_PROGRAM,
                                               TOKEN_PROGRAM, _pda)

INCINERATOR = "1nc1nerator11111111111111111111111111111111"
CURVE_MIN_LEN = 82                 # ... complete @48, creator @49..81, is_mayhem_mode @81
# Token-2022 mint extensions a Pump create_v2 mint carries; any other is unknown, never assumed safe.
EXT_TRANSFER_HOOK, BENIGN_EXTENSIONS = 14, {18, 19}       # MetadataPointer, TokenMetadata


def curve_address(mint):
    return str(_pda([b"bonding-curve", bytes(_pk(mint))], PUMP_PROGRAM))


def fee_config_address():
    return str(_pda([b"fee_config", FEE_CONFIG_SEED], FEE_PROGRAM))


def _pk(s):
    from solders.pubkey import Pubkey
    return Pubkey.from_string(s)


def _b58(raw):
    from solders.pubkey import Pubkey
    return str(Pubkey.from_bytes(raw))


def batch_addresses(mint, largest):
    """Read 2's account list: mint, curve, fee config, then the top-20 token accounts in order."""
    return [mint, curve_address(mint), fee_config_address()] + [row["address"] for row in largest]


def _data(acc):
    if not acc or not isinstance(acc.get("data"), list):
        return None
    try:
        return base64.b64decode(acc["data"][0])
    except (TypeError, ValueError):
        return None


def parse_mint(acc):
    """{supply, authority_mint, authority_freeze, transfer_hook} or ValueError(reason)."""
    raw = _data(acc)
    if raw is None or len(raw) < 82:
        raise ValueError("mint_unreadable")
    if acc.get("owner") not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise ValueError("mint_owner")
    out = {"supply": struct.unpack_from("<Q", raw, 36)[0],
           "authority_mint": "open" if struct.unpack_from("<I", raw, 0)[0] else "closed",
           "authority_freeze": "open" if struct.unpack_from("<I", raw, 46)[0] else "closed",
           "transfer_hook": None}
    if len(raw) > 166:                               # Token-2022: account type @165, TLV from 166
        o = 166
        while o + 4 <= len(raw):
            kind, size = struct.unpack_from("<HH", raw, o)
            if kind == 0:
                break
            body = raw[o + 4:o + 4 + size]
            if kind == EXT_TRANSFER_HOOK:
                program = body[32:64]
                out["transfer_hook"] = _b58(program) if len(program) == 32 and any(program) else None
            elif kind not in BENIGN_EXTENSIONS:
                raise ValueError("extension_%d" % kind)
            o += 4 + size
    return out


def parse_curve(acc):
    raw = _data(acc)
    if raw is None or len(raw) < CURVE_MIN_LEN or acc.get("owner") != PUMP_PROGRAM:
        raise ValueError("curve_unreadable")
    vt, vq, rt, rq, supply = struct.unpack_from("<5Q", raw, 8)
    return {"virtual_token_reserves": vt, "virtual_sol_reserves": vq, "real_token_reserves": rt,
            "real_sol_reserves": rq, "complete": bool(raw[48]), "creator": _b58(raw[49:81]), "mayhem": raw[81] != 0,
            "data_b64": acc["data"][0]}


def parse_token_account(acc):
    raw = _data(acc)
    if raw is None or len(raw) < 72:
        return None
    return {"mint": _b58(raw[0:32]), "owner": _b58(raw[32:64]), "amount": struct.unpack_from("<Q", raw, 64)[0]}


def round_trip_loss_pct(curve, fees, spend_lamports):
    """Buy `spend_lamports` exact-in on the curve, sell the exact tokens back at the post-buy
    reserves, both legs paying (protocol, creator) bps rounded up: (loss_pct, tokens, lamports_back),
    or None when the buy gets nothing."""
    protocol, creator = fees
    event = {**curve, "fee_basis_points": protocol, "creator_fee_basis_points": creator}
    tokens = expected_tokens_out(spend_lamports, event)
    if tokens <= 0:
        return None
    vq = curve["virtual_sol_reserves"] + curve_buy_net(spend_lamports, event)
    vt = curve["virtual_token_reserves"] - tokens
    gross = vq * tokens // (vt + tokens)
    back = gross - sum(-(-gross * bps // 10_000) for bps in (protocol, creator))
    return round((1 - back / spend_lamports) * 100, 4), tokens, back


def evaluate(candidate, *, largest, accounts, ctx_slot, sol_usd, clip_usd=25.0, max_slot_lag=25,
             creator_pct_max=None):
    """(checks, meta). `largest` = read 1's value list, `accounts` = read 2's value list (or None
    when a read failed), `ctx_slot` = read 2's context slot."""
    checks, meta = {}, {"ctx_slot": ctx_slot}
    mint = candidate["mint"]
    unknown = lambda reason: {"status": "unknown", "reason": reason}
    if accounts is None or largest is None:
        reason = "rpc_failed"
        return {k: unknown(reason) for k in ("token_safety", "holders", "market_data", "sell_probe")}, meta
    head, holders_acc = accounts[:3], accounts[3:]

    try:
        m = parse_mint(head[0])
        checks["token_safety"] = {"status": "pass", "reason": "mint_read", **{k: m[k] for k in (
            "authority_mint", "authority_freeze", "transfer_hook")}}
    except ValueError as exc:
        m, checks["token_safety"] = None, unknown(str(exc))

    curve = None
    try:
        curve = parse_curve(head[1])
    except ValueError as exc:
        checks["market_data"] = checks["sell_probe"] = unknown(str(exc))
    snap_slot = (candidate["snapshot"]["fields"].get("mcap_usd") or {}).get("slot")
    meta["snapshot_slot"] = snap_slot
    if curve is not None:
        meta["mayhem"] = curve["mayhem"]                 # immutable per coin; the tape probe's fee gate
        lag = None if snap_slot is None or ctx_slot is None else ctx_slot - snap_slot
        meta["slot_lag"] = lag
        if lag is None or lag < 0 or lag > max_slot_lag:
            checks["market_data"] = checks["sell_probe"] = unknown("reserves_stale")
        elif curve["complete"]:
            checks["market_data"] = unknown("curve_complete")
            checks["sell_probe"] = unknown("curve_complete")
        else:
            checks["market_data"] = {"status": "pass", "reason": "curve_reserves"}
            checks["sell_probe"] = _probe(curve, head[2], sol_usd, clip_usd, meta)

    checks["holders"] = _holders(mint, m, largest, holders_acc, curve_address(mint),
                                 curve["creator"] if curve else None, creator_pct_max, meta)
    return checks, meta


def _probe(curve, fee_acc, sol_usd, clip_usd, meta):
    unknown = lambda reason: {"status": "unknown", "reason": reason}
    raw = _data(fee_acc)
    if raw is None:
        return unknown("fee_config_unreadable")
    meta["fee_config_sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        fees = curve_fee_bps(parse_fee_config(fee_acc["data"][0]), curve["data_b64"])
    except ValueError:
        return unknown("fee_config_unreadable")
    if fees is None:                                  # mayhem curve or a tiered curve config
        return unknown("fee_tier_unmodelled")
    meta["fee_bps"] = list(fees)
    if not sol_usd or sol_usd <= 0:
        return unknown("sol_usd_missing")
    spend = int(clip_usd / sol_usd * 1e9)
    trip = round_trip_loss_pct(curve, fees, spend)
    if trip is None:
        return unknown("buy_gets_nothing")
    meta.update(spend_lamports=spend, tokens_out=trip[1], lamports_back=trip[2])
    return {"status": "pass", "reason": "curve_math", "round_trip_loss_pct": trip[0], "exact_quantity": True}


def _holders(mint, m, largest, accs, curve, creator, creator_pct_max, meta):
    """top10_pct over owners of the top-20 accounts, the bonding curve and the incinerator excluded.
    creator_pct (Gamma 2026-10-21), basis recorded in meta:
      top20      creator among the owners: its share (a lower bound; other wallets are unseen);
      short_page fewer than 20 accounts and creator absent: 0 exact;
      below_page creator absent from a full page: owner share unknown; one owner may hold many unseen
                 accounts. `creator_pct_max` is ignored (a fact, never a gate)."""
    unknown = lambda reason: {"status": "unknown", "reason": reason}
    if m is None or not m["supply"]:
        return unknown("supply_unknown")
    if creator is None:
        return unknown("creator_unknown")
    by_owner, verified = {}, len(accs) == len(largest)
    for acc in accs:
        tok = parse_token_account(acc)
        if tok is None or tok["mint"] != mint:
            verified = False
            continue
        if tok["owner"] in (curve, INCINERATOR):
            continue
        by_owner[tok["owner"]] = by_owner.get(tok["owner"], 0) + tok["amount"]
    pct = lambda v: round(v / m["supply"] * 100, 4)
    if creator in by_owner:
        basis, creator_share = "top20", pct(by_owner[creator])
    elif len(largest) < 20 or not by_owner:
        basis, creator_share = "short_page", 0.0
    else:
        basis, creator_share = "below_page", None
    meta["creator_pct_basis"] = basis
    top10 = sorted(by_owner.values(), reverse=True)[:10]
    # Visible account balances are lower bounds on their owners' complete holdings.
    return {"status": "pass", "reason": "creator_unresolved_below_page" if basis == "below_page" else "largest_accounts",
            "top10_pct": pct(sum(top10)),
            "creator_pct": creator_share, "creator_basis": basis, "exclusions_verified": verified}


# --- curve-math probe from the local U tape (Gamma 3a7d89c2d, docs/audits/2026-10-21-desk-u-sell-probe-evidence.md)

PUMP_VIRTUAL_TOKEN_OFFSET = 279_900_000_000_000   # virtual - real token reserves on every Pump curve


MAYHEM_AGENT_IDS = frozenset(("BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s",))   # pinned equal to research/state.py by a test
FEE_AGREE_MIN, FEE_OF_NEWEST = 2, 3   # the fee proof: at least this many of the newest this-many regular trades agree
REGULAR_FEE_TRADES = 5         # how many recent NON-agent trades the assembler hands over (newest first)


def regular_fee_pair(pairs):
    """The (protocol_bps, creator_bps) a regular trader pays on this coin, proven from its own tape, or None.
    `pairs` = (fee_bps, creator_fee_bps) of the newest NON-agent trades, newest first (up to REGULAR_FEE_TRADES). The Mayhem agent
    trades fee-free (0, 0) and is never in `pairs`; its trades must not define OUR fee. Proof: every pair is two non-negative ints, at least
    two of the newest three agree exactly. The fee used is the componentwise MAX over all given pairs, so the estimate can only be
    higher than the agreed pair, never lower. Fewer than two trades, a disagreement among the newest three, or a NULL = None (unknown)."""
    try:
        pairs = [tuple(p) for p in pairs]
        if (len(pairs) < FEE_AGREE_MIN or any(len(p) != 2 or any(type(f) is not int or isinstance(f, bool) or not 0 <= f < 10_000 for f in p)
                                  for p in pairs)):
            return None
        newest = pairs[:FEE_OF_NEWEST]
        if not any(newest.count(p) >= FEE_AGREE_MIN for p in newest):
            return None
        return (max(p[0] for p in pairs), max(p[1] for p in pairs))
    except (TypeError, ValueError):
        return None


def probe_from_tape(last_trade, *, head_slot, head_known_at, heartbeat_age_s, gap_last_slots, graduated,
                    mayhem, sol_usd, evaluated_at, clip_usd=25.0, max_age_s=10.0):
    """(sell_probe check, evidence) recomputed right before hard_filter. 0 RPC, no state.

    last_trade: the mint's newest decoded TradeEvent on the tape {slot, virtual_quote, virtual_token,
    real_token, fee_bps, creator_fee_bps} (post-trade reserves; the fees the program charged on it). The state at
    head slot H equals that trade's state only when the tape saw every slot in (trade slot, H]: an
    open gap there, a heartbeat older than max_age_s, or a head older than max_age_s at evaluation is
    tape_not_current. `mayhem` is the curve account's is_mayhem_mode (immutable per coin; None when
    unread): a mayhem coin or an unread flag is fee_unknown (note PR #519). Times are epoch seconds."""
    unknown = lambda reason: ({"status": "unknown", "reason": reason}, {"reserves_slot": head_slot})
    if heartbeat_age_s is None or heartbeat_age_s > max_age_s or head_known_at is None \
            or evaluated_at - head_known_at > max_age_s or evaluated_at < head_known_at:
        return unknown("tape_not_current")
    if last_trade is None or last_trade.get("slot") is None or last_trade["slot"] > head_slot:
        return unknown("no_trade_state")
    if any(s is None or s >= last_trade["slot"] for s in gap_last_slots):
        return unknown("tape_not_current")              # a gap opened after this state was seen
    if graduated:
        return unknown("curve_complete")
    regular = last_trade.get("regular_fees")
    if regular is not None:
        # agent-aware: the fee is what NON-agent traders paid on this coin (the fee-free Mayhem agent never defines it); a mayhem coin
        # is priced only under that proof, an unread mayhem flag is still unknown
        fees = regular_fee_pair(regular) if mayhem is not None else None
        if fees is None:
            return unknown("fee_unknown")
    else:
        fees = (last_trade.get("fee_bps"), last_trade.get("creator_fee_bps"))
        if mayhem is not False or any(type(f) is not int or f < 0 for f in fees):
            return unknown("fee_unknown")
    if not sol_usd or sol_usd <= 0:
        return unknown("sol_usd_missing")
    vq, vt, rt = last_trade.get("virtual_quote"), last_trade.get("virtual_token"), last_trade.get("real_token")
    if not vq or not vt or type(rt) is not int or rt < 0:
        return unknown("reserves_unreadable")
    if vt - rt != PUMP_VIRTUAL_TOKEN_OFFSET:            # other initial reserves: the curve model may not hold (Gamma)
        return unknown("reserves_mismatch")
    if rt == 0:
        return unknown("curve_complete")
    curve = {"virtual_sol_reserves": vq, "virtual_token_reserves": vt, "real_token_reserves": rt}
    spend = int(clip_usd / sol_usd * 1e9)
    trip = round_trip_loss_pct(curve, fees, spend)
    if trip is None:
        return unknown("buy_gets_nothing")
    evidence = {"reserves_slot": head_slot, "reserves_known_at": head_known_at, "trade_slot": last_trade["slot"],
                "fee_bps": list(fees), "fee_basis": "non_agent_trades" if regular is not None else "last_trade",
                "curve_complete": False, "spend_lamports": spend, "tokens_out": trip[1],
                "lamports_back": trip[2]}
    return {"status": "pass", "reason": "curve_math", "round_trip_loss_pct": trip[0], "exact_quantity": True}, evidence


# --- emission identity: one check shared by every reader of candidate_emissions (Codex FAIL on Beta's cluster)

EMISSION_SCHEMA = "desk-candidate/v1"


def snapshot_digest(snapshot):
    """The producer's recipe: sha256 over the snapshot without its "hash" key, JSON with sorted keys,
    separators (",", ":"), no NaN/Infinity. Same bytes as build_snapshot and step 3's validate_candidate."""
    body = {k: v for k, v in snapshot.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def pumpswap_pool_address(mint):
    """Canonical Pump-migrated WSOL pool: index zero and the mint's Pump authority PDA."""
    from solders.pubkey import Pubkey
    from sniper.services.memecoin_desk_universe import PUMP_PROGRAM
    from sniper.services.memecoin_desk_contract import WSOL_MINT
    m = bytes(Pubkey.from_string(mint))
    authority = bytes(Pubkey.find_program_address([b"pool-authority", m], Pubkey.from_string(PUMP_PROGRAM))[0])
    return str(Pubkey.find_program_address([b"pool", b"\0\0", authority, m, bytes(Pubkey.from_string(WSOL_MINT))],
        Pubkey.from_string("pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"))[0])


def pumpswap_ticket_id(network_id, mint, pool, band_entry_slot, signature, ordinal):
    key = f"{network_id}|pumpswap|{pool}|{mint}|{band_entry_slot}|{signature}|{ordinal}"
    return "desk:U:ps:" + hashlib.sha256(key.encode()).hexdigest()[:24]


def verify_u_emission(cand, *, now, max_age_s):
    """The candidate when it is a genuine, fresh producer-U emission; else ValueError(reason).
    Curve v1 retains its original identity; PumpSwap binds venue and canonical migrated pool and
    requires complete causal pool facts. Both bind the snapshot hash, emission clock and age."""
    from sniper.services import memecoin_desk_universe as U
    from sniper.services import memecoin_desk_contract as K
    try:
        pumpswap = cand.get("schema") == K.PUMPSWAP_SCHEMA
        if ((not pumpswap and (cand.get("schema") != EMISSION_SCHEMA or "venue" in cand))
                or cand.get("producer") != "U" or cand.get("network_id") != "solana"):
            raise ValueError("emission_schema")
        if pumpswap:
            K.validate_candidate(cand)
            row = cand["snapshot"]["fields"].get("pool_state")
            if (not row or row["completeness"] != "complete" or row["value"] is None
                    or row["value"]["slot"] != cand["band_entry_slot"]):
                raise ValueError("emission_pool_state")
            expected_id = pumpswap_ticket_id(cand["network_id"], cand["mint"], cand["pool"],
                cand["band_entry_slot"], cand["trigger_signature"], cand["trigger_ordinal"])
            expected_pool = pumpswap_pool_address(cand["mint"])
        else:
            expected_id = U.ticket_id(cand["network_id"], cand["mint"], cand["band_entry_slot"],
                                      cand["trigger_signature"], cand["trigger_ordinal"])
            expected_pool = curve_address(cand["mint"])
        if cand["ticket_id"] != expected_id:
            raise ValueError("emission_ticket_id")
        if cand["pool"] != expected_pool:
            raise ValueError("emission_pool")
        snap = cand["snapshot"]
        if snap.get("hash") != snapshot_digest(snap):
            raise ValueError("emission_snapshot_hash")
        emitted = snap["emission_at"]
        if type(emitted) not in (int, float) or datetime.fromisoformat(cand["emitted_at"]).timestamp() != emitted:
            raise ValueError("emission_time")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("emission_"):
            raise
        raise ValueError("emission_shape") from None
    if not 0 <= now - emitted <= max_age_s:
        raise ValueError("emission_stale")
    return cand


def verify_intake_emission(cand, *, now, max_age_s):
    """The candidate when it is a genuine, fresh desk-intake PumpSwap emission (IntakeBook); else ValueError(reason).
    Binds the canonical pool, the opportunity id re-derived from the intake claim, the snapshot hash and the age."""
    from sniper.services import memecoin_desk_contract as K
    from sniper.services.memecoin_desk_intake_book import opportunity_id
    try:
        if cand.get("schema") != K.PUMPSWAP_SCHEMA or cand.get("producer") != "U" or "intake" not in cand:
            raise ValueError("emission_schema")
        K.validate_candidate(cand)
        claim = cand["intake"]
        if cand["ticket_id"] != opportunity_id(cand["network_id"], cand["mint"], claim["feed"], claim["filter"],
                                               claim["intake_row_ref"]):
            raise ValueError("emission_ticket_id")
        if cand["pool"] != pumpswap_pool_address(cand["mint"]):
            raise ValueError("emission_pool")
        snap = cand["snapshot"]
        if snap.get("hash") != snapshot_digest(snap):
            raise ValueError("emission_snapshot_hash")
        emitted = snap["emission_at"]
        if type(emitted) not in (int, float) or datetime.fromisoformat(cand["emitted_at"]).timestamp() != emitted:
            raise ValueError("emission_time")
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("emission_"):
            raise
        raise ValueError("emission_shape") from None
    if not 0 <= now - emitted <= max_age_s:
        raise ValueError("emission_stale")
    return cand

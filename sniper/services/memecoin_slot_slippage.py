"""Optional pre-sign gas veto from existing coherent PoolTape state, with no I/O.

The fee-free constant-product result is an OUTPUT UPPER BOUND, never an executable quote.
Only an identity-bound frozen bank or complete same-bank transaction cut after the exact source
can veto a signed minimum above that bound. Unknown preserves the send; no policy is changed.
Movement after this read can still change the outcome.
In movement-only mode the same proven state also measures marginal market movement, independent
of ticket size. Missing state remains unknown and proceeds by operator policy.
"""
from __future__ import annotations

import base64
import math
import struct
import time

from solders.pubkey import Pubkey
from solders.signature import Signature

from sniper.execution import pump_curve_buy as curve
from sniper.execution import pumpswap_buy as amm
from sniper.execution.solana_native_entry_quote import accounts_for
from sniper.services.memecoin_rabbit_tape import ANCESTRY_MAX, LAG_SLOTS, _json

MAX_HEAD_AGE_S = 1.0
OPEN_MISS_REASONS = ("open_not_source_slot", "open_bank_unproved", "open_accounts_missing",
                     "open_group_unproved", "open_source_unproved", "open_not_after_source",
                     "open_state_changed", "open_source_or_state_unmodelled")


class _Unknown(ValueError):
    def __init__(self, reason, **evidence):
        super().__init__(reason)
        self.evidence = evidence


def _integer(value):
    if type(value) is not int or not 0 < value < 2**64:
        raise _Unknown("invalid_amount_or_slot")
    return value


def _raw(accounts, key, owner):
    account = accounts.get(key)
    if not isinstance(account, dict) or account.get("owner") != owner:
        raise _Unknown("account_identity")
    encoded = account.get("data")
    if not isinstance(encoded, (list, tuple)) or len(encoded) != 2 or encoded[1] != "base64":
        raise _Unknown("account_encoding")
    return base64.b64decode(encoded[0], validate=True)


def _keys(signed):
    """Cheap prospective keys; full source/account binding still runs before any verdict."""
    if signed["venue"] == "pump_curve":
        return (signed["pool"],)
    if signed["venue"] == "pump_amm":
        facts = signed["pool_facts"]
        return signed["pool"], facts["pool_base_token_account"], facts["pool_quote_token_account"]
    raise _Unknown("unsupported_venue")


def _open_accounts(rings, slot, bank, source_signature, source_groups):
    """Latest complete tx-post vector, anchored after the source in this exact open bank."""
    latest = {}
    for key, ring in rings.items():
        if not ring:
            raise _Unknown("open_accounts_missing")
        entry = ring[-1]
        account = entry[3]
        if (entry[0] != slot or type(entry[1]) is not int or entry[1] < 0 or entry[2] is not False
                or not isinstance(account, dict) or account.get("is_startup") is not False
                or type(account.get("bank_id")) is not int or account["bank_id"] != bank
                or type(account.get("txn_signature")) is not bytes or len(account["txn_signature"]) != 64):
            raise _Unknown("open_group_unproved")
        latest[key] = entry
    signature = next(iter(latest.values()))[3]["txn_signature"]
    if any(entry[3]["txn_signature"] != signature for entry in latest.values()):
        raise _Unknown("open_group_unproved")
    try:
        source = bytes(Signature.from_string(source_signature))
    except (TypeError, ValueError):
        raise _Unknown("open_source_unproved") from None
    if len(source) != 64:
        raise _Unknown("open_source_unproved")
    group_key = group = None
    retained = False
    for key, ring in rings.items():
        anchor = next((entry for entry in reversed(ring) if entry[0] == slot and entry[2] is False
                       and isinstance(entry[3], dict) and entry[3].get("txn_signature") == source
                       and type(entry[3].get("bank_id")) is int and entry[3]["bank_id"] == bank
                       and entry[3].get("is_startup") is False), None)
        if anchor is None:
            if group_key is None:
                group_key = (slot, bank, source)
                group = source_groups.get(group_key)
            anchor = next((entry for name, entry in group or () if name == key
                           and entry[0] == slot and entry[2] is False
                           and isinstance(entry[3], dict) and entry[3].get("txn_signature") == source
                           and type(entry[3].get("bank_id")) is int and entry[3]["bank_id"] == bank
                           and entry[3].get("is_startup") is False), None)
            if anchor is None:
                raise _Unknown("open_source_unproved", source_anchor_basis="unproved",
                               source_anchor_missing_key=key,
                               source_anchor_detail=("group_not_retained" if group is None else
                                                     "group_ineligible" if not group else "key_not_retained"),
                               source_anchor_retained_keys=len(group or ()),
                               source_anchor_ring_oldest_slot=ring[0][0],
                               source_anchor_ring_oldest_version=ring[0][1])
            retained = True
        version = latest[key][1]
        if (type(anchor[1]) is not int or anchor[1] < 0 or version < anchor[1]
                or signature != source and version == anchor[1]):
            raise _Unknown("open_not_after_source")
    # All writable instruction accounts are saved with their causing signature, including an
    # unchanged pool account. Never bridge an old pool/VQ onto two newer vault notifications.
    return ({key: _json(entry[3]) for key, entry in latest.items()}, signature,
            "retained_group" if retained else "raw_ring", (group_key, group) if retained else None)


def _reference(buy, signed):
    venue, mint, pool = signed["venue"], signed["mint"], signed["pool"]
    if (getattr(buy, "venue", None) != venue or not mint or not pool
            or signed["source_sig"] != buy.signature or not buy.signature
            or _integer(signed["source_slot"]) != _integer(buy.slot)):
        raise _Unknown("source_identity")
    if signed["base_token_program"] not in (curve.TOKEN_PROGRAM, curve.TOKEN_2022_PROGRAM):
        raise _Unknown("token_program")
    if signed["quote_token_program"] not in (curve.TOKEN_PROGRAM, curve.TOKEN_2022_PROGRAM):
        raise _Unknown("quote_program")
    if venue == "pump_curve":
        event = curve.parse_trade_event(buy.trade_event_log)
        sol_quote = (signed["quote_mint"] in curve.SOL_QUOTE_MINTS
                     and event["quote_mint"] in curve.SOL_QUOTE_MINTS
                     and signed["quote_token_program"] == curve.TOKEN_PROGRAM
                     and signed.get("instruction_discriminator") in (
                         curve.BUY_EXACT_SOL_IN_DISCRIMINATOR.hex(),
                         curve.BUY_EXACT_QUOTE_IN_V3_DISCRIMINATOR.hex()))
        token_quote = (signed["quote_mint"] not in curve.SOL_QUOTE_MINTS
                       and signed["quote_mint"] == event["quote_mint"]
                       and signed.get("instruction_discriminator") == curve.BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR.hex())
        if (signed["program"] != curve.PUMP_PROGRAM or buy.mint != mint
                or pool != accounts_for(mint)["curve"] or event["mint"] != mint
                or event["user"] != buy.owner or event["is_buy"] is not True
                or getattr(buy, "token_program", None) != signed["base_token_program"]
                or not (sol_quote or token_quote)):
            raise _Unknown("curve_source_unmodelled")
        return (pool,), event
    if venue == "pump_amm":
        event, facts = amm.parse_buy_event(buy.trade_event_log), signed["pool_facts"]
        if (signed["program"] != amm.AMM_PROGRAM or buy.pool != pool or event["pool"] != pool
                or event["user"] != buy.owner
                or (signed["quote_mint"] == curve.WSOL_MINT
                    and signed["quote_token_program"] != curve.TOKEN_PROGRAM)
                or facts["base_mint"] != mint or facts["quote_mint"] != signed["quote_mint"]):
            raise _Unknown("amm_source_identity")
        if signed["quote_mint"] != curve.WSOL_MINT and signed.get("funding_prefix_disjoint") is not True:
            raise _Unknown("amm_funding_prefix_unproved")
        return (pool, facts["pool_base_token_account"], facts["pool_quote_token_account"]), event
    raise _Unknown("unsupported_venue")


def _upper_bound(accounts, signed):
    pool, spend = signed["pool"], signed["spend_atomic"]
    if signed["venue"] == "pump_curve":
        raw = _raw(accounts, pool, curve.PUMP_PROGRAM)
        # Historical absent fields default to zero only at complete field boundaries. Beyond
        # holder_reward (125 bytes), fee counters and allocation capacity do not affect this bound.
        if (len(raw) < 125 and len(raw) not in (49, 81, 82, 83, 115, 123, 124)
                or raw[:8] != bytes.fromhex("17b7f83760d8ac60")
                or any(raw[offset] not in (0, 1) for offset in (48, 81, 82, 123, 124) if offset < len(raw))):
            raise _Unknown("curve_state_unmodelled")
        quote_mint = str(Pubkey.from_bytes(raw[83:115])) if len(raw) >= 115 else curve.SYSTEM_PROGRAM
        if signed.get("instruction_discriminator") == curve.BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR.hex():
            if len(raw) < 115 or quote_mint != signed["quote_mint"]:
                raise _Unknown("curve_state_unmodelled")
        elif quote_mint not in curve.SOL_QUOTE_MINTS:
            raise _Unknown("curve_state_unmodelled")
        v_token, v_quote, real_token = struct.unpack_from("<3Q", raw, 8)
        if min(v_token, v_quote) <= 0 or real_token > v_token or (raw[48] and real_token != 0):
            raise _Unknown("curve_reserves")
        gross_out = v_token * spend // (v_quote + spend)
        if (signed.get("instruction_discriminator") == curve.BUY_EXACT_QUOTE_IN_V3_DISCRIMINATOR.hex()
                and not raw[48] and gross_out >= real_token):
            # V3 may continue into synthetic migration; real_token is no longer an upper bound.
            # Total supply is conservative and keeps the independent market-movement check live.
            supply = struct.unpack_from("<Q", raw, 40)[0]
            if supply <= 0 or real_token > supply:
                raise _Unknown("curve_supply_unmodelled")
            return supply
        # A proven complete curve with zero inventory cannot satisfy a positive direct-buy floor.
        # Legacy SOL and quote-token V2 buys deduct nonnegative fees from this gross spend and
        # remain inventory-capped, even if the source used synthetic-migration V3. Offset16 is
        # virtual_quote_reserves in quote atoms; Token2022 transfer fees cannot increase output.
        # Source protocol-agent exact-out events are not inverted to infer any reserve here.
        return min(gross_out, real_token)
    raw = _raw(accounts, pool, amm.AMM_PROGRAM)
    facts = amm.parse_pool_account(base64.b64encode(raw).decode())
    expected = signed["pool_facts"]
    if any(facts[name] != expected[name] for name in (
            "base_mint", "quote_mint", "pool_base_token_account", "pool_quote_token_account")):
        raise _Unknown("pool_backbinding")
    reserves = []
    for key, mint, program in (
            (facts["pool_base_token_account"], signed["mint"], signed["base_token_program"]),
            (facts["pool_quote_token_account"], signed["quote_mint"], signed["quote_token_program"])):
        vault = _raw(accounts, key, program)
        if (len(vault) < 165 or vault[108] != 1
                or str(Pubkey.from_bytes(vault[:32])) != mint
                or str(Pubkey.from_bytes(vault[32:64])) != pool):
            raise _Unknown("vault_backbinding")
        reserves.append(struct.unpack_from("<Q", vault, 64)[0])
    base, quote = reserves
    quote += facts["virtual_quote"]
    if min(base, reserves[1]) <= 0 or not 0 < quote < 2**64:
        raise _Unknown("pool_reserves")
    # Gross spend is >= the program's net spend after nonnegative fees and its one-atom haircut.
    # Token transfer fees cannot increase the program's base_amount_out above this bound.
    return base * spend // (quote + spend)


def _market_reserves(accounts, signed):
    """Q/B from a validated exact pool vector, with no cached virtual-reserve substitute."""
    if signed["venue"] == "pump_curve":
        raw = _raw(accounts, signed["pool"], curve.PUMP_PROGRAM)
        base, quote = struct.unpack_from("<2Q", raw, 8)
        return quote, base
    facts = amm.parse_pool_account(base64.b64encode(
        _raw(accounts, signed["pool"], amm.AMM_PROGRAM)).decode())
    base, quote = [struct.unpack_from("<Q", _raw(accounts, key, program), 64)[0]
                   for key, program in (
                       (facts["pool_base_token_account"], signed["base_token_program"]),
                       (facts["pool_quote_token_account"], signed["quote_token_program"]))]
    return quote + facts["virtual_quote"], base


def _source_snapshot(tape, rings, buy):
    """Exact source-transaction post accounts; the event/cache alone cannot prove final spot."""
    bank = tape._banks.get(buy.slot)
    if type(bank) is not int or buy.slot in tape._bank_tainted:
        raise _Unknown("market_source_bank_unproved")
    signature = bytes(Signature.from_string(buy.signature))
    group_key = (buy.slot, bank, signature)
    group = tape._provenance_groups.get(group_key)
    accounts = {}
    for key, ring in rings.items():
        entries = tuple(reversed(ring or ())) + tuple(entry for name, entry in group or () if name == key)
        anchor = next((entry for entry in entries if entry[0] == buy.slot
                       and type(entry[1]) is int and entry[1] >= 0 and entry[2] is False
                       and isinstance(entry[3], dict) and entry[3].get("is_startup") is False
                       and type(entry[3].get("bank_id")) is int and entry[3]["bank_id"] == bank
                       and entry[3].get("txn_signature") == signature), None)
        if anchor is None:
            raise _Unknown("market_source_accounts_unproved")
        accounts[key] = _json(anchor[3])
    return accounts, (group_key, group)


def make_guard(tape, *, head_fn, clock=time.monotonic):
    """guard(buy, validity) -> {state: refuse|pass|unknown, reason, ...}; pass is not a quote.

    No writer lock, network, thread, DB, or shared-tape mutation. The returned closure owns only
    observational counters. Existing immutable account tuples and the tape's ancestry reader are
    reused; reconnects, changed account tuples and late-parent writes during the check go unknown.
    """
    stats = dict(checked=0, refused=0, unknown=0, not_proved_impossible=0, last_reason=None,
                 open_slot_checks=0, open_slot_hits=0,
                 open_slot_misses=dict.fromkeys(OPEN_MISS_REASONS, 0),
                 source_anchor_basis=dict.fromkeys(("raw_ring", "retained_group", "unproved"), 0))

    def result(state, reason, **evidence):
        stats["checked"] += 1
        stats[{"refuse": "refused", "unknown": "unknown", "pass": "not_proved_impossible"}[state]] += 1
        stats["last_reason"] = reason
        if evidence.get("source_anchor_basis") in stats["source_anchor_basis"]:
            stats["source_anchor_basis"][evidence["source_anchor_basis"]] += 1
        return dict(state=state, reason=reason, **evidence)

    def guard(buy, validity):
        from sniper.settings import memecoin as cfg
        from sniper.execution.entry_attribution import market_movement_check
        open_attempt = False
        evidence, anchor_snapshot, market_anchor_snapshot = {}, None, None
        market_cap = 0
        if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
            uid = getattr(buy, "source_user_id", None)
            caps = (cfg.copy_premium_cap(uid, "solana")[0],
                    cfg.solana_wallet_chase_cap_pct(uid, cfg.MEMECOIN_REAL_CHASE_MAX_PCT))
            market_cap = min((cap for cap in caps if cap and cap > 0), default=0)
            evidence["market_movement"] = market_movement_check(cap_pct=market_cap)
        try:
            if tape is None:
                raise _Unknown("tape_unavailable")
            signed = dict(validity["entry_slippage"])
            spend, floor = _integer(signed["spend_atomic"]), _integer(signed["min_tokens_atomic"])
            if _integer(signed["source_slot"]) != _integer(buy.slot):
                raise _Unknown("source_identity")
            generation = tape.generation
            revision = tape.provenance_revision
            acc, parents, head, tainted = tape._acc, tape._parent, tape.head, tape._tainted
            if not tape.connected or type(head) is not int:
                raise _Unknown("no_coverage")
            lane_head, seen = head_fn()
            age = clock() - float(seen)
            if (type(lane_head) is not int or not math.isfinite(age) or not 0 <= age <= MAX_HEAD_AGE_S):
                raise _Unknown("head_stale")
            if head < lane_head or head - lane_head > LAG_SLOTS:
                raise _Unknown("tape_lag")
            parent = parents.get(head)
            if head < buy.slot:
                raise _Unknown("state_before_source_or_coverage")
            keys = _keys(signed)
            rings = {key: acc.get(key) for key in keys}
            old_tainted = frozenset(tainted)
            # A newer relevant partial update can recover an old bad bank. Never fall back to
            # that historical veto; only a latest complete same-source-slot group can replace it.
            open_attempt = (type(parent) is not int or parent < buy.slot
                            or any(ring and ring[-1][0] > parent for ring in rings.values()))
            if open_attempt:
                stats["open_slot_checks"] += 1
                if head != buy.slot:
                    raise _Unknown("open_not_source_slot")
                bank = tape._banks.get(head)
                if type(bank) is not int or head in tape._bank_tainted:
                    raise _Unknown("open_bank_unproved")
                accounts, signature, anchor_basis, anchor_snapshot = _open_accounts(
                    rings, head, bank, buy.signature, tape._provenance_groups)
                _reference(buy, signed)
                selected = head
                evidence.update(witness="open_transaction", state_bank_id=bank,
                                state_tx_signature_hex=signature.hex(), source_anchor_basis=anchor_basis)
            else:
                if (tape.coverage_start is None or parent <= tape.coverage_start):
                    raise _Unknown("state_before_source_or_coverage")
                slot = parent
                for _ in range(ANCESTRY_MAX):
                    if slot == buy.slot:
                        break
                    slot = parents.get(slot)
                    if slot is None or slot < buy.slot:
                        raise _Unknown("source_not_on_ancestry")
                else:
                    raise _Unknown("source_ancestry_gap")
                _reference(buy, signed)
                # lookup() increments shared stats; its existing pure reader avoids that mutation.
                accounts, selected = tape._lookup(acc, parents, head, keys, head, lane_head, tainted,
                                                 extra_tainted=tape._bank_tainted)
                if accounts is None or selected != parent:
                    raise _Unknown("tape_" + str(selected))
                # RPC seeds cannot prove coverage since a vault's subscription. Only a selected
                # stream entry qualifies, even if an older seed remains in its ring.
                chain, slot = {parent}, parent
                oldest = min(entry[0] for ring in rings.values() for entry in ring)
                for _ in range(ANCESTRY_MAX):
                    if slot <= oldest:
                        break
                    slot = parents.get(slot)
                    if slot is None:
                        break
                    chain.add(slot)
                for ring in rings.values():
                    entry = next((entry for entry in reversed(ring) if entry[0] in chain), None)
                    if entry is None or entry[2] is not False:
                        raise _Unknown("seeded_reserve_uncovered")
            upper = _upper_bound(accounts, signed)
            movement = None
            if cfg.MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY:
                try:
                    source_accounts, market_anchor_snapshot = _source_snapshot(tape, rings, buy)
                    _upper_bound(source_accounts, signed)  # Same program/mint/vault validation.
                    movement = market_movement_check(source_reserves=_market_reserves(source_accounts, signed),
                                                     current_reserves=_market_reserves(accounts, signed),
                                                     cap_pct=market_cap)
                except (KeyError, TypeError, ValueError, OverflowError, struct.error):
                    # Missing movement must not erase an independently proven slippage veto.
                    movement = market_movement_check(cap_pct=market_cap)
            if (generation != tape.generation or not tape.connected or acc is not tape._acc
                    or revision != tape.provenance_revision or head != tape.head or tainted != old_tainted
                    or anchor_snapshot is not None and tape._provenance_groups.get(
                        anchor_snapshot[0]) is not anchor_snapshot[1]
                    or market_anchor_snapshot is not None and tape._provenance_groups.get(
                        market_anchor_snapshot[0]) is not market_anchor_snapshot[1]
                    or any(acc.get(key) is not rings[key] for key in keys)):
                raise _Unknown("open_state_changed" if open_attempt else "state_changed")
            if open_attempt:
                stats["open_slot_hits"] += 1
            if movement is not None:
                evidence["market_movement"] = movement
                if movement["state"] == "refuse":
                    return result("refuse", "intervening_market_cap", state_slot=selected,
                                  source_slot=buy.slot, **evidence)
            return result("refuse" if upper < floor else "pass",
                          "signed_minimum_unreachable" if upper < floor else "not_proved_impossible",
                          basis="fee_free_upper_bound", upper_bound_tokens_atomic=upper,
                          min_tokens_atomic=floor, spend_atomic=spend, state_slot=selected,
                          source_slot=buy.slot, generation=generation, head_slot=head, **evidence)
        except _Unknown as exc:
            if open_attempt:
                label = str(exc) if str(exc) in OPEN_MISS_REASONS else "open_source_or_state_unmodelled"
                stats["open_slot_misses"][label] += 1
            return result("unknown", str(exc), **{**evidence, **exc.evidence})
        except Exception:
            if open_attempt:
                stats["open_slot_misses"]["open_source_or_state_unmodelled"] += 1
            # A malformed optional witness cannot replace the existing funded policy.
            return result("unknown", "malformed_or_unavailable")

    guard.stats = stats
    return guard

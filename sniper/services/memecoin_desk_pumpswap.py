"""Pure PumpSwap research events. Pool identity is not mint metadata or pricing authority."""
from __future__ import annotations

import base64
import re
import struct
from collections import Counter

from solders.pubkey import Pubkey

from sniper.execution.pumpswap_buy import (
    AMM_PROGRAM, BUY_EVENT_DISCRIMINATOR, CREATE_POOL_EVENT_DISCRIMINATOR,
    WSOL_MINT, _BUY_EVENT_FIELDS, buy_event_gross_quote, create_pool_event,
    parse_buy_event, post_trade_reserves,
)
from sniper.execution.pumpswap_sell import SELL_EVENT_DISCRIMINATOR, parse_sell_event
from sniper.research.memecoin_opportunity.decoders import anchor_event_bytes
from sniper.services.memecoin_desk_universe import PUMP_PROGRAM
from sniper.services.memecoin_opportunity_live import program_data_logs

_INVOKE = re.compile(r'^Program (\S+) invoke \[(\d+)\]$')
_DONE = re.compile(r'^Program (\S+) (success|failed:.*)$')
_BUYS = {'buy', 'buy_exact_quote_in', 'buy_v2', 'buy_exact_quote_in_v2'}
_U64_MAX = 2**64 - 1
PUMP_AMM_IDL_SHA256 = 'b7d8c57a4d9c4dd0109a9ab893052352333252d4eedced10ac091d4d66cab89b'
# Pinned pump-public-docs/idl/pump_amm.json: these events change reserves without a proven trade state.
_STATE_CHANGES = {bytes.fromhex(disc) for disc in (
    '3f451c16305cc2b9', '78f83d531f8e6b90', 'ae7c4af90451f611',
    '82a42461e48287a5', '1609851aa02c47c0')}


SWEEP_POOL_FEE_DISCRIMINATOR = bytes.fromhex('82a42461e48287a5')        # SweepPoolFeeEvent: disc 8, timestamp 8, five pubkeys 160, amount 8, bucket 1
_SWEEP_LEN = 185
SWEEP_RECORDER_VERSION = 'sweep-rec-v1'


def sweep_events(logs):
    """Every SweepPoolFeeEvent in a complete, successful pAMM invocation stack, as dicts (ordinal = log index, pool, base_mint, quote_mint, recipient, payer, amount as an atomic
    DECIMAL STRING, bucket, event_time). One sweep emits one event per bucket for the same pool, so a pool's swept quote is the SUM over its events in the transaction. Records only; a sweep is still a
    decode loss (events() raises) and still censors until a reviewed rule says otherwise. Anything incomplete, foreign or of another length yields nothing for that event (unknown, never a guess)."""
    try:
        logs = list(logs)
    except TypeError:
        return []
    if not _complete(logs):
        return []
    out = []
    for ordinal, line in program_data_logs(logs, AMM_PROGRAM):
        try:
            raw = anchor_event_bytes(line)
        except (ValueError, TypeError, IndexError, struct.error):
            continue
        if raw[:8] != SWEEP_POOL_FEE_DISCRIMINATOR or len(raw) != _SWEEP_LEN:
            continue
        keys = [str(Pubkey.from_bytes(raw[16 + 32 * i:48 + 32 * i])) for i in range(5)]
        out.append(dict(ordinal=ordinal, pool=keys[0], base_mint=keys[1], quote_mint=keys[2], recipient=keys[3], payer=keys[4],
                        amount=str(struct.unpack_from('<Q', raw, 176)[0]), bucket=raw[184], event_time=struct.unpack_from('<q', raw, 8)[0]))
    return out


BOOST_BUY_AND_BURN_DISCRIMINATOR = bytes.fromhex('3f451c16305cc2b9')    # BoostBuyAndBurnEvent, 208 bytes
_BOOST_LEN = 208


def _confirm_boost(raw, found):
    """BoostBuyAndBurnEvent restates the pool state AFTER the burn buy; the same transaction carries that buy as an ordinary BuyEvent. Verified on a real transaction (fixture
    tests/fixtures/memecoin/pumpswap_boost_20261030/boost_buy_and_burn.json): its after-state equals the BuyEvent's post-trade reserves and both vault balances. So it adds no reserve
    information and is accepted ONLY when a decoded BuyEvent for the same pool in this transaction has exactly those post reserves; anything else is the same loss as before
    (decoder version ps-decode-v2: a boost no longer drops the valid buy it accompanies). Never raises for a match, never invents a row."""
    if len(raw) != _BOOST_LEN:
        raise DecodeLoss('pumpswap_virtual_quote_unproven')
    pool = str(Pubkey.from_bytes(raw[8 + 8 + 64:8 + 8 + 96]))                     # after timestamp, mint, bonding_curve
    off = 8 + 8 + 128 + 24 + 16                                                      # i128 virtual quote precedes the three trailing u64
    quote_after, base_after = struct.unpack_from('<QQ', raw, off)
    for _ordinal, event in reversed(found):
        if event['kind'] == 'buy' and event['pool'] == pool:
            post = (event['post_base_reserve_atomic'], event['post_quote_reserve_atomic'])
            virtual = int.from_bytes(raw[168:184], 'little', signed=True)      # strict pricing reads the buy's virtual quote
            if post == (str(base_after), str(quote_after)) and 0 <= virtual <= _U64_MAX \
                    and event.get('virtual_quote_atomic') == str(virtual):    # absent or different virtual state fails closed
                return
            break
    raise DecodeLoss('pumpswap_virtual_quote_unproven')


class DecodeLoss(ValueError):
    """An authenticated transaction may have lost a pool or swap fact; record a tape gap."""

    def __init__(self, reason, *, detail=None):
        super().__init__(reason)
        self.detail_reason = reason if detail is None else reason + ':' + detail


def _complete(logs):
    stack = []
    for line in logs:
        if not isinstance(line, str) or 'log truncated' in line.lower():
            return False
        invoke = _INVOKE.match(line)
        if invoke:
            if int(invoke[2]) != len(stack) + 1:
                return False
            stack.append(invoke[1])
        done = _DONE.match(line)
        if done:
            if not stack or stack.pop() != done[1] or done[2] != 'success':
                return False
    return not stack


def _canonical_pool(event):
    mint = bytes(Pubkey.from_string(event['base_mint']))
    authority = Pubkey.find_program_address([b'pool-authority', mint], Pubkey.from_string(PUMP_PROGRAM))[0]
    pool = Pubkey.find_program_address([b'pool', bytes(2), bytes(authority), mint,
                                       bytes(Pubkey.from_string(WSOL_MINT))],
                                      Pubkey.from_string(AMM_PROGRAM))[0]
    return (event['index'] == 0 and event['creator'] == str(authority)
            and event['quote_mint'] == WSOL_MINT and event['pool'] == str(pool))


def _virtual_quote(raw, offset, payload):
    if len(raw) <= offset:
        return None
    if len(raw) < offset + 16:
        raise ValueError('truncated virtual quote')
    value = int.from_bytes(raw[offset:offset + 16], 'little', signed=True)
    payload['virtual_quote_reserves'] = value
    return value


# Pinned by live tape 2026-10-28: 'Sell' sends lp-excluded protocol + creator fees out of the quote vault;
# 'SellV2' keeps them and sends only the buyback share. The SellEvent bytes alone cannot tell them apart.
_SELL_IXS = {'Sell': 'sell', 'SellV2': 'sell_v2'}
SELL_FEE_CAPTURE_VERSION = 'sell-fees-head360-ext392-v1'
SELL_FEE_CAPTURE_KEYS = ('lp_fee_basis_points','lp_fee','protocol_fee_basis_points','protocol_fee',
    'coin_creator_fee_basis_points','coin_creator_fee','cashback_fee_basis_points','cashback',
    'buyback_fee_basis_points','buyback_fee','sell_fee_capture_version','sell_fee_idl_sha256',
    'sell_fee_capture_reason','sell_fee_event_bytes')

BUY_EXACT_CAPTURE_VERSION = 'buy-exact-event-quotes-fees-v1'
BUY_EXACT_VALUE_KEYS = ('quote_amount_in','quote_amount_in_with_lp_fee','user_quote_amount_in',
    'max_quote_amount_in','min_base_amount_out',*SELL_FEE_CAPTURE_KEYS[:10])
BUY_EXACT_CAPTURE_KEYS = (*BUY_EXACT_VALUE_KEYS,'buy_quote_capture_version','buy_quote_idl_sha256',
    'buy_quote_capture_reason','buy_quote_event_bytes')



def instruction_names(logs):
    """{log index: anchor instruction name} for each pAMM 'Program data:' line, from its own frame's
    'Program log: Instruction: X' line. A frame without one maps to None."""
    stack, out = [], {}
    for index, line in enumerate(logs):
        invoke = _INVOKE.match(line)
        if invoke:
            stack.append([invoke[1], None, True])
            continue
        if stack and stack[-1][2]:
            stack[-1][2] = False       # only the frame's first log line may name its instruction
            if line.startswith('Program log: Instruction: '):
                stack[-1][1] = line[len('Program log: Instruction: '):]
                continue
        if line.startswith('Program data: ') and stack and stack[-1][0] == AMM_PROGRAM:
            out[index] = stack[-1][1]
            continue
        done = _DONE.match(line)
        if done and stack and stack[-1][0] == done[1]:
            stack.pop()
    return out


def sell_post_reserves(payload, raw, ix):
    """(base, quote) after a sell, proven only by the instruction name; None otherwise (never a guess)."""
    if ix not in _SELL_IXS.values() or len(raw) < 392:
        return None
    _cb_bps, cashback, _bb_bps, buyback = struct.unpack_from('<4Q', raw, 360)
    if ix == 'sell':
        out = payload['user_quote_amount_out'] + cashback + payload['protocol_fee'] + payload['coin_creator_fee']
    elif cashback:
        return None                    # SellV2 cashback is unverified on the tape
    else:
        out = payload['user_quote_amount_out'] + buyback
    base = payload['pool_base_token_reserves'] + payload['base_amount_in']
    quote = payload['pool_quote_token_reserves'] - out
    return (base, quote) if 0 <= base <= _U64_MAX and 0 <= quote <= _U64_MAX else None


def _decode(line, ix=None):
    raw = anchor_event_bytes(line)
    disc = raw[:8]
    if disc in _STATE_CHANGES:
        raise DecodeLoss('pumpswap_virtual_quote_unproven')
    if disc == CREATE_POOL_EVENT_DISCRIMINATOR:
        payload = create_pool_event([line])
        if payload is None:
            raise ValueError('truncated create pool')
        timestamp = struct.unpack_from('<q', raw, 8)[0]
        payload = {**payload, 'timestamp': timestamp, 'canonical': _canonical_pool(payload)}
        return {'kind': 'create_pool', 'pool': payload['pool'], 'actor': None,
                'event_time': timestamp, 'payload': payload, 'payload_b64': line[14:],
                'base_mint': payload['base_mint'], 'quote_mint': payload['quote_mint'],
                'pool_creator': payload['creator'], 'canonical': payload['canonical']}
    if disc == BUY_EVENT_DISCRIMINATOR:
        payload = parse_buy_event(line)
        # Preserve the full tail even when its appended signed virtual quote is understood.
        sizes = {'u64': 8, 'i64': 8, 'pubkey': 32, 'bool': 1}
        expected = 8 + sum(4 + len(payload[name].encode()) if kind == 'string' else sizes[kind]
                           for name, kind in _BUY_EVENT_FIELDS)
        quote = buy_event_gross_quote(payload)
        if payload['ix_name'] not in _BUYS or quote is None:
            category = ('unsupported_instruction' if payload['ix_name'] not in _BUYS
                        else 'inconsistent_gross_quote_' + payload['ix_name'])
            raise DecodeLoss('pumpswap_event_malformed', detail='unsupported_or_inconsistent_buy:' + category)
        if payload['ix_name']=='buy_exact_quote_in':
            for key in BUY_EXACT_VALUE_KEYS:payload.setdefault(key,None)
            payload.update(buy_quote_capture_version=BUY_EXACT_CAPTURE_VERSION,buy_quote_idl_sha256=PUMP_AMM_IDL_SHA256,
                buy_quote_event_bytes=len(raw),buy_quote_capture_reason=(
                'event_field_absent' if any(payload[k] is None for k in BUY_EXACT_VALUE_KEYS) else None))
        payload['unparsed_tail_b64'] = base64.b64encode(raw[expected:]).decode()
        virtual_quote = _virtual_quote(raw, expected, payload)
        base = payload['base_amount_out']
        try:
            post_base, post_quote = post_trade_reserves(payload)
            if not (0 <= post_base <= _U64_MAX and 0 <= post_quote <= _U64_MAX):
                raise ValueError('post reserves out of range')
        except ValueError:
            post_base = post_quote = None
        kind = 'buy'
    elif disc == SELL_EVENT_DISCRIMINATOR:
        payload = parse_sell_event(line[14:])
        if payload is None:
            raise ValueError('truncated sell')
        for offset, name in enumerate(('pool', 'user', 'user_base_token_account', 'user_quote_token_account',
                                       'protocol_fee_recipient', 'protocol_fee_recipient_token_account',
                                       'coin_creator')):
            payload[name] = str(Pubkey.from_bytes(raw[120 + offset * 32:152 + offset * 32]))
        # The shared sell parser specifies only the numeric head; retain every extension byte.
        payload['unparsed_tail_b64'] = base64.b64encode(raw[360:]).decode()
        for offset,name in enumerate(('cashback_fee_basis_points','cashback','buyback_fee_basis_points','buyback_fee')):
            at=360+offset*8
            payload[name]=struct.unpack_from('<Q',raw,at)[0] if len(raw)>=at+8 else None
        payload.update(sell_fee_capture_version=SELL_FEE_CAPTURE_VERSION,sell_fee_idl_sha256=PUMP_AMM_IDL_SHA256,
            sell_fee_event_bytes=len(raw),sell_fee_capture_reason=(None if len(raw)>=392 else
            'extension_absent' if len(raw)==360 else 'extension_partial'))
        virtual_quote = _virtual_quote(raw, 392, payload)
        base, quote = payload['base_amount_in'], payload['user_quote_amount_out']
        payload['ix_name'] = _SELL_IXS.get(ix)
        post_base, post_quote = sell_post_reserves(payload, raw, payload['ix_name']) or (None, None)
        kind = 'sell'
    else:
        return None
    # v2 retains fees and changes virtual quote; its event's post-state phase is not proven here.
    virtual_state = ({'virtual_quote_atomic': str(virtual_quote),
                      'virtual_quote_source': 'pumpswap_grpc:buy_event_virtual_quote'}
                     if kind == 'buy' and payload['ix_name'] in ('buy', 'buy_exact_quote_in')
                     and type(virtual_quote) is int and 0 <= virtual_quote <= _U64_MAX else {})
    payload.update(virtual_state)
    return {'kind': kind, 'pool': payload['pool'], 'actor': payload['user'],
            'coin_creator': payload['coin_creator'], 'event_time': payload['timestamp'],
            'base_atomic': str(base), 'quote_atomic': str(quote),
            'pre_base_reserve_atomic': str(payload['pool_base_token_reserves']),
            'pre_quote_reserve_atomic': str(payload['pool_quote_token_reserves']),
            'post_base_reserve_atomic': None if post_base is None else str(post_base),
            'post_quote_reserve_atomic': None if post_quote is None else str(post_quote),
            'lp_fee_bps': payload['lp_fee_basis_points'],
            'protocol_fee_bps': payload['protocol_fee_basis_points'],
            'creator_fee_bps': payload['coin_creator_fee_basis_points'],
            'cashback_fee_bps': payload.get('cashback_fee_basis_points'),
            'buyback_fee_bps': payload.get('buyback_fee_basis_points'),
            **virtual_state,
            'payload': payload, 'payload_b64': line[14:]}


def events(logs):
    """All known events with log ordinals, only from complete successful pAMM invocation stacks.

    The caller must also reject failed transaction metadata and turn DecodeLoss into a tape gap.
    Unknown discriminators are ignored; malformed recognized events invalidate the entire result.
    """
    try:
        logs = list(logs)
    except TypeError as exc:
        raise DecodeLoss('pumpswap_logs_unreadable') from exc
    if not _complete(logs):
        raise DecodeLoss('pumpswap_logs_incomplete')
    found = []
    names = instruction_names(logs)
    for ordinal, line in program_data_logs(logs, AMM_PROGRAM):
        try:
            raw = anchor_event_bytes(line)
        except (ValueError, TypeError, IndexError, struct.error):
            raw = b''
        if raw[:8] == BOOST_BUY_AND_BURN_DISCRIMINATOR:
            _confirm_boost(raw, found)          # matched to this transaction's own BuyEvent or the same loss as before
            continue
        try:
            event = _decode(line, names.get(ordinal))
        except DecodeLoss:
            raise
        except (ValueError, TypeError, IndexError, struct.error) as exc:
            known = {'truncated virtual quote', 'unsupported or inconsistent buy', 'truncated create pool',
                     'truncated sell', 'truncated borsh payload', 'invalid borsh bool', 'truncated Anchor event'}
            detail = str(exc).replace(' ', '_') if str(exc) in known else type(exc).__name__
            raise DecodeLoss('pumpswap_event_malformed', detail=detail) from exc
        if event is not None:
            found.append((ordinal, event))
    return found


# Pinned IDL pool-local instruction layouts: discriminator -> (pool account index, minimum accounts, data bytes).
# Global/config/admin changes deliberately have no scope proof. Remaining accounts are only accepted for the
# legacy trade builders whose optional pool-v2/buyback layout is independently pinned in pumpswap_buy/sell.
_POOL_INSTRUCTIONS = {
    '66063d1201daebea': (0, 23, 25), 'c62e1552b4d9e870': (0, 23, 25),
    'c2ab1c46684d5b2f': (0, 17, 24), 'b817ee6167c5d33d': (0, 17, 24),
    '33e685a4017f83ad': (0, 21, 24), '5df6823ce7e940b2': (0, 17, 24),
    'e992d18ecf6840bc': (0, 18, 70),
    '694406af000723a2': (0, 13, 24), 'f223c68952e1f2b6': (0, 15, 32),
    '8ce9215e845ac28f': (0, 14, 8), 'b712469c946da122': (0, 15, 32),
    '20f6bf3408c949ba': (2, 12, 8), '0830be07b644b7e5': (2, 12, 8),
}
_EVENT_CPI = bytes.fromhex('e445a52e51cb9a1d')  # Anchor event-CPI tag; emits an event, changes no pool state itself.
_EVENT_AUTHORITY = str(Pubkey.find_program_address([b'__event_authority'], Pubkey.from_string(AMM_PROGRAM))[0])
_MULTI_HOP = bytes.fromhex('2b644913e9f66f94')
# Pinned multi-hop fixed accounts; each following five-account group passes its pool/curve state key.
_MULTI_HOP_FIXED = {
    3: 'ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw',
    4: '5PHirr8joyTMp9JMm6nW7hNDVyEYdkzDqazxPD7RaTjx',
    7: 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
    8: 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
    9: '11111111111111111111111111111111', 10: _EVENT_AUTHORITY,
    11: AMM_PROGRAM, 12: PUMP_PROGRAM,
    13: '4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf',
    14: '8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt',
    15: 'Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1',
}


def _event_account_keys(logs, keys, instructions):
    """Complete account envelope for lost pinned AMM events, not a vault/state effect set."""
    if not 1 <= len(keys) <= 256:
        return None
    try:
        invoked = [m[1] for line in logs if (m := _INVOKE.match(line))]
        if any(program.startswith(('BPFLoader', 'LoaderV4')) for program in invoked):
            return None
        possible_pools, cpi_payloads, count = set(), Counter(), 0
        for ix in instructions:
            program, accounts, data = ix['program_id_index'], ix['accounts'], ix['data']
            if (type(program) is not int or not 0 <= program < len(keys)
                    or not isinstance(accounts, (list, tuple)) or len(accounts) > 256 or not isinstance(data, bytes)
                    or any(type(i) is not int or not 0 <= i < len(keys) for i in accounts)):
                return None
            if keys[program].startswith(('BPFLoader', 'LoaderV4')):
                return None
            if keys[program] != AMM_PROGRAM:
                continue
            count += 1
            if data[:8] == _EVENT_CPI:
                identities = [keys[i] for i in accounts]
                if identities not in ([_EVENT_AUTHORITY], [_EVENT_AUTHORITY, AMM_PROGRAM]) or len(data) < 16:
                    return None
                cpi_payloads[data[8:]] += 1
            elif data[:8] == _MULTI_HOP:
                n = (len(accounts) - 16) // 5
                if (len(data) != 24 or not 1 <= n <= 32 or (len(accounts) - 16) % 5
                        or any(keys[accounts[i]] != key for i, key in _MULTI_HOP_FIXED.items())):
                    return None
                volume = Pubkey.find_program_address(
                    [b'user_volume_accumulator', bytes(Pubkey.from_string(keys[accounts[0]]))],
                    Pubkey.from_string(AMM_PROGRAM))[0]
                if keys[accounts[5]] != str(volume):
                    return None
                possible_pools.update(keys[accounts[i]] for i in range(18, len(accounts), 5))
            else:
                layout = _POOL_INSTRUCTIONS.get(data[:8].hex())
                if layout is None:
                    return None
                pool_index, minimum, size = layout
                counts = range(minimum, minimum + 4) if data[:8].hex() in (
                    '66063d1201daebea', 'c62e1552b4d9e870', '33e685a4017f83ad') else (minimum,)
                if len(accounts) not in counts or len(data) != size:
                    return None
                possible_pools.add(keys[accounts[pool_index]])
        logged = invoked.count(AMM_PROGRAM)
        if not count or count != logged or len(possible_pools) > 32:
            return None
        payloads = Counter()
        for _, line in program_data_logs(logs, AMM_PROGRAM):
            raw = anchor_event_bytes(line)
            if len(raw) > 4096:
                return None
            if raw[:8] == BUY_EVENT_DISCRIMINATOR:
                event = parse_buy_event(line)
                if event['ix_name'] not in _BUYS | {'multi_hop_swap'}:
                    return None
                sizes = {'u64': 8, 'i64': 8, 'pubkey': 32, 'bool': 1}
                prefix = 8 + sum(4 + len(event[name].encode()) if kind == 'string' else sizes[kind]
                                 for name, kind in _BUY_EVENT_FIELDS)
                # Appended bytes stay opaque and participate in the exact CPI/log comparison.
                if len(raw) < prefix + 49 or raw[prefix + 16] not in (0, 1):
                    return None
                pool = event['pool']
            elif raw[:8] == SELL_EVENT_DISCRIMINATOR:
                if len(raw) < 441 or raw[408] not in (0, 1) or parse_sell_event(line[14:]) is None:
                    return None
                pool = str(Pubkey.from_bytes(raw[120:152]))
            elif raw[:8] == CREATE_POOL_EVENT_DISCRIMINATOR:
                if len(raw) < 344 or any(raw[i] not in (0, 1) for i in (333, 342, 343)):
                    return None
                event = create_pool_event([line])
                if event is None:
                    return None
                pool = event['pool']
            else:  # State-change/future event shapes keep the original global loss in this profile.
                return None
            if pool not in possible_pools:
                return None
            payloads[raw] += 1
        if payloads and payloads == cpi_payloads:
            return sorted(set(keys))
    except (ValueError, TypeError, IndexError, KeyError, struct.error):
        return None
    return None


# Instructions that write only accounts they name (no global config): user volume accumulator init/sync/close,
# coin-creator fee collect/transfer (v1, v2) and extend_account. Admin/config instructions stay global.
_LOCAL_IXS = {bytes.fromhex(d) for d in ('5e06ca73ff60e8b7', '561fc057a3574fee', 'f945a4da9667548a',
                                         'a039592ab58b2b42', '8b348655e4e56cf1', '01214eb921432c5c', 'ea66c2cb96483ee5')}
_LOCAL_EVENTS = {bytes.fromhex(d) for d in ('86240d48e86582d8', 'c57aa77c74515bff', '929fbdac925838f4',
                                            'e8f5c2eeeada3a59', '875b3870b4b893c9')} | _STATE_CHANGES
# Creator-fee transfers emit no event; pin their witnessed account counts and eight-byte ABI.
_SILENT_LOCAL_IXS = {bytes.fromhex('8b348655e4e56cf1'): 10, bytes.fromhex('01214eb921432c5c'): 12}


def _swap_tx_account_keys(logs, keys, instructions, *, extend_shadow=False):
    """Every resolved key of a swap-only transaction, or None. A transaction can touch only accounts in its complete
    key list, so a pool absent from it is untouched. Only known pool/swap or account-local AMM instructions, one known
    event per non-silent AMM call, no loader: admin/config instructions (which can move every pool) stay global."""
    if not 1 <= len(keys) <= 256:
        return None
    try:
        invoked = [m[1] for line in logs if (m := _INVOKE.match(line))]
        if any(program.startswith(('BPFLoader', 'LoaderV4')) for program in invoked):
            return None
        calls, silent_calls, cpi_payloads = 0, 0, Counter()
        for ix in instructions:
            program, accounts, data = ix['program_id_index'], ix['accounts'], ix['data']
            if (type(program) is not int or not 0 <= program < len(keys)
                    or not isinstance(accounts, (list, tuple)) or len(accounts) > 256 or not isinstance(data, bytes)
                    or any(type(i) is not int or not 0 <= i < len(keys) for i in accounts)):
                return None
            if keys[program].startswith(('BPFLoader', 'LoaderV4')):
                return None
            if keys[program] != AMM_PROGRAM:
                continue
            if data[:8] == _EVENT_CPI:
                if [keys[i] for i in accounts] not in ([_EVENT_AUTHORITY], [_EVENT_AUTHORITY, AMM_PROGRAM]) or len(data) < 16:
                    return None
                cpi_payloads[data[8:]] += 1
            elif data[:8] in _SILENT_LOCAL_IXS:
                if len(data) != 8 or len(accounts) != _SILENT_LOCAL_IXS[data[:8]]:
                    return None
                calls += 1
                silent_calls += 1
            elif data[:8].hex() in _POOL_INSTRUCTIONS or data[:8] == _MULTI_HOP or data[:8] in _LOCAL_IXS:
                calls += 1
            else:                                       # admin/config or future instruction: may move every pool
                return None
        if not calls or calls + sum(cpi_payloads.values()) != invoked.count(AMM_PROGRAM):   # event CPIs self-invoke
            return None
        payloads = Counter()
        for _, line in program_data_logs(logs, AMM_PROGRAM):
            raw = anchor_event_bytes(line)
            if (raw[:8] not in (BUY_EVENT_DISCRIMINATOR, SELL_EVENT_DISCRIMINATOR) and raw[:8] not in _LOCAL_EVENTS
                    and not (extend_shadow and raw[:8] == _EXTEND_EVENT and len(raw) == 96)
                    or len(raw) > 4096):
                return None
            payloads[raw] += 1
        if sum(payloads.values()) != calls - silent_calls or payloads != cpi_payloads:
            return None
        return sorted(set(keys))
    except (ValueError, TypeError, IndexError, KeyError, struct.error):
        return None


_EXTEND_EVENT = bytes.fromhex('6161d7905d92167c')
_EXTEND_IX = bytes.fromhex('ea66c2cb96483ee5')
EXTEND_SHADOW_VERSION = 'extend-account-96-cpi-complete-keys-v1'


def extend_account_shadow_keys(logs, *, account_keys, instructions, inner_complete, idl_sha256):
    """Candidate only. Never used by loss_details or any effective loss reader."""
    if inner_complete is not True or idl_sha256 != PUMP_AMM_IDL_SHA256:
        return None
    try:
        logs=list(logs)
        if not _complete(logs):return None
        keys=[str(Pubkey.from_bytes(k)) if isinstance(k,bytes) else str(Pubkey.from_string(k)) for k in account_keys]
        names=instruction_names(logs)
        count=0
        for ordinal,line in program_data_logs(logs,AMM_PROGRAM):
            raw=anchor_event_bytes(line)
            if raw[:8]==_EXTEND_EVENT:
                if len(raw)!=96 or names.get(ordinal)!='ExtendAccount':return None
                count+=1
        calls=0
        for ix in instructions:
            if keys[ix['program_id_index']]==AMM_PROGRAM and ix['data'][:8]==_EXTEND_IX:
                if len(ix['data'])!=8 or len(ix['accounts'])!=5:return None
                calls+=1
        if not count or count!=calls:return None
        return _swap_tx_account_keys(logs,keys,instructions,extend_shadow=True)
    except (ValueError,TypeError,IndexError,KeyError,struct.error):
        return None


def loss_details(logs, *, reason, account_keys, instructions, inner_complete):
    """Compact unsampled diagnostic + complete affected-pool proof, or unknown.

    This runs only after events() discarded the transaction. It must account for every outer/CPI instruction;
    no first-event inference, partial ALT resolution, unknown layout or truncated invocation grants pool scope.
    """
    out = {'reason': reason[:160], 'scope': 'unknown', 'pools': [], 'invocations': 0,
           'discriminator': '', 'event_length': None}
    keys = None
    def unknown(stage, data=None, accounts=None):
        suffix = 'scope_' + stage
        if data is not None:
            suffix += '_' + data[:8].hex() + '_a%d_d%d' % (len(accounts), len(data))
        out['reason'] = reason[:159-len(suffix)] + ':' + suffix
        if stage == 'unknown_instruction' and data[:8] == _MULTI_HOP and inner_complete is True:
            envelope = _event_account_keys(logs, keys, instructions)
            if envelope is not None:
                out['event_account_keys'] = envelope
        if 'event_account_keys' not in out and keys is not None and inner_complete is True and out['invocations'] >= 1:
            envelope = _swap_tx_account_keys(logs, keys, instructions)
            if envelope is not None:
                out['swap_tx_account_keys'] = envelope
        return out

    try:
        logs = list(logs)
    except TypeError:
        return unknown('logs_unreadable')
    if any(not isinstance(line, str) for line in logs):
        return unknown('logs_incomplete')
    event_discs = {BUY_EVENT_DISCRIMINATOR, SELL_EVENT_DISCRIMINATOR, CREATE_POOL_EVENT_DISCRIMINATOR} | _STATE_CHANGES
    for _, line in program_data_logs(logs, AMM_PROGRAM):
        try:
            raw = anchor_event_bytes(line)
            if not out['discriminator']:
                out.update(discriminator=raw[:8].hex(), event_length=len(raw))
            try:
                _decode(line)
            except (DecodeLoss, ValueError, TypeError, IndexError, struct.error):
                out.update(discriminator=raw[:8].hex(), event_length=len(raw))
                break
        except (ValueError, TypeError):
            continue
    if not inner_complete:
        return unknown('inner_incomplete')
    if not _complete(logs):
        return unknown('logs_incomplete')
    if not isinstance(instructions, (list, tuple)):
        return unknown('instructions_unreadable')
    if len(instructions) > 128:
        return unknown('instruction_overflow')
    try:
        raw_keys = list(account_keys)
        if any(isinstance(k, bytes) and len(k) != 32 for k in raw_keys):
            return unknown('metadata_malformed')
        keys = [str(Pubkey.from_bytes(k)) if isinstance(k, bytes) else str(Pubkey.from_string(k)) for k in raw_keys]
        pools, count = set(), 0
        for ix in instructions:
            program_index = ix['program_id_index']
            if type(program_index) is not int or not 0 <= program_index < len(keys):
                return unknown('program_index')
            program = keys[program_index]
            accounts, data = ix['accounts'], ix['data']
            if not isinstance(accounts, (list, tuple)) or not isinstance(data, bytes):
                return unknown('instruction_shape')
            if any(type(i) is not int or not 0 <= i < len(keys) for i in accounts):
                return unknown('account_index', data, accounts)
            if program != AMM_PROGRAM:
                continue
            count += 1
            out['invocations'] = count
            if data[:8] == _EVENT_CPI:
                identities = [keys[i] for i in accounts]
                if (identities not in ([_EVENT_AUTHORITY], [_EVENT_AUTHORITY, AMM_PROGRAM])
                        or len(data) < 16 or data[8:16] not in event_discs):
                    return unknown('event_cpi', data, accounts)
                continue
            layout = _POOL_INSTRUCTIONS.get(data[:8].hex())
            if layout is None:
                return unknown('unknown_instruction', data, accounts)
            pool_index, minimum, size = layout
            allowed_counts = range(minimum, minimum + 4) if data[:8].hex() in (
                '66063d1201daebea', 'c62e1552b4d9e870', '33e685a4017f83ad') else (minimum,)
            if len(accounts) not in allowed_counts:
                return unknown('layout_accounts', data, accounts)
            if len(data) != size:
                return unknown('layout_data', data, accounts)
            pools.add(keys[accounts[pool_index]])
            if len(pools) > 32:
                return unknown('pool_overflow')
        # Missing inner instructions, extra log invocations or unknown events imply incomplete scope.
        logged = sum(1 for line in logs if (m := _INVOKE.match(line)) and m[1] == AMM_PROGRAM)
        if not count or count != logged:
            return unknown('invocation_count_n%d_l%d' % (count, logged))
        for _, line in program_data_logs(logs, AMM_PROGRAM):
            if anchor_event_bytes(line)[:8] not in event_discs:
                return unknown('unknown_event')
        if pools:
            out.update(scope='complete', pools=sorted(pools))
        else:
            return unknown('no_pool')
    except (ValueError, TypeError, IndexError, KeyError):
        return unknown('metadata_malformed')
    return out

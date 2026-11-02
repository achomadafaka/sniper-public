"""Exact routed PS pool membership only. Never returns swaps, prices or reserves."""
from __future__ import annotations

import struct

from solders.pubkey import Pubkey

from sniper.services import memecoin_desk_pumpswap as P
from sniper.services import memecoin_desk_universe as U
from sniper.research.memecoin_opportunity.pumpfun import decode_pumpfun_event

PROOF = 'multi_hop_legs_v1'
_PUMP_ROUTE = bytes.fromhex('e19a7516d756f667')


def _require(condition, reason):
    if not condition:
        raise P.DecodeLoss('pumpswap_route_unproven', detail=reason)


def _frames(logs, keys, instructions):
    """Bind each PS/Pump invoke to its ordered compiled instruction, including event CPI."""
    programs = (P.AMM_PROGRAM, U.PUMP_PROGRAM)
    queues = {program: [i for i in instructions if keys[i['program_id_index']] == program] for program in programs}
    positions = dict.fromkeys(programs, 0)
    tops = {i['outer_ix']: keys[i['program_id_index']] for i in instructions if i['stack_height'] == 1}
    frames, stack = [], []
    for ordinal, line in enumerate(logs):
        invoke = P._INVOKE.match(line)
        if invoke:
            program, depth = invoke[1], int(invoke[2])
            frame = dict(program=program, ordinal=ordinal, parent=stack[-1] if stack else None, data=[], ix=None)
            if program in programs:
                pos = positions[program]
                _require(pos < len(queues[program]), 'extra_invocation')
                ix = queues[program][pos]
                positions[program] += 1
                _require(ix['stack_height'] == depth, 'invocation_depth')
                ancestor = stack[0]['program'] if stack else program
                _require(tops.get(ix['outer_ix']) == ancestor, 'outer_identity')
                if stack and stack[-1]['ix'] is not None:
                    _require(ix['outer_ix'] == stack[-1]['ix']['outer_ix'], 'outer_parent')
                frame['ix'] = ix
                frames.append(frame)
            stack.append(frame)
        elif line.startswith('Program data: '):
            _require(bool(stack), 'orphan_event')
            stack[-1]['data'].append((ordinal, line))
        elif P._DONE.match(line):
            stack.pop()  # complete successful stack was checked before binding
    _require(all(positions[p] == len(queues[p]) for p in programs), 'missing_invocation')
    return frames


def _pump_group(frame, group, keys, all_frames):
    """A canonical curve and its own program-bound TradeEvent prove a non-PS route group."""
    ix = frame['ix']
    accounts = [keys[i] for i in ix['accounts']]
    if (ix['data'][:8] != _PUMP_ROUTE or len(ix['data']) != 21 or len(accounts) != 18
            or accounts[13:16] != list(group[:3]) or U.bonding_curve(accounts[13]) != group[2]):
        return None
    if (accounts[0] != P._MULTI_HOP_FIXED[13] or accounts[1] != P._MULTI_HOP_FIXED[14]
            or accounts[10] != P._MULTI_HOP_FIXED[9] or accounts[11] != P._MULTI_HOP_FIXED[15]
            or accounts[12] != U.PUMP_PROGRAM):
        return None
    children = [f for f in all_frames if f['parent'] is frame]
    _require(len(children) == 1, 'pump_event_count')
    child = children[0]
    _require(child['ix']['data'].startswith(P._EVENT_CPI) and not child['data'] and len(frame['data']) == 1, 'pump_event_cpi')
    _require([keys[i] for i in child['ix']['accounts']] == [accounts[11]], 'pump_event_authority')
    ordinal, line = frame['data'][0]
    _require(ordinal < child['ordinal'] and child['ix']['data'][8:] == P.anchor_event_bytes(line), 'pump_event_bytes')
    trade = decode_pumpfun_event(U.PUMP_PROGRAM, line)
    _require(trade.blocker is None and trade.event_kind.value in ('buy', 'sell')
             and trade.token_id == accounts[13], 'pump_trade_identity')
    return ordinal


def prove_pools(logs, *, account_keys, instructions, inner_complete):
    """Complete exact PS pool set, or DecodeLoss. Unknown roles never become foreign proof."""
    try:
        logs = list(logs)
        _require(inner_complete is True and P._complete(logs), 'capture_incomplete')
        _require(1 <= len(account_keys) <= 256 and 1 <= len(instructions) <= 128, 'metadata_bounds')
        keys = [str(Pubkey.from_string(k)) if isinstance(k, str) else str(Pubkey.from_bytes(bytes(k)))
                for k in account_keys]
        outer, seen_tops = None, set()
        for ix in instructions:
            _require(type(ix['program_id_index']) is int and 0 <= ix['program_id_index'] < len(keys)
                     and type(ix['outer_ix']) is int and ix['outer_ix'] >= 0
                     and type(ix['stack_height']) is int and 1 <= ix['stack_height'] <= 64
                     and isinstance(ix['data'], bytes)
                     and all(type(a) is int and 0 <= a < len(keys) for a in ix['accounts']), 'instruction_shape')
            if ix['stack_height'] == 1:
                _require(ix['outer_ix'] not in seen_tops and (outer is None or ix['outer_ix'] > outer), 'outer_order')
                outer = ix['outer_ix']
                seen_tops.add(outer)
            else:
                _require(outer == ix['outer_ix'], 'instruction_parent')
        _require(P._event_account_keys(logs, keys, instructions) is not None, 'account_envelope')
        frames = _frames(logs, keys, instructions)
        ps = [f for f in frames if f['program'] == P.AMM_PROGRAM]
        roots = [f for f in ps if not f['ix']['data'].startswith(P._EVENT_CPI)]
        _require(any(f['ix']['data'].startswith(P._MULTI_HOP) for f in roots), 'not_multi_hop')
        pools = set()
        for root in roots:
            _require(len(root['data']) == len([f for f in ps if f['parent'] is root]), 'unbound_ps_event')
            _require(not root['parent'] or root['parent']['program'] != P.AMM_PROGRAM, 'reentrant_ps')
            children = [f for f in ps if f['parent'] is root]
            events = []
            for index, child in enumerate(children):
                _require(child['ix']['data'].startswith(P._EVENT_CPI) and not child['data'], 'event_cpi')
                _require([keys[i] for i in child['ix']['accounts']] in
                         ([P._EVENT_AUTHORITY], [P._EVENT_AUTHORITY, P.AMM_PROGRAM]), 'event_authority')
                ordinal, line = root['data'][index]
                _require(ordinal < child['ordinal'] and (index+1 == len(children)
                         or child['ordinal'] < root['data'][index+1][0]), 'event_cpi_order')
                raw = P.anchor_event_bytes(line)
                _require(child['ix']['data'][8:] == raw, 'event_bytes')
                _require(raw[:8] == P.BUY_EVENT_DISCRIMINATOR, 'unsupported_leg_event')
                event = P.parse_buy_event(line)
                gross = event['quote_amount_in']
                _require(gross > 0 and gross == event['quote_amount_in_with_lp_fee'] + event['protocol_fee']
                         + event['coin_creator_fee'] + event['cashback'], 'gross_quote')
                events.append((ordinal, event))
            accounts = [keys[a] for a in root['ix']['accounts']]
            if root['ix']['data'].startswith(P._MULTI_HOP):
                groups = [accounts[i:i+5] for i in range(16, len(accounts), 5)]
                _require(all(a[0] == b[1] for a, b in zip(groups, groups[1:])), 'mint_chain')
                consumed, previous, last_ordinal = 0, None, -1
                pump_children = [f for f in frames if f['parent'] is root and f['program'] == U.PUMP_PROGRAM]
                used_pump = set()
                for group in groups:
                    if consumed < len(events) and events[consumed][1]['pool'] == group[2]:
                        ordinal, event = events[consumed]
                        _require(event['ix_name'] == 'multi_hop_swap' and ordinal > last_ordinal, 'event_order')
                        if previous is not None:
                            _require(previous['base_amount_out'] == event['quote_amount_in'], 'amount_chain')
                        pools.add(event['pool'])
                        consumed += 1
                        previous, last_ordinal = event, ordinal
                    else:
                        matches = [(f, _pump_group(f, group, keys, frames)) for f in pump_children
                                   if f['ordinal'] not in used_pump]
                        matches = [(f, at) for f, at in matches if at is not None]
                        _require(len(matches) == 1 and matches[0][1] > last_ordinal, 'missing_group')
                        used_pump.add(matches[0][0]['ordinal'])
                        previous, last_ordinal = None, matches[0][1]
                _require(consumed == len(events) and len(used_pump) == len(pump_children), 'extra_group_event')
            else:
                layout = P._POOL_INSTRUCTIONS.get(root['ix']['data'][:8].hex())
                _require(layout is not None and len(events) == 1, 'direct_leg')
                _require(events[0][1]['pool'] == accounts[layout[0]], 'direct_pool')
                pools.add(events[0][1]['pool'])
        _require(len(ps) == len(roots) + sum(len(f['data']) for f in ps) and 1 <= len(pools) <= 32,
                 'invocation_completeness')
        return sorted(pools)
    except P.DecodeLoss:
        raise
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, struct.error, OverflowError) as exc:
        raise P.DecodeLoss('pumpswap_route_unproven', detail='metadata_malformed') from exc

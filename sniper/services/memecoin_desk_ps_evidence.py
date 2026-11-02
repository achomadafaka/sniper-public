"""Local-tape intake evidence; modeled PS round trip, never a quote, submission or RPC.

Only legacy WSOL states with independently known fee components and no cashback are
modeled. Mint identity is bound by the caller's existing batch read, not by token suffix.
"""
from __future__ import annotations

import base64
import bisect
import math
import json
import re
import struct
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from sniper.services import memecoin_desk_contract as K
from sniper.services import memecoin_desk_safety as S
from sniper.services import memecoin_desk_capture as CAP
from sniper.services import memecoin_desk_ps_tape as PT
from sniper.services import memecoin_desk_ps_losses as PSL
from sniper.services import memecoin_desk_lessons as L
from sniper.services import memecoin_desk_ps_asof as PSA
from sniper.services.memecoin_desk_ps_asof import VQ_SOURCE, RESERVE_MAX_AGE_S, SOL_MAX_AGE_S, _int_text, _state_indexes
from sniper.services.memecoin_desk_asof import StaleSnapshot

TOKEN_PROGRAM = S.TOKEN_PROGRAM
TOKEN_2022_PROGRAM = S.TOKEN_2022_PROGRAM
U64 = 2**64 - 1


def iso(at):
    return datetime.fromtimestamp(at, timezone.utc).isoformat()


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def uint(value, *, positive=False):
    if type(value) is not int or not int(positive) <= value <= U64:
        raise ValueError('atomic_units_unknown')
    return value


def ceil_fee(amount, bps):
    return (amount * bps + 9999) // 10000


def round_trip(*, base, real_quote, virtual_quote, fees, spend):
    """Atomic legacy exact-base buy within the clip budget, then full modeled sell.

    The maximum affordable net input pays individually rounded fees. The vault receives
    the net input plus LP fees; protocol/creator fees leave it. No real-vault clipping
    invents an executable sell. Unspent budget is reported but not credited: loss_pct is a conservative full-clip
    cost bound. Rent, network fees and future fee changes are excluded.
    """
    base, real_quote, virtual_quote, spend = (uint(base, positive=True), uint(real_quote, positive=True),
                                            uint(virtual_quote), uint(spend, positive=True))
    if (not isinstance(fees, (tuple, list)) or len(fees) != 4
            or any(type(f) is not int or not 0 <= f < 10000 for f in fees)
            or sum(fees) >= 10000 or fees[-1] != 0):
        raise ValueError('pool_fee_unknown')
    # Exact-base buy under a quote budget: each fee rounds independently, so the
    # aggregate-rate division alone can overspend by several lamports.
    lo, hi = 0, spend
    while lo < hi:
        mid = (lo+hi+1)//2
        if mid+sum(ceil_fee(mid, f) for f in fees) <= spend:
            lo = mid
        else:
            hi = mid-1
    net_for_tokens = min(lo, spend*10000//(10000+sum(fees))-1)
    tokens = base*net_for_tokens//(real_quote+virtual_quote+net_for_tokens)
    if not 0 < tokens < base:
        raise ValueError('dust_buy')
    net = ((real_quote+virtual_quote)*tokens+base-tokens-1)//(base-tokens)
    actual_spend = net+sum(ceil_fee(net, f) for f in fees)
    cash_left = spend-actual_spend
    if net <= 0 or cash_left < 0:
        raise ValueError('fee_rounding_budget')
    post_quote = real_quote + net + ceil_fee(net, fees[0])
    uint(post_quote)
    gross = (post_quote + virtual_quote) * tokens // base
    if gross > post_quote:
        raise ValueError('real_quote_insufficient')
    back = gross - sum(ceil_fee(gross, f) for f in fees)
    if not 0 < back <= actual_spend:
        raise ValueError('dust_or_invalid_sell')
    numerator=(spend-back)*100
    loss=numerator/spend
    n,d=loss.as_integer_ratio()
    if n*spend<numerator*d:loss=math.nextafter(loss,math.inf)
    return {'tokens_atomic': str(tokens), 'quote_back_atomic': str(back),
            'post_buy_quote_atomic': str(post_quote), 'quote_spent_atomic': str(actual_spend),
            'cash_left_atomic': str(cash_left), 'loss_pct': loss}


def parse_mint(account):
    """Validate the full SPL/TLV boundary before reusing the existing authority parser."""
    if not isinstance(account, dict) or account.get('executable') is not False:
        raise ValueError('mint_unreadable')
    data = account.get('data')
    if not isinstance(data, list) or len(data) != 2 or data[1] != 'base64':
        raise ValueError('mint_encoding')
    try:
        raw = base64.b64decode(data[0], validate=True)
    except (TypeError, ValueError):
        raise ValueError('mint_encoding') from None
    owner = account.get('owner')
    if owner not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
        raise ValueError('mint_owner')
    if (len(raw) < 82 or raw[45] != 1 or raw[44] > 18
            or any(struct.unpack_from('<I', raw, at)[0] not in (0, 1) for at in (0, 46))):
        raise ValueError('mint_layout')
    if owner == TOKEN_PROGRAM and len(raw) != 82:
        raise ValueError('mint_layout')
    if len(raw) != 82:
        if len(raw) < 166 or any(raw[82:165]) or raw[165] != 1:
            raise ValueError('mint_layout')
        seen, pos = set(), 166
        while pos < len(raw):
            if len(raw)-pos < 4:
                raise ValueError('mint_tlv_truncated')
            kind, size = struct.unpack_from('<HH', raw, pos)
            if kind == 0:
                if any(raw[pos:]):
                    raise ValueError('mint_tlv_padding')
                break
            if kind in seen or size > len(raw)-pos-4:
                raise ValueError('mint_tlv_truncated_or_duplicate')
            if kind == S.EXT_TRANSFER_HOOK and size != 64:
                raise ValueError('mint_hook_layout')
            if kind == 18 and size != 64:
                raise ValueError('mint_metadata_pointer_layout')
            if kind not in S.BENIGN_EXTENSIONS | {S.EXT_TRANSFER_HOOK}:
                raise ValueError('extension_%d' % kind)
            seen.add(kind)
            pos += 4+size
    parsed = S.parse_mint(account)
    if parsed['supply'] <= 0:
        raise ValueError('mint_supply_unknown')
    return {k: parsed[k] for k in ('authority_mint', 'authority_freeze', 'transfer_hook')} | {
        'supply_atomic': str(parsed['supply']), 'decimals': raw[44]}


def _intake(candidate):
    claim = candidate.get('intake')
    if not isinstance(claim, dict): return False
    keys = {'feed','filter','provider_rank','rank_kind','observed_at','host_received_at','clock_offset_s','intake_row_ref'}
    return (set(claim) == keys and isinstance(claim['feed'], str) and claim['feed'] in ('pumpfun','dexscreener','fomo')
            and isinstance(claim['filter'], str) and bool(K._MACHINE.fullmatch(claim['filter']))
            and (claim['provider_rank'] is None or type(claim['provider_rank']) is int and claim['provider_rank'] >= 1)
            and claim['rank_kind'] in (None, 'list_position')
            and all(finite(claim[k]) for k in ('observed_at','host_received_at','clock_offset_s'))
            and min(claim['observed_at'],claim['host_received_at']) >= 0
            and isinstance(claim['intake_row_ref'], str) and bool(K._OPPORTUNITY.fullmatch(claim['intake_row_ref'])))


def _load_state(candidate, desk_dir, as_of, evaluated, *, integrity=False):
    mint, pool = candidate['mint'], candidate['pool']
    if candidate.get('network_id') != 'solana' or pool != PT.canonical_pool(mint):
        raise ValueError('pool_identity')
    source = candidate['snapshot']['fields']['pool_state']
    state = source['value']
    intake = _intake(candidate)
    if not isinstance(state, dict) or source.get('completeness') not in (('complete','partial') if intake else ('complete',)):
        raise ValueError('pool_state_incomplete')
    at = state.get('reserve_known_at')
    if not finite(at) or not 0 <= as_of-at <= RESERVE_MAX_AGE_S:
        raise ValueError('pool_state_stale')
    if (state.get('quote_mint') != PT.WSOL_MINT
            or state.get('virtual_quote_source') != VQ_SOURCE
            or state.get('virtual_quote_known_at') != at
            or state.get('virtual_quote_slot') != state.get('slot')):
        raise ValueError('pool_reserve_proof_unknown')
    if (not finite(source.get('known_at')) or source['known_at']>as_of
            or state.get('map_known_at',float('inf'))>at):
        raise ValueError('pool_metadata_future')
    for name in (('map_known_at',) if intake and state.get('metadata_known_at') is None else ('map_known_at', 'metadata_known_at')):
        if not finite(state.get(name)) or not 0 <= state[name] <= as_of:
            raise ValueError('pool_metadata_unknown')
    try:
        beat=json.loads((Path(desk_dir)/'universe.heartbeat').read_text())
    except (OSError,ValueError,TypeError):
        raise ValueError('ps_capture_unproven') from None
    if not isinstance(beat,dict):
        raise ValueError('ps_gap_pending')
    pending=beat.get('ps_pending_gaps',{}) if integrity else {}
    if integrity:
        if (not isinstance(pending,dict) or type(beat.get('ps_gap_pending')) is not bool
                or bool(pending)!=beat['ps_gap_pending']):
            raise ValueError('ps_gap_pending')
    elif beat.get('ps_gap_pending') is not False:
        raise ValueError('ps_gap_pending')
    if not integrity:
        proof = CAP.capture_proof(desk_dir, evaluated, venues=('ps',), window_s=evaluated-at)
        if proof is None or proof['from'] > at or proof['through'] < as_of:
            raise ValueError('ps_capture_unproven')
    inclusive_cut=math.nextafter(as_of,math.inf)
    conn=PT._ro(desk_dir)
    if conn is None:raise ValueError('pool_tape_unreadable')
    try:
        if integrity:
            schemas=PSA._attach_days(conn,desk_dir,evaluated)
            conn.execute('BEGIN')
            try:
                _state_indexes(conn)
            except StaleSnapshot:
                raise ValueError('ps_tape_index_unknown') from None
        derived = intake and state.get('map_source') == 'intake_canonical_pda'
        if derived and conn.execute('SELECT 1 FROM pools WHERE pool=? AND known_at<=? LIMIT 1',(pool,as_of)).fetchone():
            raise ValueError('pool_binding_exists')
        columns='received_at,slot,tx_index,payload_json,map_status,mint,quote_mint'
        cut_slot=None
        if integrity:
            committed=beat.get('ps_committed_through')
            if not finite(committed) or not 0<=committed<=evaluated:
                raise ValueError('ps_commit_unproven')
            context_cut=math.nextafter(min(evaluated,committed),math.inf)
            blocks=PSA._day_rows(conn,schemas,'blocks',
                'SELECT slot,received_at FROM {table} WHERE received_at<? ORDER BY slot DESC LIMIT 1',(context_cut,))
            if (not blocks or any(type(s) is not int or s<=0 or not finite(t) or not 0<=t<=evaluated for s,t in blocks)):
                raise ValueError('block_unknown')
            block_slot,block_known=max(blocks)
            cut_slot=block_slot+1
            if type(state.get('slot')) is not int or not 0<state['slot']<cut_slot:
                raise ValueError('pool_block_cut_unknown')
            # Initial emissions do not carry an AS cut. Validate the block when actually learned; never backdate it.
            state=dict(state,_market_cut={'receipt_cut':as_of,'cut_slot':cut_slot,'block_known_at':block_known})
            columns+=',is_buy,post_base_reserve_atomic,post_quote_reserve_atomic,lp_fee_bps,protocol_fee_bps,creator_fee_bps,cashback_fee_bps'
            try:
                if PSL.losses(desk_dir,pool,at,inclusive_cut,cut_slot=cut_slot,pending=pending,connection=conn):
                    raise ValueError('pool_tape_gap')
            except PSL.LossUnreadable:
                raise ValueError('pool_loss_unreadable') from None
            proof=CAP.capture_proof(desk_dir,evaluated,venues=('ps',),window_s=evaluated-at,
                                    pool=pool,integrity=True,connection=conn,dependency_cut=as_of)
            if proof is None or proof['from']>at or proof['through']<as_of:
                raise ValueError('ps_capture_unproven')
        columns+=',map_known_at'
        tips=PT.window_rows(conn,columns,pool,PT.retained_from(conn,pool,inclusive_cut),inclusive_cut,
                            cut_slot=cut_slot,limit=1,order='slot DESC,tx_index DESC,ordinal DESC')
        tip=tips[0] if tips else None
        binding='create_pool'
        if (tip is not None and not derived and tip[4]=='mapped_curve_derived' and tip[5]==mint and tip[6]==PT.WSOL_MINT
                and finite(tip[-1]) and 0<=tip[-1]<=tip[0]):
            binding=_curve_binding(conn,pool,mint,tip[1],tip[0])
        ambiguous=conn.execute("SELECT 1 FROM swaps_ps WHERE pool=? AND slot=? AND received_at<? GROUP BY tx_index HAVING tx_index IS NULL OR typeof(tx_index)!='integer' OR tx_index<=0 OR count(DISTINCT signature)>1 LIMIT 1",(pool,state['slot'],inclusive_cut)).fetchone()
        split_signature=conn.execute("SELECT 1 FROM swaps_ps WHERE pool=? AND slot=? AND received_at<? GROUP BY signature HAVING count(DISTINCT tx_index)>1 LIMIT 1",(pool,state['slot'],inclusive_cut)).fetchone()
    finally:conn.close()
    if ambiguous or split_signature:raise ValueError('pool_order_unknown')
    if (tip is None or tip[0]!=at or tip[1]!=state.get('slot') or type(tip[2]) is not int or tip[2]<=0
            or PT._proof(tip[3])!=int(state['virtual_quote_atomic'])
            or tip[4:7] != (('unmapped',None,None) if derived else
                            (('mapped_curve_derived' if binding!='create_pool' else 'mapped'),mint,PT.WSOL_MINT))
            or derived and state['map_known_at'] != at):
        raise ValueError('pool_tip_unproven')
    if integrity:
        # The independently proven latest buy supplies its own complete reserves and VQ. No historical carry,
        # next-row witness, decimal default or unbounded learning scan participates in this hard-filter bound.
        parts=tuple(_int_text(v) for v in tip[8:10])+(PT._proof(tip[3]),)
        fees=tuple(_int_text(v) for v in tip[10:14])
        expected=tuple(int(state[k]) for k in ('base_reserve_atomic','quote_reserve_atomic','virtual_quote_atomic'))
        expected_fees=tuple(state[k] for k in ('lp_fee_bps','protocol_fee_bps','creator_fee_bps'))+(0,)
        if (tip[7]!=1 or any(v is None for v in parts+fees) or min(parts[:2])<=0
                or parts!=expected or fees!=expected_fees or sum(fees)>=10000):
            raise ValueError('pool_snapshot_mismatch')
        return dict(state,_binding_kind=binding),parts,fees
    invalid = []
    rows = L.load_pool(desk_dir, mint, as_of-RESERVE_MAX_AGE_S, inclusive_cut, invalid_states=invalid)
    if rows is None:
        raise ValueError('pool_tape_unreadable')
    if derived:
        # The exact canonical PDA binds WSOL locally; never rewrite shared tape rows.
        rows = [dict(row, quote_mint=PT.WSOL_MINT) if row.get('quote_mint') is None else row for row in rows]
    tape = L._LearningTape(L.Tape([], rows, coverage_until=as_of, pool_invalid_states=invalid))
    j = bisect.bisect_right(tape.pool_t, as_of)-1
    if j < 0 or tape.pool_value_at(as_of, 1) is None:
        raise ValueError(next(iter(sorted(tape.used_errors)), 'pool_state_unknown'))
    latest = tape.pool[j]
    if latest.get('from_next'):
        raise ValueError('pool_next_state_unproven')
    parts = (int(state['base_reserve_atomic']), int(state['quote_reserve_atomic']),
             int(state['virtual_quote_atomic']))
    fees = tuple(state[k] for k in ('lp_fee_bps', 'protocol_fee_bps', 'creator_fee_bps'))+(0,)
    if (at != latest['t'] or parts != (latest['base'], latest['rq'], latest['vq'])
            or fees != tuple(latest['bps'])):
        raise ValueError('pool_snapshot_mismatch')
    return dict(state, _binding_kind=binding), parts, fees


def _curve_binding(conn, pool, mint, slot, received):
    """Cohort D Amendment 23 (Gamma): a 'mapped_curve_derived' tip binds only under ps_paper's Am19 checks: every
    graduation row for the pool names this mint and WSOL, strictly earlier slot, known by the swap; any create_pool row
    must agree. Returns binding_kind curve_derived_checked (a create_pool row agrees) or curve_derived_unchecked.
    Tripwire: one create_pool row anywhere that disagrees with its curve-derived binding stops accepting unchecked
    bindings until reviewed."""
    grads = conn.execute('SELECT mint,quote_mint,slot,known_at FROM ps_grads WHERE pool=? LIMIT 101', (pool,)).fetchall()
    if not (0 < len(grads) <= 100 and all(m == mint and q == PT.WSOL_MINT and type(s) is int and s < slot
                                           and finite(k) and 0 <= k <= received for m, q, s, k in grads)):
        raise ValueError('pool_binding_unknown')
    # Complete, bounded aggregate over every create_pool row of the pool (Delta HOLD d2e26800a: never a truncated read)
    total, bad = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(NOT (mint IS ? AND quote_mint IS ? AND canonical IS 1 "
        "AND typeof(known_at) IN ('real','integer') AND known_at>=0 AND known_at<=?)),0) "
        "FROM pools WHERE pool=? AND (known_at<=? OR known_at IS NULL OR typeof(known_at) NOT IN ('real','integer'))",
        (mint, PT.WSOL_MINT, received, pool, received)).fetchone()
    if total:
        if bad:
            raise ValueError('pool_binding_unknown')
        return 'curve_derived_checked'
    if conn.execute('SELECT 1 FROM ps_grads g JOIN pools p ON p.pool=g.pool '
                    'WHERE p.mint IS NOT g.mint OR p.quote_mint IS NOT g.quote_mint LIMIT 1').fetchone():
        raise ValueError('curve_binding_tripwire')
    return 'curve_derived_unchecked'


def _pool_inputs(candidate, desk_dir, as_of_host, evaluated, clip_usd, sol_usd, sol_at, integrity):
    if not finite(sol_usd) or sol_usd <= 0: raise ValueError('sol_usd_unknown')
    if not finite(sol_at): raise ValueError('sol_usd_unknown')
    if sol_at > as_of_host: raise ValueError('sol_usd_future')
    if not 0 <= as_of_host-sol_at <= SOL_MAX_AGE_S: raise ValueError('sol_usd_stale')
    if not finite(clip_usd) or clip_usd <= 0: raise ValueError('clip_invalid')
    return _load_state(candidate, desk_dir, as_of_host, evaluated, integrity=integrity)


def _pool_reason(exc):
    return str(exc) if isinstance(exc, ValueError) and re.fullmatch(r'[a-z][a-z0-9_]{0,100}',str(exc)) else 'pool_unreadable'


def pool_refusal(candidate, *, desk_dir, as_of_host, clip_usd, sol_usd, sol_at, evaluated_at_host=None, integrity=False):
    """The pool/FX part of pumpswap_evidence alone, with no mint account: its refusal reason, or None when it passes.
    Local tape only, so the intake assembler runs it before the paid mint read (a pass still needs the full evidence)."""
    evaluated = as_of_host if evaluated_at_host is None else evaluated_at_host
    try:
        _pool_inputs(candidate, desk_dir, as_of_host, evaluated, clip_usd, sol_usd, sol_at, integrity)
    except (ValueError, TypeError, KeyError, OverflowError, OSError, sqlite3.Error, PT.TapeUnreadable) as exc:
        return _pool_reason(exc)
    return None


def pumpswap_evidence(candidate, *, desk_dir, as_of_host, clip_usd, sol_usd, sol_at,
                       mint_account, mint_known_at=None, evaluated_at_host=None, integrity=False):
    """Existing hard-filter envelope. Pool/FX use frozen cut; mint proof uses actual read time.

    Unknown soft facts are never zeros or substitutes for a mandatory proof. The caller
    may replace soft unknowns with separately bound facts; this module collects none.
    """
    evaluated = as_of_host if evaluated_at_host is None else evaluated_at_host
    def unknown(reason): return {'status': 'unknown', 'reason': reason}
    checks = {k: unknown('not_collected') for k in ('market_data', 'token_safety', 'sell_probe', 'holders', 'cluster', 'dump_group')}
    valid_clock = finite(as_of_host) and finite(evaluated) and 0 <= as_of_host <= evaluated
    known = max(as_of_host, mint_known_at) if valid_clock and finite(mint_known_at) and 0 <= mint_known_at <= evaluated else evaluated
    out = {k: candidate.get(k) for k in ('network_id', 'mint', 'pool')}
    out.update(snapshot_hash=candidate.get('snapshot', {}).get('hash'),
               known_at=iso(known) if finite(known) and known >= 0 else None, checks=checks)
    try:
        if not valid_clock:
            raise ValueError('clock_invalid')
        if not finite(mint_known_at): raise ValueError('mint_clock_unknown')
        if mint_known_at > evaluated: raise ValueError('mint_future')
        if not 0 <= evaluated-mint_known_at <= SOL_MAX_AGE_S: raise ValueError('mint_stale')
        parsed = parse_mint(mint_account)
        context = mint_account.get('context_slot')
        slot = candidate['snapshot']['fields']['pool_state']['value']['slot']
        if type(context) is not int or context < slot or context <= 0:
            raise ValueError('mint_context_stale')
        checks['token_safety'] = {'status': 'pass', 'reason': 'mint_read', 'mint_context_slot': context, **parsed}
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        reason = str(exc) if isinstance(exc, ValueError) else 'mint_unreadable'
        checks.update({k: unknown(reason) for k in ('token_safety', 'market_data', 'sell_probe')})
        return out
    try:
        state, (base, quote, virtual), fees = _pool_inputs(candidate, desk_dir, as_of_host, evaluated, clip_usd,
                                                           sol_usd, sol_at, integrity)
        state=dict(state)
        binding=state.pop('_binding_kind','create_pool')
        if integrity:
            out['market_cut']=state.pop('_market_cut')
            known=max(known,out['market_cut']['block_known_at'])
            out['known_at']=iso(known)
        if any(state.get(key) != parsed[field] and not (_intake(candidate) and state.get(key) is None)
               for key, field in (('supply_atomic','supply_atomic'),('base_decimals','decimals'))):
            raise ValueError('mint_supply_mismatch')
        if (state.get('sol_usd')!=sol_usd or not finite(state.get('sol_age_s'))
                or not 0<=state['sol_age_s']<=SOL_MAX_AGE_S):
            raise ValueError('sol_usd_snapshot_mismatch')
        # Am23: the binding kind rides in the closed {status, reason} row; strata are never pooled
        checks['market_data'] = {'status': 'pass', 'reason': 'ps_tape' if binding == 'create_pool' else 'ps_tape_' + binding}
        if parsed['transfer_hook'] is not None: raise ValueError('transfer_hook_model_unknown')
        spend = int((Decimal(str(clip_usd))*Decimal(10**9)/Decimal(str(sol_usd))).to_integral_value(rounding=ROUND_FLOOR))
        model = round_trip(base=base, real_quote=quote, virtual_quote=virtual, fees=fees, spend=spend)
        checks['sell_probe'] = {'status': 'pass', 'reason': 'pumpswap_math_bound', 'round_trip_loss_pct': model['loss_pct'], 'exact_quantity': True}
    except (ValueError, TypeError, KeyError, OverflowError, OSError, sqlite3.Error, PT.TapeUnreadable) as exc:
        reason = _pool_reason(exc)
        checks['sell_probe'] = unknown(reason)
        if checks['market_data']['status'] != 'pass': checks['market_data'] = unknown(reason)
    return out

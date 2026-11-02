"""Read-only PumpSwap paper prices at a frozen host cut; no RPC, signing or ledger writes.

Only same-event legacy buy reserves/virtual proof and zero-cashback fees are supported.
The engine owns admitted mint safety/decimals, delays, FX, retries and prior marks.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
from pathlib import Path

from sniper.services import memecoin_desk_capture as C
from sniper.services import memecoin_desk_ps_asof as A
from sniper.services import memecoin_desk_ps_evidence as E
from sniper.services import memecoin_desk_ps_tape as T
from sniper.services.memecoin_desk_asof import StaleSnapshot


ENTRY_CARRY_MAX_AGE_S=120.   # Gamma 2026-10-30: dump-stratum calibration has n>=100 only under 120 s


def _pool_binding(conn,pool,mint,status,row_mint,row_quote,map_at,received,slot,cut,at,integrity=False):
    """The same identity proof for a strict quote and an estimated mark: this swap is this coin against SOL, known before the swap."""
    maps=conn.execute("SELECT mint,quote_mint,canonical,known_at FROM pools WHERE pool=? AND (known_at<=? OR known_at IS NULL OR typeof(known_at) NOT IN ('real','integer'))"+(' LIMIT 20001' if integrity else ''),(pool,at)).fetchall()
    if integrity and len(maps)>20000: raise ValueError('pool_map_row_limit')
    if status=='unmapped' and row_mint is None and row_quote is None and not maps:
        binding='intake_canonical_pda'
    elif (status=='mapped' and row_mint==mint and row_quote==T.WSOL_MINT
          and E.finite(map_at) and 0<=map_at<=received
          and maps and all(m==mint and q==T.WSOL_MINT and canonical==1
                           and E.finite(known) and 0<=known<=received for m,q,canonical,known in maps)):
        binding='create_pool'
    elif status=='mapped_curve_derived' and row_mint==mint and row_quote==T.WSOL_MINT and E.finite(map_at) and 0<=map_at<=received:
        # D Amendment 19: the graduation binds the pool, strictly before this swap; a create_pool row must agree.
        grads=conn.execute('SELECT mint,quote_mint,slot,known_at FROM ps_grads WHERE pool=? LIMIT 101',(pool,)).fetchall()
        if not (0<len(grads)<=100 and all(m==mint and q==T.WSOL_MINT and type(s) is int and s<slot
                                           and E.finite(k) and 0<=k<=received for m,q,s,k in grads)):
            raise ValueError('pool_binding_unknown')
        if maps and not all(m==mint and q==T.WSOL_MINT and canonical==1 and E.finite(known) and 0<=known<=received
                            for m,q,canonical,known in maps):
            raise ValueError('pool_binding_unknown')
        binding='curve_derived'
    else: raise ValueError('pool_binding_unknown')
    return binding,maps


def _state(desk_dir, mint, at, evaluated_at, *, integrity=False, carry_sell_tip=False, live_now=None, detail=None):
    if not all(E.finite(t) for t in (at,evaluated_at)) or not 0 <= at <= evaluated_at:
        raise ValueError('clock_invalid')
    beat=json.loads((Path(desk_dir)/'universe.heartbeat').read_text())
    if (not isinstance(beat,dict) or (not integrity and beat.get('ps_gap_pending') is not False)
            or not E.finite(beat.get('ts')) or not E.finite(beat.get('ps_committed_through'))
            or beat['ps_committed_through'] > beat['ts']):
        raise ValueError('ps_commit_unproven')
    if at>beat['ps_committed_through']:     # retry the same cut next tick (Alpha 2026-10-28); never clamp or backdate
        raise ValueError('frontier_not_committed')
    pool=T.canonical_pool(mint)
    conn=A._ro(T.tape_path(desk_dir))
    if conn is None: raise ValueError('ps_tape_missing')
    cut=math.nextafter(at,math.inf)
    try:
        schemas=A._attach_days(conn,desk_dir,at)
        conn.execute('BEGIN')
        if integrity:
            A._state_indexes(conn)
        blocks=A._day_rows(conn,schemas,'blocks','SELECT slot,received_at FROM {table} WHERE received_at<? ORDER BY slot DESC LIMIT 1',(cut,))
        if not blocks: raise ValueError('no_block_before_as_of')
        if any(type(s) is not int or s<=0 or not E.finite(t) or t<0 for s,t in blocks):
            raise ValueError('block_proof_unknown')
        block=max(blocks)
        floor=T.retained_from(conn,pool,cut)
        # One read: the highest-slot receipt is either past the block cut (unproven) or the tip.
        tips=T.window_rows(conn,A._COLS,pool,floor,cut,limit=1,order='slot DESC,tx_index DESC,ordinal DESC')
        tip=tips[0] if tips else None
        if tip is None: raise ValueError('no_swaps_before_cut')
        if tip[0]>=block[0]+1: raise ValueError('pool_block_cut_unproven')
        (slot,sig,ordinal,index,is_buy,base,real,lp,proto,creator,received,map_at,status,row_mint,row_quote,payload,cashback)=tip
        # Within the approved retained window, capture proof and gap scans cover [received, at].
        if not E.finite(received) or not 0<=at-received:
            raise ValueError('pool_state_stale')
        tip_start=start=min(received,block[1])
        source=None
        if is_buy==0 and carry_sell_tip:   # a buy trades against current reserves whichever side the last swap was; only the quote is carried
            source=T._proof_before(conn,pool,cut,identity=True)
            if source is None: raise ValueError('pool_tip_unproven')
            if not E.finite(source['received_at']) or evaluated_at-source['received_at']>ENTRY_CARRY_MAX_AGE_S:
                raise ValueError('entry_carry_stale')
            start=min(start,source['received_at'])   # capture and gap proof cover the carried quote's whole span
        def captured(since):
            got=C.capture_proof(desk_dir,evaluated_at,venues=('ps',),window_s=evaluated_at-since,
                                **({'pool':pool,'integrity':True,'connection':conn,'dependency_cut':at} if integrity else {}),live_now=live_now,why=detail)
            if got is not None and not (got['through']>=at and got['from']<=since):
                if detail is not None:detail.append('capture_window_short')
                return None
            return got
        proof=captured(start)
        if proof is None:
            # A hole only in the carried span never heals; one covering the tip is the usual transient refusal.
            raise ValueError('entry_carry_span_gap' if source is not None and captured(tip_start) is not None else 'ps_capture_unproven')
        if type(index) is not int or index<=0 or type(ordinal) is not int or ordinal<0:
            raise ValueError('pool_order_unknown')
        ambiguous=conn.execute("SELECT 1 FROM swaps_ps WHERE pool=? AND slot=? AND received_at<? GROUP BY tx_index HAVING tx_index IS NULL OR typeof(tx_index)!='integer' OR tx_index<=0 OR count(DISTINCT signature)>1 LIMIT 1",(pool,slot,cut)).fetchone()
        split=conn.execute('SELECT 1 FROM swaps_ps WHERE pool=? AND slot=? AND received_at<? GROUP BY signature HAVING count(DISTINCT tx_index)>1 LIMIT 1',(pool,slot,cut)).fetchone()
        if ambiguous or split: raise ValueError('pool_order_unknown')
        binding,maps=_pool_binding(conn,pool,mint,status,row_mint,row_quote,map_at,received,slot,cut,at,integrity)
        check=binding if binding!='curve_derived' else 'create_pool_agrees' if maps else 'unchecked'
        if source is not None:
            bound=conn.execute('SELECT mint,quote_mint,map_status,map_known_at FROM swaps_ps WHERE signature=? AND ordinal=? AND slot=? AND pool=? LIMIT 1',(source['signature'],source['ordinal'],source['slot'],pool)).fetchone()
            if bound is None: raise ValueError('pool_binding_unknown')
            _pool_binding(conn,pool,mint,bound[2],bound[0],bound[1],bound[3],source['received_at'],source['slot'],cut,at,integrity)
        gaps=A._day_rows(conn,schemas,'gaps','SELECT at FROM {table} WHERE at>=? AND at<=?',(start,at))
        if gaps: raise ValueError('capture_gap' if source is None or any(g[0]>=tip_start for g in gaps) else 'entry_carry_span_gap')
        lp,proto,creator,fee_basis=A.resolve_fees(conn,pool,slot,received,lp,proto,creator)
    finally:
        conn.close()
    virtual=source['value'] if source is not None else T._proof(payload)
    if source is None and (is_buy!=1 or virtual is None): raise ValueError('pool_tip_unproven')
    base,real=(A._int_text(x) for x in (base,real))
    if base is None or real is None: raise ValueError('pool_reserve_unknown')
    E.uint(base,positive=True);E.uint(real,positive=True);E.uint(virtual)
    fees=tuple(A._int_text(str(f)) if f is not None else None for f in (lp,proto,creator,cashback))
    if any(f is None or f>=10000 for f in fees) or sum(fees)>=10000 or fees[-1]!=0:
        raise ValueError('pool_fee_unknown')
    kind=binding if check!='unchecked' else 'curve_derived_unchecked'    # D Am19 (c): reads stratify by it
    carried={} if source is None else dict(quote_source_signature=source['signature'],quote_source_ordinal=source['ordinal'],quote_source_slot=source['slot'],
                                           carried_age_s=at-source['received_at'],carried_age_at_observation_s=evaluated_at-source['received_at'],
                                           newest_swap_age_s=at-received,newest_swap_age_at_observation_s=evaluated_at-received)
    if source is not None:kind='carried'
    return {'state_at':received,'pool':pool,'binding_kind':kind,'proof':{'as_of_host':at,'evaluated_at_host':evaluated_at,
            'committed_through':proof['through'],'slot':slot,'signature':sig,'ordinal':ordinal,
            'tx_index':index,'map_source':binding,'binding_check':check,'tip_side':'buy' if is_buy==1 else 'sell',**carried,**({} if fee_basis=='tip' else {'fee_basis':fee_basis})}},(base,real,virtual),fees


def _quote(side,desk_dir,mint,amount,at,evaluated_at,*,integrity=False,carry_sell_tip=False,live_now=None,detail=None):
    try:
        E.uint(amount,positive=True)
        out,(base,real,virtual),fees=_state(desk_dir,mint,at,evaluated_at,integrity=integrity,carry_sell_tip=carry_sell_tip,live_now=live_now,detail=detail)
        if side=='buy':
            model=E.round_trip(base=base,real_quote=real,virtual_quote=virtual,fees=fees,spend=amount)
            out.update(tokens_atomic=model['tokens_atomic'],spend_lamports=model['quote_spent_atomic'],cash_left_atomic=model['cash_left_atomic'])
        else:
            # This leg uses the observed exit state, never a persisted hypothetical entry reserve.
            gross=(real+virtual)*amount//(base+amount)
            if gross>real: raise ValueError('real_quote_insufficient')
            net=gross-sum(E.ceil_fee(gross,f) for f in fees)
            if net<0: raise ValueError('dust_or_invalid_sell')
            out['lamports']=str(net)
        return out,None
    except (ValueError,TypeError,KeyError,OverflowError,OSError,sqlite3.Error,T.TapeUnreadable,StaleSnapshot) as exc:
        reason=str(exc) if isinstance(exc,(ValueError,StaleSnapshot)) else 'pool_unreadable'
        return None,reason


ESTIMATE_TIP_MAX_AGE_S=60.
ESTIMATE_CARRY_MAX_AGE_S=300.


def estimate_sell(desk_dir,mint,qty_atomic,at,*,observed_at=None):
    """A mark, not a fill: the pool's newest recorded swap's own reserves, buy or sell, with the virtual quote that swap proves, or carried from the newest proving buy. Skips the capture and loss proof, so lost swaps can make it stale; the bounds and labels say so. Never for a fill or an entry."""
    try:
        E.uint(qty_atomic,positive=True)
        observed=at if observed_at is None else observed_at   # the bounds run to when the rail decides, not to the cut
        pool=T.canonical_pool(mint)
        conn=A._ro(T.tape_path(desk_dir))
        if conn is None:return None,'ps_tape_missing'
        cut=math.nextafter(at,math.inf)
        try:
            floor=T.retained_from(conn,pool,cut)
            tips=T.window_rows(conn,A._COLS,pool,floor,cut,limit=1,order='slot DESC,tx_index DESC,ordinal DESC')
            if not tips:return None,'no_swaps_before_cut'
            slot,sig,_o,_i,_b,base,real,lp,proto,creator,received,_m,status,row_mint,row_quote,payload,cashback=tips[0]
            # Skipping the loss proof never skips identity: this swap must be this coin against SOL.
            try:_pool_binding(conn,pool,mint,status,row_mint,row_quote,_m,received,slot,cut,at)
            except ValueError:return None,'estimate_identity_unknown'
            if not E.finite(received) or not 0<=at-received or observed-received>ESTIMATE_TIP_MAX_AGE_S:return None,'estimate_tip_stale'
            virtual=T._proof(payload);carried_at=None;source=sig
            if virtual is None:
                proof=T._proof_before(conn,pool,cut,identity=True)
                if proof is None:return None,'estimate_no_carry'
                virtual,carried_at,source=proof['value'],proof['received_at'],proof['signature']
                bound=conn.execute('SELECT mint,quote_mint,map_status,map_known_at FROM swaps_ps WHERE signature=? AND ordinal=? AND slot=? AND pool=? LIMIT 1',(source,proof['ordinal'],proof['slot'],pool)).fetchone()
                try:
                    if bound is None:raise ValueError('pool_binding_unknown')
                    _pool_binding(conn,pool,mint,bound[2],bound[0],bound[1],bound[3],carried_at,proof['slot'],cut,at)
                except ValueError:return None,'estimate_identity_unknown'
                if not E.finite(carried_at) or observed-carried_at>ESTIMATE_CARRY_MAX_AGE_S:return None,'estimate_carry_stale'
            lp,proto,creator,fee_basis=A.resolve_fees(conn,pool,slot,received,lp,proto,creator)
            losses=conn.execute('SELECT count(*) FROM ps_decode_losses WHERE received_at>=? AND received_at<?',(received,cut)).fetchone()[0]
        finally:conn.close()
        base,real=(A._int_text(x) for x in (base,real))
        if base is None or real is None:return None,'pool_reserve_unknown'
        E.uint(base,positive=True);E.uint(real,positive=True);E.uint(virtual)
        fees=tuple(A._int_text(str(f)) if f is not None else None for f in (lp,proto,creator,cashback))
        if any(f is None or f>=10000 for f in fees) or sum(fees)>=10000:return None,'pool_fee_unknown'
        gross=(real+virtual)*qty_atomic//(base+qty_atomic)
        if gross>real:return None,'real_quote_insufficient'
        net=gross-sum(E.ceil_fee(gross,f) for f in fees)
        if net<0:return None,'dust_or_invalid_sell'
        return dict(pool=pool,lamports=str(net),binding_kind='carried' if carried_at is not None else 'estimated',state_at=received,
                    proof=dict(**({} if fee_basis=='tip' else {'fee_basis':fee_basis}),newest_swap_age_s=at-received,newest_swap_age_at_observation_s=observed-received,carried_age_s=None if carried_at is None else at-carried_at,carried_age_at_observation_s=None if carried_at is None else observed-carried_at,quote_source_signature=source,tape_loss_receipts_n=losses,estimate=True)),None
    except (ValueError,TypeError,KeyError,OverflowError,OSError,sqlite3.Error,T.TapeUnreadable,StaleSnapshot) as exc:
        return None,str(exc) if isinstance(exc,(ValueError,StaleSnapshot)) else 'pool_unreadable'


def buy(desk_dir,mint,spend_lamports,at,*,evaluated_at,integrity=False):
    """Atomic modeled buy within clip; returned actual spend plus cash left equals clip."""
    return _quote('buy',desk_dir,mint,spend_lamports,at,evaluated_at,integrity=integrity)


def entry_buy(desk_dir,mint,spend_lamports,at,*,evaluated_at,integrity=False,live_now=None,detail=None):
    """The Office entry: a sell tip uses newest reserves with a carried quote; every other check is buy's. Kill switch OFFICE_ENTRY_CARRIED=0."""
    return _quote('buy',desk_dir,mint,spend_lamports,at,evaluated_at,integrity=integrity,carry_sell_tip=os.environ.get('OFFICE_ENTRY_CARRIED','1')!='0',live_now=live_now,detail=detail)


def sell(desk_dir,mint,qty_atomic,at,*,evaluated_at,integrity=False,live_now=None,detail=None):
    """Exact full-quantity modeled sell; proven zero differs from unavailable proof."""
    return _quote('sell',desk_dir,mint,qty_atomic,at,evaluated_at,integrity=integrity,live_now=live_now,detail=detail)

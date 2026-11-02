"""Host-only evaluated PAPER order admission. No model, network or funded capabilities.

The caller supplies committed blind Judge rows and a validated host Trader context.
Store bundle freezing and ledger admission are separate commits; deterministic identity
and the atomic Trader receipt make a crash between them safely retryable.
"""
import hashlib
import json
import sqlite3

from . import ledger as L, probe as P, store as S, trader as T
from .tools import sheet as SH

PODS_RULE={'version':'office-paper-eval-1-pods-v1',
    'score':{'metric':'realized_net_usd','window_periods':7,'min_closed':5},
    'step_pct':10.,'floor_pct':10.,'min_transfer_usd':25.,
    'max_concurrent_total':3,'daily_loss_usd_total':100.}


def _result(state,reason=None,*,order_id=None,bundle_hash=None,outcome=None):
    return dict(state=state,reason=reason,order_id=order_id,bundle_hash=bundle_hash,outcome=outcome)


def _scope(conn,context,now):
    if getattr(context,'role',None)!='trader':return 'role_not_trader'
    if not L._num(now) or not L._num(getattr(context,'deadline_at',None)) or now>=context.deadline_at:
        return 'decision_deadline_unknown_or_expired'
    if any(type(getattr(context,k,None)) is not str or not getattr(context,k) for k in
           ('book_id','epoch_id','actor_id','decision_id')):return 'context_identity_unknown'
    row=conn.execute('SELECT epoch_id,kind FROM books WHERE book_id=?',(context.book_id,)).fetchone()
    if row is None or row['epoch_id']!=context.epoch_id or row['kind']!='evaluated':return 'book_epoch_mismatch'
    return None


def _receipt(conn,context,candidate,now):
    if type(candidate) is not dict:raise ValueError('candidate_unknown')
    hit=S.latest(conn,'receipts','cache_key',candidate.get('cache_key'))
    if hit is None or hit['row_hash']!=candidate.get('receipt_row_hash'):raise ValueError('receipt_identity_unknown')
    if len(hit['payload_json'].encode())>65536:raise ValueError('receipt_budget')
    raw=json.loads(hit['payload_json'])
    rec=SH._verify(hit,candidate.get('cache_key'),raw.get('definition_bundle_hash'),raw.get('strategy_epoch'))
    if rec.get('state')!='known':raise ValueError('receipt_unverified')
    at=rec.get('clock_sample',{}).get('corrected_utc')
    cut=context.cut.get('knowledge_cut')
    if not L._num(at) or not L._num(cut) or not 0<=at<=cut<=now:raise ValueError('receipt_cut_unknown')
    request=rec['request']
    if (any(candidate.get(k)!=request.get(k) for k in ('mint','pool'))
            or candidate.get('feed')!=request.get('feed','pass')
            or candidate.get('sheet_hash')!=rec['sheet_hash']
            or SH._sha(candidate.get('sheet'))!=rec['sheet_hash']
            or SH._sha(candidate.get('lenses'))!=rec['lenses_hash']):raise ValueError('candidate_receipt_mismatch')
    return rec


def enter(store_conn,ledger_conn,policy,*,context,candidate,judgements,now,sol_usd=None,sol_at=None):
    """Submit at most one full-clip paper order for this committed host decision."""
    bundle_hash=None
    try:
        reason=_scope(ledger_conn,context,now)
        if reason:return _result('refused',reason)
        with S.write_transaction(store_conn):
            receipt=_receipt(store_conn,context,candidate,now)
            bundle_hash=SH.freeze_bundle(store_conn,context.decision_id,receipt['cache_key'],
                definition_bundle_hash=receipt['definition_bundle_hash'],strategy_epoch=receipt['strategy_epoch'])
        if type(judgements) is not dict or set(judgements)!=set(P.JUDGE_SLOTS):
            return _result('unknown','judge_set_incomplete',bundle_hash=bundle_hash,outcome='unknown')
        for slot,row in judgements.items():
            if (type(row) is not dict or row.get('decision') not in ('ENTER','SKIP','WAIT')
                    or type(row.get('memo')) is not str or len(row['memo'])>16384
                    or type(row.get('served_model')) is not str or not row['served_model'].startswith(P.SLOT_FAMILY[slot])
                    or row.get('receipt_row_hash')!=receipt['row_hash']):
                return _result('unknown','judge_identity_or_answer_unknown',bundle_hash=bundle_hash,outcome='unknown')
        decision,why=P.aggregate(judgements)
        answers=[judgements[s]['decision'] for s in P.JUDGE_SLOTS]
        outcome=answers[0] if len(set(answers))==1 else 'split'
        bundle,risk=P.risk_link(policy,decision,context.decision_id,bundle_hash,receipt)
        action_key=hashlib.sha256(S.canonical_json([context.epoch_id,context.book_id,context.decision_id])).hexdigest()
        body=receipt['sheet']
        metadata={'ticker':body.get('ticker',body.get('symbol')),
                  'decimals':(body.get('pool_state') or {}).get('base_decimals'),
                  'metadata_known_at':receipt['clock_sample']['corrected_utc']}
        result=T.execute_decision(ledger_conn,policy,context=context,action_key=action_key,bundle=bundle,
            risk_result=risk,now=now,sol_usd=sol_usd,sol_at=sol_at,order_metadata=metadata,
            decision_evidence={'judgements_hash':SH._sha(judgements),'decision_label':outcome})
        return _result('submitted' if result['outcome']=='submitted' else 'refused',result['reason'],
                       order_id=result['order_id'],bundle_hash=bundle_hash,outcome=outcome)
    except sqlite3.Error:return _result('unknown','database_unknown',bundle_hash=bundle_hash,outcome='unknown')
    except ValueError as exc:
        if str(exc)=='action_key_conflict':return _result('refused','decision_identity_conflict',bundle_hash=bundle_hash)
        return _result('unknown','order_proof_unknown',bundle_hash=bundle_hash,outcome='unknown')
    except (TypeError,KeyError,AttributeError):
        return _result('unknown','order_proof_unknown',bundle_hash=bundle_hash,outcome='unknown')


def _asker(asker):
    """(slot, decision_id, canonical answers) for a committed Judge EXIT, or None. answers maps every Judge slot to its committed
    decision, or None when that Judge has not committed (so the split is recorded, never inferred)."""
    if type(asker) is not dict or set(asker)!={'slot','decision_id','answers'}:return None
    slot,decision_id,answers=asker['slot'],asker['decision_id'],asker['answers']
    if (slot not in P.JUDGE_SLOTS or type(decision_id) is not str or not decision_id or type(answers) is not dict
            or set(answers)!=set(P.JUDGE_SLOTS) or answers[slot]!='EXIT'
            or any(v is not None and v not in ('ENTER','SKIP','WAIT','EXIT') for v in answers.values())):return None
    return slot,decision_id,json.dumps(answers,sort_keys=True)


def exit(ledger_conn,*,context,order_id,reason,now,asker):
    """Host calls after ANY committed Judge EXIT on this open order (Alpha ruling). `asker` names the committing Judge and every
    Judge's committed answer (prereg eval-1 Am4): each asking Judge is recorded once per order in exit_askers, a second Judge with
    another reason is an additional asker, and the order's one exit request keeps the first reason and deadline."""
    try:
        refusal=_scope(ledger_conn,context,now)
        if refusal:return _result('refused',refusal)
        if type(order_id) is not str or not order_id or type(reason) is not str or not 1<=len(reason)<=160:
            return _result('refused','exit_request_invalid')
        who=_asker(asker)
        if who is None:return _result('refused','exit_asker_invalid')
        slot,decision_id,answers=who
        with L._write(ledger_conn):
            row=ledger_conn.execute('SELECT * FROM orders WHERE order_id=?',(order_id,)).fetchone()
            if row is None or row['book_id']!=context.book_id or row['epoch_id']!=context.epoch_id:
                return _result('refused','order_scope_mismatch')
            bundle=row['decision_bundle_hash']
            prior=ledger_conn.execute('SELECT decision_id,reason,answers_json FROM exit_askers WHERE order_id=? AND slot=?',(order_id,slot)).fetchone()
            if prior is not None:
                if tuple(prior)!=(decision_id,reason,answers):return _result('refused','exit_asker_conflict',order_id=order_id,bundle_hash=bundle,outcome='EXIT')
            else:
                # Every asker, first or later, is recorded only if the position was open at THIS Judge's own cut: a Judge that never
                # saw an open position (or saw it after the close) leaves no asker row; a close that landed first is a trace, not a loss.
                cut=context.cut.get('knowledge_cut')
                if not (L._num(cut) and 0<=cut<=now and L._num(row['filled_at']) and row['filled_at']<=cut
                        and (row['closed_at'] is None or row['closed_at']>cut)):
                    return _result('refused','order_not_open_at_cut',order_id=order_id,bundle_hash=bundle,outcome='EXIT')
                if row['exit_requested_at'] is None:
                    result=L.request_exit(ledger_conn,order_id=order_id,reason=reason,requested_by='judge',now=now,exit_by='judge')
                    if not result.ok:return _result('refused',result.reason,order_id=order_id,bundle_hash=bundle,outcome='EXIT')
                elif row['state']=='closed':
                    ledger_conn.execute('INSERT INTO exit_askers(order_id,slot,decision_id,reason,answers_json,asked_at) VALUES(?,?,?,?,?,?)',
                                        (order_id,slot,decision_id,reason,answers,now))
                    return _result('refused','already_closed',order_id=order_id,bundle_hash=bundle,outcome='EXIT')
            if prior is None:
                ledger_conn.execute('INSERT INTO exit_askers(order_id,slot,decision_id,reason,answers_json,asked_at) VALUES(?,?,?,?,?,?)',
                                    (order_id,slot,decision_id,reason,answers,now))
            asked_by_judge=ledger_conn.execute('SELECT exit_by FROM orders WHERE order_id=?',(order_id,)).fetchone()[0]=='judge'
            return _result('submitted' if asked_by_judge else 'refused',None if asked_by_judge else 'exit_already_requested',
                           order_id=order_id,bundle_hash=bundle,outcome='EXIT')
    except sqlite3.Error:return _result('unknown','database_unknown',outcome='unknown')
    except (ValueError,TypeError,KeyError,AttributeError):return _result('unknown','exit_proof_unknown',outcome='unknown')

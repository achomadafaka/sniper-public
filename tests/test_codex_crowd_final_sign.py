import sqlite3
import pytest
from sniper.execution import solana_real_seams as S,solana_swap_executor as swap
from sniper.persistence import db_memecoin as M
from test_memecoin_entry_slot_seams import factory,reserve,invoke


def ledger():
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;M.ensure_memecoin_tables(c);return c


@pytest.mark.parametrize('failure',['expired','exception'])
def test_crowd_build_delay_never_reaches_custody_or_fast_builder(monkeypatch,failure):
    c=ledger();built=[];late=[False]
    def guard():
        if late[0]:
            if failure=='exception':raise RuntimeError('clock unavailable')
            return 'crowd_entry_expired'
    guard.require_final_check=True
    def inspect(_conn,**kw):
        kw['sign_fn']({});pytest.fail('late guarded signer returned')
    def build(*args,**kw):built.append(True);late[0]=True;return 'unsigned'
    monkeypatch.setattr(swap,'execute_entry',inspect)
    monkeypatch.setattr(S,'jupiter_swap_tx',build)
    monkeypatch.setattr(S,'sign_swap_tx',lambda *a,**k:pytest.fail('late custody signature'))
    ex,kp,_=factory(monkeypatch,fast_builder=lambda q:pytest.fail('guarded monolithic fastbuilder'))
    pid=reserve(c);result=invoke(c,ex,kp,pid,'ordinary',presign_refusal_fn=guard)
    assert built==[True] and result['status']=='aborted'
    assert result['reason']==('crowd_entry_expired' if failure=='expired' else 'crowd_presign_unknown')
    assert M.pending_row(c,pid)['tx_sig'] is None


def test_crowd_no_route_never_uses_unguarded_native_fallback(monkeypatch):
    c=ledger();guard=lambda:None;guard.require_final_check=True
    def inspect(_conn,**kw):kw['quote_fn']('coin',10.);pytest.fail('no-route quote returned')
    # Existing desk quote's miss raises the seam-private _DeskNoRoute.
    monkeypatch.setattr(S.EntryQuoteMissRecorder,'quote',lambda *a,**k:None)
    monkeypatch.setattr(S.EntryQuoteMissRecorder,'miss',lambda *a:{'code':'no_route'})
    monkeypatch.setattr(swap,'execute_entry',inspect)
    ex,kp,_=factory(monkeypatch)
    pid=reserve(c)
    out=invoke(c,ex,kp,pid,'ordinary',presign_refusal_fn=guard,desk_price_cap={'cap_price_usd':1.,'ref_price_usd':1.,'clip_usd':10.})
    assert out['status']=='aborted' and out['reason']=='crowd_native_fallback_unguarded'
    assert M.pending_row(c,pid)['tx_sig'] is None


def test_crowd_guard_runs_after_fee_clamp_immediately_before_custody(monkeypatch):
    from sniper.execution import solana_sender_ab as ab
    c=ledger();late=[False];events=[]
    def guard():
        events.append('check')
        return 'crowd_entry_expired' if late[0] else None
    guard.require_final_check=True
    def inspect(_conn,**kw):kw['sign_fn']({});pytest.fail('late guarded signer returned')
    def clamp(unsigned,**kw):events.append('clamp');late[0]=True;return unsigned,{}
    monkeypatch.setattr(swap,'execute_entry',inspect)
    monkeypatch.setattr(S,'jupiter_swap_tx',lambda *a,**k:'unsigned')
    monkeypatch.setattr(ab,'payload_tip',lambda *a,**k:0)
    monkeypatch.setattr(ab,'cap_unsigned_entry_priority',clamp)
    monkeypatch.setattr(S,'sign_swap_tx',lambda *a,**k:pytest.fail('expired transaction signed'))
    ex,kp,_=factory(monkeypatch);pid=reserve(c)
    out=invoke(c,ex,kp,pid,'ordinary',presign_refusal_fn=guard)
    assert out['reason']=='crowd_entry_expired' and events[-2:]==['clamp','check']
    assert M.pending_row(c,pid)['tx_sig'] is None

"""New routed attribution never emits swaps or retroactively cleans legacy losses."""
import base64
import json
import sqlite3

import pytest

from sniper.services import memecoin_desk_ps_losses as L
from sniper.services import memecoin_desk_pumpswap_routed as R
from sniper.util import clock
from test_desk_ps_routed_scope import FIXTURES, receipt
from test_memecoin_desk_pumpswap_worker import worker
from test_memecoin_desk_pumpswap import ACTOR


def processed(tmp_path,monkeypatch,*,enabled=True,missing_alt=False,name='multi_hop_26_amm',commit=True,other_processing=None):
    saved=json.loads((FIXTURES/(name+'.json')).read_text())
    rx=saved['received_at']
    monkeypatch.setattr(clock,'offset_s',lambda:0.)
    monkeypatch.setenv('MEMECOIN_DESK_PS_ROUTED_SCOPE','1' if enabled else '0')
    w=worker(tmp_path,monkeypatch)
    monkeypatch.setattr(__import__('time'),'time',lambda:rx+.05)
    emitted=[]
    if other_processing is not None:
        w.curve_capture=False
        monkeypatch.setattr(w,'_retired_curve_event',lambda *a,**k:other_processing.append(a))
    monkeypatch.setattr(w,'on_pumpswap',lambda *args:emitted.append(args))
    raw=base64.b64decode(saved['raw_b64'])
    if missing_alt:
        from sniper.feeds.yellowstone import geyser_pb2 as pb
        update=pb.SubscribeUpdate.FromString(raw)
        meta=update.transaction.transaction.meta
        if meta.loaded_readonly_addresses:
            meta.loaded_readonly_addresses.pop()
        else:
            lookup=update.transaction.transaction.transaction.message.address_table_lookups.add()
            lookup.account_key=bytes([9])*32
            lookup.readonly_indexes=b'\x00'
        raw=update.SerializeToString()
    w.process(raw,received_at=rx)
    if commit:w.commit_files()
    conn=w.files.conn('ps_decode_losses',rx)
    return w,conn,saved,emitted


def test_complete_route_scopes_loss_only_and_never_emits_swap(tmp_path,monkeypatch):
    w,conn,saved,emitted=processed(tmp_path,monkeypatch)
    encoded,=conn.execute('SELECT pool_keys_json FROM ps_loss_scope').fetchone()
    proof=json.loads(encoded)
    expected=R.prove_pools(*receipt()[0:1],account_keys=receipt()[1],instructions=receipt()[2],inner_complete=True)
    assert proof['keys']==expected and proof['complete'] is True and proof['proof']=='multi_hop_legs_v1'
    assert not emitted and conn.execute('SELECT count(*) FROM swaps_ps').fetchone()[0]==0
    rx=saved['received_at']
    assert L.losses(tmp_path,expected[0],rx-1,rx+1)==[rx]
    assert L.losses(tmp_path,ACTOR,rx-1,rx+1)==[]


def test_knob_rollback_preserves_original_unknown_loss(tmp_path,monkeypatch):
    w,conn,saved,emitted=processed(tmp_path,monkeypatch,enabled=False)
    assert conn.execute('SELECT count(*) FROM ps_loss_scope').fetchone()[0]==0 and not emitted
    # Existing Bloom absence may still exclude; disabling the new proof changes no old mechanism.


@pytest.mark.parametrize('change',[{'signature':'wrong'},{'complete':False},{'known_at':'bad'},
                                    {'keys':[ACTOR]},{'proof':'forged'},{'sha256':'0'*64}])
def test_partial_or_forged_new_proof_cannot_authorize_foreign_exclusion(tmp_path,monkeypatch,change):
    w,conn,saved,_=processed(tmp_path,monkeypatch)
    proof=json.loads(conn.execute('SELECT pool_keys_json FROM ps_loss_scope').fetchone()[0])
    proof.update(change)
    # Remove the independent Bloom proof to isolate this new proof's refusal.
    conn.execute('DELETE FROM ps_loss_event_envelopes')
    conn.execute('UPDATE ps_loss_scope SET pool_keys_json=?',(json.dumps(proof),));conn.commit()
    rx=saved['received_at']
    try:
        assert L.losses(tmp_path,ACTOR,rx-1,rx+1)==[rx]
    except L.LossUnreadable:
        pass


def test_historical_null_keys_do_not_become_complete(tmp_path,monkeypatch):
    w,conn,saved,_=processed(tmp_path,monkeypatch)
    conn.execute('DELETE FROM ps_loss_event_envelopes')
    conn.execute('DELETE FROM ps_loss_scope');conn.commit()
    rx=saved['received_at']
    assert L.losses(tmp_path,ACTOR,rx-1,rx+1)==[rx]


def test_valid_future_or_partial_proof_remains_loss_until_actual_known_cut(tmp_path,monkeypatch):
    import hashlib
    w,conn,saved,_=processed(tmp_path,monkeypatch)
    rx=saved['received_at']
    body=json.loads(conn.execute('SELECT pool_keys_json FROM ps_loss_scope').fetchone()[0])
    conn.execute('DELETE FROM ps_loss_event_envelopes')
    body['known_at']=rx+2
    payload={k:v for k,v in body.items() if k!='sha256'}
    body['sha256']=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    conn.execute('UPDATE ps_loss_scope SET pool_keys_json=?,known_at=?',(json.dumps(body),rx+2));conn.commit()
    assert L.losses(tmp_path,ACTOR,rx-1,rx+1)==[rx]
    assert L.losses(tmp_path,ACTOR,rx-1,rx+3)==[]
    body['complete']=False
    payload={k:v for k,v in body.items() if k!='sha256'}
    body['sha256']=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
    conn.execute('UPDATE ps_loss_scope SET pool_keys_json=?,complete=0',(json.dumps(body),));conn.commit()
    assert L.losses(tmp_path,ACTOR,rx-1,rx+3)==[rx]


def test_new_schema_keeps_old_four_column_writer_compatible(tmp_path):
    from test_memecoin_desk_pumpswap_worker import W
    files=W.DayFiles(tmp_path)
    conn=files.conn('ps_decode_losses',100)
    assert [r[1] for r in conn.execute('PRAGMA table_info(ps_decode_losses)')]==['signature','cause','slot','received_at']
    conn.execute('INSERT INTO ps_decode_losses VALUES(?,?,?,?)',('old','decode_loss',1,100.))
    assert conn.execute('SELECT count(*) FROM ps_loss_scope').fetchone()[0]==0


def test_partial_alt_metadata_never_writes_complete_scope(tmp_path,monkeypatch):
    w,conn,saved,emitted=processed(tmp_path,monkeypatch,missing_alt=True)
    assert conn.execute('SELECT count(*) FROM ps_loss_scope').fetchone()[0]==0
    assert not emitted


@pytest.mark.parametrize('error',[__import__('struct').error,OverflowError,RuntimeError])
def test_optional_scope_parser_failure_preserves_original_loss_and_other_frame_processing(tmp_path,monkeypatch,error):
    def broken(*args,**kwargs):
        raise error('injected scope parser bug')
    monkeypatch.setattr(R,'prove_pools',broken)
    other=[]
    w,conn,saved,emitted=processed(tmp_path,monkeypatch,name='multi_hop_26_pump',commit=False,other_processing=other)
    assert conn.execute('SELECT count(*) FROM ps_decode_losses').fetchone()[0]==1
    assert conn.execute('SELECT count(*) FROM ps_loss_scope').fetchone()[0]==0
    assert w.ps_loss_pending['decode_loss']==[saved['signature']]
    assert w.stats['ps_routed_scope_refused']==1
    assert not emitted and other

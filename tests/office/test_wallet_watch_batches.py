"""Bounded expansion commits once and exports before advancing its durable position."""
import json
import pytest
from sniper.office import wallet_watch as W
from sniper.office.wallet_watch_collect import Collector
from .test_wallet_flow_consumer import source,insert,rows,NOW,A
from .test_wallet_coverage_continuation import many_rotations


@pytest.fixture(autouse=True)
def infrastructure(monkeypatch):monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset())


def test_token_batch_exports_once_before_progress(source,monkeypatch):
    net,chain,clock,desk=source;raw,owners=many_rotations(5);insert(source,raw=raw.SerializeToString())
    original=W._sync;exports=[]
    def sync(conn,desk,now):
        exports.append({r[0] for r in conn.execute('SELECT address FROM watch_list WHERE removed_at IS NULL')})
        return original(conn,desk,now)
    monkeypatch.setattr(W,'_sync',sync)
    result=Collector(net,desk).consume_flows()
    assert result['rows']==1 and len(exports)==2  # one expansion export, one final alias refresh
    assert set(owners)<=exports[0]
    assert rows(net.path,'watch_flow_expansions')[0]['state']=='done'


def test_export_failure_keeps_batch_pending_and_restart_repairs_it(source,monkeypatch):
    net,chain,clock,desk=source;raw,owners=many_rotations(5);insert(source,raw=raw.SerializeToString())
    original=W._sync
    monkeypatch.setattr(W,'_sync',lambda *a:(_ for _ in ()).throw(OSError('disk full')))
    Collector(net,desk).consume_flows()
    task=rows(net.path,'watch_flow_expansions')[0]
    assert task['state']=='pending' and task['token_outer']==-1 and task['token_done']==0
    assert set(owners)<={r['address'] for r in rows(net.path,'watch_list')}
    monkeypatch.setattr(W,'_sync',original)
    result=Collector(net,desk).consume_flows()
    assert result['rows']==0 and rows(net.path,'watch_flow_expansions')[0]['state']=='done'
    assert set(owners)<=set(json.loads(W.watch_file(desk).read_text())['wallets'])


def test_finite_cap_partial_batch_is_exported_then_resumed(source,monkeypatch):
    net,chain,clock,desk=source;raw,owners=many_rotations(5);insert(source,raw=raw.SerializeToString())
    monkeypatch.setattr(W,'MAX_LISTED',3)
    Collector(net,desk).consume_flows()
    task=rows(net.path,'watch_flow_expansions')[0]
    assert task['state']=='pending' and len(W.read_list(net))==3
    assert len(json.loads(W.watch_file(desk).read_text())['owner_wallets'])==3
    monkeypatch.setattr(W,'MAX_LISTED',-1)
    Collector(net,desk).consume_flows()
    assert rows(net.path,'watch_flow_expansions')[0]['state']=='done'
    assert set(owners)<={r['address'] for r in W.read_list(net)}


def test_sol_batch_exports_all_children_once(source,monkeypatch):
    import base58,struct
    from .test_wallet_flow_consumer import wire
    net,chain,clock,desk=source;raw,owners=many_rotations(5);u=wire()
    info=u.transaction.transaction;msg=info.transaction.message
    del msg.account_keys[:];msg.account_keys.extend(base58.b58decode(a) for a in [A,*owners,'11111111111111111111111111111111'])
    del msg.instructions[:]
    for i in range(5):
        ix=msg.instructions.add();ix.program_id_index=6;ix.accounts=bytes([0,i+1]);ix.data=struct.pack('<IQ',2,10000)
    del info.meta.pre_balances[:];del info.meta.post_balances[:]
    info.meta.pre_balances.extend([100000,0,0,0,0,0,0]);info.meta.post_balances.extend([45000,10000,10000,10000,10000,10000,0])
    insert(source,raw=u.SerializeToString());original=W._sync;exports=[]
    def sync(conn,desk,now):
        exports.append({r[0] for r in conn.execute('SELECT address FROM watch_list WHERE removed_at IS NULL')})
        return original(conn,desk,now)
    monkeypatch.setattr(W,'_sync',sync)
    Collector(net,desk).consume_flows()
    assert len(exports)==2 and set(owners)<=exports[0]
    assert rows(net.path,'watch_flow_expansions')[0]['sol_cursor']==5


def test_sql_failure_rolls_back_whole_batch_before_retry(source,monkeypatch):
    import sqlite3
    net,chain,clock,desk=source;raw,owners=many_rotations(5);insert(source,raw=raw.SerializeToString())
    original=W._add_on_connection;calls=[]
    def add(*args,**kw):
        calls.append(kw['address'])
        if len(calls)==2:raise sqlite3.OperationalError('database locked')
        return original(*args,**kw)
    monkeypatch.setattr(W,'_add_on_connection',add)
    Collector(net,desk).consume_flows()
    assert {r['address'] for r in W.read_list(net)}=={A}
    assert rows(net.path,'watch_flow_expansions')[0]['state']=='pending'
    monkeypatch.setattr(W,'_add_on_connection',original)
    Collector(net,desk).consume_flows()
    assert set(owners)<={r['address'] for r in W.read_list(net)}


def test_removal_between_batch_commit_and_export_stays_removed(source,monkeypatch):
    net,chain,clock,desk=source;raw,owners=many_rotations(5);insert(source,raw=raw.SerializeToString())
    original=W._sync;removed=[]
    def sync(conn,desk,now):
        assert not conn.in_transaction
        if not removed:
            removed.append(owners[0]);W.remove(net,desk,address=owners[0],now=now)
        return original(conn,desk,now)
    monkeypatch.setattr(W,'_sync',sync)
    Collector(net,desk).consume_flows();Collector(net,desk).consume_flows()
    assert owners[0] not in {r['address'] for r in W.read_list(net)}
    assert owners[0] not in json.loads(W.watch_file(desk).read_text())['wallets']
    assert rows(net.path,'watch_flow_expansions')[0]['state']=='done'

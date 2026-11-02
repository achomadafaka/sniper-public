"""Local protobuf tape to existing graph only; any network attempt fails this fixture."""
import base64
import json
import sqlite3
import struct

import base58
import pytest
from solders.pubkey import Pubkey
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.office.chain_data import ChainData
from sniper.office.wallet_network import WalletNetwork
from sniper.office.wallet_watch_collect import Collector
from sniper.office import wallet_watch as W

A,B=(str(Pubkey.from_bytes(bytes([n])*32)) for n in (1,2))
NOW=1793536000
DDL='''CREATE TABLE wallet_flow_transactions(flow_id INTEGER PRIMARY KEY AUTOINCREMENT,signature TEXT UNIQUE,slot INTEGER,tx_index INTEGER,block_time INTEGER,received_at REAL,processed_at REAL,commitment TEXT,payload_json TEXT);'''


def wire(n=3):
    u=pb.SubscribeUpdate();u.transaction.slot=100
    info=u.transaction.transaction;info.signature=bytes([n])*64;info.transaction.signatures.append(info.signature)
    msg=info.transaction.message;msg.header.num_required_signatures=1;msg.header.num_readonly_unsigned_accounts=1
    msg.account_keys.extend([base58.b58decode(A),base58.b58decode(B),bytes(32)]);msg.recent_blockhash=bytes([4])*32
    ix=msg.instructions.add();ix.program_id_index=2;ix.accounts=b'\x00\x01';ix.data=struct.pack('<IQ',2,10000)
    meta=info.meta;meta.fee=5000;meta.pre_balances.extend([100000,0,0]);meta.post_balances.extend([85000,10000,0]);meta.log_messages.append('Program success')
    return u


@pytest.fixture
def source(tmp_path):
    def rpc(*args):raise AssertionError('NO RPC allowed')
    clock=[NOW]
    chain=ChainData(tmp_path/'chain.db',lambda:'https://unused.invalid',clock=lambda:clock[0],rpc=rpc)
    net=WalletNetwork(tmp_path/'network.db',chain_db=chain.path,chain=chain,clock=lambda:clock[0])
    desk=tmp_path/'desk';(desk/'pumpswap').mkdir(parents=True)
    W.add(net,desk,address=A,operator_id=None,evidence='fixture',source='host',actor='crew',now=NOW-10)
    c=sqlite3.connect(desk/'pumpswap/tape.db');c.execute(DDL);c.close()
    return net,chain,clock,desk


def insert(source,*,n=3,bt=NOW-1,commitment='confirmed',raw=None):
    net,chain,clock,desk=source;u=wire(n)
    if raw is None:raw=u.SerializeToString()
    payload=json.dumps(dict(raw_update_b64=base64.b64encode(raw).decode()))
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:
        c.execute('INSERT INTO wallet_flow_transactions(signature,slot,tx_index,block_time,received_at,processed_at,commitment,payload_json) VALUES(?,?,?,?,?,?,?,?)',
                  (base58.b58encode(u.transaction.transaction.signature).decode(),100,None,bt,NOW-2,NOW-1,commitment,payload))


def rows(path,table):
    with sqlite3.connect(path) as c:
        c.row_factory=sqlite3.Row
        return [dict(r) for r in c.execute('SELECT * FROM '+table)]


def test_stream_funding_and_movements_preserve_confirmed_provenance_and_restart(source):
    net,chain,clock,desk=source;insert(source)
    out=Collector(net,desk).consume_flows()
    assert out['rows']==1 and out['state']=='partial'
    edge,=rows(net.path,'edges')
    assert (edge['src'],edge['dst'],edge['known_at'],edge['observed_at'])==(B,A,NOW,NOW-1)
    assert edge['proof_state']=='verified'
    facts=rows(chain.path,'fetches')
    assert facts and all(json.loads(r['result'])['provenance']=='research_stream' for r in facts)
    assert all(json.loads(r['result'])['commitment']=='confirmed' for r in facts)
    assert not rows(chain.path,'cache') and not rows(chain.path,'calls')
    assert any(r['address']==A and r['delta_atomic']=='-15000' for r in rows(net.path,'watch_movements'))
    assert B in {r['address'] for r in W.read_list(net)}
    assert Collector(net,desk).consume_flows()['rows']==0
    assert len(rows(net.path,'edges'))==1


def test_missing_clock_does_not_block_later_rows_and_is_retried(source):
    net,chain,clock,desk=source;insert(source,bt=None);insert(source,n=4)
    out=Collector(net,desk).consume_flows(limit=1)
    assert out['pending']==1 and not rows(net.path,'edges')
    out=Collector(net,desk).consume_flows(limit=1)
    assert out['rows']>=1 and rows(net.path,'edges')
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('UPDATE wallet_flow_transactions SET block_time=? WHERE flow_id=1',(NOW-1,))
    clock[0]+=5
    out=Collector(net,desk).consume_flows(limit=1)
    assert out['pending']==0
    assert any(r['known_at']==NOW+5 for r in rows(net.path,'edges'))


def test_table_absent_and_source_replacement_are_unknown(source):
    net,chain,clock,desk=source
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('DROP TABLE wallet_flow_transactions')
    assert Collector(net,desk).consume_flows()['state']=='unknown'
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute(DDL)
    insert(source);Collector(net,desk).consume_flows()
    path=desk/'pumpswap/tape.db';path.rename(path.with_suffix('.old'))
    sqlite3.connect(path).close()
    assert Collector(net,desk).consume_flows()['reason']=='flow_source_replaced'


@pytest.mark.parametrize('bad',['processed','raw_malformed','failed'])
def test_invalid_stream_row_is_not_funding_and_does_not_stall_cursor(source,bad):
    net,chain,clock,desk=source
    u=wire();u.transaction.transaction.meta.err.err=b'failed'
    insert(source,commitment='processed' if bad=='processed' else 'confirmed',raw=b'bad' if bad=='raw_malformed' else u.SerializeToString() if bad=='failed' else None)
    out=Collector(net,desk).consume_flows()
    assert out['unknown']>=1 and not rows(net.path,'edges')
    assert Collector(net,desk).consume_flows()['rows']==0


def test_http_credit_cap_does_not_stop_local_stream_work(source):
    net,chain,clock,desk=source;insert(source)
    with net._conn() as c:net._count(c,NOW,credits=33333)
    assert Collector(net,desk).consume_flows()['rows']==1
    assert rows(net.path,'edges') and not rows(chain.path,'calls')


def test_infrastructure_stops_expansion_but_not_evidence(source,monkeypatch):
    net,chain,clock,desk=source;insert(source)
    monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset({A}))
    Collector(net,desk).consume_flows()
    assert rows(net.path,'edges')
    assert {r['address'] for r in W.read_list(net)}=={A}


def test_stream_fact_replay_is_stable_and_never_poisoned_as_rpc(source):
    net,chain,clock,desk=source
    fact=dict(state='known',asof=dict(kind='confirmed_transaction',block_time_s=NOW-1),funder=A,recipient=B)
    first=chain.record_stream_fact('stream_test',dict(signature='s'),fact,commitment='confirmed')
    clock[0]+=10
    second=chain.record_stream_fact('stream_test',dict(signature='s'),fact,commitment='confirmed')
    assert second==first and len(rows(chain.path,'fetches'))==1
    conflict=chain.record_stream_fact('stream_test',dict(signature='s'),dict(fact,recipient=A),commitment='confirmed')
    assert conflict['state']=='unknown'
    assert not rows(chain.path,'cache')
    with pytest.raises(ValueError):chain.record_stream_fact('stream_test',{},fact,commitment='processed')


def test_capture_gap_counts_are_exposed_not_hidden(source):
    net,chain,clock,desk=source;insert(source)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:
        c.execute('CREATE TABLE wallet_flow_gaps(day,reason,n,first_at,last_at,known_at)')
        c.execute('INSERT INTO wallet_flow_gaps VALUES(?,?,?,?,?,?)',('2026-11-01','raw_cap',2,NOW-5,NOW-4,NOW-3))
    out=Collector(net,desk).consume_flows()
    assert out['stream_gaps']['items'][0]['n']==2


def test_expansion_watch_cap_is_reported_and_keeps_existing_evidence(source,monkeypatch):
    monkeypatch.setattr(W,'MAX_LISTED',500)
    net,chain,clock,desk=source;insert(source)
    with net._conn() as c:
        for i in range(499):
            address=str(Pubkey.from_bytes(i.to_bytes(2,'big')+bytes([9])*30))
            c.execute('INSERT INTO watch_list VALUES(?,NULL,?,?,?, ?,NULL)',(address,'fixture','host','crew',NOW))
    out=Collector(net,desk).consume_flows()
    assert out['reason']=='watch_list_full'
    assert rows(net.path,'edges') and len(W.read_list(net))==500


def test_graph_commit_failure_after_fetch_retries_idempotently_at_actual_time(source,monkeypatch):
    net,chain,clock,desk=source;insert(source)
    collector=Collector(net,desk);original=collector._open;fail=[True]
    class Connection:
        def __init__(self):self.conn=original()
        def __getattr__(self,name):return getattr(self.conn,name)
        def __enter__(self):self.conn.__enter__();return self
        def __exit__(self,kind,value,tb):
            if kind is None and fail[0] and self.conn.execute('SELECT count(*) FROM edges').fetchone()[0]:
                fail[0]=False;self.conn.rollback();raise sqlite3.OperationalError('injected graph commit failure')
            return self.conn.__exit__(kind,value,tb)
    monkeypatch.setattr(collector,'_open',Connection)
    assert collector.consume_flows()['state']=='unknown'
    assert rows(chain.path,'fetches') and not rows(net.path,'edges')
    fetches=len(rows(chain.path,'fetches'));clock[0]+=5
    assert Collector(net,desk).consume_flows()['rows']==1
    edge,=rows(net.path,'edges')
    assert edge['known_at']==NOW+5 and len(rows(chain.path,'fetches'))==fetches
    assert Collector(net,desk).consume_flows()['rows']==0


def test_pending_clock_remains_retryable_beyond_thirty_days(source):
    net,chain,clock,desk=source;insert(source,bt=None)
    Collector(net,desk).consume_flows()
    clock[0]+=31*86400
    out=Collector(net,desk).consume_flows()
    assert out['pending']==1
    assert not rows(net.path,'watch_flow_receipts')
    assert not rows(net.path,'edges')


def test_old_completed_outcomes_remain_without_replaying_source(source):
    net,chain,clock,desk=source;insert(source)
    Collector(net,desk).consume_flows();clock[0]+=31*86400
    out=Collector(net,desk).consume_flows()
    assert out['rows']==0 and len(rows(net.path,'watch_flow_receipts'))==1
    assert len(rows(net.path,'edges'))==1


def test_disk_cap_prevents_new_stream_facts(source):
    net,chain,clock,desk=source;insert(source);net.max_bytes=1
    assert Collector(net,desk).consume_flows()['reason']=='disk_cap'
    assert not rows(net.path,'edges')


def test_cli_runs_local_flows_before_http_credit_cap(source,tmp_path,monkeypatch,capsys):
    import importlib.util
    from pathlib import Path
    from sniper.office import chain_data,wallet_watch_collect
    net,chain,clock,desk=source;insert(source)
    office=tmp_path/'data/office';(office/'v2').mkdir(parents=True)
    chain._conn().close()
    net.path.rename(office/'wallet_network.db');chain.path.rename(office/'v2/chain.db')
    desk.rename(tmp_path/'data/desk')
    calls=[]
    original=wallet_watch_collect.Collector
    class LocalCollector(original):
        def consume_flows(self,**kwargs):calls.append('flows');return super().consume_flows(**kwargs)
        def consume_setups(self,**kwargs):calls.append('setups');return dict(state='unknown',reason='no_setup')
        def tick(self,**kwargs):calls.append('poll');return dict(state='unknown',reason='credit_cap')
    monkeypatch.setattr(wallet_watch_collect,'Collector',LocalCollector)
    monkeypatch.setattr(chain_data,'research_url_of',lambda path:lambda:'https://unused.invalid')
    spec=importlib.util.spec_from_file_location('stream_collector_cli',Path(__file__).resolve().parents[2]/'scripts/office_wallet_watch_collect.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    assert cli.main(['--enabled','--state-root',str(tmp_path),'--research-env-file','unused','--ticks','1'])==0
    out=json.loads(capsys.readouterr().out)
    assert calls.index('flows')<calls.index('poll')
    assert out['flows']['rows']==1 and out['poll']['reason']=='credit_cap'


def test_in_place_source_replacement_cannot_reuse_old_cursor(source):
    net,chain,clock,desk=source;insert(source)
    Collector(net,desk).consume_flows()
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('UPDATE wallet_flow_transactions SET signature=? WHERE flow_id=1',('a'*64,))
    out=Collector(net,desk).consume_flows()
    assert out['state']=='unknown' and out['reason']=='flow_source_replaced'


def test_stream_proof_store_cap_is_typed_and_never_deletes_foreign_facts(source,monkeypatch):
    from sniper.office import chain_data
    net,chain,clock,desk=source
    existing=chain.record_stream_fact('existing',{},dict(state='known'),commitment='confirmed')
    monkeypatch.setattr(chain_data,'STREAM_PROOF_MAX_BYTES',1,raising=False)
    out=chain.record_stream_fact('new',{},dict(state='known'),commitment='confirmed')
    assert out['state']=='unknown' and out['reason']=='stream_proof_store_cap'
    assert len(rows(chain.path,'fetches'))==1
    assert chain.record_stream_fact('existing',{},dict(state='known'),commitment='confirmed')==existing


def test_stream_store_cap_stays_pending_and_recovers_without_rpc(source,monkeypatch):
    from sniper.office import chain_data
    net,chain,clock,desk=source;insert(source)
    monkeypatch.setattr(chain_data,'STREAM_PROOF_MAX_BYTES',1)
    out=Collector(net,desk).consume_flows()
    assert out['reason']=='stream_proof_store_cap' and out['pending']==1
    assert not rows(net.path,'edges') and not rows(chain.path,'cache')
    monkeypatch.setattr(chain_data,'STREAM_PROOF_MAX_BYTES',1<<30);clock[0]+=2
    assert Collector(net,desk).consume_flows()['pending']==0
    assert rows(net.path,'edges')[0]['known_at']==NOW+2


def test_pending_capacity_does_not_block_later_clocked_rows(source):
    net,chain,clock,desk=source
    for n in range(3,204):insert(source,n=n,bt=None)
    insert(source,n=204)
    collector=Collector(net,desk,expand=False)
    collector.consume_flows(limit=100);collector.consume_flows(limit=100)
    out=collector.consume_flows(limit=100)
    assert out['pending']==201 and out['unknown']==0
    assert rows(net.path,'edges')


def test_unreadable_local_graph_is_typed_unknown_not_an_exception(source,monkeypatch):
    net,chain,clock,desk=source
    collector=Collector(net,desk)
    def unavailable():raise sqlite3.OperationalError('locked')
    monkeypatch.setattr(collector,'_open',unavailable)
    out=collector.consume_flows()
    assert out['state']=='unknown' and out['reason']=='flow_store_unknown'


def creation_wire():
    import hashlib
    u=wire();info=u.transaction.transaction;msg=info.transaction.message
    pump='6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
    curve=str(Pubkey.find_program_address([b'bonding-curve',bytes(Pubkey.from_string(B))],Pubkey.from_string(pump))[0])
    accounts=[str(Pubkey.from_bytes(bytes([n])*32)) for n in range(10,26)]
    accounts[0]=B;accounts[2]=curve;accounts[5]=A;accounts[-1]=pump
    keys=[A,B]+[k for k in accounts if k not in (A,B)]
    del msg.account_keys[:];msg.account_keys.extend(base58.b58decode(k) for k in keys)
    msg.header.num_required_signatures=2
    info.transaction.signatures.append(bytes([5])*64)
    del msg.instructions[:]
    data=hashlib.sha256(b'global:create_v2').digest()[:8]+b''.join(struct.pack('<I',1)+v for v in (b'n',b's',b'u'))+bytes([3])*32+b'\0\0'
    msg.instructions.add(program_id_index=keys.index(pump),accounts=bytes(keys.index(k) for k in accounts),data=data)
    del info.meta.pre_balances[:];del info.meta.post_balances[:]
    info.meta.pre_balances.extend([100000]+[0]*(len(keys)-1));info.meta.post_balances.extend([95000]+[0]*(len(keys)-1))
    return u


def test_stream_launch_enters_existing_graph_with_actual_knowledge_time(source):
    net,chain,clock,desk=source
    insert(source,raw=creation_wire().SerializeToString())
    out=Collector(net,desk).consume_flows()
    assert out['rows']==1
    c=net._conn();rows=c.execute("SELECT * FROM edges WHERE type='created_mint'").fetchall()
    assert len(rows)==1
    edge=rows[0]
    assert (edge['src'],edge['dst'],edge['known_at'],edge['observed_at'])==(A,B,NOW,NOW-1)
    assert edge['proof_kind']=='research_stream' and edge['method_version']=='wallet-watch-launch-v1'
    assert c.execute('SELECT count(*) FROM watch_list').fetchone()[0]==1
    c.close()
    f=chain._conn().execute('SELECT result FROM fetches WHERE fetch_id=?',(edge['proof_ref'],)).fetchone()
    fact=json.loads(f[0]);assert fact['creator_argument']==str(Pubkey.from_bytes(bytes([3])*32))
    assert fact['commitment']=='confirmed' and fact['ownership_proven'] is False
    clock[0]+=1;Collector(net,desk).consume_flows()
    c=net._conn();assert c.execute("SELECT count(*) FROM edges WHERE type='created_mint'").fetchone()[0]==1;c.close()


def test_unsigned_mint_is_unknown_launch_without_erasing_balance_observation(source):
    net,chain,clock,desk=source;u=creation_wire()
    u.transaction.transaction.transaction.message.header.num_required_signatures=1
    u.transaction.transaction.transaction.signatures.pop()
    insert(source,raw=u.SerializeToString())
    Collector(net,desk).consume_flows()
    c=net._conn()
    assert c.execute("SELECT count(*) FROM edges WHERE type='created_mint'").fetchone()[0]==0
    assert c.execute('SELECT reason FROM watch_flow_receipts').fetchone()[0]=='launch_evidence_unproven'
    assert c.execute('SELECT count(*) FROM watch_movements').fetchone()[0]>0
    c.close()


def test_launch_proof_store_failure_retries_without_backdating_or_duplicate(source,monkeypatch):
    net,chain,clock,desk=source;insert(source,raw=creation_wire().SerializeToString())
    original=chain.record_stream_fact
    def fail(tool,*args,**kwargs):
        if tool=='watch_stream_launch':return dict(state='unknown',reason='stream_proof_store_cap')
        return original(tool,*args,**kwargs)
    monkeypatch.setattr(chain,'record_stream_fact',fail)
    assert Collector(net,desk).consume_flows()['pending']==1
    clock[0]+=2;monkeypatch.setattr(chain,'record_stream_fact',original)
    assert Collector(net,desk).consume_flows()['pending']==0
    c=net._conn();row,=c.execute("SELECT * FROM edges WHERE type='created_mint'").fetchall()
    assert row['known_at']==NOW+2 and row['observed_at']==NOW-1
    c.close()


def test_clock_regression_after_local_proof_never_backdates_graph(source,monkeypatch):
    net,chain,clock,desk=source;insert(source)
    original=chain.record_stream_fact
    def regress(*args,**kwargs):
        result=original(*args,**kwargs)
        clock[0]=NOW-10
        return result
    monkeypatch.setattr(chain,'record_stream_fact',regress)
    out=Collector(net,desk).consume_flows()
    c=net._conn();count=c.execute('SELECT count(*) FROM edges').fetchone()[0];c.close()
    assert count==0 and out['pending']==1
    monkeypatch.setattr(chain,'record_stream_fact',original)
    assert Collector(net,desk).consume_flows()['pending']==1
    clock[0]=NOW+3
    assert Collector(net,desk).consume_flows()['pending']==0
    c=net._conn();edge,=c.execute('SELECT * FROM edges').fetchall();c.close()
    assert edge['known_at']==NOW+3


def test_older_frame_clock_floor_survives_pending_retry_restart(source,monkeypatch):
    net,chain,clock,desk=source;insert(source,bt=NOW-30)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('UPDATE wallet_flow_transactions SET received_at=?,processed_at=?',(NOW-21,NOW-20))
    original=chain.record_stream_fact
    def regress(*args,**kwargs):
        proof=original(*args,**kwargs);clock[0]=NOW-10;return proof
    monkeypatch.setattr(chain,'record_stream_fact',regress)
    assert Collector(net,desk).consume_flows()['pending']==1
    monkeypatch.setattr(chain,'record_stream_fact',original)
    Collector(net,desk).consume_flows();Collector(net,desk).consume_flows()
    c=net._conn();assert c.execute('SELECT count(*) FROM edges').fetchone()[0]==0;c.close()
    clock[0]=NOW+1;assert Collector(net,desk).consume_flows()['pending']==0
    c=net._conn();assert c.execute('SELECT known_at FROM edges').fetchone()[0]==NOW+1;c.close()


def test_graph_waits_for_persisted_proof_time_even_when_attempt_floor_is_older(source,monkeypatch):
    net,chain,clock,desk=source;insert(source,bt=NOW-30)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('UPDATE wallet_flow_transactions SET received_at=?,processed_at=?',(NOW-21,NOW-20))
    original=chain.record_stream_fact
    def future_proof(tool,*args,**kwargs):
        if tool=='watch_stream_funding':clock[0]=NOW+10
        result=original(tool,*args,**kwargs);clock[0]=NOW+1;return result
    monkeypatch.setattr(chain,'record_stream_fact',future_proof)
    assert Collector(net,desk).consume_flows()['pending']==1
    monkeypatch.setattr(chain,'record_stream_fact',original)
    assert Collector(net,desk).consume_flows()['pending']==1
    c=net._conn();assert c.execute('SELECT count(*) FROM edges').fetchone()[0]==0;c.close()
    clock[0]=NOW+11;assert Collector(net,desk).consume_flows()['pending']==0
    c=net._conn();assert c.execute('SELECT known_at FROM edges').fetchone()[0]==NOW+11;c.close()


@pytest.mark.parametrize('two_frames',[False,True])
def test_unwritten_proof_still_preserves_observed_clock_floor(source,monkeypatch,two_frames):
    from sniper.office import wallet_watch_collect as module
    net,chain,clock,desk=source;insert(source,bt=NOW-30)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('UPDATE wallet_flow_transactions SET received_at=?,processed_at=?',(NOW-21,NOW-20))
    if two_frames:insert(source,n=4,bt=NOW-30)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('UPDATE wallet_flow_transactions SET received_at=?,processed_at=?',(NOW-21,NOW-20))
    expected=2 if two_frames else 1
    calls=[0]
    original_funding=module.verified_sol_funding;original_store=chain.record_stream_fact
    def advance(*args):
        result=original_funding(*args);calls[0]+=1
        if calls[0]==1:clock[0]=NOW+10
        return result
    def refuse(*args,**kwargs):
        clock[0]=NOW+1;return dict(state='unknown',reason='stream_proof_store_cap')
    monkeypatch.setattr(module,'verified_sol_funding',advance);monkeypatch.setattr(chain,'record_stream_fact',refuse)
    assert Collector(net,desk).consume_flows()['pending']==expected
    monkeypatch.setattr(module,'verified_sol_funding',original_funding);monkeypatch.setattr(chain,'record_stream_fact',original_store)
    assert Collector(net,desk).consume_flows()['pending']==expected
    c=net._conn();assert c.execute('SELECT count(*) FROM edges').fetchone()[0]==0;c.close()
    clock[0]=NOW+11;assert Collector(net,desk).consume_flows()['pending']==0


def test_row_budget_cap_cannot_discard_discovery_clock_floor(source,monkeypatch):
    from sniper.office import wallet_watch_collect as module
    net,chain,clock,desk=source;insert(source,bt=NOW-30)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('UPDATE wallet_flow_transactions SET received_at=?,processed_at=?',(NOW-21,NOW-20))
    original_funding=module.verified_sol_funding;original_gate=net._refusal
    def advance(*args):
        facts=original_funding(*args);clock[0]=NOW+10;return facts
    def cap(conn,now,rows=0):
        if rows>2:clock[0]=NOW+1;return 'daily_row_cap'
        return None
    monkeypatch.setattr(module,'verified_sol_funding',advance);monkeypatch.setattr(net,'_refusal',cap)
    assert Collector(net,desk).consume_flows()['pending']==1
    monkeypatch.setattr(module,'verified_sol_funding',original_funding);monkeypatch.setattr(net,'_refusal',original_gate)
    assert Collector(net,desk).consume_flows()['pending']==1
    clock[0]=NOW+11;assert Collector(net,desk).consume_flows()['pending']==0
    c=net._conn();assert c.execute('SELECT known_at FROM edges').fetchone()[0]==NOW+11;c.close()


def test_network_tip_payment_is_recorded_without_expanding_busy_fee_account(source):
    from sniper.services.memecoin_desk_txshape import JITO_TIP_ACCOUNTS
    net,chain,clock,desk=source;tip=sorted(JITO_TIP_ACCOUNTS)[0];u=wire()
    u.transaction.transaction.transaction.message.account_keys[1]=base58.b58decode(tip)
    insert(source,raw=u.SerializeToString())
    Collector(net,desk).consume_flows()
    c=net._conn()
    assert c.execute("SELECT count(*) FROM edges WHERE src=? AND type='funded_by'",(tip,)).fetchone()[0]==1
    assert c.execute('SELECT count(*) FROM watch_list WHERE address=?',(tip,)).fetchone()[0]==0
    c.close()


def test_unknown_fee_infrastructure_keeps_transfer_fact_without_expansion(source,monkeypatch):
    from sniper.services import memecoin_fee_reconciler
    net,chain,clock,desk=source;insert(source)
    def unavailable():raise ImportError('fixture')
    monkeypatch.setattr(memecoin_fee_reconciler,'tip_accounts',unavailable)
    out=Collector(net,desk).consume_flows()
    assert out['reason']=='expansion_infrastructure_unknown'
    c=net._conn();assert c.execute('SELECT count(*) FROM edges').fetchone()[0]==1
    assert c.execute('SELECT count(*) FROM watch_list').fetchone()[0]==1;c.close()


@pytest.mark.parametrize('removed',[False,True])
def test_existing_watch_record_needs_no_funding_graph_walk(source,removed):
    net,chain,clock,desk=source
    W.add(net,desk,address=B,operator_id=None,evidence='fixture',source='host',actor='crew',now=NOW)
    c=net._conn()
    if removed:
        c.execute('UPDATE watch_list SET removed_at=? WHERE address=?',(NOW,B));c.commit()
    def deny_graph(action,table,*args):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table=='edges' else sqlite3.SQLITE_OK
    c.set_authorizer(deny_graph)
    assert Collector(net,desk)._expand(c,A,[B],'fixture',NOW) is None
    c.set_authorizer(None)
    assert (c.execute('SELECT removed_at FROM watch_list WHERE address=?',(B,)).fetchone()[0] is not None)==removed
    c.close()


def test_high_fanout_funding_can_watch_a_bounded_campaign(source,monkeypatch):
    net,chain,clock,desk=source
    monkeypatch.setattr(W,'FANOUT_HUB',1)
    insert(source)
    out=Collector(net,desk).consume_flows()
    assert out['unknown']==0
    child=next(r for r in W.read_list(net) if r['address']==B)
    assert child['operator_id']==f'hub60:{A}:{int((NOW-1)//60)*60}'


def test_hub_campaign_expansion_requires_event_clock(source,monkeypatch):
    net,chain,clock,desk=source
    monkeypatch.setattr(W,'FANOUT_HUB',0)
    c=Collector(net,desk)._open()
    assert Collector(net,desk)._expand(c,A,[B],'fixture',NOW)=='expansion_campaign_clock_unknown'
    assert B not in {r['address'] for r in W.read_list(net)}
    c.close()


def test_hub_campaign_expansion_still_excludes_known_infrastructure(source,monkeypatch):
    net,chain,clock,desk=source
    monkeypatch.setattr(W,'FANOUT_HUB',1)
    monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset({A}))
    insert(source);Collector(net,desk).consume_flows()
    assert B not in {r['address'] for r in W.read_list(net)}


def test_hub_campaign_expansion_keeps_membership_without_twenty_limit(source,monkeypatch):
    net,chain,clock,desk=source
    monkeypatch.setattr(W,'FANOUT_HUB',0)
    children=[str(Pubkey.from_bytes(bytes([n])*32)) for n in range(3,24)]
    c=Collector(net,desk)._open()
    assert Collector(net,desk)._expand(c,A,children,'fixture',NOW,event_at=NOW-1) is None
    watched=W.read_list(net)
    assert len(watched)==22  # all21 recipients remain eligible
    assert len({r['operator_id'] for r in watched if r['operator_id']})==1
    assert children[-1] in {r['address'] for r in watched}
    c.close()


@pytest.mark.parametrize('event_at',[True,float('nan'),float('inf'),-1,NOW+1])
def test_hub_campaign_invalid_event_clock_does_not_add_watches(source,monkeypatch,event_at):
    net,chain,clock,desk=source
    monkeypatch.setattr(W,'FANOUT_HUB',0)
    c=Collector(net,desk)._open()
    assert Collector(net,desk)._expand(c,A,[B],'fixture',NOW,event_at=event_at)=='expansion_campaign_clock_unknown'
    assert len(W.read_list(net))==1
    c.close()


def test_old_verified_hub_campaign_is_not_rejected_by_agent_age_limit(source,monkeypatch):
    net,chain,clock,desk=source
    monkeypatch.setattr(W,'FANOUT_HUB',0)
    monkeypatch.setattr(W,'add',lambda *a,**k:dict(state='added'))
    c=Collector(net,desk)._open()
    assert Collector(net,desk)._expand(c,A,[B],'fixture',NOW,event_at=NOW-121) is None
    c.close()


def test_frontier_status_distinguishes_empty_backlog_and_caught_up(source):
    net,chain,clock,desk=source
    empty=Collector(net,desk).consume_flows(limit=1)
    assert empty['raw_frontier']==0 and empty['raw_caught_up'] is True
    insert(source);insert(source,n=4)
    first=Collector(net,desk).consume_flows(limit=1)
    assert first['raw_frontier']==2 and first['cursor']==1 and first['raw_caught_up'] is False
    last=Collector(net,desk).consume_flows(limit=1)
    assert last['raw_frontier']==2 and last['cursor']==2 and last['raw_caught_up'] is True


@pytest.mark.parametrize('recover',[False,True])
def test_old_pending_gets_a_turn_under_continuous_fresh_work(source,monkeypatch,recover):
    import types
    from sniper.office import wallet_watch_collect as C
    net,chain,clock,desk=source
    insert(source,bt=None);collector=Collector(net,desk,expand=False)
    assert collector.consume_flows(limit=1)['pending']==1
    if recover:
        with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('UPDATE wallet_flow_transactions SET block_time=? WHERE flow_id=1',(NOW-1,))
    insert(source,n=4);insert(source,n=5)
    mono=[0.];original=collector._stream_row;attempts=[]
    def decode(*args):
        attempts.append(args[1]['flow_id'])
        result=original(*args);mono[0]+=1.1
        return result
    monkeypatch.setattr(C,'time',types.SimpleNamespace(monotonic=lambda:mono[0]))
    monkeypatch.setattr(collector,'_stream_row',decode)
    first=collector.consume_flows(limit=1)
    assert attempts==[1] and first['cursor']==1
    assert first['pending']==(0 if recover else 1)
    # A permanently missing old proof must also leave room for fresh input.
    restarted=Collector(net,desk,expand=False)
    monkeypatch.setattr(restarted,'_stream_row',decode)
    second=restarted.consume_flows(limit=1)
    assert attempts[-1]==2 and second['cursor']==2
    assert second['raw_caught_up'] is False


def test_pending_and_fresh_overlap_is_attempted_once(source,monkeypatch):
    net,chain,clock,desk=source;insert(source)
    collector=Collector(net,desk,expand=False);conn=collector._open()
    with conn:conn.execute('INSERT INTO watch_flow_pending VALUES(?,?,?,?,?)',(str(desk/'pumpswap/tape.db'),1,NOW-2,NOW-2,'stream_proof_store_unknown'))
    conn.close();original=collector._stream_row;attempts=[]
    def decode(*args):attempts.append(args[1]['flow_id']);return original(*args)
    monkeypatch.setattr(collector,'_stream_row',decode)
    result=collector.consume_flows(limit=1)
    assert attempts==[1] and result['pending']==0 and result['cursor']==1


def test_large_bounded_page_keeps_failed_proofs_unknown_and_restarts_after_exact_cursor(source):
    net,chain,clock,desk=source
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:
        for i in range(1,501):
            u=wire();info=u.transaction.transaction;info.signature=i.to_bytes(64,'little');info.transaction.signatures[0]=info.signature;info.meta.err.err=b'failed'
            payload=json.dumps(dict(raw_update_b64=base64.b64encode(u.SerializeToString()).decode()))
            c.execute('INSERT INTO wallet_flow_transactions(signature,slot,tx_index,block_time,received_at,processed_at,commitment,payload_json) VALUES(?,?,?,?,?,?,?,?)',(base58.b58encode(info.signature).decode(),100,None,NOW-1,NOW-2,NOW-1,'confirmed',payload))
    first=Collector(net,desk).consume_flows(limit=499)
    assert first['rows']==499 and first['unknown']==499 and first['cursor']==499
    assert first['raw_caught_up'] is False
    assert first['watch_export']['state']=='known'
    second=Collector(net,desk).consume_flows(limit=499)
    assert second['rows']==1 and second['cursor']==500 and second['raw_caught_up'] is True
    assert rows(net.path,'edges')==[]
    assert len(rows(net.path,'watch_flow_receipts'))==500

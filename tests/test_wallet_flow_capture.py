"""Bounded, default-off capture; no env-file reads, channels, RPC or funded DB."""
import asyncio
import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pytest
from sniper.office.wallet_stream_proof import decode_payload
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_desk_universe as U

spec=importlib.util.spec_from_file_location('wallet_flow_capture',Path(__file__).resolve().parents[1]/'scripts/memecoin_desk_universe_worker.py')
W=importlib.util.module_from_spec(spec)
with patch('sniper.util.envfiles.load_env_files'):spec.loader.exec_module(W)
A,B=U.b58encode(bytes([1])*32),U.b58encode(bytes([2])*32)


def frame(n=3, *, filters=('wallet_flow',), slot=123, failed=False):
    u=pb.SubscribeUpdate();u.filters.extend(filters);u.transaction.slot=slot
    info=u.transaction.transaction;info.signature=bytes([n])*64;info.index=0
    info.transaction.signatures.append(info.signature)
    msg=info.transaction.message;msg.header.num_required_signatures=1;msg.header.num_readonly_unsigned_accounts=1
    msg.account_keys.extend([bytes([1])*32,bytes([2])*32,bytes(32)]);msg.recent_blockhash=bytes(32)
    import struct
    ix=msg.instructions.add();ix.program_id_index=2;ix.accounts=bytes([0,1]);ix.data=struct.pack('<IQ',2,100)
    info.meta.fee=5;info.meta.pre_balances.extend([1000,0,0]);info.meta.post_balances.extend([895,100,0]);info.meta.log_messages.append('Program success')
    if failed:info.meta.err.err=b'failed'
    return u.SerializeToString()


def worker(tmp_path,monkeypatch,enabled=True):
    monkeypatch.setenv('MEMECOIN_DESK_PUMPSWAP_CAPTURE','1');monkeypatch.setenv('MEMECOIN_DESK_INTAKE_EMIT','0')
    cfg=W.knobs({});cfg.update(wallet_flow='1' if enabled else '0',txshape_pct='0')
    return W.Worker(cfg,base=tmp_path,sol_price_fn=lambda:None,alert_fn=lambda *a:None)


def listed(w,addresses):
    p=Path(w.ps_ext_path).with_name('watch_wallets.json');p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(dict(wallets=addresses)))


def table(w,name):
    return w.files.conn('swaps_ps',100.).execute('SELECT * FROM '+name).fetchall()


def test_baseline_request_bytes_unchanged_and_empty_invalid_never_all_chain():
    expected=pb.SubscribeRequest(transactions={'pump':pb.SubscribeRequestFilterTransactions(vote=False,failed=False,account_include=[U.PUMP_PROGRAM]),'pumpswap':pb.SubscribeRequestFilterTransactions(vote=False,failed=False,account_include=[W.PUMPSWAP_PROGRAM])},blocks_meta={'meta':pb.SubscribeRequestFilterBlocksMeta()},commitment=pb.CommitmentLevel.CONFIRMED)
    assert W.build_request(pb,True).SerializeToString(deterministic=True)==expected.SerializeToString(deterministic=True)
    for addresses in ([],['bad']):assert 'wallet_flow' not in W.build_request(pb,True,wallet_flow=addresses).transactions
    req=W.build_request(pb,True,wallet_flow=[B,A,A])
    assert list(req.transactions['wallet_flow'].account_include)==sorted([A,B])
    assert not req.transactions['wallet_flow'].HasField('failed')


def test_default_off_never_reads_watchfile_or_queues_flow(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch,False)
    assert not w.wallet_flow_enabled and w.wallet_flow_selectors()==()
    assert w.stats['wallet_flow_selector_errors']==0
    w.push_wallet_flow(frame(),received_at=100.)
    assert not w.wallet_flow_ring and w.wallet_flow_ring_bytes==0


def test_selector_refresh_uses_same_request_stream_and_withdraws_invalid(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);listed(w,[A])
    clock=[0.]
    async def no_wait(seconds):
        assert seconds==.25;clock[0]+=seconds
    monkeypatch.setattr(W,'asyncio',SimpleNamespace(sleep=no_wait))
    monkeypatch.setattr(W,'time',SimpleNamespace(monotonic=lambda:clock[0],time=lambda:100.+clock[0]))
    async def run():
        gen=W.request_updates(w,pb,True)
        first=await anext(gen);assert list(first.transactions['wallet_flow'].account_include)==[A]
        listed(w,[B]);second=await anext(gen);assert list(second.transactions['wallet_flow'].account_include)==[B]
        third=await anext(gen);assert third.HasField('ping') and not third.transactions
        listed(w,['bad']);fourth=await anext(gen);assert 'wallet_flow' not in fourth.transactions and 'pump' in fourth.transactions
        await gen.aclose()
    asyncio.run(run())


def test_separate_ring_is_lower_priority_and_never_blinds_program_capture(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);w.ps_committed_through=99;w.curve_committed_through=88
    w.push(b'pump',received_at=100.);w.push(b'ps',received_at=100.,pumpswap=True)
    raw=b'x'*W.WALLET_FLOW_FRAME_MAX
    for _ in range(70):w.push_wallet_flow(raw,received_at=100.)
    assert w.wallet_flow_ring_bytes<=8*1024*1024 and w.stats['wallet_flow_dropped']>0
    assert w.ring_bytes==4 and w.ps_ring_bytes==2 and (w.ps_committed_through,w.curve_committed_through)==(99,88)
    assert w.next_frame()[2]=='curve';assert w.next_frame()[2]=='pumpswap';assert w.next_frame()[2]=='wallet_flow'
    assert w.stats['gaps']==0 and not w.ps_pending_gaps


def test_flow_only_bypasses_program_rings_cotags_keep_old_route(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch)
    w.queue_frame(frame(),received_at=100.,has_ps=True)
    assert len(w.wallet_flow_ring)==1 and not w.ring and not w.ps_ring
    w.queue_frame(frame(filters=('wallet_flow','pump')),received_at=101.,has_ps=True)
    assert len(w.wallet_flow_ring)==2 and len(w.ring)==1


def test_raw_record_dedupe_failed_and_same_slot_metadata(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);raw=frame()
    w.wallet_flow_process(raw,received_at=100.);w.wallet_flow_process(raw,received_at=101.)
    rows=table(w,'wallet_flow_transactions');assert len(rows)==1
    r=rows[0];assert r[2:5]==(123,None,None) and r[5]==100. and r[7]=='confirmed'
    assert decode_payload(r[8])==raw
    meta=pb.SubscribeUpdate();meta.block_meta.slot=124;meta.block_meta.block_time.timestamp=1700000000
    w.process(meta.SerializeToString(),received_at=102.);assert table(w,'wallet_flow_transactions')[0][4] is None
    meta.block_meta.slot=123;w.process(meta.SerializeToString(),received_at=103.)
    r=table(w,'wallet_flow_transactions')[0];assert r[4]==1700000000 and r[5]==100.
    assert w.stats['wallet_flow_metadata_pending']==0
    w.wallet_flow_process(frame(4,failed=True),received_at=104.)
    assert len(table(w,'wallet_flow_transactions'))==2 and w.stats['wallet_flow_failed']==1


def test_uncapped_payload_and_malformed_frame_do_not_touch_program_gaps(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch)
    w.wallet_flow_process(frame(),received_at=100.)
    assert len(table(w,'wallet_flow_transactions'))==1 and w.stats['wallet_flow_dropped']==0
    w.wallet_flow_process(b'bad',received_at=101.)
    assert w.stats['wallet_flow_errors']>=1 and not w.ps_pending_gaps and not w.curve_pending_gaps


def test_retention_keeps_monotonic_ids_and_exact_byte_accounting(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);w.wallet_flow_process(frame(),received_at=100.)
    c=w.files.conn('swaps_ps',100.);first=table(w,'wallet_flow_transactions')[0]
    w.wallet_flow_maintenance(100.+31*86400)
    assert len(table(w,'wallet_flow_transactions'))==1
    assert c.execute('SELECT payload_bytes FROM wallet_flow_meta WHERE id=1').fetchone()[0]==len(first[8].encode())
    w.wallet_flow_process(frame(4),received_at=100.+31*86400)
    assert table(w,'wallet_flow_transactions')[-1][0]>first[0]
    assert c.execute('SELECT payload_bytes FROM wallet_flow_meta WHERE id=1').fetchone()[0]==sum(len(r[8].encode()) for r in table(w,'wallet_flow_transactions'))

def test_request_submission_is_reported_without_claiming_ack(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);req=W.build_request(pb,True,wallet_flow=[A])
    raw=W.serialize_request(w,pb,req)
    assert raw==req.SerializeToString() and w.wallet_flow_sent==(A,)
    assert w.stats['wallet_flow_submitted_updates']==1
    W.serialize_request(w,pb,pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1)))
    assert w.stats['wallet_flow_submitted_updates']==1

def fail_counter_updates(c):
    c.execute("CREATE TRIGGER fail_flow_counter BEFORE UPDATE ON wallet_flow_meta BEGIN SELECT RAISE(ABORT,'injected'); END")


def test_failed_counter_insert_rolls_back_only_flow_writes(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);c=w.wallet_flow_conn(100.)
    c.execute("INSERT INTO gaps_ps VALUES(50.,'existing_ps',123)")
    fail_counter_updates(c);w.wallet_flow_process(frame(),received_at=100.)
    assert not table(w,'wallet_flow_transactions')
    assert c.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]==0
    assert c.execute("SELECT count(*) FROM gaps_ps WHERE cause='existing_ps'").fetchone()[0]==1


def test_failed_metadata_counter_keeps_block_time_unknown(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);w.wallet_flow_process(frame(),received_at=100.)
    c=w.files.conn('swaps_ps',100.);fail_counter_updates(c)
    w.wallet_flow_metadata(123,1700000000,101.)
    assert table(w,'wallet_flow_transactions')[0][4] is None
    assert c.execute('SELECT pending_metadata FROM wallet_flow_meta').fetchone()[0]==1


def test_maintenance_does_not_reclaim_raw_or_change_payload_counters(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);w.wallet_flow_process(frame(),received_at=100.)
    c=w.files.conn('swaps_ps',100.);before=c.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]
    w.wallet_flow_maintenance(100.+31*86400)
    assert len(table(w,'wallet_flow_transactions'))==1
    assert c.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]==before

def test_restart_preserves_all_payload_and_reports_heartbeat(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);w.wallet_flow_process(frame(),received_at=100.)
    c=w.files.conn('swaps_ps',100.);used=c.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]
    w.files.commit()
    for connection in w.files._open.values():connection.close()
    w=worker(tmp_path,monkeypatch)
    w.wallet_flow_process(frame(4),received_at=101.)
    assert len(table(w,'wallet_flow_transactions'))==2 and w.stats['wallet_flow_dropped']==0
    w.heartbeat(True);beat=json.loads((tmp_path/'universe.heartbeat').read_text())
    assert beat['wallet_flow']['enabled'] and beat['wallet_flow']['ring_cap_bytes']==8*1024*1024
    assert beat['wallet_flow']['payload_bytes']>used and beat['wallet_flow']['selector_acknowledged'] is None

@pytest.mark.parametrize('program',['11111111111111111111111111111111','TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'])
def test_executable_program_selector_rejects_whole_file(tmp_path,monkeypatch,program):
    w=worker(tmp_path,monkeypatch);listed(w,[A,program])
    assert w.wallet_flow_selectors()==() and w.stats['wallet_flow_selector_errors']==1
    assert 'wallet_flow' not in W.build_request(pb,True,wallet_flow=[A,program]).transactions

def test_existing_pump_create_from_setup_lead_is_copied_with_curve_off(tmp_path,monkeypatch):
    import hashlib,struct
    w=worker(tmp_path,monkeypatch);w.curve_capture=False
    w.ps_watch=frozenset({B});w.ps_setup_watch[B]=__import__('time').time()+120
    u=pb.SubscribeUpdate.FromString(frame(filters=('pump',)))
    msg=u.transaction.transaction.transaction.message;msg.account_keys[0]=U.b58decode(B)
    msg.account_keys.append(U.b58decode(U.PUMP_PROGRAM));ix=msg.instructions[0];ix.program_id_index=3
    strings=b''.join(struct.pack('<I',len(s))+s for s in (b'name',b'SYM',b'uri'))
    ix.data=hashlib.sha256(b'global:create').digest()[:8]+strings+bytes([2])*32
    raw=u.SerializeToString();w.process(raw,received_at=100.)
    assert len(w.wallet_flow_ring)==1 and w.wallet_flow_ring[0]==(raw,100.)
    assert not w.curve_capture  # capture does not decode or grant launch/funding authority


def test_cotagged_program_frame_is_not_copied_twice(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);w.ps_watch=frozenset({A})
    raw=frame(filters=('pump','wallet_flow'));w.queue_frame(raw,received_at=100.,has_ps=True)
    w.process(raw,received_at=100.)
    assert len(w.wallet_flow_ring)==1


def test_local_program_copy_rejects_program_watch_target(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);w.ps_watch=frozenset({'11111111111111111111111111111111'})
    w.process(frame(filters=('pump',)),received_at=100.)
    assert not w.wallet_flow_ring

def test_local_copy_failure_does_not_abort_program_processing(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);w.ps_watch=frozenset({A})
    def fail(*args,**kwargs):raise RuntimeError('injected')
    monkeypatch.setattr(w,'wallet_flow_local_copy',fail)
    w.process(frame(filters=('pump',)),received_at=100.)
    assert w.stats['wallet_flow_errors']==1 and not w.ps_pending_gaps


def test_flow_queue_failure_preserves_cotagged_program_queue(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch)
    def fail(*args,**kwargs):raise RuntimeError('injected')
    monkeypatch.setattr(w,'push_wallet_flow',fail)
    w.queue_frame(frame(filters=('wallet_flow','pump')),received_at=100.,has_ps=True)
    assert len(w.ring)==1 and w.stats['wallet_flow_errors']==1

def test_failed_gap_flush_retains_original_snapshot(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);c=w.wallet_flow_conn(100.);w.wallet_flow_gap('ring_overflow',100.)
    class FailLate:
        def __getattr__(self,k):return getattr(c,k)
        def execute(self,sql,*args):
            if sql.startswith('SELECT payload_bytes,pending_metadata FROM wallet_flow_meta'):raise __import__('sqlite3').OperationalError('injected')
            return c.execute(sql,*args)
    monkeypatch.setattr(w,'wallet_flow_conn',lambda now:FailLate())
    w.wallet_flow_maintenance(101.)
    assert not c.execute("SELECT 1 FROM wallet_flow_gaps WHERE reason='ring_overflow'").fetchone()
    assert w.wallet_flow_gaps['ring_overflow'][0]==1


def test_outer_commit_failure_retains_loss_and_reconciles_gauges(tmp_path,monkeypatch):
    import sqlite3
    w=worker(tmp_path,monkeypatch);c=w.wallet_flow_conn(100.);c.commit()
    w.wallet_flow_process(frame(),received_at=100.);w.wallet_flow_gap('ring_overflow',100.)
    w.wallet_flow_maintenance(101.)
    class FailCommit:
        def __getattr__(self,k):return getattr(c,k)
        def commit(self):raise sqlite3.OperationalError('injected outer commit')
    w.files._open[('pumpswap','all')]=FailCommit();w.commit_files()
    assert not c.execute("SELECT 1 FROM wallet_flow_gaps WHERE reason='ring_overflow'").fetchone()
    assert w.wallet_flow_gaps['ring_overflow'][0]==1
    assert w.stats['wallet_flow_payload_bytes']==0 and w.stats['wallet_flow_metadata_pending']==0
    w.files._open[('pumpswap','all')]=c
    w.wallet_flow_gap('ring_overflow',102.);w.wallet_flow_maintenance(102.)
    w.wallet_flow_maintenance(102.5)  # no double-add before an outer commit
    w.wallet_flow_gap('ring_overflow',103.)
    w.commit_files()
    assert w.wallet_flow_gaps['ring_overflow'][0]==1
    assert c.execute("SELECT n FROM wallet_flow_gaps WHERE reason='ring_overflow'").fetchone()[0]==2
    w.wallet_flow_maintenance(104.);w.commit_files()
    assert 'ring_overflow' not in w.wallet_flow_gaps
    assert c.execute("SELECT n FROM wallet_flow_gaps WHERE reason='ring_overflow'").fetchone()[0]==3


def test_successful_flush_is_not_acknowledged_before_outer_commit(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);w.wallet_flow_gap('ring_overflow',100.)
    w.wallet_flow_maintenance(101.)
    assert w.wallet_flow_gaps['ring_overflow'][0]==1
    w.commit_files()
    assert 'ring_overflow' not in w.wallet_flow_gaps

def test_direct_ps_commit_acks_once_before_later_file_failure(tmp_path,monkeypatch):
    import sqlite3
    w=worker(tmp_path,monkeypatch);c=w.wallet_flow_conn(100.)
    w.wallet_flow_gap('ring_overflow',100.);w.wallet_flow_maintenance(101.)
    w.ps_gap('write_error',101.);w.flush_ps_gaps(102.)
    assert 'ring_overflow' not in w.wallet_flow_gaps
    w.wallet_flow_gap('ring_overflow',103.)
    other=w.files.conn('blocks',100.)
    class FailOther:
        def __getattr__(self,k):return getattr(other,k)
        def commit(self):raise sqlite3.OperationalError('injected other file')
    key=('blocks',W.utc_day(100.));w.files._open[key]=FailOther()
    with pytest.raises(sqlite3.Error):w.commit_files()
    assert w.wallet_flow_gaps['ring_overflow'][0]==1
    assert c.execute("SELECT n FROM wallet_flow_gaps WHERE reason='ring_overflow'").fetchone()[0]==1
    w.files._open[key]=other;w.wallet_flow_maintenance(104.);w.commit_files()
    assert c.execute("SELECT n FROM wallet_flow_gaps WHERE reason='ring_overflow'").fetchone()[0]==2
    assert 'ring_overflow' not in w.wallet_flow_gaps


def test_context_manager_commit_acknowledges_only_success(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);c=w.wallet_flow_conn(100.)
    w.wallet_flow_gap('ring_overflow',100.);w.wallet_flow_maintenance(101.)
    with c:pass
    assert 'ring_overflow' not in w.wallet_flow_gaps


def test_post_commit_callback_failure_cannot_relabel_durable_commit(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);c=w.wallet_flow_conn(100.)
    w.wallet_flow_gap('ring_overflow',100.);w.wallet_flow_maintenance(101.)
    def fail(*args):raise RuntimeError('injected callback')
    c.wallet_flow_outcome=fail
    c.commit()  # must not raise: SQLite has already committed the evidence
    assert c.execute("SELECT n FROM wallet_flow_gaps WHERE reason='ring_overflow'").fetchone()[0]==1
    assert c.wallet_flow_callback_errors==1

def test_default_off_keeps_standard_sqlite_connection(tmp_path,monkeypatch):
    import sqlite3
    w=worker(tmp_path,monkeypatch,False)
    assert type(w.files.conn('swaps_ps',100.)) is sqlite3.Connection


def test_context_rollback_keeps_unacknowledged_gaps(tmp_path,monkeypatch):
    w=worker(tmp_path,monkeypatch);c=w.wallet_flow_conn(100.);c.commit()
    w.wallet_flow_gap('ring_overflow',100.);w.wallet_flow_maintenance(101.)
    with pytest.raises(RuntimeError):
        with c:raise RuntimeError('injected context failure')
    assert w.wallet_flow_gaps['ring_overflow'][0]==1 and not w.wallet_flow_staged
    assert not c.execute("SELECT 1 FROM wallet_flow_gaps WHERE reason='ring_overflow'").fetchone()

def test_shared_transaction_rollback_persists_a_flow_unknown_incident(tmp_path,monkeypatch):
    import time
    w=worker(tmp_path,monkeypatch);now=time.time();c=w.wallet_flow_conn(now);c.commit()
    w.wallet_flow_process(frame(),received_at=now)
    assert len(table(w,'wallet_flow_transactions'))==1
    c.rollback()
    assert not table(w,'wallet_flow_transactions')
    w.wallet_flow_maintenance(time.time());c.commit()
    assert c.execute("SELECT n FROM wallet_flow_gaps WHERE reason='transaction_rollback'").fetchone()[0]>=1

def test_ps_storage_error_leaves_flow_gap_and_recovers(tmp_path,monkeypatch):
    import sqlite3,time
    w=worker(tmp_path,monkeypatch);now=time.time();c=w.wallet_flow_conn(now);c.commit()
    w.wallet_flow_process(frame(),received_at=now)
    w.ps_storage_error(sqlite3.OperationalError('injected write failure'))
    assert not table(w,'wallet_flow_transactions')
    w.wallet_flow_maintenance(time.time());w.commit_files()
    assert c.execute("SELECT n FROM wallet_flow_gaps WHERE reason='transaction_rollback'").fetchone()[0]>=1


def test_rolled_back_bootstrap_reinitializes_flow_schema(tmp_path,monkeypatch):
    import time
    w=worker(tmp_path,monkeypatch);now=time.time();c=w.files.conn('swaps_ps',now)
    c.execute("INSERT INTO gaps_ps VALUES(?,'existing_ps',123)",(now,))
    w.wallet_flow_process(frame(),received_at=now);c.rollback()
    assert not w.wallet_flow_ready
    w.wallet_flow_maintenance(time.time());c.commit()
    assert w.wallet_flow_ready and not table(w,'wallet_flow_transactions')
    assert c.execute("SELECT n FROM wallet_flow_gaps WHERE reason='transaction_rollback'").fetchone()[0]>=1
    w.wallet_flow_process(frame(4),received_at=time.time())
    assert len(table(w,'wallet_flow_transactions'))==1


@pytest.mark.parametrize('snapshot',[False,True])
def test_metadata_busy_snapshot_reports_exact_typed_callsite(tmp_path,monkeypatch,snapshot):
    import sqlite3,time
    w=worker(tmp_path,monkeypatch);now=time.time()
    w.wallet_flow_process(frame(),received_at=now);c=w.wallet_flow_conn(now);c.commit()
    if snapshot:
        c.execute('BEGIN');c.execute('SELECT * FROM wallet_flow_meta').fetchone()
    other=sqlite3.connect(w.files.base/'pumpswap/tape.db')
    other.execute('UPDATE wallet_flow_meta SET payload_bytes=payload_bytes+1 WHERE id=1')
    if snapshot:other.commit()
    try:
        w.wallet_flow_metadata(123,int(now)-1,time.time())
        error=w.wallet_flow_last_error
        assert error['stage']=='metadata' and error['sqlite_errorname']==('SQLITE_BUSY_SNAPSHOT' if snapshot else 'SQLITE_BUSY')
        assert error['sqlite_errorcode']==(sqlite3.SQLITE_BUSY_SNAPSHOT if snapshot else sqlite3.SQLITE_BUSY)
        assert error['site'].startswith('wallet_flow_metadata:')
        assert error['at']>=now and 'message' not in error
        assert w.wallet_flow_gaps['metadata_error'][0]==1
        c.rollback()
        assert w.wallet_flow_last_error==error  # Outer rollback must not erase the observed failure.
    finally:other.rollback();other.close();c.rollback();c.close()


def test_archive_staging_uses_capture_commit_and_rollback_restores_payload_counter(tmp_path,monkeypatch):
    import time
    from sniper.office import wallet_flow_archive as A
    w=worker(tmp_path,monkeypatch);now=time.time();w.wallet_flow_process(frame(),received_at=now)
    c=w.wallet_flow_conn(now);c.commit();path=tmp_path/'pumpswap/tape.db';directory=path.parent/'wallet-flow-archive'
    before=c.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]
    assert A.archive_rows(path,directory,before=now+1)['queued']==1
    batch=A.next_batch(path,directory);w.wallet_archive_batch=batch
    w.wallet_flow_maintenance(time.time())
    assert c.in_transaction and w.wallet_archive_status['staged']==1
    assert w.wallet_archive_status['last_committed_at'] is None
    c.rollback()
    assert w.stats['wallet_flow_payload_bytes']==before and w.wallet_archive_batch==batch
    assert w.wallet_archive_status['staged']==0
    w.wallet_flow_maintenance(time.time());c.commit()
    assert w.wallet_archive_status['last_committed_at']>=now
    assert w.wallet_archive_batch is None and w.wallet_archive_status['staged']==0
    assert w.stats['wallet_flow_payload_bytes']==c.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]
    assert w.stats['wallet_flow_payload_bytes']!=before


def test_archive_verification_is_single_background_job_not_capture_io(tmp_path,monkeypatch):
    import threading,time
    from sniper.office import wallet_flow_archive as A
    w=worker(tmp_path,monkeypatch);now=time.time();w.wallet_flow_process(frame(),received_at=now);w.wallet_flow_conn(now).commit()
    path=tmp_path/'pumpswap/tape.db';directory=path.parent/'wallet-flow-archive'
    assert A.archive_rows(path,directory,before=now+1)['queued']==1
    original=A.next_batch;threads=[];owner=threading.get_ident()
    def verify(*a,**kw):threads.append(threading.get_ident());return original(*a,**kw)
    monkeypatch.setattr(A,'next_batch',verify)
    async def run():
        w.wallet_flow_archive_poll();future=w.wallet_archive_future
        w.wallet_flow_archive_poll();assert w.wallet_archive_future is future
        await asyncio.wait_for(asyncio.shield(future),1)
        w.wallet_flow_archive_poll()
        assert w.wallet_archive_batch['items'] and w.wallet_archive_future is None
    asyncio.run(run())
    assert len(threads)==1 and threads[0]!=owner


def test_archive_inner_maintenance_failure_cannot_ack_rolled_back_staging(tmp_path,monkeypatch):
    import time,sqlite3
    from sniper.office import wallet_flow_archive as A
    w=worker(tmp_path,monkeypatch);now=time.time();w.wallet_flow_process(frame(),received_at=now)
    c=w.wallet_flow_conn(now);c.commit();path=tmp_path/'pumpswap/tape.db';directory=path.parent/'wallet-flow-archive'
    A.archive_rows(path,directory,before=now+1);w.wallet_archive_batch=A.next_batch(path,directory)
    original=w.wallet_flow_archive_apply
    def fail_after_stage(*args):original(*args);raise sqlite3.OperationalError('disk full after archive staging')
    monkeypatch.setattr(w,'wallet_flow_archive_apply',fail_after_stage)
    w.wallet_flow_maintenance(time.time());c.commit()
    assert w.wallet_archive_status['last_committed_at'] is None and w.wallet_archive_status['staged']==0
    assert w.wallet_archive_batch is not None
    assert c.execute('SELECT payload_archived FROM wallet_flow_transactions').fetchone()[0]==0


def test_failed_outer_commit_keeps_archive_batch_staged_without_double_charge(tmp_path,monkeypatch):
    import time
    from sniper.office import wallet_flow_archive as A
    w=worker(tmp_path,monkeypatch);now=time.time();w.wallet_flow_process(frame(),received_at=now)
    c=w.wallet_flow_conn(now);c.commit();path=tmp_path/'pumpswap/tape.db';directory=path.parent/'wallet-flow-archive'
    A.archive_rows(path,directory,before=now+1);w.wallet_archive_batch=A.next_batch(path,directory)
    w.wallet_flow_maintenance(time.time());accounted=c.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]
    w.wallet_flow_transaction('commit_failed')
    assert w.wallet_archive_staged is not None and w.wallet_archive_status['last_committed_at'] is None
    w.wallet_flow_maintenance(time.time())
    assert c.execute('SELECT payload_bytes FROM wallet_flow_meta').fetchone()[0]==accounted
    c.commit()
    assert w.wallet_archive_status['last_committed_at'] is not None and w.wallet_archive_batch is None


def test_archive_apply_and_commit_do_not_read_packs_or_fsync(tmp_path,monkeypatch):
    import time
    from sniper.office import wallet_flow_archive as A
    w=worker(tmp_path,monkeypatch);now=time.time();w.wallet_flow_process(frame(),received_at=now)
    c=w.wallet_flow_conn(now);c.commit();path=tmp_path/'pumpswap/tape.db';directory=path.parent/'wallet-flow-archive'
    A.archive_rows(path,directory,before=now+1);w.wallet_archive_batch=A.next_batch(path,directory)
    def unsafe(*args):raise AssertionError('archive filesystem work reached producer apply/commit')
    monkeypatch.setattr(A,'read_payload',unsafe);monkeypatch.setattr(A,'_publish',unsafe);monkeypatch.setattr(A,'_sync_directory',unsafe)
    w.wallet_flow_maintenance(time.time());c.commit()
    assert w.wallet_archive_status['last_committed_at'] is not None


def test_archive_capture_open_refuses_path_replaced_during_connect(tmp_path,monkeypatch):
    import sqlite3,time
    w=worker(tmp_path,monkeypatch);now=time.time();path=tmp_path/'pumpswap/tape.db';path.parent.mkdir(parents=True,exist_ok=True)
    initial=sqlite3.connect(path);initial.execute('CREATE TABLE original(value)');initial.commit();initial.close()
    real=sqlite3.connect;opened=[]
    def replaced(database,*args,**kwargs):
        conn=real(database,*args,**kwargs)
        if Path(database)==path and kwargs.get('factory') is W.WalletFlowConnection:
            opened.append(conn);path.rename(path.with_name('old.db'))
            replacement=real(path);replacement.execute('CREATE TABLE replacement(value)');replacement.commit();replacement.close()
        return conn
    monkeypatch.setattr(W.sqlite3,'connect',replaced)
    with pytest.raises(sqlite3.OperationalError,match='identity'):
        w.files.conn('swaps_ps',now)
    assert ('pumpswap','all') not in w.files._open
    with pytest.raises(sqlite3.ProgrammingError):opened[0].execute('SELECT 1')


@pytest.mark.parametrize('method',['execute','executemany'])
def test_implicit_sqlite_rollback_clears_archive_staging_and_keeps_gap_unknown(tmp_path,monkeypatch,method):
    import time,sqlite3
    from sniper.office import wallet_flow_archive as A
    w=worker(tmp_path,monkeypatch);now=time.time();w.wallet_flow_process(frame(),received_at=now)
    c=w.wallet_flow_conn(now);c.commit();path=tmp_path/'pumpswap/tape.db';directory=path.parent/'wallet-flow-archive'
    A.archive_rows(path,directory,before=now+1);batch=A.next_batch(path,directory);w.wallet_archive_batch=batch
    w.wallet_flow_maintenance(time.time());w.wallet_flow_transaction('commit_failed')
    assert w.wallet_archive_staged is not None
    sql='INSERT OR ROLLBACK INTO wallet_flow_meta VALUES(1,0,0)'
    with pytest.raises(sqlite3.IntegrityError):
        if method=='execute':c.execute(sql)
        else:c.executemany(sql,[()])
    assert not c.in_transaction and w.wallet_archive_staged is None
    assert w.wallet_archive_batch==batch and w.wallet_archive_status['staged']==0
    assert w.wallet_archive_status['last_committed_at'] is None
    assert w.wallet_flow_last_commit=='unknown' and w.wallet_flow_gaps['transaction_rollback'][0]==1
    c.execute("INSERT OR IGNORE INTO gaps_ps VALUES(?,'unrelated',123)",(now,));c.commit()
    assert w.wallet_archive_status['last_committed_at'] is None  # New transaction cannot acknowledge rolled-back archive.
    assert c.execute('SELECT payload_archived FROM wallet_flow_transactions').fetchone()[0]==0


def test_implicit_rollback_callback_failure_preserves_original_sqlite_exception():
    import sqlite3
    c=sqlite3.connect(':memory:',factory=W.WalletFlowConnection)
    c.execute('CREATE TABLE unique_row(id INTEGER PRIMARY KEY)');c.execute('INSERT INTO unique_row VALUES(1)');c.commit()
    def fail(*args):raise RuntimeError('injected callback failure')
    c.wallet_flow_outcome=fail;c.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(sqlite3.IntegrityError):c.execute('INSERT OR ROLLBACK INTO unique_row VALUES(1)')
        assert c.wallet_flow_callback_errors==1 and not c.in_transaction
    finally:c.close()


def test_implicit_rollback_inside_savepoint_notifies_once_per_transaction():
    import sqlite3
    c=sqlite3.connect(':memory:',factory=W.WalletFlowConnection);events=[];c.wallet_flow_outcome=events.append
    c.execute('CREATE TABLE unique_row(id INTEGER PRIMARY KEY)');c.execute('INSERT INTO unique_row VALUES(1)');c.commit();events.clear()
    try:
        for expected in (1,2):
            with pytest.raises(sqlite3.IntegrityError):
                with W.wallet_flow_write(c):c.execute('INSERT OR ROLLBACK INTO unique_row VALUES(1)')
            assert events.count('rollback')==expected
            assert not c.in_transaction
    finally:c.close()


def test_selector_digest_binds_actual_submission_without_changing_wire(tmp_path,monkeypatch):
    from sniper.office.wallet_selector_identity import selector_digest
    w=worker(tmp_path,monkeypatch)
    assert w.wallet_flow_submitted_digest is None
    first=W.build_request(pb,True,wallet_flow=[A])
    assert W.serialize_request(w,pb,first)==first.SerializeToString()
    assert w.wallet_flow_submitted_digest==selector_digest([A])
    w.heartbeat(False)
    snapshot=json.loads((tmp_path/'universe.heartbeat').read_text())['wallet_flow']
    assert snapshot['submitted_selector_digest']==selector_digest([A])
    assert snapshot['selector_acknowledged'] is None
    previous=w.wallet_flow_submitted_digest
    W.serialize_request(w,pb,pb.SubscribeRequest(ping=pb.SubscribeRequestPing(id=1)))
    assert w.wallet_flow_submitted_digest==previous
    W.serialize_request(w,pb,W.build_request(pb,True,wallet_flow=[B]))
    assert len(w.wallet_flow_sent)==1 and w.wallet_flow_submitted_digest!=previous
    assert w.wallet_flow_submitted_digest==selector_digest([B])
    W.serialize_request(w,pb,W.build_request(pb,True))
    assert w.wallet_flow_submitted_digest==selector_digest([])


def test_selector_digest_is_set_identity_and_rejects_bad_values():
    from sniper.office.wallet_selector_identity import selector_digest
    assert selector_digest([B,A,A])==selector_digest([A,B])
    assert selector_digest([A])!=selector_digest([B])
    for bad in (None,'abc',['bad'],[None],[A,3]):
        with pytest.raises(ValueError):selector_digest(bad)

import copy
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_cpmm as C
from sniper.services import memecoin_desk_cpmm_capture as S

FIX = json.loads((Path(__file__).parent/'fixtures/desk_cpmm_transaction.json').read_text())


def fact():
    b=C.pool_binding(FIX['frame']['account_keys'][3], FIX['binding_accounts'],999,FIX['binding_context_slot'])
    return C.decode_transaction(copy.deepcopy(FIX['frame']),{b['pool']:b})['rows'][0]


def test_atomic_text_and_identity_survive_restart_and_midnight(tmp_path):
    row=fact(); row['token_atomic']=str(2**64-1)
    with S.Store(tmp_path) as s:
        s.record(row)
    with S.Store(tmp_path) as s:
        assert not s.record(row)
        assert s.conn.execute('SELECT token_atomic,typeof(token_atomic) FROM swaps').fetchone()==(str(2**64-1),'text')
        bad=dict(row,token_atomic='1')
        with pytest.raises(S.IntegrityLoss): s.record(bad)
        assert s.conn.execute('SELECT count(*) FROM swaps').fetchone()[0]==1


def test_binding_is_immutable_and_never_backdates_metadata(tmp_path):
    b=C.pool_binding(FIX['frame']['account_keys'][3],FIX['binding_accounts'],1001,FIX['binding_context_slot'])
    with S.Store(tmp_path) as s:
        s.bind(b);s.bind(b)
        assert s.bindings(1000)=={}
        assert s.bindings(1001)[b['pool']]==b
        with pytest.raises(S.IntegrityLoss):s.bind(dict(b,base_decimals=8))


def test_fence_only_commits_queued_data_and_gap_breaks_continuity(tmp_path):
    with S.Store(tmp_path) as s:
        s.begin(990)
        s.record(fact());s.fence(101,1002)
        assert s.head()['through']==1002 and s.head()['from']==990
        s.gap('disconnect',1003,1005)
        s.fence(102,1006)
        assert s.conn.execute('SELECT cause,start_at,end_at FROM gaps').fetchone()==('disconnect',1003,1005)
        late=dict(fact(),event_id='late',received_at=1001)
        s.record(late)
        assert s.conn.execute("SELECT count(*) FROM gaps WHERE cause='late_frame'").fetchone()[0]==1
        with pytest.raises(S.IntegrityLoss):s.fence(100,1007)


def test_read_rpc_budget_reserved_persistently_before_calls(tmp_path):
    with S.Store(tmp_path) as s:
        assert s.reserve_rpc(1000,daily_cap=2,min_spacing=10)
        assert not s.reserve_rpc(1001,daily_cap=2,min_spacing=10)
    with S.Store(tmp_path) as s:
        assert s.reserve_rpc(1010,daily_cap=2,min_spacing=10)
        assert not s.reserve_rpc(1020,daily_cap=2,min_spacing=10)
        assert s.reserve_rpc(86401,daily_cap=2,min_spacing=10)


def test_bounded_queue_overflow_never_grants_clean_coverage(tmp_path):
    with S.Store(tmp_path) as s:
        cap=S.Capture(s,max_bytes=10)
        assert cap.offer(b'12345',1000)
        assert not cap.offer(b'123456',1001)
        assert cap.pending_gap==1001
        cap.flush_loss(1002)
        assert s.conn.execute('SELECT cause FROM gaps').fetchone()[0]=='queue_overflow'
        assert cap.pop()==(b'12345',1000) and cap.pop() is None
        assert cap.bytes==0


def test_disk_cap_stops_without_deletion_or_fresh_fence(tmp_path):
    with S.Store(tmp_path,max_bytes=1) as s:
        with pytest.raises(S.StorageFull):s.record(fact())
        assert s.head()['through'] is None and s.path.exists()


def test_proto_adapter_preserves_loaded_keys_and_unknown_zero_index():
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    u=pb.SubscribeUpdate();u.transaction.slot=42
    i=u.transaction.transaction;i.signature=bytes([3])*64
    i.transaction.message.account_keys.append(bytes([1])*32)
    i.transaction.message.header.num_required_signatures=1
    i.meta.loaded_writable_addresses.append(bytes([2])*32)
    i.meta.loaded_readonly_addresses.append(bytes([4])*32)
    f=S.transaction_frame(u,received_at=1000,clock_offset_s=7)
    assert f['observed_at']==993 and f['slot']==42 and len(f['account_keys'])==3
    assert f['tx_index'] is None
    i.index=8
    assert S.transaction_frame(u,received_at=1000,clock_offset_s=None)['tx_index']==8


def test_provider_failure_is_typed_and_lookup_never_upgrades_first_row(tmp_path):
    with S.Store(tmp_path) as s:
        p=S.Processor(s,lookup=lambda *args: (_ for _ in ()).throw(TimeoutError()))
        p.transaction(FIX['frame'],now=1002)
        assert s.conn.execute('SELECT cause FROM gaps WHERE cause=?',('rpc_timeout',)).fetchone()
        row=json.loads(s.conn.execute('SELECT payload FROM swaps').fetchone()[0])
        assert row['binding_status']=='unknown'
        assert s.conn.execute('SELECT sum(used) FROM rpc_budget').fetchone()[0]==1
        p.transaction(FIX['frame'],now=1003)
        assert s.conn.execute('SELECT sum(used) FROM rpc_budget').fetchone()[0]==1


def test_successful_lookup_is_prior_only_for_future_transactions(tmp_path):
    with S.Store(tmp_path) as s:
        b=C.pool_binding(FIX['frame']['account_keys'][3],FIX['binding_accounts'],1002,FIX['binding_context_slot'])
        p=S.Processor(s,lookup=lambda *args:b)
        p.transaction(FIX['frame'],now=1002)
        f=copy.deepcopy(FIX['frame']);f['received_at']=1003;f['signature']='4'*88
        p.transaction(f,now=1004)
        rows=[json.loads(r[0]) for r in s.conn.execute('SELECT payload FROM swaps ORDER BY received_at')]
        assert [r['binding_status'] for r in rows]==['unknown','proven']
        assert s.conn.execute('SELECT count(*) FROM bindings').fetchone()[0]==1


def test_older_slot_received_after_fence_records_a_continuity_gap(tmp_path):
    with S.Store(tmp_path) as s:
        s.begin(990);s.fence(999999999,1000)
        row=fact();row['received_at']=1001
        s.record(row)
        assert s.conn.execute("SELECT cause FROM gaps WHERE cause='late_slot'").fetchone()


def test_proto_adapter_requires_explicit_transaction_metadata():
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    u=pb.SubscribeUpdate();u.transaction.slot=42
    u.transaction.transaction.signature=bytes([3])*64
    with pytest.raises(C.DecodeLoss,match='metadata_unknown'):
        S.transaction_frame(u,received_at=1000)


def test_second_writer_is_refused_without_blocking_readers(tmp_path):
    with S.Store(tmp_path):
        with pytest.raises(BlockingIOError):S.Store(tmp_path)
    with S.Store(tmp_path):pass


def test_storage_failure_never_publishes_a_new_fence(tmp_path):
    with S.Store(tmp_path) as s:
        s.begin(990);s.fence(100,1000)
        s.conn.execute('PRAGMA query_only=ON')
        with pytest.raises(sqlite3.OperationalError):s.fence(101,1001)
        conn=sqlite3.connect(s.path)
        try:assert conn.execute('SELECT through FROM head').fetchone()[0]==1000
        finally:conn.close()


def test_gap_writes_obey_the_same_disk_stop(tmp_path):
    with S.Store(tmp_path,max_bytes=1) as s:
        with pytest.raises(S.StorageFull):s.gap('error',1000,1001)


@pytest.mark.parametrize('code,cause',[(429,'rpc_rate_limit'),(403,'rpc_forbidden'),(500,'rpc_rejected')])
def test_provider_http_failures_remain_typed(tmp_path,code,cause):
    from urllib.error import HTTPError
    with S.Store(tmp_path) as s:
        p=S.Processor(s,lookup=lambda *args:(_ for _ in ()).throw(HTTPError('redacted',code,'error',{},None)))
        p.transaction(FIX['frame'],now=1002)
        assert s.conn.execute('SELECT cause FROM gaps WHERE cause=?',(cause,)).fetchone()


def test_provider_backoff_survives_process_restart(tmp_path):
    with S.Store(tmp_path) as s:
        p=S.Processor(s,lookup=lambda *args:(_ for _ in ()).throw(TimeoutError()))
        p.transaction(FIX['frame'],now=1002)
        assert p.retry_at==1022
    with S.Store(tmp_path) as s:
        p=S.Processor(s,lookup=lambda *args:(_ for _ in ()).throw(AssertionError('cooldown lost')))
        assert p.retry_at==1022
        p.transaction(FIX['frame'],now=1012)
        assert s.conn.execute('SELECT sum(used) FROM rpc_budget').fetchone()[0]==1

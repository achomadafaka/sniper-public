"""The volume source must prove coverage before it can affect an entry."""
import threading

import pytest
from sniper.services import memecoin_volume_writer as w
from sniper.services.memecoin_evm_v4_events import V4_SWAP
from sniper.execution.evm_v4_launchpad import pool_id_for_key

CHAIN='4663'
MINT='0x'+'ab'*20
KEY={'currency0':w.NATIVE,'currency1':MINT,'fee':3000,'tick_spacing':60,'hooks':w.NATIVE}
POOL=pool_id_for_key(KEY)

def swap(block=4, index=0, tx='0xother', block_hash=None):
    """A V4 Swap log. `block_hash` FOLLOWS THE HEADER, never the height: a node puts the same hash
    string on the header and on every log of that block (verified against the chain 2026-09-23,
    block 70666239). A test that gives a height two spellings makes the canonical-identity join
    read one block as two."""
    words=[10**18, (1<<256)-10**18, 1, 1, 0, 3000]
    return {'address':w.MANAGERS[CHAIN],'topics':[V4_SWAP,POOL,'0x'+'00'*32],
            'data':'0x'+''.join(f'{v:064x}' for v in words),'blockNumber':hex(block),
            'logIndex':hex(index),'transactionHash':tx,
            'blockHash':block_hash or f'0x{block:x}'}

@pytest.fixture(autouse=True)
def _no_writer_thread_outlives_its_test():
    """A writer thread a test starts ends inside that test. A hop-prices-4663 thread that died after
    its test landed its exception in whichever test ran next (Gamma 2026-10-02, -W error)."""
    before = set(threading.enumerate())
    yield
    left = [t.name for t in threading.enumerate() if t not in before and t.is_alive()
            and t.name.startswith(('hop-prices-', 'v3-pools-'))]
    assert not left, left


@pytest.fixture
def tape(tmp_path):
    c=w.connect(tmp_path/'tape.db');t=w.VolumeTape(c,CHAIN)
    t.register_pool(POOL,KEY,1000)
    t.head({'number':'0x1','timestamp':hex(1000),'hash':'0x1'},1000)
    t.log(swap(1, tx="0xwarmup"),1000)
    for n,ts in [(2,1100),(3,1200),(4,1301)]:
        t.head({'number':hex(n),'timestamp':hex(ts),'hash':f'0x{n:x}'},ts)
    t.set_quote_price(2400,1301)
    yield t
    c.close()

def test_duplicates_and_source_exclusion(tape):
    tape.log(swap(tx='0xsource'),1301);tape.log(swap(tx='0xsource'),1301)
    tape.log(swap(index=1),1301)
    out=w.measure(tape.conn,CHAIN,MINT,1301,exclude_tx='0xsource')
    assert out['swaps']==1 and out['usd']==2400
    assert out['coverage']=='partial' and out['pools'][0]['pool']==POOL

def test_quiet_window_is_zero_only_after_full_coverage(tape):
    assert w.measure(tape.conn,CHAIN,MINT,1301)['usd']==0
    tape.reset(1301)
    assert w.measure(tape.conn,CHAIN,MINT,1301)['usd'] is None

def test_gap_and_reorg_reset_the_window(tape):
    # Block 5 never arrives. The hole is held for the reorder grace, and while it is open the
    # frontier does NOT advance -- coverage must never claim a block whose predecessor is missing.
    for n,ts in [(6,1302),(7,1303),(8,1304)]:
        tape.head({'number':hex(n),'timestamp':hex(ts),'hash':hex(n)},ts)
    assert tape.last_block==4, 'the frontier stops at the hole'
    assert _gap_resets(tape)==[], 'still inside the grace'
    for n,ts in [(9,1305),(10,1306),(11,1307),(12,1308),(13,1309)]:
        tape.head({'number':hex(n),'timestamp':hex(ts),'hash':hex(n)},ts)
    assert any('hole_expired:5' in r[1] for r in _gap_resets(tape)), _gap_resets(tape)


def test_unknown_header_is_not_silent_zero(tape):
    tape.log(swap(99),1301)
    assert w.measure(tape.conn,CHAIN,MINT,1301)['usd'] is None


def test_pool_created_after_start_is_visible(tape):
    key=dict(KEY,currency1='0x'+'cd'*20);pool=pool_id_for_key(key)
    tape.register_pool(pool,key,1301)
    out=w.measure(tape.conn,CHAIN,key['currency1'],1301)
    assert out['usd']==0
    assert out['pools'][0]['pool']==pool


def test_stale_price_is_unknown(tape):
    tape.log(swap(),1301)
    tape.set_quote_price(2400,1000)
    assert w.measure(tape.conn,CHAIN,MINT,1301)['usd'] is None


def test_removed_log_invalidates_coverage(tape):
    tape.log(dict(swap(),removed=True),1301)
    assert w.measure(tape.conn,CHAIN,MINT,1301)['usd'] is None


def test_funded_ledger_path_is_refused(tmp_path,monkeypatch):
    monkeypatch.setenv('SNIPER_STATE_ROOT',str(tmp_path))
    with pytest.raises(ValueError):w.connect(tmp_path/'sniper_tracker.db')


def test_initialize_after_start_registers_its_verified_pool(tape):
    from sniper.services.memecoin_evm_v4_events import V4_INITIALIZE
    key=dict(KEY,currency1='0x'+'de'*20);pool=pool_id_for_key(key)
    words=[3000,60,0,1,0]
    log={'address':w.MANAGERS[CHAIN],'topics':[V4_INITIALIZE,pool,'0x'+'00'*32,
                                     '0x'+'00'*12+key['currency1'][2:]],
         'data':'0x'+''.join(f'{v:064x}' for v in words)}
    tape.log(log,1301)
    assert w.measure(tape.conn,CHAIN,key['currency1'],1301)['pools'][0]['pool']==pool


def test_coverage_is_not_inherited_across_a_process_restart(tape):
    new=w.VolumeTape(tape.conn,CHAIN)
    assert w.measure(new.conn,CHAIN,MINT,1301)['usd'] is None


def test_watchdog_refuses_a_mutable_checkout(tmp_path):
    import os, subprocess
    from pathlib import Path
    script=Path('scripts/memecoin_onchain_volume_watchdog.sh').resolve()
    out=subprocess.run(['bash',str(script)],env={**os.environ,'SNIPER_STATE_ROOT':str(tmp_path)},capture_output=True)
    assert out.returncode==1 and not (tmp_path/'data').exists()


def test_watchdog_stays_inert_when_the_release_has_no_opt_in(tmp_path):
    import os, shutil, subprocess
    from pathlib import Path
    scripts=tmp_path/'.runtime/memecoin/releases/test/scripts';scripts.mkdir(parents=True)
    script=scripts/'memecoin_onchain_volume_watchdog.sh'
    shutil.copyfile(Path('scripts')/script.name,script)
    (tmp_path/'.env').write_text('MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS=\n')
    out=subprocess.run(['bash',str(script)],env={**os.environ,'SNIPER_STATE_ROOT':str(tmp_path)},capture_output=True)
    assert out.returncode==0 and not (tmp_path/'data').exists()


def test_stream_loop_uses_epoch_clock_and_owns_only_its_database(tmp_path,monkeypatch):
    import json,sqlite3
    from sniper.execution import evm_v4_quote,evm_wallet
    from sniper.util import clock
    monkeypatch.setattr(clock,'real_utc_now',lambda:1000.0)
    monkeypatch.setattr(evm_v4_quote,'_stored_pool_key',lambda *a:KEY)
    monkeypatch.setattr(evm_wallet,'quote_usd_price',lambda *a:2400)
    from sniper.settings import memecoin as _cfg
    monkeypatch.setattr(_cfg,'MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED',False)
    messages=[{'id':1,'result':hex(int(CHAIN))},{'id':10,'result':'0x'+f'{6:064x}'},
              {'id':2,'result':'heads'},{'id':3,'result':'logs'},
              {'params':{'subscription':'heads','result':{'number':'0x1','timestamp':hex(1000),'hash':'0x1'}}},
              {'params':{'subscription':'logs','result':swap(1)}}]
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def send(self,*a):pass
        def recv(self,**k):
            if not messages:raise KeyboardInterrupt
            return json.dumps(messages.pop(0))
    path=tmp_path/'volume.db'
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN,'wss://unit-test',path=path,connect_fn=lambda *a,**k:Socket())
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT COUNT(*) FROM pool_ticks').fetchone()[0]==1
        assert c.execute('SELECT since FROM volume_feed').fetchone()[0] is None
    assert not (tmp_path/'sniper_tracker.db').exists()


@pytest.mark.parametrize("db_value", [None, "sniper_tracker.db"])
def test_worker_uses_the_state_root_for_pool_identity_reads(tmp_path,monkeypatch,db_value):
    import os,runpy
    from sniper.settings import memecoin as cfg
    monkeypatch.setenv('SNIPER_STATE_ROOT',str(tmp_path))
    monkeypatch.delenv('DB_PATH',raising=False)
    if db_value:monkeypatch.setenv('DB_PATH',db_value)
    monkeypatch.setenv('MEMECOIN_ONCHAIN_M5_WS_URL_4663','wss://unit-test')
    monkeypatch.setattr(cfg,'MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS',{'4663'})
    paths=[]
    monkeypatch.setattr(w,'run',lambda *a,**k:paths.append(os.getenv('DB_PATH')))
    script=runpy.run_path('scripts/memecoin_onchain_volume_worker.py')
    assert script['main']()==0
    assert paths==[str(tmp_path/'sniper_tracker.db')]


def test_gate_reader_uses_one_database_snapshot(tape,monkeypatch):
    from sniper.util import clock
    monkeypatch.setattr(clock,'real_utc_now',lambda:1301)
    path=tape.conn.execute('PRAGMA database_list').fetchone()[2]
    monkeypatch.setattr(w,'db_path',lambda:path)
    original=w.measure
    def update_during_read(conn,*args,**kwargs):
        tape.conn.execute('UPDATE volume_feed SET through=999999')
        tape.conn.commit()
        return original(conn,*args,**kwargs)
    monkeypatch.setattr(w,'measure',update_during_read)
    assert w.read_for_gate(CHAIN,MINT)['usd']==0


# ------------------------------------------------------------------ 2026-09-16 coverage and liveness
def _usdg():
    from sniper.settings import memecoin as cfg
    return cfg.MEMECOIN_ONCHAIN_M5_USD_STABLES[CHAIN][0]


def _usdg_pool(tape):
    usdg=_usdg()
    key={'currency0':usdg,'currency1':MINT,'fee':500,'tick_spacing':10,'hooks':w.NATIVE}
    if int(usdg,16)>int(MINT,16):
        key=dict(key,currency0=MINT,currency1=usdg)
    pool=pool_id_for_key(key)
    tape.register_pool(pool,key,1301)
    return pool, key


def _stable_swap(pool, key, usd, tx, block=4, index=7):
    stable=[int(usd*10**6), -10**18] if key['currency0']==_usdg() else [-10**18, int(usd*10**6)]
    words=[v % (1<<256) for v in stable]+[1,1,0,500]
    return {'address':w.MANAGERS[CHAIN],'topics':[V4_SWAP,pool,'0x'+'00'*32],
            'data':'0x'+''.join(f'{v:064x}' for v in words),'blockNumber':hex(block),
            'logIndex':hex(index),'transactionHash':tx,'blockHash':f'0x{block:x}'}


def test_a_usd_stable_pool_is_priced_with_its_cached_onchain_decimals(tape):
    assert tape.set_token_decimals(_usdg(),6,1301)
    pool,key=_usdg_pool(tape)
    tape.set_quote_price(2400,1301)
    tape.log(_stable_swap(pool,key,5.0,'0xstable'),1301)
    out=w.measure(tape.conn,CHAIN,MINT,1301)
    assert out['usd']==pytest.approx(5.0) and out['unpriced_pools']==[]


def test_a_stable_without_cached_decimals_is_skipped_never_assumed_six(tape):
    pool,key=_usdg_pool(tape)
    tape.log(_stable_swap(pool,key,5.0,'0xstable'),1301)
    tape.log(swap(tx='0xeth'),1301)
    out=w.measure(tape.conn,CHAIN,MINT,1301)
    assert out['usd']==2400
    assert out['coverage']=='partial' and [p['pool'] for p in out['unpriced_pools']]==[pool]


@pytest.mark.parametrize('bad',[None,-1,256,'6',6.5,True])
def test_an_invalid_decimals_answer_is_never_cached(tape,bad):
    assert not tape.set_token_decimals(_usdg(),bad,1301)
    assert tape.token_decimals(_usdg()) is None


def test_the_window_covers_only_the_current_connection_and_says_how_much(tape):
    tape.log(swap(4,tx='0xbefore_reset'),1301)
    tape.reset(1302,'disconnect:test')
    tape.head({'number':'0x5','timestamp':hex(1310),'hash':'0x5'},1310)
    tape.head({'number':'0x6','timestamp':hex(1400),'hash':'0x6'},1400)
    tape.log(swap(6,tx='0xinside'),1400)
    tape.set_quote_price(2400,1400)
    out=w.measure(tape.conn,CHAIN,MINT,1400)
    assert out['usd']==2400 and out['swaps']==1
    assert out['covered_s']==pytest.approx(90) and out['window_s']==pytest.approx(90)
    assert out['coverage']=='partial'


def test_a_full_window_reports_its_full_coverage(tape):
    assert w.measure(tape.conn,CHAIN,MINT,1301)['covered_s']==pytest.approx(300)


def test_a_reorged_swap_is_deleted_so_a_short_window_cannot_count_it(tape):
    tape.reset(1302,'test')
    for n,ts in [(5,1310),(6,1320)]:
        tape.head({'number':hex(n),'timestamp':hex(ts),'hash':f'0x{n:x}'},ts)
    tape.log(swap(6,tx='0xreorged'),1320)
    tape.log(dict(swap(6,tx='0xreorged'),removed=True),1321)
    for n,ts in [(5,1315),(6,1320),(7,1400)]:
        tape.head({'number':hex(n),'timestamp':hex(ts),'hash':f'0x{n:x}b'},ts)
    tape.log(swap(7,tx='0xreal',block_hash='0x7b'),1400)
    tape.set_quote_price(2400,1400)
    assert tape.conn.execute("SELECT COUNT(*) FROM pool_ticks WHERE tx_sig='0xreorged'").fetchone()[0]==0
    assert w.measure(tape.conn,CHAIN,MINT,1400)['usd']==2400


def test_a_reset_that_loses_a_live_window_is_recorded_with_time_reason_and_length(tape):
    tape.reset(1400,'disconnect:ConnectionClosedError')
    tape.reset(1401,'connect')
    assert tape.conn.execute('SELECT received FROM volume_feed').fetchone()[0]==1301, \
        'a reset must not make a dark feed look fresh'
    rows=tape.conn.execute('SELECT at,reason,window_s FROM volume_feed_resets').fetchall()
    assert [tuple(r) for r in rows]==[(1400,'disconnect:ConnectionClosedError',400)]


def test_a_process_restart_counts_the_window_it_drops(tape):
    w.VolumeTape(tape.conn,CHAIN)
    assert [r[0] for r in tape.conn.execute('SELECT reason FROM volume_feed_resets')]==['init']


def test_feed_status_reports_feed_age_window_age_and_resets(tape):
    tape.reset(1400,'disconnect:x')
    tape.head({'number':'0x9','timestamp':hex(1500),'hash':'0x9'},1500)
    path=tape.conn.execute('PRAGMA database_list').fetchone()[2]
    status=w.feed_status(CHAIN,1530,path=path)
    assert status['feed_age_s']==pytest.approx(30) and status['window_age_s']==pytest.approx(30)
    assert status['resets_24h']==1 and status['last_reset_at']==1400 and status['last_reset_reason']=='disconnect:x'
    assert w.feed_status(CHAIN,1400+86401,path=path)['resets_24h']==0


def test_a_rolled_back_writer_can_still_use_the_store(tape):
    old=[r[1] for r in tape.conn.execute('PRAGMA table_info(volume_feed)')]
    assert len(old)==5, 'the previous release inserts five positional values into volume_feed'
    tape.conn.execute('INSERT OR REPLACE INTO volume_feed VALUES (?,?,?,?,?)',(CHAIN,None,None,1,0))


def test_feed_status_reads_a_store_from_before_the_reset_table(tmp_path):
    import sqlite3
    path=tmp_path/'old.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE volume_feed (chain_id TEXT PRIMARY KEY, since REAL, through REAL, received REAL, logs_seen INTEGER)')
        c.execute('INSERT INTO volume_feed VALUES (?,?,?,?,?)',(CHAIN,900,995,995,4))
    status=w.feed_status(CHAIN,1000,path=path)
    assert status['available'] and status['feed_age_s']==5 and status['resets_24h'] is None


def test_feed_status_of_a_missing_store_is_unavailable_not_fresh(tmp_path):
    status=w.feed_status(CHAIN,1000,path=tmp_path/'absent.db')
    assert status['available'] is False and status['feed_age_s'] is None


def test_a_stale_feed_alerts_once_and_recovers_once():
    sent=[];state={}
    fresh={'available':True,'feed_age_s':1,'window_age_s':400,'resets_24h':3}
    stale={'available':True,'feed_age_s':121,'window_age_s':None,'resets_24h':4}
    for status in (fresh,stale,stale,dict(stale,feed_age_s=500),fresh,fresh,
                   {'available':False,'feed_age_s':None}):
        w.watch_feed(state,CHAIN,status,threshold_s=120,send_fn=lambda *a,**k:sent.append(a))
    assert [a[0] for a in sent]==['onchain_volume_feed']*3
    assert 'STALE' in sent[0][1] and 'recovered' in sent[1][1] and 'STALE' in sent[2][1]


def _stream(messages, clock_values, monkeypatch, *, ping_log=None, sent=None):
    import json
    from sniper.execution import evm_v4_quote,evm_wallet
    from sniper.util import clock
    ticks=iter(clock_values)
    last=[clock_values[0]]
    def now():
        last[0]=next(ticks,last[0]);return last[0]
    monkeypatch.setattr(clock,'real_utc_now',now)
    monkeypatch.setattr(evm_v4_quote,'_stored_pool_key',lambda *a:KEY)
    monkeypatch.setattr(evm_wallet,'quote_usd_price',lambda *a:2400)
    from sniper.settings import memecoin as _cfg
    monkeypatch.setattr(_cfg,'MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED',False)   # a stream test drives ONE socket
    class Socket:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def send(self,body):
            if sent is not None:sent.append(json.loads(body))
        def ping(self):
            if ping_log is not None:ping_log.append(last[0])
        def recv(self,**k):
            if not messages:raise KeyboardInterrupt
            return json.dumps(messages.pop(0))
    return Socket


def test_the_stream_reads_stable_decimals_once_over_its_own_socket(tmp_path,monkeypatch):
    import sqlite3
    path=tmp_path/'volume.db'
    for expect_call in (True,False):
        sent=[]
        msgs=[{'id':1,'result':hex(int(CHAIN))}]+([{'id':10,'result':'0x'+f'{6:064x}'}] if expect_call else [])
        Socket=_stream(msgs,[1000.0],monkeypatch,sent=sent)
        with pytest.raises(KeyboardInterrupt):
            w.run(CHAIN,'wss://unit-test',path=path,connect_fn=lambda *a,**k:Socket())
        calls=[m for m in sent if m.get('method')=='eth_call']
        assert bool(calls)==expect_call
    assert calls==[] and sqlite3.connect(path).execute('SELECT decimals FROM volume_token_decimals').fetchone()[0]==6


def test_the_stream_pings_so_the_server_never_sees_an_idle_client(tmp_path,monkeypatch):
    pings=[]
    heads=[{'params':{'subscription':'heads','result':{'number':hex(n),'timestamp':hex(1000+n),'hash':hex(n)}}}
           for n in range(1,8)]
    msgs=[{'id':1,'result':hex(int(CHAIN))},{'id':10,'result':'0x'+f'{6:064x}'},
          {'id':2,'result':'heads'},{'id':3,'result':'logs'}]+heads
    Socket=_stream(msgs,[1000.0+10*i for i in range(40)],monkeypatch,ping_log=pings)
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN,'wss://unit-test',path=tmp_path/'v.db',connect_fn=lambda *a,**k:Socket())
    assert pings and all(b-a>=w.PING_INTERVAL_S for a,b in zip(pings,pings[1:]))


def test_a_disconnect_is_logged_with_its_time_and_cause(tmp_path,monkeypatch,capsys):
    from sniper.util import clock
    monkeypatch.setattr(clock,'real_utc_now',lambda:1789581095.0)
    import time
    from types import SimpleNamespace
    def stop(_s):raise KeyboardInterrupt
    monkeypatch.setattr(w,'time',SimpleNamespace(sleep=stop,monotonic=time.monotonic))
    def refuse(*a,**k):raise ConnectionError('received 1006 (connection closed abnormally)')
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN,'wss://unit-test',path=tmp_path/'v.db',connect_fn=refuse)
    line=[l for l in capsys.readouterr().out.splitlines() if 'disconnected' in l][0]
    assert line.startswith('2026-09-16T17:51:35') and 'ConnectionError' in line and '1006' in line


def test_watch_feeds_reads_only_enabled_chains_and_pages_through_one_path(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg,'MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS',{'4663'})
    monkeypatch.setattr(cfg,'MEMECOIN_ONCHAIN_M5_FEED_STALE_ALERT_S',120.0)
    monkeypatch.setattr(w,'feed_status',lambda chain,now,**k:{'available':True,'feed_age_s':now-1000,'resets_24h':2})
    sent=[];state={}
    assert w.watch_feeds(state,1100,send_fn=lambda *a,**k:sent.append(a))['4663']['feed_age_s']==100
    assert w.watch_feeds(state,1200,send_fn=lambda *a,**k:sent.append(a))['4663']['feed_age_s']==200
    assert len(sent)==1 and 'STALE' in sent[0][1]
    monkeypatch.setattr(cfg,'MEMECOIN_ONCHAIN_M5_VOLUME_CHAINS',set())
    assert w.watch_feeds(state,1300,send_fn=lambda *a,**k:sent.append(a)) is None


def test_the_runner_heartbeat_carries_the_feed_fields_by_name(tmp_path):
    from sniper.services import memecoin_runner as run
    path=str(tmp_path/'hb.json')
    status={'available':True,'feed_age_s':1.5,'window_age_s':250.0,'logs_seen':9,'resets_24h':3,
            'last_reset_at':1000.0,'last_reset_reason':'disconnect:x','url':'wss://secret'}
    run.write_heartbeat({'events':0,'opened':0},auth_ok=True,path=path,onchain_volume_feed={'4663':status})
    got=run.read_heartbeat(path)['onchain_volume_feed']['4663']
    assert got=={k:status[k] for k in w.FEED_STATUS_FIELDS}


def test_decimals_cached_on_a_later_connection_reprice_pools_already_registered(tmp_path,monkeypatch):
    import sqlite3
    path=tmp_path/'v.db'
    usdg=_usdg()
    key={'currency0':usdg,'currency1':MINT,'fee':500,'tick_spacing':10,'hooks':w.NATIVE}
    if int(usdg,16)>int(MINT,16):
        key=dict(key,currency0=MINT,currency1=usdg)
    pool=pool_id_for_key(key)
    log={'params':{'subscription':'logs','result':_stable_swap(pool,key,5.0,'0xs',block=1)}}
    head={'params':{'subscription':'heads','result':{'number':'0x1','timestamp':hex(1000),'hash':'0x1'}}}
    subs=[{'id':2,'result':'heads'},{'id':3,'result':'logs'}]
    from sniper.execution import evm_v4_quote
    monkeypatch.setattr(evm_v4_quote,'_stored_pool_key',lambda *a:key)
    connections=[
        [{'id':1,'result':hex(int(CHAIN))},{'id':10,'error':{'message':'busy'}}]+subs+[head,log],
        [{'id':1,'result':hex(int(CHAIN))},{'id':10,'result':'0x'+f'{6:064x}'}]+subs+[head,log],
    ]
    import json
    from sniper.util import clock
    monkeypatch.setattr(clock,'real_utc_now',lambda:1000.0)
    from sniper.settings import memecoin as _cfg
    monkeypatch.setattr(_cfg,'MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED',False)   # this test drives one socket per connection
    class Socket:
        def __init__(self,msgs):self.msgs=msgs
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def send(self,*a):pass
        def ping(self):pass
        def recv(self,**k):
            if not self.msgs:raise ConnectionError('closed')
            return json.dumps(self.msgs.pop(0))
    def connect_fn(*a,**k):
        if not connections:raise KeyboardInterrupt
        return Socket(connections.pop(0))
    monkeypatch.setattr(w.time,'sleep',lambda s:None)
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN,'wss://unit-test',path=path,connect_fn=connect_fn)
    with sqlite3.connect(path) as c:
        assert c.execute('SELECT quote_side FROM pool_meta WHERE pool=?',(pool,)).fetchone()[0] in (0,1)


def test_a_lagging_first_header_cannot_pull_the_previous_connections_swaps_into_the_window(tape):
    """A reconnect can land on a node whose first header is OLDER than swaps the previous socket
    stored. If one of those was orphaned, its `removed` notice never reaches the new socket. The
    window therefore starts when THIS connection received its first header, not at that header's
    block time."""
    tape.log(swap(4,tx='0xprevious_socket'),1301)
    tape.reset(1302,'disconnect:test')
    tape.head({'number':'0x3','timestamp':hex(1290),'hash':'0x3b'},1303)
    tape.head({'number':'0x4','timestamp':hex(1301),'hash':'0x4b'},1304)
    tape.head({'number':'0x5','timestamp':hex(1400),'hash':'0x5b'},1400)
    tape.log(swap(5,tx='0xthis_socket',block_hash='0x5b'),1400)
    tape.set_quote_price(2400,1400)
    out=w.measure(tape.conn,CHAIN,MINT,1400)
    assert out['usd']==2400 and out['covered_s']==pytest.approx(97)


# ------------------------------------------------------------ out-of-order headers (2026-09-16)
# publicnode delivered ...136, 138, 137, 139 at 19:10:24Z and again at 19:15:51Z, with no block
# missing. A strict-successor tape reset three times each and dropped a ~327 s window, 2-3 times
# an hour. A late header is accepted only inside a block-count grace; a block that never arrives
# still resets.
@pytest.fixture
def bare(tmp_path):
    c=w.connect(tmp_path/'reorder.db');t=w.VolumeTape(c,CHAIN,0)
    yield t
    c.close()


def _heads(tape, sequence):
    for item in sequence:
        block, suffix = item if isinstance(item, tuple) else (item, '')
        tape.head({'number':hex(block),'timestamp':hex(2000+block),'hash':f'0x{block:x}{suffix}'},2000+block)


def _gap_resets(tape):
    return [tuple(r) for r in tape.conn.execute(
        "SELECT at,reason FROM volume_feed_resets WHERE reason LIKE 'head_gap%' ORDER BY at")]


def _feed(tape):
    return tape.conn.execute('SELECT since,through FROM volume_feed').fetchone()


def test_a_single_reordered_header_keeps_the_window(bare):
    _heads(bare, [136, 138, 137, 139])
    assert _gap_resets(bare) == []
    assert tuple(_feed(bare)) == (2136, 2139)
    assert [r[0] for r in bare.conn.execute('SELECT block_no FROM block_times ORDER BY block_no')] \
        == [136, 137, 138, 139]


def test_a_block_that_never_arrives_resets_once_the_grace_expires(bare):
    """The grace widened from 3 headers to 8 (2026-09-23) because real reorders span up to 8
    blocks. The PROPERTY is unchanged: a block that never comes still drops the window."""
    _heads(bare, [136, 138, 139])
    assert _gap_resets(bare) == [], 'still inside the grace'
    _heads(bare, [140, 141, 142, 143, 144, 145])
    resets = _gap_resets(bare)
    assert resets and resets[0][1].startswith('head_gap:hole_expired:'), resets
    # WHICHEVER RULE FIRES FIRST. `_heads` advances the clock one second per block, so the
    # 3-second bound is reached at 2141 before the 8-header bound would be. On the real chain at
    # ~13 blocks/s the header rule wins instead; both are pinned, here and in the test above.
    assert _feed(bare)['since'] == 2141, 'the window restarts at the header that closed the grace'


def test_the_late_header_may_be_the_third_header(bare):
    _heads(bare, [136, 138, 139, 137, 140])
    assert _gap_resets(bare) == []
    assert tuple(_feed(bare)) == (2136, 2140)


def test_a_duplicate_header_neither_resets_nor_spends_the_grace(bare):
    _heads(bare, [136, 138, 138, 138, 139, 137, 140, 140])
    assert _gap_resets(bare) == []


def test_a_late_header_does_not_move_the_newest_time_back(bare):
    _heads(bare, [136, 138, 137])
    assert _feed(bare)['through'] == 2138


def test_two_missing_blocks_are_held_then_reset_when_they_never_arrive(bare):
    """CHANGED 2026-09-23. Two missing blocks used to reset AT ONCE, and that was the bug: the
    03:27:44 burst opened exactly this shape (687 then 690) and the pair arrived a moment later.
    They are now held as holes and only reset when they genuinely never come."""
    _heads(bare, [136, 139])
    assert _gap_resets(bare) == [], 'a +3 arrival is the commonest reorder shape'
    _heads(bare, [140, 141, 142, 143, 144, 145, 146, 147])
    resets = _gap_resets(bare)
    assert resets and resets[0][1].startswith('head_gap:hole_expired:'), resets


@pytest.mark.parametrize('sequence', [
    [136, 137, (137, 'b')],          # same number, different hash: a reorg, not a duplicate
    # [136, 137, 138, 135] was here: an old header inside the window that fills no hole. It
    # stopped resetting on 2026-09-23 and that is deliberate -- a height we already covered,
    # never seen under a different hash, invalidates nothing and cannot extend coverage. A
    # DIFFERENT hash at a covered height is a reorg and still resets, which the two cases
    # either side of this pin.
    [136, 138, (137, 'x'), 139, 140, 137],   # the hole filled, then a stale copy with a new hash
])
def test_headers_outside_the_grace_still_reset(bare, sequence):
    _heads(bare, sequence)
    assert _gap_resets(bare), sequence


def test_two_open_holes_are_both_tolerated_until_the_grace_expires(bare):
    """CHANGED 2026-09-23. Two simultaneous holes used to reset immediately. The 03:20:00.764Z
    burst (674, 677, 678, 675, 676, 679) holds two at once and every block arrived."""
    _heads(bare, [136, 138, 140])
    assert _gap_resets(bare) == []
    _heads(bare, [137, 139, 141])                 # both late headers land
    assert _gap_resets(bare) == [], 'both holes filled: nothing was ever missing'
    assert bare.last_block == 141 and bare.holes == {}


# --- per-chain PoolManager (2026-09-22, Base 8453) -----------------------------------------------
#
# The writer was hard-gated to 4663 and read one module-level MANAGER. Base carries its memecoin
# liquidity on the same Uniswap V4 PoolManager events (V2 on Base totals $42), so the observer
# generalises by naming. The risk this pins is a WRONG address: it does not fail loudly, it
# subscribes to a contract that never emits, and the gate then reads an empty window as "no
# volume" rather than "no feed".


def test_a_chain_without_a_verified_manager_refuses(tmp_path):
    """Refuse rather than guess. `56` has no verified manager, so it must never build a tape."""
    c = w.connect(tmp_path / 'tape.db')
    try:
        with pytest.raises(ValueError, match='no verified V4 PoolManager'):
            w.VolumeTape(c, '56')
        with pytest.raises(ValueError, match='no verified V4 PoolManager'):
            w.manager_for('56')
    finally:
        c.close()


def test_base_and_robinhood_have_different_verified_managers():
    """A shared constant would have pointed Base at the Robinhood manager and logged nothing.

    8453 verified on chain 2026-09-22: 24,009 bytes of code and 1,687 V4 Swap logs across 309
    distinct pool ids in 200 blocks.
    """
    assert w.manager_for('8453') == '0x498581ff718922c3f8e6a244956af099b2652b2b'
    assert w.manager_for('4663') != w.manager_for('8453')
    assert w.manager_for(8453) == w.manager_for('8453'), 'an int chain id must resolve too'


def test_a_tape_ignores_a_swap_from_another_chains_manager(tmp_path):
    """Cross-chain address confusion. A Base tape must not count a log the 4663 manager emitted."""
    c = w.connect(tmp_path / 'tape.db')
    try:
        t = w.VolumeTape(c, '8453')
        t.register_pool(POOL, KEY, 1000)
        t.head({'number': '0x1', 'timestamp': hex(1000), 'hash': '0x1'}, 1000)
        foreign = swap(1, tx='0xforeign')
        foreign['address'] = w.MANAGERS['4663']
        t.log(foreign, 1000)
        assert t.logs_seen == 0, 'a foreign manager log entered the Base window'
        own = swap(1, tx='0xown')
        own['address'] = w.MANAGERS['8453']
        t.log(own, 1000)
        assert t.logs_seen == 1, 'the Base tape ignored its own manager'
    finally:
        c.close()


def test_hop_refresher_runs_on_its_own_thread_and_needs_the_read_rpc(tmp_path, monkeypatch):
    """The refresh must never run inside the observer loop: 66-80 eth_calls exceeded the 15 s
    head-stall rule and reset the 4663 window every 30 s (2026-09-22 21:03-21:10Z)."""
    monkeypatch.delenv('MEMECOIN_ONCHAIN_EVM_RPC_URL_4663', raising=False)
    assert w._start_hop_refresher('4663', tmp_path / 'v.db') is None
    calls = []
    monkeypatch.setenv('MEMECOIN_ONCHAIN_EVM_RPC_URL_4663', 'https://read.example')
    from sniper.services import memecoin_equity_hop_price as hop
    done = threading.Event()

    def fake_refresh(conn, chain, rpc, now):
        calls.append((chain, rpc, threading.current_thread().name)); done.set()
        raise SystemExit  # end the thread after one pass
    monkeypatch.setattr(hop, 'refresh', fake_refresh)
    # The SystemExit that ends the thread comes to this hook and the thread is joined here, so it
    # cannot reach a later test as an unhandled-thread warning. Any other exception goes on to the
    # real hook.
    ended, real_hook = [], threading.excepthook
    monkeypatch.setattr(threading, 'excepthook', lambda a: ended.append(a.thread.name)
                        if a.exc_type is SystemExit else real_hook(a))
    w._HOP_THREADS.clear()
    t = w._start_hop_refresher('4663', tmp_path / 'v.db')
    assert t is not None and done.wait(5)
    t.join(5)
    assert not t.is_alive() and ended == ['hop-prices-4663']
    assert calls and calls[0][0] == '4663' and calls[0][1] == 'https://read.example'
    assert calls[0][2] == 'hop-prices-4663' and calls[0][2] != threading.current_thread().name


# --- the window backfill -----------------------------------------------------------------------

def _chain_call(head=1000, swaps=(900, 990), fail=None, calls=None, launches=()):
    """A chain where block n has timestamp 1000+n, with V4 swaps in POOL at the given blocks.

    Each page also asks the Pons v2 LAUNCHER for its `Launched` logs (2026-09-23), because that
    contract is a singleton and so the request is address-filtered, which is the only kind this
    endpoint serves. `launches` are (block, token, curve) triples.
    """
    def call(reqs):
        if calls is not None: calls.append([m for m, _p in reqs])
        if fail: raise fail
        out = []
        for m, p in reqs:
            if m == 'eth_blockNumber': res = hex(head)
            elif m == 'eth_getBlockByNumber':
                n = int(p[0], 16); res = {'number': hex(n), 'timestamp': hex(1000 + n), 'hash': f'0x{n:064x}'}
            elif m == 'eth_getLogs':
                lo, hi = int(p[0]['fromBlock'], 16), int(p[0]['toBlock'], 16)
                if p[0]['address'] == w.PONS_V2_LAUNCHER:
                    res = [{'address': w.PONS_V2_LAUNCHER,
                            'topics': [w.PONS_V2_LAUNCHED, '0x' + '00' * 12 + token[2:],
                                       '0x' + '00' * 12 + curve[2:], '0x' + '00' * 32],
                            'data': '0x' + '00' * 96, 'blockNumber': hex(b),
                            'logIndex': '0x0', 'transactionHash': f'0xlaunch{b}',
                            'blockHash': f'0x{b:064x}'}
                           for b, token, curve in launches if lo <= b <= hi]
                else:
                    assert p[0]['address'] == w.MANAGERS[CHAIN]
                    res = [swap(b, 0, tx=f'0xback{b}', block_hash=f'0x{b:064x}')
                           for b in swaps if lo <= b <= hi]
            else: raise AssertionError(m)
            out.append(res)
        return out
    return call


def test_the_backfill_fills_the_full_window_before_the_first_socket_header(tmp_path):
    c = w.connect(tmp_path / 'tape.db'); t = w.VolumeTape(c, CHAIN, 1990.0)
    t.register_pool(POOL, KEY, 1990.0); t.set_quote_price(2400, 2000.0)
    calls = []
    out = w.backfill_window(t, CHAIN, _chain_call(calls=calls), 2000.0, stored_pool_key=lambda *a: KEY)
    assert out and out['swaps'] == 2 and out['inserted'] == 2 and out['head'] == 1000
    assert out['covered_s'] >= 300 and out['start'] <= 700
    assert c.execute('SELECT COUNT(*) FROM pool_ticks').fetchone()[0] == 2
    # every block from start to head has a time: exact at the seed range and the anchors, interpolated between
    assert c.execute('SELECT COUNT(*) FROM block_times').fetchone()[0] == out['blocks'] == 1000 - out['start'] + 1
    assert out['exact_headers'] < out['blocks']
    for block in (out['start'], 900, 990, 1000):
        row = c.execute('SELECT block_ts FROM block_times WHERE block_no=?', (block,)).fetchone()
        assert abs(w.datetime.fromisoformat(row[0]).timestamp() - (1000 + block)) < 1.0
    feed = c.execute('SELECT since,through,received,logs_seen FROM volume_feed').fetchone()
    assert feed[0] == 1000 + out['start'] and feed[1] == 2000 and feed[2] == 2000.0 and feed[3] == 2
    m = w.measure(c, CHAIN, MINT, 2001.0)
    assert m['reason'] == 'ok' and m['swaps'] == 2 and m['usd'] == 4800 and m['covered_s'] == 300
    assert sum(1 for cl in calls if cl == ['eth_getLogs']) <= 4 and all(len(cl) <= 100 for cl in calls)
    assert out['pons_launches'] == 0, 'this chain launched no Pons coin in the window'
    c.close()


def test_the_backfill_registers_a_pons_curve_that_launched_during_the_blind_window(tmp_path):
    """A coin launched while the socket was down is attributable the moment its first trade lands.

    Its TRADES cannot be backfilled — they ride a topic-only subscription this endpoint refuses over
    getLogs, the same asymmetry V3 lives with — but the curve -> mint mapping can be, and without it
    the first minutes of a new coin are summed for nobody.
    """
    token, curve = '0x' + 'ab' * 20, '0x' + 'cd' * 20
    c = w.connect(tmp_path / 'tape.db'); t = w.VolumeTape(c, CHAIN, 1990.0)
    t.register_pool(POOL, KEY, 1990.0)
    out = w.backfill_window(t, CHAIN, _chain_call(launches=[(950, token, curve)]), 2000.0,
                            stored_pool_key=lambda *a: KEY)
    assert out and out['pons_launches'] == 1
    assert {r[0] for r in c.execute(
        'SELECT mint FROM onchain_pool_protocols WHERE pool=? AND protocol=?',
        (curve, 'pons_v2'))} == {token}
    assert c.execute('SELECT quote_side FROM pool_meta WHERE pool=?', (curve,)).fetchone()[0] is None
    c.close()


def test_a_failed_backfill_writes_nothing_and_leaves_the_window_unfilled(tmp_path, capsys):
    c = w.connect(tmp_path / 'tape.db'); t = w.VolumeTape(c, CHAIN, 1990.0)
    t.register_pool(POOL, KEY, 1990.0)
    assert w.backfill_window(t, CHAIN, _chain_call(fail=ConnectionError('down')), 2000.0) is None
    assert w.backfill_window(t, CHAIN, _chain_call(), 2000.0, budget_s=-1) is None
    assert c.execute('SELECT COUNT(*) FROM pool_ticks').fetchone()[0] == 0
    assert c.execute('SELECT COUNT(*) FROM block_times').fetchone()[0] == 0
    assert c.execute('SELECT since FROM volume_feed').fetchone()[0] is None and t.since is None
    assert w.measure(c, CHAIN, MINT, 2001.0)['reason'] == 'window_incomplete'
    assert 'backfill skipped' in capsys.readouterr().out
    c.close()


def test_headers_queued_on_the_socket_during_the_backfill_arrive_as_duplicates_not_as_a_reset(tmp_path):
    c = w.connect(tmp_path / 'tape.db'); t = w.VolumeTape(c, CHAIN, 1990.0)
    t.register_pool(POOL, KEY, 1990.0)
    assert w.backfill_window(t, CHAIN, _chain_call(), 2000.0, stored_pool_key=lambda *a: KEY)
    since = t.since
    # the socket subscribed at block 950 and queued 950..1000 while the backfill ran
    for n in range(950, 1001):
        t.head({'number': hex(n), 'timestamp': hex(1000 + n), 'hash': f'0x{n:064x}'}, 2000.0 + (n - 950) * 0.01)
    assert t.since == since and t.last_block == 1000, 'an older queued header must not reset the window'
    t.head({'number': hex(1001), 'timestamp': hex(2001), 'hash': f'0x{1001:064x}'}, 2001.0)
    assert t.since == since and t.last_block == 1001 and t.through == 2001
    assert c.execute('SELECT COUNT(*) FROM volume_feed_resets').fetchone()[0] == 0
    c.close()


def test_the_stream_backfills_over_a_second_socket_and_the_knob_turns_it_off(tmp_path, monkeypatch):
    from sniper.settings import memecoin as cfg
    filled = []
    monkeypatch.setattr(w, 'backfill_window', lambda tape, chain, call, now, **k: filled.append(chain) or None)
    msgs = [{'id': 1, 'result': hex(int(CHAIN))}, {'id': 10, 'result': '0x' + f'{6:064x}'}]
    for enabled in (True, False):
        opened = []
        Socket = _stream(list(msgs), [1000.0], monkeypatch)
        monkeypatch.setattr(cfg, 'MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED', enabled)   # after _stream, which turns it off
        def connect_fn(url, **kw):
            opened.append((url, kw.get('max_size', 'default')))
            return Socket()
        with pytest.raises(KeyboardInterrupt):
            w.run(CHAIN, 'wss://unit-test', path=tmp_path / 'v.db', connect_fn=connect_fn)
        # the backfill socket is a SECOND connection to the same url, with no size cap
        assert len(opened) == (2 if enabled else 1) and (not enabled or opened[1] == ('wss://unit-test', None))
    assert filled == [CHAIN]


def test_a_hop_priced_unit_stays_usable_for_three_minutes_while_a_base_price_does_not(tape):
    """The hop pass runs every 30 s and takes 20-50 s; at a 60 s limit priced units flapped to
    quote_not_priceable between passes (GIGABYTE, PAWSINU 2026-09-22)."""
    unit='0x'+'ee'*20
    key2={'currency0':MINT,'currency1':unit,'fee':3000,'tick_spacing':60,'hooks':w.NATIVE}   # MINT < unit
    pool2=pool_id_for_key(key2)
    tape.conn.execute('INSERT OR REPLACE INTO volume_token_decimals VALUES (?,?,?,?)',(CHAIN,unit,18,1301))
    tape.conn.execute('INSERT OR REPLACE INTO volume_quote_prices VALUES (?,?,?,?)',(CHAIN,unit,50.0,1301-150))
    tape.register_pool(pool2,key2,1301)
    assert tape.conn.execute('SELECT quote_side FROM pool_meta WHERE pool=?',(pool2,)).fetchone()[0]==1
    log=dict(swap(4,7,tx='0xhop'),topics=[V4_SWAP,pool2,'0x'+'00'*32])
    tape.log(log,1301)
    tape.log(swap(4,8,tx='0xeth'),1301)                      # one swap in the ETH pool too
    m=w.measure(tape.conn,CHAIN,MINT,1302)
    assert m['reason']=='ok' and m['usd']==pytest.approx(2400+50), 'a 150 s old hop price still prices its swap'
    tape.conn.execute('UPDATE volume_quote_prices SET observed=? WHERE token=?',(1302-70,w.NATIVE))
    tape.conn.execute('UPDATE volume_quote_prices SET observed=? WHERE token=?',(1302-70,tape.conn.execute('SELECT token FROM volume_quote_prices WHERE token!=? AND token!=?',(unit,w.NATIVE)).fetchone()[0]))
    m=w.measure(tape.conn,CHAIN,MINT,1302)
    assert m['unpriced_pools'] and m['usd']==50, 'a 70 s old base price is stale, the hop price is not'


# --- Uniswap V3 pools ------------------------------------------------------------------------

V3POOL = '0x' + '5e' * 20
UNIT_X = '0x' + '9a' * 20


def _v3_swap(pool=V3POOL, block=4, index=3, tx='0xv3', amount0=10**18, amount1=-(2 * 10**18)):
    from sniper.services.memecoin_tick_stream import V3_SWAP
    words = [amount0 % (1 << 256), amount1 % (1 << 256), 5 << 96, 10**20, (7) % (1 << 256)]
    return {'address': pool, 'topics': [V3_SWAP, '0x' + '00' * 12 + 'ab' * 20, '0x' + '00' * 12 + 'cd' * 20],
            'data': '0x' + ''.join(f'{v:064x}' for v in words), 'blockNumber': hex(block),
            'logIndex': hex(index), 'transactionHash': tx, 'blockHash': f'0x{block:x}'}


def _v3_call(token0, token1, fee=3000, fail=None, calls=None):
    def call(reqs):
        if calls is not None: calls.append(len(reqs))
        if fail: raise fail
        out = []
        for method, params in reqs:
            assert method == 'eth_call' and params[1] == 'latest'
            sel = params[0]['data']
            val = {w.V3_SEL_TOKEN0: token0, w.V3_SEL_TOKEN1: token1}.get(sel)
            out.append(('0x' + '00' * 12 + val[2:]) if val else ('0x' + f'{fee:064x}'))
        return out
    return call


def test_a_v3_swap_is_a_tick_and_counts_for_the_mint_once_its_pool_resolves(tape):
    tape.log(swap(4, 0, tx='0xv4'), 1301)                 # one V4 swap in the window: $2,400
    tape.log_v3(_v3_swap(), 1301)
    row = tape.conn.execute("SELECT pool, kind, amount0, amount1, sqrt_price_x96, liquidity FROM pool_ticks WHERE tx_sig='0xv3'").fetchone()
    assert row[0] == V3POOL and row[1] == 'v3_swap' and row[2] == str(10**18) and row[3] == str(-(2 * 10**18))
    assert row[4] == str(5 << 96) and row[5] == str(10**20)
    m = w.measure(tape.conn, CHAIN, MINT, 1302)
    assert m['reason'] == 'ok' and m['usd'] == 2400, 'an unresolved V3 pool is not yet a pool of the mint'
    calls = []
    assert w.resolve_v3_pools(tape.conn, CHAIN, _v3_call(w.NATIVE, MINT, calls=calls), 1301) == 1
    assert calls == [3], 'three eth_calls per pool, in one batch'
    meta = tape.conn.execute('SELECT token0, token1, quote_side, token0_decimals FROM pool_meta WHERE pool=?', (V3POOL,)).fetchone()
    assert tuple(meta) == (w.NATIVE, MINT, 0, 18)
    assert tape.conn.execute("SELECT protocol FROM onchain_pool_protocols WHERE pool=? AND mint=?", (V3POOL, MINT)).fetchone()[0] == 'v3'
    m = w.measure(tape.conn, CHAIN, MINT, 1302)
    assert m['reason'] == 'ok' and m['swaps'] == 2 and m['usd'] == 2400 + 2400, 'V3 is a covered protocol now'
    assert m['coverage_scope'] == 'known_v4_v3_pools' and {p['protocol'] for p in m['pools']} == {'v4', 'v3'}
    assert w.resolve_v3_pools(tape.conn, CHAIN, _v3_call(w.NATIVE, MINT), 1301) == 0, 'nothing left to resolve'


def test_a_v3_pool_quoted_in_an_unpriced_unit_is_an_unpriced_pool_not_a_zero(tape):
    tape.log(swap(4, 0, tx='0xv4'), 1301)
    tape.log_v3(_v3_swap(), 1301)
    assert w.resolve_v3_pools(tape.conn, CHAIN, _v3_call(MINT, UNIT_X), 1301) == 1
    assert tape.conn.execute('SELECT quote_side FROM pool_meta WHERE pool=?', (V3POOL,)).fetchone()[0] is None
    m = w.measure(tape.conn, CHAIN, MINT, 1302)
    assert m['usd'] == 2400 and m['unpriced_pools'] and m['unpriced_pools'][0]['pool'] == V3POOL and m['coverage'] == 'partial'


def test_a_failed_resolve_registers_nothing_and_is_retried_later(tape, capsys):
    tape.log_v3(_v3_swap(), 1301)
    with pytest.raises(ConnectionError):
        w.resolve_v3_pools(tape.conn, CHAIN, _v3_call(w.NATIVE, MINT, fail=ConnectionError('down')), 1301)
    assert tape.conn.execute('SELECT COUNT(*) FROM pool_meta WHERE pool=?', (V3POOL,)).fetchone()[0] == 0
    # a contract that is not a V3 pool answers garbage and is skipped, not registered
    assert w.resolve_v3_pools(tape.conn, CHAIN, lambda reqs: ['0x'] * len(reqs), 1301) == 0


def test_the_stream_subscribes_to_v3_swaps_by_topic_and_routes_them_to_the_tape(tmp_path, monkeypatch):
    import sqlite3
    from sniper.services.memecoin_tick_stream import V3_SWAP
    sent = []
    started = []
    monkeypatch.setattr(w, '_start_v3_resolver', lambda chain, path, ws_url, **k: started.append((chain, ws_url)))
    msgs = [{'id': 1, 'result': hex(int(CHAIN))}, {'id': 10, 'result': '0x' + f'{6:064x}'},
            {'id': 2, 'result': 'heads'}, {'id': 3, 'result': 'logs'}, {'id': 4, 'result': 'v3'},
            {'params': {'subscription': 'heads', 'result': {'number': '0x1', 'timestamp': hex(1000), 'hash': '0x1'}}},
            {'params': {'subscription': 'v3', 'result': _v3_swap(block=1)}}]
    Socket = _stream(msgs, [1000.0], monkeypatch, sent=sent)
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN, 'wss://unit-test', path=tmp_path / 'v.db', connect_fn=lambda *a, **k: Socket())
    subs = [m['params'] for m in sent if m.get('method') == 'eth_subscribe']
    assert ['logs', {'topics': [[V3_SWAP, w.PANCAKE_V3_SWAP]]}] in subs, 'topic-only, no address: publicnode accepts it'
    with sqlite3.connect(tmp_path / 'v.db') as c:
        assert c.execute("SELECT kind FROM pool_ticks WHERE tx_sig='0xv3'").fetchone()[0] == 'v3_swap'
    assert started == [(CHAIN, 'wss://unit-test')], 'the resolver gets the tape provider socket url'


def test_pons_frames_start_metadata_resolution_without_a_v3_frame(tmp_path, monkeypatch):
    from sniper.services.memecoin_tick_stream import PONS_V2_CURVE_BUY
    started = []
    monkeypatch.setattr(w, '_start_v3_resolver', lambda chain,path,ws_url,**k:started.append((chain,ws_url)))
    pons = dict(_v3_swap(block=1), topics=[PONS_V2_CURVE_BUY, '0x'+'00'*32],
                data='0x'+f'{10**18:064x}'+f'{10**24:064x}')
    msgs = [{'id':1,'result':hex(int(CHAIN))}, {'id':10,'result':'0x'+f'{18:064x}'},
            {'id':2,'result':'heads'}, {'id':5,'result':'pons'},
            {'params':{'subscription':'heads','result':{'number':'0x1','timestamp':hex(1000),'hash':'0x1'}}},
            {'params':{'subscription':'pons','result':pons}}]
    Socket = _stream(msgs,[1000.0],monkeypatch)
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN,'wss://unit-test',path=tmp_path/'v.db',connect_fn=lambda *a,**k:Socket())
    assert started == [(CHAIN,'wss://unit-test')]


def test_v3_timeout_does_not_skip_pons_metadata_or_share_its_backoff(tape, monkeypatch):
    from sniper.services import memecoin_pons_metadata as pm
    seen = []
    monkeypatch.setattr('sniper.util.clock.real_utc_now',lambda:2000)
    class Socket:
        def __enter__(self): seen.append('socket'); return self
        def __exit__(self,*a): pass
    def bad(*a): raise TimeoutError('unrelated V3 batch')
    monkeypatch.setattr(w,'resolve_v3_pools',bad)
    monkeypatch.setattr(pm,'resolve_pons_curves',lambda *a:seen.append('pons') or 1)
    monkeypatch.setattr(w,'_ws_caller',lambda *a,**k:object())
    due, pauses = {}, {}
    w._resolve_pool_metadata_pass(tape.conn,CHAIN,'wss://test',lambda *a,**k:Socket(),2000,
                                  due=due,pauses=pauses,period_s=5)
    assert seen == ['socket','socket','pons'], 'failed socket must not serve the next family'
    assert due['pons'] == 2005 and due['v3'] == 2015
    seen.clear()
    w._resolve_pool_metadata_pass(tape.conn,CHAIN,'wss://test',lambda *a,**k:Socket(),2005,
                                  due=due,pauses=pauses,period_s=5)
    assert seen == ['socket','pons'], 'healthy Pons cadence survives V3 backoff'


def test_metadata_backoff_starts_when_the_failed_call_finishes(tape, monkeypatch):
    from sniper.services import memecoin_pons_metadata as pm
    clock = [2000]
    monkeypatch.setattr('sniper.util.clock.real_utc_now',lambda:clock[0])
    class Socket:
        def __enter__(self): return self
        def __exit__(self,*a): pass
    def bad(*a): clock[0] += 30; raise TimeoutError('slow batch')
    times = []
    monkeypatch.setattr(w,'resolve_v3_pools',bad)
    monkeypatch.setattr(pm,'resolve_pons_curves',lambda *a:times.append(a[3]) or 0)
    monkeypatch.setattr(w,'_ws_caller',lambda *a,**k:object())
    due,pauses = {},{}
    w._resolve_pool_metadata_pass(tape.conn,CHAIN,'wss://test',lambda *a,**k:Socket(),2000,
                                  due=due,pauses=pauses,period_s=5)
    assert due['v3'] == 2045 and due['pons'] == 2035
    assert times == [2030]


def test_every_swap_is_committed_at_once_and_the_connection_never_fsyncs(tmp_path):
    """Batching swaps into the header commit (e18427f6..587bd181) held a write transaction open
    almost continuously and the other writers on the one store file died with "database is
    locked" (15 restarts an hour). A swap is visible to a second connection as soon as it is
    logged, and the saving stays: WAL + synchronous=NORMAL, no fsync per commit."""
    import sqlite3
    path = tmp_path / 'tape.db'
    c = w.connect(path); t = w.VolumeTape(c, CHAIN, 1000)
    t.register_pool(POOL, KEY, 1000)
    t.head({'number': '0x1', 'timestamp': hex(1000), 'hash': '0x1'}, 1000)
    reader = sqlite3.connect(path)
    t.log(swap(2, 0, tx='0xa'), 1100)
    assert reader.execute("SELECT COUNT(*) FROM pool_ticks WHERE tx_sig='0xa'").fetchone()[0] == 1
    assert not c.in_transaction, 'no write transaction stays open between swaps'
    t.log_v3(_v3_swap(block=2, tx='0xv3b'), 1100)
    assert reader.execute("SELECT COUNT(*) FROM pool_ticks WHERE tx_sig='0xv3b'").fetchone()[0] == 1
    assert not c.in_transaction
    assert c.execute('PRAGMA synchronous').fetchone()[0] == 1, 'WAL + NORMAL: no fsync per commit'
    reader.close(); c.close()


def test_a_reverting_emitter_is_marked_and_the_rest_of_the_batch_still_resolves(tape):
    """Base, 2026-09-23: one contract emitting the V3 Swap topic reverted on token0() and the
    resolver's whole batch failed every cycle. Now: the other pools resolve, the emitter gets an
    empty pair so it is never asked again, and its ticks stay unpriced."""
    import json as _json
    OTHER = '0x' + '6f' * 20
    tape.log_v3(_v3_swap(pool=V3POOL, tx='0xv3a'), 1301)
    tape.log_v3(_v3_swap(pool=OTHER, index=9, tx='0xv3o'), 1301)

    class Sock:
        def __init__(self): self.q = []
        def send(self, body):
            r = _json.loads(body); to = r['params'][0]['to']; sel = r['params'][0]['data']
            if to == OTHER:
                self.q.append({'jsonrpc': '2.0', 'id': r['id'], 'error': {'code': 3, 'message': 'execution reverted'}})
            else:
                val = {w.V3_SEL_TOKEN0: w.NATIVE, w.V3_SEL_TOKEN1: MINT}.get(sel)
                self.q.append({'jsonrpc': '2.0', 'id': r['id'], 'result': ('0x' + '00' * 12 + val[2:]) if val else '0x' + f'{3000:064x}'})
        def recv(self, timeout=None): return _json.dumps(self.q.pop(0))
    call = w._ws_caller(Sock(), tolerate_errors=True)
    assert w.resolve_v3_pools(tape.conn, CHAIN, call, 1301) == 1
    assert tape.conn.execute('SELECT token0 FROM pool_meta WHERE pool=?', (OTHER,)).fetchone()[0] == ''
    assert tuple(tape.conn.execute('SELECT token0, token1, quote_side FROM pool_meta WHERE pool=?', (V3POOL,)).fetchone()) == (w.NATIVE, MINT, 0)
    assert w.resolve_v3_pools(tape.conn, CHAIN, call, 1301) == 0, 'nothing left: the emitter is not re-asked'
    m = w.measure(tape.conn, CHAIN, MINT, 1302)
    assert m['reason'] == 'ok' and m['pools'] and OTHER not in [p['pool'] for p in m['pools']]
    # without tolerance the batch still fails loudly (the backfill keeps that contract)
    with pytest.raises(RuntimeError):
        w._ws_caller(Sock())([('eth_call', [{'to': OTHER, 'data': w.V3_SEL_TOKEN0}, 'latest'])])


def test_a_mid_stream_reset_triggers_a_throttled_backfill(tmp_path, monkeypatch):
    """publicnode delivered 4663 headers out of order in bursts (2026-09-23 03:19-03:31Z, 21 resets
    in an hour); each reset dropped the window and the gate read minutes of history. The backfill
    now runs after any reset, at most once per throttle, not only at connect."""
    import json as _json
    from sniper.settings import memecoin as cfg
    from sniper.execution import evm_v4_quote, evm_wallet
    from sniper.util import clock
    filled = []
    monkeypatch.setattr(w, 'backfill_window', lambda tape, chain, call, now, **k: filled.append(round(now)) or None)
    monkeypatch.setattr(evm_v4_quote, '_stored_pool_key', lambda *a: KEY)
    monkeypatch.setattr(evm_wallet, 'quote_usd_price', lambda *a: 2400)
    monkeypatch.setattr(cfg, 'MEMECOIN_ONCHAIN_M5_BACKFILL_ENABLED', True)
    t = {'now': 1000.0}
    def tick():                       # every clock() call is 10 s later: deterministic throttle arithmetic
        t['now'] += 10.0
        return t['now']
    monkeypatch.setattr(clock, 'real_utc_now', tick)
    def hdr(n): return {'params': {'subscription': 'heads', 'result': {'number': hex(n), 'timestamp': hex(1000 + n), 'hash': hex(n)}}}
    msgs = [{'id': 1, 'result': hex(int(CHAIN))}, {'id': 10, 'result': '0x' + f'{6:064x}'},
            {'id': 2, 'result': 'heads'}, {'id': 3, 'result': 'logs'},
            hdr(1), hdr(2),
            hdr(9),      # jump -> reset -> backfill
            hdr(20),     # jump 10 s later -> inside the 15 s throttle -> no backfill
            hdr(40)]     # jump 20 s after the last backfill -> backfill again
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def send(self, *a): pass
        def ping(self): pass
        def recv(self, **k):
            if not msgs: raise KeyboardInterrupt
            return _json.dumps(msgs.pop(0))
    script = list(msgs)
    # throttle 15 s: the connect backfill, then mid-stream backfills spaced by at least the throttle
    monkeypatch.setattr(w, 'BACKFILL_RESET_THROTTLE_S', 15.0)
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN, 'wss://unit-test', path=tmp_path / 'v.db', connect_fn=lambda *a, **k: Socket())
    assert len(filled) >= 2 and filled[1] > filled[0], filled
    assert all(b - a >= 15 for a, b in zip(filled[1:], filled[2:])), filled
    # an effectively infinite throttle: the connect backfill and exactly ONE mid-stream backfill
    filled.clear(); msgs[:] = script; t['now'] = 1000.0
    monkeypatch.setattr(w, 'BACKFILL_RESET_THROTTLE_S', 1e9)
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN, 'wss://unit-test', path=tmp_path / 'v2.db', connect_fn=lambda *a, **k: Socket())
    assert len(filled) == 2, filled


# --- a permutation is not a gap (2026-09-23) ----------------------------------------------------
#
# publicnode began delivering 4663 headers permuted inside single milliseconds. Comparing each
# arrival against the previous ARRIVAL made every permutation look like a jump: 21 window resets in
# the 03:19-03:31Z hour, of which 16 were permutations with NOTHING missing. Each reset dropped the
# covered window, so the m5 gate read minutes of history instead of five and fell back to the
# provider.
#
# The four sequences below are RECONSTRUCTED FROM THE LIVE LOG, not invented: every reset line
# records a consecutive `prev->block` pair, and any pair that was NOT logged was contiguous, so the
# received order is recoverable exactly. The 03:27:42 trio is deliberately absent -- whether its
# middle blocks ever arrived cannot be recovered, and a fixture for it would be a guess.

REAL_BURSTS = {
    "03:20:00.764Z": [70186674, 70186677, 70186678, 70186675, 70186676, 70186679],
    "03:27:08Z":     [70189187, 70189190, 70189189, 70189188, 70189191],
    "03:27:44Z":     [70189687, 70189690, 70189688, 70189689, 70189692,
                      70189693, 70189691, 70189694],
    "03:30:03Z":     [70192259, 70192262, 70192261, 70192260, 70192263],
}


def _burst(tape, blocks, start=5000.0, step=0.0002):
    """Replay at the real cadence: these landed inside a single millisecond.

    The header timestamp tracks the clock, as a real chain's does -- deriving it from the block
    NUMBER would put small fixtures in 1970 and read as `feed_stale`.
    """
    now = start
    for b in blocks:
        tape.head({'number': hex(b), 'timestamp': hex(int(now)), 'hash': f'0x{b:x}'}, now)
        now += step
    return now


@pytest.mark.parametrize("when,blocks", sorted(REAL_BURSTS.items()))
def test_a_real_publicnode_permutation_never_resets_the_window(bare, when, blocks):
    """THE PROPERTY. Each of these is a COMPLETE run of blocks -- nothing was ever missing."""
    assert set(blocks) == set(range(min(blocks), max(blocks) + 1)), "fixture must be a permutation"
    before = bare.reset_count
    _burst(bare, blocks)
    assert _gap_resets(bare) == [], f"{when} is a permutation, not a gap"
    assert bare.reset_count == before, "Alpha's backfill hook must not see a reset"


@pytest.mark.parametrize("when,blocks", sorted(REAL_BURSTS.items()))
def test_a_permutation_leaves_the_frontier_at_the_highest_block(bare, when, blocks):
    """The window must end up covering the whole run, not stop at the first hole."""
    _burst(bare, blocks)
    assert bare.last_block == max(blocks)
    assert bare.holes == {}


def test_a_block_that_never_arrives_still_resets(bare):
    """A true gap must keep resetting, or the fix would trade one silent error for another."""
    before = bare.reset_count
    _burst(bare, [100, 101, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112])
    reasons = [r[1] for r in _gap_resets(bare)]
    assert any("hole_expired:102" in r for r in reasons), reasons
    assert bare.reset_count > before, "the backfill hook must see this one"


def test_a_hole_expires_on_time_even_while_headers_keep_arriving(bare):
    """The seconds rule, independent of the header count: fewer than 8 headers, but over 3s."""
    now = 5000.0
    for b in (100, 101, 103):
        bare.head({'number': hex(b), 'timestamp': hex(b), 'hash': f'0x{b:x}'}, now)
        now += 0.1
    bare.head({'number': hex(104), 'timestamp': hex(104), 'hash': '0x104'}, now + 4.0)
    assert any("hole_expired:102" in r[1] for r in _gap_resets(bare)), _gap_resets(bare)


def test_a_hole_filled_inside_the_grace_never_touches_reset_count(bare):
    """Alpha's contract, pinned explicitly: a filled hole is not a reset."""
    before = bare.reset_count
    _burst(bare, [100, 102, 101, 103])
    assert _gap_resets(bare) == []
    assert bare.reset_count == before


def test_repeated_duplicate_headers_never_reset(bare):
    before = bare.reset_count
    _burst(bare, [100, 101, 101, 101, 102, 102, 103])
    assert _gap_resets(bare) == [] and bare.reset_count == before


def test_a_reconnect_far_backward_still_resets(bare):
    """Tonight's two genuine resets were -348 and -1442 blocks after minutes of silence. Those are
    a different socket, not a reorder, and must still drop the window."""
    _burst(bare, [70186934])
    bare.head({'number': hex(70186586), 'timestamp': hex(70186586), 'hash': '0xold'}, 5400.0)
    assert any("backward" in r[1] for r in _gap_resets(bare)), _gap_resets(bare)


def test_a_swap_in_a_late_filled_block_is_still_counted(tape):
    """The point of not resetting: the window survives the reorder, so a swap carried by the
    out-of-order block is still inside it. Under the strict-successor tape the reset dropped the
    window and this swap became unmeasurable."""
    for n, ts in [(6, 1302), (5, 1303), (7, 1304)]:          # 5 arrives AFTER 6
        tape.head({'number': hex(n), 'timestamp': hex(ts), 'hash': f'0x{n:x}'}, ts)
    assert _gap_resets(tape) == [], 'a reorder must not drop the window'
    tape.log(swap(5, tx='0xlate'), 1304)
    out = w.measure(tape.conn, CHAIN, MINT, 1304)
    assert out['swaps'] == 1 and out['usd'] == 2400, out


# --- F5: a transient error must not retire a pool for good --------------------------------------

def _rpc(*per_pool):
    """One answer triple per pool, in the order `resolve_v3_pools` asks for them."""
    flat = [a for triple in per_pool for a in triple]
    return lambda reqs: list(flat[:len(reqs)])


def _v3_pool_with_a_tick(tape, pool):
    tape.log_v3(dict(_v3_swap(), address=pool), 1301)
    return pool


def test_a_rate_limited_resolve_writes_nothing_and_is_asked_again(tape):
    """The F5 defect: `_ws_caller` flattened every error to None and the resolver cached it as
    "not a V3 pool" forever, so ONE rate limit retired a real pool from the m5 sum permanently."""
    pool = _v3_pool_with_a_tick(tape, V3POOL)
    limited = w.RpcErrorReply({"code": -32005, "message": "rate limit exceeded"})
    assert w.resolve_v3_pools(tape.conn, CHAIN, _rpc((limited, limited, limited)), 1301) == 0
    assert tape.conn.execute("SELECT COUNT(*) FROM pool_meta WHERE pool=?", (pool,)).fetchone()[0] == 0
    assert w.resolve_v3_pools(tape.conn, CHAIN, _v3_call(w.NATIVE, MINT), 1302) == 1, \
        "the next pass must still see it"


def test_a_reply_that_never_arrived_writes_nothing_either(tape):
    """None means no reply came back, which says nothing about the contract."""
    pool = _v3_pool_with_a_tick(tape, V3POOL)
    assert w.resolve_v3_pools(tape.conn, CHAIN, _rpc((None, None, None)), 1301) == 0
    assert tape.conn.execute("SELECT COUNT(*) FROM pool_meta WHERE pool=?", (pool,)).fetchone()[0] == 0


def test_a_revert_is_a_property_of_the_contract_and_is_never_asked_again(tape, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_V3_SOFT_RETRY_S", 1.0, raising=False)
    pool = _v3_pool_with_a_tick(tape, V3POOL)
    rev = w.RpcErrorReply({"code": 3, "message": "execution reverted"})
    assert w.resolve_v3_pools(tape.conn, CHAIN, _rpc((rev, rev, rev)), 1301) == 0
    row = tape.conn.execute("SELECT token0, resolve_state FROM pool_meta WHERE pool=?",
                            (pool,)).fetchone()
    assert tuple(row) == ("", "reverted")
    asked = []
    w.resolve_v3_pools(tape.conn, CHAIN, lambda reqs: asked.append(reqs) or [None] * len(reqs), 99999)
    assert asked == [] or all(not r for r in asked), "a revert does not expire"


def test_an_ambiguous_answer_expires_and_is_asked_again(tape, monkeypatch):
    """`0x` is what a contract without token0() returns AND what a node that cannot serve the state
    returns, so the row is cached for the retry window and no longer."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_V3_SOFT_RETRY_S", 100.0, raising=False)
    pool = _v3_pool_with_a_tick(tape, V3POOL)
    assert w.resolve_v3_pools(tape.conn, CHAIN, lambda reqs: ["0x"] * len(reqs), 1301) == 0
    assert tape.conn.execute("SELECT resolve_state FROM pool_meta WHERE pool=?",
                             (pool,)).fetchone()[0] == "ambiguous"
    assert w.resolve_v3_pools(tape.conn, CHAIN, _v3_call(w.NATIVE, MINT), 1350) == 0, \
        "inside the retry window the row stands"
    assert w.resolve_v3_pools(tape.conn, CHAIN, _v3_call(w.NATIVE, MINT), 1500) == 1, \
        "after the window the pool is asked again, and this time it answers"


def test_a_zero_retry_window_keeps_an_ambiguous_row_permanent(tape, monkeypatch):
    """The rollback: 0 restores the behaviour before the review for the rows that carry one."""
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_V3_SOFT_RETRY_S", 0.0, raising=False)
    _v3_pool_with_a_tick(tape, V3POOL)
    assert w.resolve_v3_pools(tape.conn, CHAIN, lambda reqs: ["0x"] * len(reqs), 1301) == 0
    assert w.resolve_v3_pools(tape.conn, CHAIN, _v3_call(w.NATIVE, MINT), 999999) == 0


def test_the_error_reply_carries_its_cause_instead_of_becoming_none():
    import json as _json

    class Sock:
        def __init__(self): self.q = []
        def send(self, body):
            r = _json.loads(body)
            self.q.append({"jsonrpc": "2.0", "id": r["id"],
                           "error": {"code": -32005, "message": "rate limit exceeded"}})
        def recv(self, timeout=None): return _json.dumps(self.q.pop(0))

    out = w._ws_caller(Sock(), tolerate_errors=True)([("eth_call", [{}, "latest"])])
    assert isinstance(out[0], w.RpcErrorReply)
    assert out[0].code == -32005 and out[0].reverted is False
    assert w.RpcErrorReply({"code": 3, "message": "execution reverted"}).reverted is True


# --- per-subscription delivery lag ---------------------------------------------------------------

def _only_new_samples(tape):
    """The `tape` fixture logs a warm-up swap, whose 0 s sample would sit in the median."""
    for samples in tape.lag.values():
        samples.clear()


def test_the_tape_measures_each_subscription_separately(tape):
    """The 2026-09-23 stall in miniature: V4 frames arriving minutes after their block while V3
    frames on the same socket arrive immediately."""
    _only_new_samples(tape)
    tape.head({'number': hex(9), 'timestamp': hex(1400), 'hash': '0x9'}, 1400)
    tape.log(swap(9, tx='0xlate', block_hash='0x9'), 1400 + 600)       # ten minutes late
    tape.log_v3(dict(_v3_swap(), blockNumber=hex(9), blockHash='0x9'), 1400 + 1)
    assert tape.protocol_lag_s('v4') == pytest.approx(600.0)
    assert tape.protocol_lag_s('v3') == pytest.approx(1.0)
    # Persisted on the next header, which is the UPDATE the writer already runs; the gate
    # reads the table rather than computing this, because computing it there costs p50
    # 17.8 ms on the live 4663 tape.
    tape.head({'number': hex(10), 'timestamp': hex(1401), 'hash': '0xa'}, 1401)
    row = tape.conn.execute('SELECT v4_lag_s, v3_lag_s, samples FROM volume_feed_lag '
                            'WHERE chain_id=?', (CHAIN,)).fetchone()
    assert row['v4_lag_s'] == pytest.approx(600.0) and row['v3_lag_s'] == pytest.approx(1.0)
    assert row['samples'] == 2


def test_a_frame_whose_header_never_arrived_contributes_no_sample(tape):
    """A guessed sample is worse than none: it would read as healthy."""
    _only_new_samples(tape)
    tape.log(swap(999, tx='0xorphan', block_hash='0x3e7'), 1400)
    assert tape.protocol_lag_s('v4') is None


def test_a_fresh_socket_has_no_lag_evidence_at_all(tape):
    """Unknown, never zero. A reconnect must not inherit the previous socket's delivery record."""
    tape.head({'number': hex(9), 'timestamp': hex(1400), 'hash': '0x9'}, 1400)
    tape.log(swap(9, tx='0xa', block_hash='0x9'), 1401)
    assert tape.protocol_lag_s('v4') is not None
    tape.reset(1500, 'test')
    assert tape.protocol_lag_s('v4') is None
    assert tape.stalled_window_s(30.0) is None


def test_only_a_measured_lag_asks_for_a_refill(tape):
    """A chain that is simply quiet has no samples, so nothing is fetched. The difference between
    quiet and late is exactly what the samples carry."""
    _only_new_samples(tape)
    tape.head({'number': hex(9), 'timestamp': hex(1400), 'hash': '0x9'}, 1400)
    assert tape.stalled_window_s(30.0) is None, 'no evidence, no refill'
    tape.log(swap(9, tx='0xlate', block_hash='0x9'), 1400 + 400)
    assert tape.stalled_window_s(30.0) == pytest.approx(400.0)
    assert tape.stalled_window_s(30.0, cap_s=120.0) == pytest.approx(120.0), 'bounded'
    assert tape.stalled_window_s(0.0) is None, 'a zero ceiling disables the trigger'


def test_the_lag_never_touches_volume_feeds_five_columns(tape):
    """A rolled-back release inserts five positional values into `volume_feed`; SQLite refuses five
    values for nine columns, so the lag lives in its own table."""
    assert len([r[1] for r in tape.conn.execute('PRAGMA table_info(volume_feed)')]) == 5
    assert [r[1] for r in tape.conn.execute('PRAGMA table_info(volume_feed_lag)')] == \
        ['chain_id', 'v4_lag_s', 'v3_lag_s', 'samples', 'observed', 'pons_lag_s']


# --- a write may add a block's identity, never erase it ------------------------------------------

def test_a_backfill_stamps_every_block_that_carries_a_swap(tape):
    """2026-09-23: the backfill wrote `headers[b]` alone, and `headers` holds only the ~294 anchor
    heights it fetches out of ~3,458 interpolated ones, so ~92% of its rows were hash-less. Every
    block that has a tick has its identity in the LOG's own `blockHash`, at zero extra RPC."""
    # 700 is deliberately NOT an anchor height: anchors are every BACKFILL_ANCHOR_BLOCKS (60) plus
    # the last BACKFILL_SEED_HEADERS (240), so with head=1000 everything from 761 up is fetched and
    # only a swap below that, off the 60-grid, can show this defect at all. Its hash can come from
    # nowhere but the log.
    w.backfill_window(tape, CHAIN, _chain_call(swaps=(700,)), 2000, stored_pool_key=lambda *a: KEY)
    rows = tape.conn.execute(
        "SELECT b.block_no, b.block_hash FROM block_times b "
        "WHERE b.chain_id=? AND b.block_no IN (SELECT DISTINCT block_no FROM pool_ticks "
        "                                       WHERE chain_id=?)", (CHAIN, CHAIN)).fetchall()
    assert rows, 'the backfill wrote no block with a tick'
    assert all(r['block_hash'] for r in rows), \
        [dict(r) for r in rows if not r['block_hash']]


def test_a_backfill_never_erases_a_hash_the_live_path_already_wrote(tape):
    """It was INSERT OR REPLACE, so a height the live `_record` had stamped from its own newHeads
    frame was overwritten with NULL by the next backfill that spanned it. That is what left the
    newest rows carrying a hash and everything behind them empty."""
    w.backfill_window(tape, CHAIN, _chain_call(), 2000, stored_pool_key=lambda *a: KEY)
    # A height this backfill cannot identify: no anchor header, no log. The live path may still
    # know it, and the next backfill must not take that away.
    blind = tape.conn.execute("SELECT block_no FROM block_times WHERE chain_id=? "
                              "AND block_hash IS NULL LIMIT 1", (CHAIN,)).fetchone()
    assert blind is not None, 'fixture no longer produces an unidentifiable height'
    tape.conn.execute("UPDATE block_times SET block_hash=? WHERE chain_id=? AND block_no=?",
                      ('0xlivehash', CHAIN, blind[0]))
    tape.conn.commit()
    w.backfill_window(tape, CHAIN, _chain_call(), 2100, stored_pool_key=lambda *a: KEY)
    kept = tape.conn.execute("SELECT block_hash FROM block_times WHERE chain_id=? AND block_no=?",
                             (CHAIN, blind[0])).fetchone()[0]
    assert kept == '0xlivehash', 'a write may add an identity and never erase one'


def test_a_block_with_neither_a_header_nor_a_log_keeps_null_and_holds_no_ticks(tape):
    """NULL is honest there -- we do not know the hash -- and harmless, because the strict join
    only cares about blocks that carry ticks."""
    w.backfill_window(tape, CHAIN, _chain_call(), 2000, stored_pool_key=lambda *a: KEY)
    nulls = [r[0] for r in tape.conn.execute(
        "SELECT block_no FROM block_times WHERE chain_id=? AND block_hash IS NULL", (CHAIN,))]
    if nulls:
        marks = ",".join("?" * len(nulls))
        assert tape.conn.execute(
            f"SELECT COUNT(*) FROM pool_ticks WHERE chain_id=? AND block_no IN ({marks})",
            (CHAIN, *nulls)).fetchone()[0] == 0


def test_a_legacy_empty_pair_row_with_no_state_is_asked_again(tape, monkeypatch):
    """F5, the half the state column cannot express. `resolve_state` arrived by ALTER, so every row
    written before it carries NULL — and NULL is also what a RESOLVED pool carries. Selecting only
    `ambiguous` left a legacy empty-pair row unasked forever: no pair to price with, no state to
    expire.

    ⚠️ THE REACH VALUE IS THE PAIR, NOT THE STATE. The row must have NULL state AND an empty
    token0; give it a pair and it is a resolved pool that must never be re-asked, which the next
    test pins.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_V3_SOFT_RETRY_S", 100.0, raising=False)
    pool = _v3_pool_with_a_tick(tape, V3POOL)
    tape.conn.execute(
        "INSERT OR REPLACE INTO pool_meta (chain_id, pool, token0, token1, quote_side, "
        "resolved_at, resolve_state) VALUES (?,?,?,?,?,?,NULL)",
        # ⚠️ `resolved_at` IS TEXT ON EVERY LIVE ROW (verified: 106,934 of 106,934 typeof 'text').
        # Seeding it as a float made the expiry comparison `1301.0 <= '2026-...'` — SQLite orders
        # every number before every string, so the row read as expired at ANY time and the test
        # "passed" the retry it was meant to bound.
        (CHAIN, pool, "", "", None, w.iso(1301.0)))
    tape.conn.commit()
    assert w.resolve_v3_pools(tape.conn, CHAIN, _v3_call(w.NATIVE, MINT), 1350) == 0, \
        "inside the retry window even a legacy row stands"
    assert w.resolve_v3_pools(tape.conn, CHAIN, _v3_call(w.NATIVE, MINT), 1500) == 1, \
        "after the window it is asked again and resolves"


def test_a_legacy_row_that_already_has_a_pair_is_never_re_asked(tape, monkeypatch):
    """The other side of the same NULL: a resolved pool written before the column existed.

    Measured on the live store 2026-09-24: all 106,926 `pool_meta` rows carry NULL state and a
    pair, so a rule keyed on NULL alone would have re-resolved the entire table.
    """
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_V3_SOFT_RETRY_S", 100.0, raising=False)
    pool = _v3_pool_with_a_tick(tape, V3POOL)
    tape.conn.execute(
        "INSERT OR REPLACE INTO pool_meta (chain_id, pool, token0, token1, quote_side, "
        "resolved_at, resolve_state) VALUES (?,?,?,?,?,?,NULL)",
        (CHAIN, pool, w.NATIVE, MINT, 0, w.iso(1301.0)))
    tape.conn.commit()
    asked = []
    w.resolve_v3_pools(tape.conn, CHAIN,
                       lambda reqs: asked.append(reqs) or [None] * len(reqs), 99999)
    assert asked == [], "a resolved pool is never re-asked, whatever its state column says"


def test_a_lag_sample_expires_by_age_and_stops_refilling_for_ever(tape):
    """N2. The deque evicts by LENGTH only, so a subscription that stops delivering kept whatever
    it last measured: one 600 s sample held a protocol `lagging` indefinitely, refilled the window
    on every pass, and was written out with `observed=now` as if freshly measured.

    ⚠️ THE REACH VALUE IS THE GAP BETWEEN THE SAMPLE'S ARRIVAL AND `now`. It must exceed
    `LAG_SAMPLE_MAX_AGE_S`; inside that window the sample is still evidence and the old behaviour
    and the new one agree, so a smaller gap proves nothing.
    """
    tape.block_ts[10] = 1000.0
    tape.note_delivery('v4', 10, 1600.0)              # a 600 s lag, arriving at t=1600
    assert tape.protocol_lag_s('v4', 1600.0) == 600.0, "fresh, it is evidence"
    assert tape.protocol_lag_s('v4', 1600.0 + w.LAG_SAMPLE_MAX_AGE_S + 1) is None, \
        "expired, it is UNKNOWN — never 0.0, which would claim a protocol measured as prompt"
    assert tape.stalled_window_s(30.0, now=1600.0) is not None, "still lagging while fresh"
    assert tape.stalled_window_s(30.0, now=1600.0 + w.LAG_SAMPLE_MAX_AGE_S + 1) is None, \
        "an expired sample must not keep refilling the window"


def test_the_last_delivery_separates_no_recent_sample_from_never_any(tape):
    """The evidence the median cannot carry once it expires.

    ⚠️ ON A PROTOCOL THE SHARED FIXTURE HAS NOT TOUCHED. The `tape` fixture opens with a head, so
    v4 has already delivered by the time a test runs and "never delivered" is unreachable there —
    the first version of this test asserted None against a tape that had a 600 s sample.
    """
    assert tape.last_delivery_s('v3', 1600.0) is None, "this protocol has never delivered"
    tape.block_ts[10] = 1000.0
    tape.note_delivery('v3', 10, 1600.0)
    assert tape.last_delivery_s('v3', 1600.0) == 0.0
    assert tape.last_delivery_s('v3', 1900.0) == 300.0


# ---- Pons gets its own delivery key (ULTRONICS, 4663, 2026-10-07) ----

def _pons_frame(block, tx='0xpons'):
    from sniper.services.memecoin_tick_stream import PONS_V2_CURVE_BUY
    return dict(_v3_swap(block=block), transactionHash=tx, blockHash='0x%x' % block,
                topics=[PONS_V2_CURVE_BUY, '0x' + '00' * 32, '0x' + '00' * 32],
                data='0x' + ''.join('%064x' % v for v in (10 ** 18, 10 ** 24, 10 ** 16, 2 * 10 ** 16)))


def test_pons_frames_are_measured_under_their_own_key_never_v3(tape):
    """A dead Pons subscription read healthy because its frames were noted as V3 delivery."""
    _only_new_samples(tape)
    tape.head({'number': hex(9), 'timestamp': hex(1400), 'hash': '0x9'}, 1400)
    tape.log_pons(_pons_frame(9), 1400 + 2)
    assert tape.protocol_lag_s('pons') == pytest.approx(2.0)
    assert tape.protocol_lag_s('v3') is None
    tape.head({'number': hex(10), 'timestamp': hex(1401), 'hash': '0xa'}, 1401)
    row = tape.conn.execute('SELECT v3_lag_s, pons_lag_s FROM volume_feed_lag WHERE chain_id=?',
                            (CHAIN,)).fetchone()
    assert row['pons_lag_s'] == pytest.approx(2.0) and row['v3_lag_s'] is None
    assert w._protocol_lags(tape.conn, CHAIN)['pons'] == pytest.approx(2.0)


def test_a_store_without_the_pons_column_reads_without_a_pons_key(tmp_path):
    import sqlite3
    c = sqlite3.connect(tmp_path / 'old.db')
    c.execute('CREATE TABLE volume_feed_lag (chain_id TEXT PRIMARY KEY, v4_lag_s REAL, v3_lag_s REAL, '
              'samples INTEGER, observed REAL)')
    c.execute("INSERT INTO volume_feed_lag VALUES (?, 0.5, 0.7, 2, 1.0)", (CHAIN,))
    assert w._protocol_lags(c, CHAIN) == {'v4': 0.5, 'v3': 0.7}


# ---- (b) Pons from a second provider (ULTRONICS, 2026-10-07): publicnode's topic-only Pons
# subscription delivered 0/119 while the research QuickNode WS delivered 26/26 ----

def test_the_pons_side_url_is_opt_in_and_never_the_main_socket(monkeypatch):
    monkeypatch.delenv('MEMECOIN_ONCHAIN_M5_PONS_WS_URL_4663', raising=False)
    assert w.pons_side_url('4663', 'wss://main') is None
    monkeypatch.setenv('MEMECOIN_ONCHAIN_M5_PONS_WS_URL_4663', 'wss://main/')
    assert w.pons_side_url('4663', 'wss://main') is None
    monkeypatch.setenv('MEMECOIN_ONCHAIN_M5_PONS_WS_URL_4663', 'https://not-a-socket')
    assert w.pons_side_url('4663', 'wss://main') is None
    monkeypatch.setenv('MEMECOIN_ONCHAIN_M5_PONS_WS_URL_4663', 'wss://side')
    assert w.pons_side_url('4663', 'wss://main') == 'wss://side'


def test_the_side_socket_queues_pons_frames_and_nothing_else():
    import json
    import queue
    import threading
    from sniper.services.memecoin_tick_stream import PONS_V2_CURVE_BUY
    stop, out, sent = threading.Event(), queue.Queue(maxsize=10), []
    frame = _pons_frame(9)
    msgs = [{'id': 1, 'result': hex(int(CHAIN))}, {'id': 5, 'result': 'pons'}, {'id': 6, 'result': 'launch'},
            {'params': {'subscription': 'pons', 'result': frame}},
            {'params': {'subscription': 'other', 'result': {'x': 1}}}]

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def send(self, body): sent.append(json.loads(body))
        def recv(self, timeout=None):
            if not msgs:
                stop.set()
                raise TimeoutError
            return json.dumps(msgs.pop(0))
    w._pons_side_socket(CHAIN, 'wss://side', out, stop, connect_fn=lambda *a, **k: Socket(),
                        clock=lambda: 1234.5, sleep=lambda s: None)
    got = [out.get_nowait() for _ in range(out.qsize())]
    assert got == [(frame, 1234.5)]
    subs = [m['params'] for m in sent if m.get('method') == 'eth_subscribe']
    assert ['logs', {'topics': [[PONS_V2_CURVE_BUY, w.PONS_V2_CURVE_SELL]]}] in subs


def test_a_wrong_chain_side_socket_queues_nothing_and_backs_off():
    import json
    import queue
    import threading
    stop, out, slept = threading.Event(), queue.Queue(maxsize=10), []

    class Socket:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def send(self, body): pass
        def recv(self, timeout=None): return json.dumps({'id': 1, 'result': hex(1)})
    w._pons_side_socket(CHAIN, 'wss://side', out, stop, connect_fn=lambda *a, **k: Socket(),
                        clock=lambda: 1.0, sleep=lambda s: slept.append(s) or stop.set())
    assert out.qsize() == 0 and slept == [1]


def test_the_main_loop_writes_side_frames_on_its_own_thread(tape):
    """The writer drains the side queue into log_pons: the tick lands and Pons delivery is measured."""
    import queue
    _only_new_samples(tape)
    tape.head({'number': hex(9), 'timestamp': hex(1400), 'hash': '0x9'}, 1400)
    q = queue.Queue()
    q.put((_pons_frame(9, tx='0xside'), 1401.5))
    assert w.drain_pons_side(tape, q) == 1
    row = tape.conn.execute("SELECT kind FROM pool_ticks WHERE tx_sig='0xside'").fetchone()
    assert row[0] == 'pons_v2_swap' and tape.protocol_lag_s('pons') == pytest.approx(1.5)
    assert w.drain_pons_side(tape, None) == 0


def test_run_with_a_side_url_writes_side_pons_frames_and_stops_its_thread(tmp_path, monkeypatch):
    import json
    import sqlite3
    import threading
    monkeypatch.setenv('MEMECOIN_ONCHAIN_M5_PONS_WS_URL_%s' % CHAIN, 'wss://side')
    delivered = threading.Event()
    frame = dict(_pons_frame(1, tx='0xfromside'))
    main_msgs = [{'id': 1, 'result': hex(int(CHAIN))}, {'id': 10, 'result': '0x' + f'{18:064x}'},
                 {'id': 2, 'result': 'heads'}, {'id': 3, 'result': 'v4'},
                 {'params': {'subscription': 'heads', 'result': {'number': '0x1', 'timestamp': hex(1000),
                                                                 'hash': '0x1'}}}]
    Main = _stream(main_msgs, [1000.0], monkeypatch)
    side_msgs = [{'id': 1, 'result': hex(int(CHAIN))}, {'id': 5, 'result': 'pons'},
                 {'params': {'subscription': 'pons', 'result': frame}}]

    class Side:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def send(self, body): pass
        def recv(self, timeout=None):
            if side_msgs:
                return json.dumps(side_msgs.pop(0))
            delivered.set()
            raise TimeoutError

    class GatedMain(Main):
        def recv(self, **k):
            if len(main_msgs) == 1:                   # the head frame: wait for the side frame
                delivered.wait(2)
            return super().recv(**k)

    def connect(url, **k):
        return GatedMain() if url == 'wss://unit-test' else Side()
    with pytest.raises(KeyboardInterrupt):
        w.run(CHAIN, 'wss://unit-test', path=tmp_path / 'v.db', connect_fn=connect)
    assert not [t for t in threading.enumerate() if t.name.startswith('pons-side-') and t.is_alive()]
    with sqlite3.connect(tmp_path / 'v.db') as c:
        assert c.execute("SELECT kind FROM pool_ticks WHERE tx_sig='0xfromside'").fetchone() == ('pons_v2_swap',)

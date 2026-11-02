"""Historical arm schema upgrades preserve every row; live CROWD needs causal proof."""
import sqlite3
import pytest
from sniper.office import large_buyer_arm as A

HISTORICAL_DDL = {'bd73000c6': '\nCREATE TABLE IF NOT EXISTS twins(pool TEXT,wallet TEXT,variant TEXT NOT NULL,mint TEXT,state TEXT NOT NULL,spend_lamports TEXT,tokens_atomic TEXT,filled_at REAL,peak_lamports TEXT,last_mark_at REAL,marks_null INTEGER NOT NULL DEFAULT 0,requested_at REAL,exit_reason TEXT,exit_due REAL,exit_at REAL,exit_fill_kind TEXT,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,reason TEXT,stress_gross_pct REAL,stress_reason TEXT,PRIMARY KEY(pool,wallet,variant));\nCREATE INDEX IF NOT EXISTS twins_state ON twins(state);\nCREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL);\nCREATE TABLE IF NOT EXISTS wallets(pool TEXT,wallet TEXT,bought INTEGER NOT NULL DEFAULT 0,sold INTEGER NOT NULL DEFAULT 0,fired INTEGER NOT NULL DEFAULT 0,last_at REAL,PRIMARY KEY(pool,wallet)) WITHOUT ROWID;\nCREATE INDEX IF NOT EXISTS wallets_last ON wallets(last_at);\nCREATE TABLE IF NOT EXISTS seen(signature TEXT,ordinal INTEGER,at REAL,PRIMARY KEY(signature,ordinal)) WITHOUT ROWID;\nCREATE INDEX IF NOT EXISTS seen_at ON seen(at);\nCREATE TABLE IF NOT EXISTS triggers(pool TEXT,wallet TEXT,mint TEXT,signature TEXT,trigger_at REAL,detected_at REAL,state TEXT NOT NULL,reason TEXT,entry_due REAL,entry_at REAL,tokens_atomic TEXT,exit_due REAL,exit_at REAL,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,entry_tries INTEGER NOT NULL DEFAULT 0,exit_tries INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(pool,wallet));\nCREATE INDEX IF NOT EXISTS triggers_state ON triggers(state);\n', 'd23f451f8': '\nCREATE TABLE IF NOT EXISTS twins(pool TEXT,wallet TEXT,variant TEXT NOT NULL,mint TEXT,state TEXT NOT NULL,spend_lamports TEXT,tokens_atomic TEXT,filled_at REAL,peak_lamports TEXT,last_mark_at REAL,marks_null INTEGER NOT NULL DEFAULT 0,requested_at REAL,exit_reason TEXT,exit_due REAL,exit_at REAL,exit_fill_kind TEXT,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,reason TEXT,stress_gross_pct REAL,stress_reason TEXT,PRIMARY KEY(pool,wallet,variant));\nCREATE INDEX IF NOT EXISTS twins_state ON twins(state);\nCREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL);\nCREATE TABLE IF NOT EXISTS wallets(pool TEXT,wallet TEXT,bought INTEGER NOT NULL DEFAULT 0,sold INTEGER NOT NULL DEFAULT 0,fired INTEGER NOT NULL DEFAULT 0,last_at REAL,PRIMARY KEY(pool,wallet)) WITHOUT ROWID;\nCREATE INDEX IF NOT EXISTS wallets_last ON wallets(last_at);\nCREATE TABLE IF NOT EXISTS seen(signature TEXT,ordinal INTEGER,at REAL,PRIMARY KEY(signature,ordinal)) WITHOUT ROWID;\nCREATE INDEX IF NOT EXISTS seen_at ON seen(at);\nCREATE TABLE IF NOT EXISTS triggers(pool TEXT,wallet TEXT,mint TEXT,signature TEXT,trigger_at REAL,detected_at REAL,state TEXT NOT NULL,reason TEXT,entry_due REAL,entry_at REAL,tokens_atomic TEXT,exit_due REAL,exit_at REAL,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,entry_tries INTEGER NOT NULL DEFAULT 0,exit_tries INTEGER NOT NULL DEFAULT 0,pool_quote_before TEXT,post_quote TEXT,post_base TEXT,PRIMARY KEY(pool,wallet));\nCREATE INDEX IF NOT EXISTS triggers_state ON triggers(state);\n', 'aa97295f8': '\nCREATE TABLE IF NOT EXISTS twins(pool TEXT,wallet TEXT,variant TEXT NOT NULL,mint TEXT,state TEXT NOT NULL,spend_lamports TEXT,tokens_atomic TEXT,filled_at REAL,peak_lamports TEXT,last_mark_at REAL,marks_null INTEGER NOT NULL DEFAULT 0,requested_at REAL,exit_reason TEXT,exit_due REAL,exit_at REAL,exit_fill_kind TEXT,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,reason TEXT,stress_gross_pct REAL,stress_reason TEXT,exit_elapsed_s REAL,stress_at REAL,stress_elapsed_s REAL,PRIMARY KEY(pool,wallet,variant));\nCREATE INDEX IF NOT EXISTS twins_state ON twins(state);\nCREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL);\nCREATE TABLE IF NOT EXISTS wallets(pool TEXT,wallet TEXT,bought INTEGER NOT NULL DEFAULT 0,sold INTEGER NOT NULL DEFAULT 0,fired INTEGER NOT NULL DEFAULT 0,last_at REAL,PRIMARY KEY(pool,wallet)) WITHOUT ROWID;\nCREATE INDEX IF NOT EXISTS wallets_last ON wallets(last_at);\nCREATE TABLE IF NOT EXISTS seen(signature TEXT,ordinal INTEGER,at REAL,PRIMARY KEY(signature,ordinal)) WITHOUT ROWID;\nCREATE INDEX IF NOT EXISTS seen_at ON seen(at);\nCREATE TABLE IF NOT EXISTS triggers(pool TEXT,wallet TEXT,mint TEXT,signature TEXT,trigger_at REAL,detected_at REAL,state TEXT NOT NULL,reason TEXT,entry_due REAL,entry_at REAL,tokens_atomic TEXT,exit_due REAL,exit_at REAL,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,entry_tries INTEGER NOT NULL DEFAULT 0,exit_tries INTEGER NOT NULL DEFAULT 0,pool_quote_before TEXT,post_quote TEXT,post_base TEXT,entry_elapsed_s REAL,exit_elapsed_s REAL,PRIMARY KEY(pool,wallet));\nCREATE INDEX IF NOT EXISTS triggers_state ON triggers(state);\n', '374d39507': '\nCREATE TABLE IF NOT EXISTS twins(pool TEXT,wallet TEXT,variant TEXT NOT NULL,mint TEXT,state TEXT NOT NULL,spend_lamports TEXT,tokens_atomic TEXT,filled_at REAL,peak_lamports TEXT,last_mark_at REAL,marks_null INTEGER NOT NULL DEFAULT 0,requested_at REAL,exit_reason TEXT,exit_due REAL,exit_at REAL,exit_fill_kind TEXT,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,reason TEXT,stress_gross_pct REAL,stress_reason TEXT,exit_elapsed_s REAL,stress_at REAL,stress_elapsed_s REAL,PRIMARY KEY(pool,wallet,variant));\nCREATE INDEX IF NOT EXISTS twins_state ON twins(state);\nCREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL);\nCREATE TABLE IF NOT EXISTS wallets(pool TEXT,wallet TEXT,bought INTEGER NOT NULL DEFAULT 0,sold INTEGER NOT NULL DEFAULT 0,fired INTEGER NOT NULL DEFAULT 0,last_at REAL,PRIMARY KEY(pool,wallet)) WITHOUT ROWID;\nCREATE TABLE IF NOT EXISTS seen(signature TEXT,ordinal INTEGER,at REAL,PRIMARY KEY(signature,ordinal)) WITHOUT ROWID;\nCREATE INDEX IF NOT EXISTS seen_at ON seen(at);\nCREATE TABLE IF NOT EXISTS triggers(pool TEXT,wallet TEXT,mint TEXT,signature TEXT,trigger_at REAL,detected_at REAL,state TEXT NOT NULL,reason TEXT,entry_due REAL,entry_at REAL,tokens_atomic TEXT,exit_due REAL,exit_at REAL,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,entry_tries INTEGER NOT NULL DEFAULT 0,exit_tries INTEGER NOT NULL DEFAULT 0,pool_quote_before TEXT,post_quote TEXT,post_base TEXT,entry_elapsed_s REAL,exit_elapsed_s REAL,PRIMARY KEY(pool,wallet));\nCREATE INDEX IF NOT EXISTS triggers_state ON triggers(state);\n', 'c8544e66b': '\nCREATE TABLE IF NOT EXISTS twins(pool TEXT,wallet TEXT,variant TEXT NOT NULL,mint TEXT,state TEXT NOT NULL,spend_lamports TEXT,tokens_atomic TEXT,filled_at REAL,peak_lamports TEXT,last_mark_at REAL,marks_null INTEGER NOT NULL DEFAULT 0,requested_at REAL,exit_reason TEXT,exit_due REAL,exit_at REAL,exit_fill_kind TEXT,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,reason TEXT,stress_gross_pct REAL,stress_reason TEXT,exit_elapsed_s REAL,stress_at REAL,stress_elapsed_s REAL,PRIMARY KEY(pool,wallet,variant));\nCREATE INDEX IF NOT EXISTS twins_state ON twins(state);\nCREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL);\nCREATE TABLE IF NOT EXISTS wallets(pool TEXT,wallet TEXT,bought INTEGER NOT NULL DEFAULT 0,sold INTEGER NOT NULL DEFAULT 0,fired INTEGER NOT NULL DEFAULT 0,last_at REAL,PRIMARY KEY(pool,wallet)) WITHOUT ROWID;\nCREATE TABLE IF NOT EXISTS seen(signature TEXT,ordinal INTEGER,at REAL,PRIMARY KEY(signature,ordinal)) WITHOUT ROWID;\nCREATE INDEX IF NOT EXISTS seen_at ON seen(at);\nCREATE TABLE IF NOT EXISTS triggers(pool TEXT,wallet TEXT,mint TEXT,signature TEXT,trigger_at REAL,detected_at REAL,state TEXT NOT NULL,reason TEXT,entry_due REAL,entry_at REAL,tokens_atomic TEXT,exit_due REAL,exit_at REAL,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,entry_tries INTEGER NOT NULL DEFAULT 0,exit_tries INTEGER NOT NULL DEFAULT 0,pool_quote_before TEXT,post_quote TEXT,post_base TEXT,entry_elapsed_s REAL,exit_elapsed_s REAL,buyer_first_sell_at REAL,PRIMARY KEY(pool,wallet));\nCREATE INDEX IF NOT EXISTS triggers_state ON triggers(state);\n', '3732bea23': '\nCREATE TABLE IF NOT EXISTS twins(pool TEXT,wallet TEXT,variant TEXT NOT NULL,mint TEXT,state TEXT NOT NULL,spend_lamports TEXT,tokens_atomic TEXT,filled_at REAL,peak_lamports TEXT,last_mark_at REAL,marks_null INTEGER NOT NULL DEFAULT 0,requested_at REAL,exit_reason TEXT,exit_due REAL,exit_at REAL,exit_fill_kind TEXT,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,reason TEXT,stress_gross_pct REAL,stress_reason TEXT,exit_elapsed_s REAL,stress_at REAL,stress_elapsed_s REAL,PRIMARY KEY(pool,wallet,variant));\nCREATE INDEX IF NOT EXISTS twins_state ON twins(state);\nCREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value REAL);\nCREATE TABLE IF NOT EXISTS wallets(pool TEXT,wallet TEXT,bought INTEGER NOT NULL DEFAULT 0,sold INTEGER NOT NULL DEFAULT 0,fired INTEGER NOT NULL DEFAULT 0,last_at REAL,PRIMARY KEY(pool,wallet)) WITHOUT ROWID;\nCREATE TABLE IF NOT EXISTS seen(signature TEXT,ordinal INTEGER,at REAL,PRIMARY KEY(signature,ordinal)) WITHOUT ROWID;\nCREATE INDEX IF NOT EXISTS seen_at ON seen(at);\nCREATE TABLE IF NOT EXISTS triggers(pool TEXT,wallet TEXT,mint TEXT,signature TEXT,trigger_at REAL,detected_at REAL,state TEXT NOT NULL,reason TEXT,entry_due REAL,entry_at REAL,tokens_atomic TEXT,exit_due REAL,exit_at REAL,proceeds_lamports TEXT,gross_pct REAL,net_pct REAL,entry_tries INTEGER NOT NULL DEFAULT 0,exit_tries INTEGER NOT NULL DEFAULT 0,pool_quote_before TEXT,post_quote TEXT,post_base TEXT,entry_elapsed_s REAL,exit_elapsed_s REAL,buyer_first_sell_at REAL,others_60s INTEGER,PRIMARY KEY(pool,wallet));\nCREATE TABLE IF NOT EXISTS reaches(pool TEXT,at REAL,wallet TEXT,PRIMARY KEY(pool,at,wallet)) WITHOUT ROWID;\nCREATE INDEX IF NOT EXISTS triggers_state ON triggers(state);\n'}

@pytest.mark.parametrize('sha',list(HISTORICAL_DDL))
def test_every_shipped_schema_upgrades_additively_and_idempotently(tmp_path,sha):
    path=tmp_path/'arm.db';old=sqlite3.connect(path);old.executescript(HISTORICAL_DDL[sha])
    old.execute("INSERT INTO wallets(pool,wallet,bought,sold,fired,last_at) VALUES('P','sold',123,1,0,1)")
    old.execute("INSERT INTO triggers(pool,wallet,mint,signature,trigger_at,detected_at,state,entry_due) VALUES('P','w','M','s',2,3,'pending',12)")
    old.execute("INSERT INTO twins(pool,wallet,variant,mint,state) VALUES('P','w','rails','M','open')")
    old.execute("INSERT INTO meta VALUES('cursor',5)");old.commit();old.close()
    for _ in range(2):
        conn=A.open_db(path)
        assert conn.execute("SELECT bought,sold,last_at FROM wallets WHERE wallet='sold'").fetchone()[:]==(123,1,1)
        row=conn.execute('SELECT * FROM triggers').fetchone()
        assert row['signature']=='s' and row['state']=='pending'
        for field in ('pool_quote_before','post_quote','post_base','entry_elapsed_s','exit_elapsed_s','buyer_first_sell_at','others_60s','crowd_others_60s','crowd_state','crowd_reason','source_slot','source_tx_index','source_ordinal','source_signature','detect_lag_s','detection_state','history_state','history_reason'):
            assert row[field] is None
        twin=conn.execute('SELECT * FROM twins').fetchone()
        for field in ('exit_elapsed_s','stress_at','stress_elapsed_s'):assert twin[field] is None
        assert conn.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0]==5
        conn.close()


def test_half_applied_upgrade_resumes_without_replacing_history(tmp_path):
    path=tmp_path/'arm.db';conn=sqlite3.connect(path);conn.executescript(HISTORICAL_DDL['bd73000c6'])
    conn.execute('ALTER TABLE triggers ADD COLUMN pool_quote_before TEXT')
    conn.execute("INSERT INTO triggers(pool,wallet,state,pool_quote_before) VALUES('P','w','closed','40000000000')")
    conn.commit();conn.close();conn=A.open_db(path)
    row=conn.execute('SELECT * FROM triggers').fetchone()
    assert row['pool_quote_before']=='40000000000' and row['state']=='closed'
    assert row['others_60s'] is None and row['post_base'] is None
    conn.close()

T0=A.START_AT+100

@pytest.fixture
def world(tmp_path):
    conn=A.open_db(tmp_path/'arm.db');tape=sqlite3.connect(':memory:')
    tape.execute('CREATE TABLE swaps_ps(signature,ordinal,pool,mint,actor,is_buy,quote_atomic,quote_mint,received_at,slot,tx_index,pre_quote_reserve_atomic,post_quote_reserve_atomic,post_base_reserve_atomic)')
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(T0-120,));conn.commit()
    yield conn,tape
    conn.close();tape.close()


def swap(tape,wallet,at,*,slot=10,index=1,ordinal=0,signature=None,buy=True,sol=25,pool='P'):
    tape.execute('INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(signature or wallet,ordinal,pool,'M',wallet,int(buy),str(int(sol*10**9)),A.WSOL,at,slot,index,'40000000000','65000000000','1000000000000'))


def trigger(conn,wallet):return conn.execute('SELECT * FROM triggers WHERE wallet=?',(wallet,)).fetchone()


def test_same_receipt_causal_lower_ordinal_counts_and_later_sell_keeps_membership(world):
    conn,tape=world
    swap(tape,'first',T0,signature='bundle',ordinal=0)
    swap(tape,'first',T0,signature='bundle',ordinal=1,buy=False,sol=1)
    swap(tape,'next',T0,signature='bundle',ordinal=2)
    A.ingest(conn,tape,T0+0.25)
    row=trigger(conn,'next')
    assert row['crowd_others_60s']==1 and row['crowd_state']=='known'
    assert row['source_slot']==10 and row['source_tx_index']==1 and row['source_ordinal']==2
    assert row['source_signature']=='bundle' and row['detect_lag_s']==pytest.approx(.25)
    assert row['detection_state']=='known' and row['entry_due']==T0+10
    assert trigger(conn,'first')['buyer_first_sell_at']==T0
    assert A.ingest(conn,tape,T0+1)['fired']==0
    assert trigger(conn,'next')['detected_at']==T0+.25


def test_later_source_print_does_not_count_even_if_receipt_was_earlier(world):
    conn,tape=world
    swap(tape,'chain_future',T0-1,slot=20)
    swap(tape,'current',T0,slot=10)
    A.ingest(conn,tape,T0+1)
    assert trigger(conn,'current')['crowd_others_60s']==0


@pytest.mark.parametrize('index',[None,0,-1])
def test_unknown_source_order_never_creates_known_crowd(world,index):
    conn,tape=world
    swap(tape,'first',T0-1,index=index)
    swap(tape,'next',T0,slot=11)
    A.ingest(conn,tape,T0+1)
    assert trigger(conn,'first')['crowd_state']=='unknown'
    assert trigger(conn,'next')['crowd_others_60s'] is None
    assert trigger(conn,'next')['crowd_state']=='unknown'


def test_sell_before_own_reach_blocks_but_never_erases_lifetime_history(world):
    conn,tape=world
    swap(tape,'sold',T0-100,slot=1,buy=False,sol=1)
    swap(tape,'sold',T0,slot=2)
    swap(tape,'next',T0+1,slot=3)
    A.ingest(conn,tape,T0+2)
    assert trigger(conn,'sold') is None
    assert trigger(conn,'next')['crowd_others_60s']==0
    assert conn.execute("SELECT sold FROM wallets WHERE wallet='sold'").fetchone()[0]==1


def test_old_null_history_is_not_reconstructed_as_a_known_crowd(world):
    conn,tape=world
    conn.execute("INSERT INTO reaches(pool,at,wallet) VALUES('P',?,'legacy')",(T0-1,))
    swap(tape,'new',T0,slot=20)
    A.ingest(conn,tape,T0+1)
    row=trigger(conn,'new')
    assert row['others_60s']==1 and row['crowd_others_60s'] is None and row['crowd_state']=='unknown'


def test_old_wallet_partial_buy_cannot_become_proven_after_upgrade(world):
    conn,tape=world
    conn.execute("INSERT INTO wallets(pool,wallet,bought,sold,fired) VALUES('P','old',20000000000,0,0)")
    swap(tape,'old',T0,sol=5)
    A.ingest(conn,tape,T0+1)
    row=trigger(conn,'old')
    assert row['crowd_state']=='unknown' and row['crowd_others_60s'] is None


@pytest.mark.parametrize('lag,state',[(0,'known'),(5,'known'),(5.001,'stale'),(300,'stale'),(-1,'unknown')])
def test_detect_receipt_lag_is_measured_and_late_history_cannot_look_fresh(world,lag,state):
    conn,tape=world
    swap(tape,'first',T0-1,slot=9);swap(tape,'next',T0)
    A.ingest(conn,tape,max(T0,T0+lag),detected_now=lambda:T0+lag)
    row=trigger(conn,'next')
    assert row['detect_lag_s']==pytest.approx(lag)
    assert row['detection_state']==state
    assert row['state']=='pending'  # original paper/first-buyer cohort is unchanged


def test_crowd_window_is_inclusive_at_60_seconds_distinct_wallet_and_same_pool(world):
    conn,tape=world
    swap(tape,'old',T0-60.001,slot=1)
    swap(tape,'edge',T0-60,slot=2)
    swap(tape,'other_pool',T0-1,slot=3,pool='Q')
    swap(tape,'next',T0,slot=4)
    A.ingest(conn,tape,T0+1)
    assert trigger(conn,'next')['crowd_others_60s']==1


def test_detector_timestamp_measures_processing_delay_not_only_poll_start(world):
    conn,tape=world;swap(tape,'w',T0)
    A.ingest(conn,tape,T0+1,detected_now=lambda:T0+6)
    row=trigger(conn,'w')
    assert row['detected_at']==T0+6 and row['detect_lag_s']==6 and row['detection_state']=='stale'


@pytest.mark.parametrize('bad',[None,float('nan'),float('inf')])
def test_bad_detection_clock_rolls_back_the_whole_ingest(world,bad):
    conn,tape=world;swap(tape,'w',T0)
    with pytest.raises(ValueError,match='detection_clock_unknown'):
        A.ingest(conn,tape,T0+1,detected_now=lambda:bad)
    assert conn.execute('SELECT count(*) FROM seen').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM triggers').fetchone()[0]==0


def test_conflicting_same_slot_transaction_indexes_are_not_order_proof(world):
    conn,tape=world
    swap(tape,'a',T0,signature='one',index=1)
    swap(tape,'b',T0,signature='two',index=1)
    A.ingest(conn,tape,T0+1)
    assert trigger(conn,'a')['crowd_state']=='unknown'
    assert trigger(conn,'b')['crowd_others_60s'] is None


def test_causal_wallet_accumulation_and_out_of_order_old_buy_fail_closed(world):
    conn,tape=world
    swap(tape,'a',T0-1,slot=9,sol=20,signature='first')
    swap(tape,'a',T0,slot=8,sol=5,signature='late_old')
    A.ingest(conn,tape,T0+1)
    assert trigger(conn,'a')['crowd_state']=='unknown'
    assert conn.execute("SELECT bought FROM wallets WHERE wallet='a'").fetchone()[0]==25*10**9


def test_two_concurrent_schema_openers_preserve_existing_rows(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    path=tmp_path/'arm.db';old=sqlite3.connect(path);old.executescript(HISTORICAL_DDL['bd73000c6'])
    old.execute("INSERT INTO wallets(pool,wallet,bought,sold,fired) VALUES('P','w',1,1,1)");old.commit();old.close()
    def reopen(_):
        conn=A.open_db(path)
        try:return conn.execute('SELECT bought,sold FROM wallets').fetchone()[:]
        finally:conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:assert list(pool.map(reopen,range(2)))==[(1,1),(1,1)]


def test_standalone_worker_only_ingests_stored_tape_never_grades_or_places_orders(world,tmp_path,monkeypatch):
    import importlib.util,json
    from pathlib import Path
    path=Path(__file__).resolve().parents[2]/'scripts/memecoin_crowd_trigger_worker.py'
    spec=importlib.util.spec_from_file_location('crowd_trigger_worker',path);worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    conn,tape=world;swap(tape,'first',T0-1,slot=9);swap(tape,'next',T0)
    desk=tmp_path/'desk';(desk/'pumpswap').mkdir(parents=True);tape.commit()
    dest=sqlite3.connect(desk/'pumpswap/tape.db');tape.backup(dest)
    dest.execute('CREATE INDEX swaps_ps_received ON swaps_ps(received_at)');dest.execute('CREATE INDEX swaps_ps_pool_slot ON swaps_ps(pool,slot)');dest.close()
    before=(desk/'pumpswap/tape.db').read_bytes()
    def forbidden(*args,**kwargs):raise AssertionError('paper grading forbidden in pilot trigger worker')
    monkeypatch.setattr(A,'grade',forbidden);monkeypatch.setattr(A,'twin_tick',forbidden)
    clock=[T0+1]
    worker.run(db=tmp_path/'arm.db',desk_dir=desk,heartbeat=tmp_path/'heartbeat.json',period_s=.5,until=T0+1.5,clock=lambda:clock[0],sleep=lambda s:clock.__setitem__(0,clock[0]+s))
    beat=json.loads((tmp_path/'heartbeat.json').read_text())
    assert beat['mode']=='trigger_only' and beat['state']=='known' and beat['fired']==2
    assert beat['detect_lag_p90_s']==2 and trigger(conn,'next')['crowd_others_60s']==1
    assert conn.execute('SELECT count(*) FROM twins').fetchone()[0]==0
    assert (desk/'pumpswap/tape.db').read_bytes()==before


@pytest.mark.parametrize('period',[0,1.01,float('nan')])
def test_standalone_worker_refuses_an_unbounded_or_slow_poll_period(tmp_path,period):
    import importlib.util
    from pathlib import Path
    path=Path(__file__).resolve().parents[2]/'scripts/memecoin_crowd_trigger_worker.py'
    spec=importlib.util.spec_from_file_location('crowd_trigger_worker',path);worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
    with pytest.raises(ValueError,match='period'):
        worker.run(db=tmp_path/'never.db',desk_dir=tmp_path,heartbeat=tmp_path/'never.json',period_s=period)
    assert not (tmp_path/'never.db').exists()


def test_future_receipt_cannot_advance_the_cursor_and_hide_current_prints(world):
    conn,tape=world
    swap(tape,'future',T0+3600,slot=20);swap(tape,'current',T0,slot=10)
    assert A.ingest(conn,tape,T0+1)['fired']==1
    assert trigger(conn,'future') is None and trigger(conn,'current') is not None
    assert conn.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0]==T0


def test_source_order_read_is_slot_bounded_even_with_large_pool_history(world):
    conn,tape=world
    tape.execute('CREATE INDEX swaps_ps_received ON swaps_ps(received_at)')
    tape.execute('CREATE INDEX swaps_ps_pool_slot ON swaps_ps(pool,slot)')
    tape.execute('CREATE INDEX swaps_ps_pool_received ON swaps_ps(pool,received_at)')
    for n in range(5000):swap(tape,'historic'+str(n),T0-1000,slot=n+100,signature='h'+str(n))
    swap(tape,'first',T0-1,slot=9000);swap(tape,'next',T0,slot=9001)
    operations=[0]
    def budget():
        operations[0]+=1000
        return operations[0]>10000
    tape.set_progress_handler(budget,1000)
    try:A.ingest(conn,tape,T0+1)
    finally:tape.set_progress_handler(None,0)
    assert trigger(conn,'next')['crowd_others_60s']==1
    assert operations[0]<=10000


def test_whole_ingest_budget_rolls_back_without_half_proof(world):
    conn,tape=world;swap(tape,'w',T0)
    with pytest.raises(ValueError,match='ingest_budget_exhausted'):
        A.ingest(conn,tape,T0+1,should_stop=lambda:True)
    assert conn.execute('SELECT count(*) FROM seen').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM triggers').fetchone()[0]==0


def test_budget_expiring_on_last_trigger_rolls_back_before_commit(world):
    conn,tape=world;swap(tape,'w',T0);expired=[False]
    def detected():
        expired[0]=True
        return T0+1
    with pytest.raises(ValueError,match='ingest_budget_exhausted'):
        A.ingest(conn,tape,T0+1,detected_now=detected,should_stop=lambda:expired[0])
    assert conn.execute('SELECT count(*) FROM triggers').fetchone()[0]==0
    assert conn.execute('SELECT count(*) FROM seen').fetchone()[0]==0


def test_complete_history_is_explicitly_unproven_and_never_reconstructed(world):
    conn,tape=world;swap(tape,'first',T0-1,slot=9);swap(tape,'next',T0)
    A.ingest(conn,tape,T0+1)
    row=trigger(conn,'next')
    assert row['history_state']=='unknown' and row['history_reason']=='complete_pool_history_unproven'
    assert row['crowd_others_60s']==1  # observed split remains separate from complete history


def test_history_capture_start_marker_is_write_once_without_claiming_coverage(tmp_path):
    path=tmp_path/'arm.db';conn=A.open_db(path)
    first=conn.execute("SELECT value FROM meta WHERE key='arm_history_start_host'").fetchone()[0]
    assert isinstance(first,(float,int)) and first>0
    conn.close();conn=A.open_db(path)
    assert conn.execute("SELECT value FROM meta WHERE key='arm_history_start_host'").fetchone()[0]==first
    conn.close()


def test_trigger_unit_sets_external_state_root_after_any_inherited_environment():
    from pathlib import Path
    unit=(Path(__file__).resolve().parents[2]/'deploy/office/memecoin-crowd-trigger.service').read_text()
    command=next(line for line in unit.splitlines() if line.startswith('ExecStart='))
    assert command.startswith('ExecStart=/usr/bin/env SNIPER_STATE_ROOT=%h/Desktop/Sniper ')
    assert '--db %h/Desktop/Sniper/data/office/v2/large-buyer-arm/arm.db' in command
    assert '--desk-dir %h/Desktop/Sniper/data/desk' in command

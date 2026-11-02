import sqlite3
import pytest
from sniper.office import large_buyer_arm as A,ledger as L

T0=A.START_AT+100
S=10**9


@pytest.fixture
def world(tmp_path):
    tape=sqlite3.connect(':memory:')
    tape.execute('CREATE TABLE swaps_ps(signature,ordinal,pool,mint,actor,is_buy,quote_atomic,quote_mint,received_at,slot,tx_index,pre_quote_reserve_atomic,post_quote_reserve_atomic,post_base_reserve_atomic)')
    return A.open_db(tmp_path/'a.db'),tape


def swap(tape,n,actor,buy,sol,at,pool='P',quote=A.WSOL):
    tape.execute('INSERT INTO swaps_ps VALUES(?,0,?,?,?,?,?,?,?,?,0,?,?,?)',('s%d'%n,pool,'M',actor,int(buy),str(int(sol*S)),quote,at,n,str(100*S),str(125*S),str(10**15)))


def test_first_reach_fires_once_and_cumulates(world):
    conn,tape=world
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(T0-1,))
    swap(tape,1,'w',1,20,T0);swap(tape,2,'w',1,5,T0+1);swap(tape,3,'w',1,50,T0+2)
    assert A.ingest(conn,tape,T0+3)['fired']==1
    assert A.ingest(conn,tape,T0+4)['fired']==0     # overlap read does not double count
    t=conn.execute('SELECT * FROM triggers').fetchone()
    assert t['trigger_at']==T0+1 and t['entry_due']==T0+11 and t['signature']=='s2'


def test_an_earlier_sell_spends_the_trigger_and_later_buys_do_not_fire(world):
    conn,tape=world
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(T0-1,))
    swap(tape,1,'w',0,1,T0);swap(tape,2,'w',1,30,T0+1)
    assert A.ingest(conn,tape,T0+2)['fired']==0
    swap(tape,3,'w',1,30,T0+3)
    assert A.ingest(conn,tape,T0+4)['fired']==0


def test_other_quote_mint_and_before_start_do_not_fire_but_history_builds(world):
    conn,tape=world
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(A.START_AT-50,))
    swap(tape,1,'u',1,99,T0,quote='USDC')
    swap(tape,2,'w',1,30,A.START_AT-10)
    swap(tape,3,'w',1,30,A.START_AT+10)
    assert A.ingest(conn,tape,T0+1)['fired']==0
    assert conn.execute('SELECT count(*) FROM triggers').fetchone()[0]==0


def test_grade_uses_frozen_times_and_censors_rather_than_prices_late(world,monkeypatch):
    conn,tape=world
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(T0-1,))
    swap(tape,1,'w',1,25,T0);A.ingest(conn,tape,T0+1)
    cuts=[]
    def buy(desk,mint,amount,cut,**k):
        cuts.append(cut);return dict(pool='P',tokens_atomic='1000'),None
    def sell(desk,mint,amount,cut,**k):
        cuts.append(cut);return dict(pool='P',lamports=str(int(A.CLIP_LAMPORTS*1.1))),None
    monkeypatch.setattr(L,'_ps_buy',buy);monkeypatch.setattr(L,'_ps_sell',sell)
    assert A.grade(conn,'/none',T0+5)['entered']==0          # not due
    assert A.grade(conn,'/none',T0+11)['entered']==1
    assert A.grade(conn,'/none',T0+200)['closed']==0
    assert A.grade(conn,'/none',T0+311)['closed']==1
    t=conn.execute('SELECT * FROM triggers').fetchone()
    assert cuts==[T0+10,T0+310] and t['gross_pct']==pytest.approx(10) and t['net_pct']==pytest.approx(7)


def test_no_quote_retries_then_censors_counted(world,monkeypatch):
    conn,tape=world
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(T0-1,))
    swap(tape,1,'w',1,25,T0);A.ingest(conn,tape,T0+1)
    monkeypatch.setattr(L,'_ps_buy',lambda *a,**k:(None,'pool_tip_unproven'))
    assert A.grade(conn,'/none',T0+20)['censored']==0
    assert A.grade(conn,'/none',T0+41)['censored']==1
    assert A.report(conn)['censored_reasons']=={'entry:pool_tip_unproven':1}


def test_a_batch_limit_never_skips_rows(world,monkeypatch):
    conn,tape=world
    monkeypatch.setattr(A,'BATCH',2)
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(T0-1,))
    monkeypatch.setattr(A,'OVERLAP_S',0.)
    for n in range(5):swap(tape,n,'w',1,10,T0+n)
    for k in range(4):A.ingest(conn,tape,T0+9)
    assert conn.execute('SELECT bought FROM wallets').fetchone()[0]==50*S


def open_twin(world,monkeypatch):
    conn,tape=world
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(T0-1,))
    swap(tape,1,'w',1,25,T0);A.ingest(conn,tape,T0+1)
    monkeypatch.setattr(L,'_ps_buy',lambda *a,**k:(dict(pool='P',tokens_atomic='1000'),None))
    A.grade(conn,'/none',T0+11)
    monkeypatch.setattr(L,'_committed_cut',lambda d,c:T0+10**6)
    return conn


def value(pct):
    return lambda *a,**k:(dict(pool='P',lamports=str(int(A.CLIP_LAMPORTS*(1+pct/100)))),None)


def test_twin_has_the_same_entry_and_its_rail_exits_apart_from_the_fixed_row(world,monkeypatch):
    conn=open_twin(world,monkeypatch)
    monkeypatch.setattr(L,'_ps_sell',value(50));A.twin_tick(conn,'/none',T0+20)
    monkeypatch.setattr(L,'_ps_sell',value(10));A.twin_tick(conn,'/none',T0+24)    # 50 to 10 is a trail
    t=conn.execute('SELECT * FROM twins').fetchone()
    assert t['state']=='exiting' and t['exit_reason']=='trail' and t['tokens_atomic']=='1000'
    A.twin_tick(conn,'/none',T0+24+L.EXIT_DELAY_S)
    t=conn.execute('SELECT * FROM twins').fetchone()
    assert t['state']=='closed' and t['exit_fill_kind']=='strict' and t['gross_pct']==pytest.approx(10)
    assert conn.execute('SELECT state FROM triggers').fetchone()[0]=='open'


def test_twin_books_a_labelled_estimate_at_landing_then_censors(world,monkeypatch):
    monkeypatch.setattr(A,'VARIANTS',dict(rails=A.RAILS))
    conn=open_twin(world,monkeypatch)
    monkeypatch.setattr(L,'_ps_sell',value(-70));A.twin_tick(conn,'/none',T0+20)
    assert conn.execute('SELECT exit_reason FROM twins').fetchone()[0]=='catastrophe'
    monkeypatch.setattr(L,'_ps_sell',lambda *a,**k:(None,'pool_tip_unproven'))
    cuts=[]
    def est(d,m,q,cut,observed_at=None):
        cuts.append(cut);return (dict(pool='P',lamports=str(A.CLIP_LAMPORTS//2)),None) if len(cuts)>1 else (None,'x')
    monkeypatch.setattr(L,'_ps_estimate',est)
    A.twin_tick(conn,'/none',T0+31);assert conn.execute('SELECT state FROM twins').fetchone()[0]=='exiting'
    A.twin_tick(conn,'/none',T0+32)
    t=conn.execute('SELECT * FROM twins').fetchone()
    assert t['exit_fill_kind']=='estimated_at_landing' and cuts[-1]==T0+23


def test_twin_max_hold_fires_with_no_mark_and_unknown_is_not_a_price(world,monkeypatch):
    conn=open_twin(world,monkeypatch)
    monkeypatch.setattr(L,'_ps_sell',lambda *a,**k:(None,'x'));monkeypatch.setattr(L,'_ps_estimate',lambda *a,**k:(None,'x'))
    A.twin_tick(conn,'/none',T0+20)
    t=conn.execute('SELECT * FROM twins').fetchone();assert t['state']=='open' and t['marks_null']==1
    A.twin_tick(conn,'/none',T0+10+3600)
    assert conn.execute('SELECT exit_reason FROM twins').fetchone()[0]=='max_hold'
    A.twin_tick(conn,'/none',T0+10+3600+121+L.EXIT_DELAY_S)
    assert conn.execute('SELECT state,reason FROM twins').fetchone()[:]==('censored','exit:x')


def test_each_variant_exits_by_its_own_config_on_the_same_entry(world,monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setitem(A.VARIANTS,'tp_a',SimpleNamespace(sl_pct=30.,tp_pct=40.,trail_pct=0.,catastrophe_pct=60.,max_hold_s=3600.,lag_s=10.))
    conn=open_twin(world,monkeypatch)
    monkeypatch.setattr(L,'_ps_sell',value(50));A.twin_tick(conn,'/none',T0+20)
    rows={r['variant']:r for r in conn.execute('SELECT * FROM twins')}
    assert rows['tp_a']['state']=='exiting' and rows['tp_a']['exit_reason']=='tp' and rows['rails']['state']=='open'
    assert rows['tp_a']['tokens_atomic']==rows['rails']['tokens_atomic']


def test_amendment_2_arms_book_at_10_s_stress_at_30_and_time_stop_300(world,monkeypatch):
    conn=open_twin(world,monkeypatch)
    q={'tp40':None}
    monkeypatch.setattr(L,'_ps_sell',value(45));A.twin_tick(conn,'/none',T0+20)
    rows={r['variant']:r for r in conn.execute('SELECT * FROM twins')}
    assert rows['tp40']['exit_reason']=='tp' and rows['tp40_sl30']['exit_reason']=='tp' and rows['tp40']['exit_due']==T0+30
    monkeypatch.setattr(L,'_ps_sell',value(20));A.twin_tick(conn,'/none',T0+31)
    assert conn.execute("SELECT state,gross_pct FROM twins WHERE variant='tp40'").fetchone()[:]==('closed',pytest.approx(20))
    monkeypatch.setattr(L,'_ps_sell',value(5));A.twin_tick(conn,'/none',T0+51)
    assert conn.execute("SELECT stress_gross_pct FROM twins WHERE variant='tp40'").fetchone()[0]==pytest.approx(5)


def test_stop_only_on_the_sl_arm_and_the_time_stop_fires_on_both(world,monkeypatch):
    conn=open_twin(world,monkeypatch)
    monkeypatch.setattr(L,'_ps_sell',value(-35));A.twin_tick(conn,'/none',T0+20)
    rows={r['variant']:r for r in conn.execute('SELECT * FROM twins')}
    assert rows['tp40_sl30']['exit_reason']=='stop' and rows['tp40']['state']=='open'
    A.twin_tick(conn,'/none',T0+311)
    assert conn.execute("SELECT exit_reason,exit_due FROM twins WHERE variant='tp40'").fetchone()[:]==('max_hold',T0+321)


def test_lag_exit_with_no_quote_censors_after_60_s(world,monkeypatch):
    conn=open_twin(world,monkeypatch)
    monkeypatch.setattr(L,'_ps_sell',value(45));A.twin_tick(conn,'/none',T0+20)
    monkeypatch.setattr(L,'_ps_sell',lambda *a,**k:(None,'capture_gap'))
    A.twin_tick(conn,'/none',T0+89);assert conn.execute("SELECT state FROM twins WHERE variant='tp40'").fetchone()[0]=='exiting'
    A.twin_tick(conn,'/none',T0+91);assert conn.execute("SELECT state,reason FROM twins WHERE variant='tp40'").fetchone()[:]==('censored','exit:capture_gap')


def test_a_valid_quote_after_the_deadline_censors_and_elapsed_is_stored(world,monkeypatch):
    conn,tape=world
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(T0-1,))
    swap(tape,1,'w',1,25,T0);swap(tape,2,'x',1,25,T0);A.ingest(conn,tape,T0+1)
    monkeypatch.setattr(L,'_ps_buy',lambda *a,**k:(dict(pool='P',tokens_atomic='1000'),None))
    conn.execute("UPDATE triggers SET entry_due=? WHERE wallet='x'",(T0-40,))
    A.grade(conn,'/none',T0+11)
    assert conn.execute("SELECT state,reason FROM triggers WHERE wallet='x'").fetchone()[:]==('censored','entry:deadline_passed')
    assert conn.execute("SELECT entry_elapsed_s FROM triggers WHERE wallet='w'").fetchone()[0]==pytest.approx(1)
    monkeypatch.setattr(L,'_ps_sell',value(9))
    A.grade(conn,'/none',T0+341)
    assert conn.execute("SELECT state,reason FROM triggers WHERE wallet='w'").fetchone()[:]==('censored','exit:deadline_passed')


def test_a_sell_a_month_ago_still_blocks_the_trigger(world):
    conn,tape=world
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(T0-1,))
    swap(tape,1,'w',0,1,T0);A.ingest(conn,tape,T0+1)
    swap(tape,2,'w',1,30,T0+30*86400)
    assert A.ingest(conn,tape,T0+30*86400+1)['fired']==0


def test_the_buyers_first_sell_after_the_trigger_is_stored_once(world):
    conn,tape=world
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(T0-1,))
    swap(tape,1,'w',1,25,T0);swap(tape,2,'w',0,1,T0+5);swap(tape,3,'w',0,1,T0+9)
    A.ingest(conn,tape,T0+10)
    assert conn.execute('SELECT buyer_first_sell_at FROM triggers').fetchone()[0]==T0+5


def test_crowd_count_uses_only_other_wallets_in_the_prior_60_s(world):
    conn,tape=world
    conn.execute("INSERT INTO meta VALUES('cursor',?)",(A.START_AT-100,))
    swap(tape,1,'early',1,25,A.START_AT-5)       # before the first test day still counts as known
    swap(tape,2,'a',1,25,T0-60+1-101+5)
    swap(tape,3,'old',1,25,T0-70);swap(tape,4,'b',1,25,T0-30);swap(tape,5,'w',1,25,T0)
    swap(tape,6,'z',1,25,T0+10,pool='Q')
    A.ingest(conn,tape,T0+11)
    got={r['wallet']:r['others_60s'] for r in conn.execute('SELECT wallet,others_60s FROM triggers')}
    assert got['w']==1 and got['z']==0 and got['b']==1      # old at -70 s is out of w's window, in b's

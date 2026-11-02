"""Cooperative yielding commits complete rows; hard expiry still rolls back a call."""
from contextlib import closing
import sqlite3
import pytest
from sniper.office import large_buyer_arm as A

AT=A.START_AT+100
EVENTS=[
    ('bundle',0,10,1,'A',1,15),('bundle',1,10,1,'A',1,10),
    ('beta',0,11,1,'B',1,25),('alpha-sell',0,12,1,'A',0,1),
    ('b-more',0,12,2,'B',1,5),('b-more',1,12,2,'B',0,1),
    ('gamma',0,13,1,'C',1,25),('gamma',1,13,1,'C',1,1),
]


@pytest.fixture
def world(tmp_path,monkeypatch):
    monkeypatch.setattr(A,'BATCH',3)
    path=tmp_path/'arm.db'
    with closing(A.open_db(path)) as c:
        c.execute("INSERT INTO meta VALUES('cursor',?)",(AT-120,));c.commit()
    tape=sqlite3.connect(':memory:')
    tape.execute('CREATE TABLE swaps_ps(signature,ordinal,pool,mint,actor,is_buy,quote_atomic,quote_mint,received_at,slot,tx_index,pre_quote_reserve_atomic,post_quote_reserve_atomic,post_base_reserve_atomic)')
    for event in reversed(EVENTS):insert(tape,event)
    tape.commit()
    yield path,tape
    tape.close()


def insert(tape,event):
    sig,ordinal,slot,index,wallet,buy,sol=event
    tape.execute('INSERT INTO swaps_ps VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (sig,ordinal,'P','M',wallet,buy,str(sol*10**9),A.WSOL,AT,slot,index,
                  str(40*10**9),str(65*10**9),str(10**12)))


def seen(c):return c.execute('SELECT count(*) FROM seen').fetchone()[0]


def checkpoint(c):
    tables=['seen','wallets','reaches','triggers','meta']
    if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_progress'").fetchone():tables.append('ingest_progress')
    return {table:sorted(map(tuple,c.execute('SELECT * FROM '+table)),key=repr) for table in tables}


def step(path,tape):
    # A fresh connection each time forbids relying on an in-process resume cursor.
    with closing(A.open_db(path)) as c:
        start=seen(c)
        A.ingest(c,tape,AT+1,detected_now=lambda:AT+1,
                 should_yield=lambda:seen(c)>=start+2,should_stop=lambda:False)
    with closing(A.open_db(path)) as persisted:return seen(persisted)


def test_cooperative_prefix_is_durable_and_resumes_all_cursor_ties(world):
    path,tape=world
    assert step(path,tape)==2  # two complete events, rather than the whole batch or a rollback
    for _ in range(12):
        if step(path,tape)==len(EVENTS):break
    with closing(A.open_db(path)) as c:
        assert seen(c)==len(EVENTS)
        assert {tuple(r) for r in c.execute('SELECT signature,ordinal FROM seen')}=={e[:2] for e in EVENTS}
        wallets={r['wallet']:(r['bought'],r['sold'],r['fired']) for r in c.execute('SELECT * FROM wallets')}
        assert wallets=={'A':(25*10**9,1,1),'B':(30*10**9,1,1),'C':(26*10**9,0,1)}
        assert {r['wallet']:r['crowd_others_60s'] for r in c.execute('SELECT * FROM triggers')}=={'A':0,'B':1,'C':2}
        assert c.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()[0]==AT
        before={k:v for k,v in checkpoint(c).items() if k in ('seen','wallets','reaches','triggers')}
        A.ingest(c,tape,AT+1,detected_now=lambda:AT+1,should_yield=lambda:False,should_stop=lambda:False)
        assert {k:v for k,v in checkpoint(c).items() if k in before}==before  # overlap replay never doubles a wallet or trigger


def test_hard_expiry_overrides_yield_and_rolls_back_only_current_call(world):
    path,tape=world
    assert step(path,tape)==2
    with closing(A.open_db(path)) as c:
        before=checkpoint(c);start=seen(c)
        expired=lambda:seen(c)>start
        with pytest.raises(ValueError,match='ingest_budget_exhausted'):
            A.ingest(c,tape,AT+1,detected_now=lambda:AT+1,
                     should_yield=expired,should_stop=expired)
    with closing(A.open_db(path)) as c:
        assert checkpoint(c)==before  # includes persisted resume metadata, not just row counts
    assert step(path,tape)>2  # retry the rolled-back rows without losing the durable prefix


def test_overlap_finds_late_commit_behind_a_completed_tied_cursor(world):
    path,tape=world
    for _ in range(12):
        if step(path,tape)==len(EVENTS):break
    with closing(A.open_db(path)) as c:assert seen(c)==len(EVENTS)
    # Same receipt timestamp, lower source tuple: it committed after our previous tape read.
    insert(tape,('late-commit',0,9,1,'D',1,25));tape.commit()
    for _ in range(12):
        if step(path,tape)==len(EVENTS)+1:break
    with closing(A.open_db(path)) as c:
        assert seen(c)==len(EVENTS)+1
        assert c.execute("SELECT bought FROM wallets WHERE wallet='D'").fetchone()[0]==25*10**9
        assert c.execute('SELECT count(*) FROM triggers').fetchone()[0]==4


def test_null_transaction_index_ties_resume_without_fabricating_causal_proof(world):
    path,tape=world
    tape.execute('DELETE FROM swaps_ps')
    for i in reversed(range(7)):insert(tape,('null-'+str(i),0,20,None,'U',1,1))
    tape.commit()
    for _ in range(12):
        if step(path,tape)==7:break
    with closing(A.open_db(path)) as c:
        assert seen(c)==7
        assert {tuple(r) for r in c.execute('SELECT signature,ordinal FROM seen')}=={('null-'+str(i),0) for i in range(7)}
        row=c.execute("SELECT bought,crowd_bought FROM wallets WHERE wallet='U'").fetchone()
        assert row['bought']==7*10**9 and row['crowd_bought'] is None
        assert c.execute('SELECT count(*) FROM triggers').fetchone()[0]==0

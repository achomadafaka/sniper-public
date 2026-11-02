import sqlite3
import pytest
from sniper.office import wallet_activity as W


@pytest.fixture
def db(tmp_path):
    p=tmp_path/'f.db';c=sqlite3.connect(p)
    c.execute('CREATE TABLE memecoin_wallets(source_user_id,handle,label,paused,excluded,real_approved,real_chains)')
    c.execute('CREATE TABLE memecoin_events(id INTEGER PRIMARY KEY,source_user_id,source_handle,mint,ticker,side,their_size_usd,their_market_cap,network_id,signal_source,delivery_lane,created_at,received_at,real_decision)')
    c.execute('CREATE INDEX idx_mc_events_mint ON memecoin_events(mint)');c.execute('CREATE INDEX idx_mc_events_user_time ON memecoin_events(source_user_id,created_at)')
    c.executemany('INSERT INTO memecoin_wallets VALUES(?,?,?,0,0,?,?)',[('u1','big','KOL',1,'solana'),('u2','small',None,0,None)])
    c.executemany('INSERT INTO memecoin_events(source_user_id,source_handle,mint,side,their_size_usd,network_id,created_at,real_decision) VALUES(?,?,?,?,?,?,?,?)',
                  [('u1','big','M','buy',500.,'solana','2026-10-31T10:00:00.000Z','entered'),('u2','small','M','sell',5.,'solana','2026-10-31T11:00:00.000Z',None),('u2','small','N','buy',50.,'56','2026-10-31T12:00:00.000Z',None)])
    c.commit();c.close();return str(p)


def test_selectors_and_no_decisions_leak(db):
    got=W.read(db,mint='M')
    assert [r['source_handle'] for r in got['items']]==['small','big'] and 'real_decision' not in got['items'][0]
    assert got['items'][1]['wallet']['armed'] is True
    assert [r['mint'] for r in W.read(db,wallet='BIG')['items']]==['M'] and W.read(db,label='kol')['items'][0]['source_handle']=='big'
    assert W.read(db,wallet='nobody')['reason']=='wallet_not_followed'
    assert W.read(db,wallet='u1',mint='M')['state']=='refused' and W.read(db,limit=0)['state']=='refused' and W.read(db,since=float('nan'))['state']=='refused'


def test_biggest_since_and_truncation(db):
    got=W.read(db,limit=1)
    assert got['truncated'] and [b['handle'] for b in got['biggest_by_buy_usd']]==['big','small']
    assert W.read(db,mint='M',since=1893456000)['items']==[]


def test_read_only_and_failure_is_unknown(db,tmp_path):
    assert W.read(str(tmp_path/'none.db'))['state']=='unknown'
    conn=W._open(db)
    with pytest.raises(sqlite3.OperationalError):conn.execute("DELETE FROM memecoin_events")
    conn.close()


def test_touched_names_our_wallets_and_empty_is_known_zero(db):
    got=W.touched(db,'M')
    assert got['count']==2 and {w['handle'] for w in got['wallets']}=={'big','small'} and W.touched(db,'X')==dict(state='known',count=0,truncated=False,wallets=[])


def test_handle_cannot_wait_past_its_budget(db):
    conn=W._open(db)
    assert conn.execute('PRAGMA busy_timeout').fetchone()[0]==100
    conn.close()

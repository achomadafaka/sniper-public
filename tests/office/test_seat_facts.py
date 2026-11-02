import json,sqlite3
import pytest
from sniper.office import seat_facts as F
from sniper.services import memecoin_desk_ps_tape as T


def tape(tmp_path,rows):
    (tmp_path/'pumpswap').mkdir();c=sqlite3.connect(tmp_path/'pumpswap/tape.db')
    c.execute('CREATE TABLE swaps_ps(pool,received_at,actor,is_buy,quote_atomic,post_base_reserve_atomic,post_quote_reserve_atomic,payload_json)');c.execute('CREATE INDEX swaps_ps_pool_received ON swaps_ps(pool,received_at)')
    c.executemany("INSERT INTO swaps_ps VALUES('P',?,?,?,?,?,?,?)",rows);c.commit();c.close()


@pytest.fixture(autouse=True)
def proof(monkeypatch):
    monkeypatch.setattr(F,'_proof',lambda p:1000 if p else None)


def test_run_up_is_against_retained_prints_and_stops_at_the_read(tmp_path):
    tape(tmp_path,[(10,'a',1,'1','1000','1000','p'),(20,'a',1,'1','1000','3000',None),(30,'b',0,'1','1000','1000',None),(99,'z',1,'1','1000','99000',None)])
    got=F.run_up(tmp_path,'P',40.)
    assert got['vs_first_retained_print_pct']==0. and got['vs_high_pct']==-50. and got['s_since_high']==20 and got['prints']==3
    assert F.run_up(tmp_path,'other',40.)['state']=='unknown' and F.run_up(tmp_path/'x','P',40.)['reason']=='tape_unreadable'


def test_triggers_fire_on_fall_flip_and_large_buyer_and_missing_fires_nothing(tmp_path):
    tape(tmp_path,[(50,'big',1,str(10**10),'1','1',None),(120,'x',1,str(10**9),'1','1',None),(160,'big',0,str(6*10**9),'1','1',None)])
    order=dict(spend_lamports='100',peak_lamports='200',pool='P',filled_at=100.)
    got=F.triggers(order,[dict(lamports='150',binding_kind='carried')],tmp_path,170.)
    assert got['mark_fall']['fall_from_peak_pct']==25. and got['flow_flip']['last_30s_net_sol']<0 and got['large_buyer_selling']['actor']=='big'
    assert F.triggers(dict(order,spend_lamports=None),[],tmp_path,170.)=={}
    assert F.triggers(dict(order,peak_lamports='100'),[],tmp_path/'none',170.)=={}


def test_closes_on_coin_names_an_unread_book(tmp_path):
    c=sqlite3.connect(tmp_path/'a.db');c.execute('CREATE TABLE orders(epoch_id,exit_reason,closed_at,spend_usd,exit_usd,costs_usd,mint,state)');c.execute("INSERT INTO orders VALUES('E','tp',90.,25.,39.,1.,'M','closed')");c.commit();c.close()
    got=F.closes_on_coin([tmp_path/'a.db',tmp_path/'none/b.db'],'M',100.)
    assert got['items']==[dict(book='E',exit_reason='tp',s_ago=10,net_pct=52.)] and got['state']=='partial' and got['unread']==['none']

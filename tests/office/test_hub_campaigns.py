"""High degree is evidence to preserve, never a common-controller assertion."""
import concurrent.futures
import json
import sqlite3
import threading

import pytest
from solders.pubkey import Pubkey
from sniper.office import wallet_watch as W, wallet_family as F
from sniper.office.wallet_network import WalletNetwork


def address(n):return str(Pubkey.from_bytes(n.to_bytes(32,'big')))
A,H,B,C=(address(n) for n in (1,2,3,4))


@pytest.fixture
def hub(tmp_path,monkeypatch):
    monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset())
    net=WalletNetwork(tmp_path/'network.db',clock=lambda:1000.)
    c=net._conn()
    for n in range(100,200):
        edge(net,c,address(n),H,100.,200.)
    edge(net,c,A,H,985.,990.)
    c.commit();c.close()
    return net


def edge(net,c,src,dst,observed,known,**kw):
    defaults=dict(source='host',proof_state='verified',proof_kind='research_stream',method='wallet-watch-stream-v1')
    defaults.update(kw)
    net._insert(c,src=src,dst=dst,edge_type='funded_by',actor='fixture',observed_at=observed,known_at=known,proof_ref=src+str(observed),**defaults)


def test_hub_evidence_is_additive_direct_strict_causal_and_bounded(hub):
    c=hub._conn()
    edge(hub,c,B,H,986.,991.)
    edge(hub,c,C,H,960.,992.)
    edge(hub,c,address(5),H,999.,1001.)  # learned later
    edge(hub,c,address(6),H,1001.,999.)  # future event
    edge(hub,c,address(7),H,980.,990.,source='seat')
    edge(hub,c,address(8),H,980.,990.,method='legacy')
    edge(hub,c,address(9),H,980.,990.,proof_state='claimed')
    edge(hub,c,H,address(10),975.,980.)  # no upstream campaign walk
    c.commit();c.close()
    out=W.funding_links(hub,address=A,as_of=1000.,verified_only=True,limit=2)
    assert out['funding_chain']==[] and out['siblings']==[]
    assert out['excluded_funders'][0]['excluded']=='high_fanout_funder'
    direct,=out['hub_funding']
    assert direct['src']==A and direct['dst']==H and direct['observed_state']=='known'
    campaign,=out['hub_campaigns']
    assert (campaign['funder'],campaign['bucket_start'],campaign['bucket_end'])==(H,960,1020)
    assert campaign['ownership_proven'] is False and campaign['relation']=='temporal_coincidence'
    assert campaign['members_truncated'] and len(campaign['members'])==2
    assert {m['src'] for m in campaign['members']} <= {A,B,C}
    assert all(m['known_at']<=1000 and 960<=m['observed_at']<=1000 for m in campaign['members'])
    assert W.funding_links(hub,address=A,as_of=1000.,verified_only=True,limit=2)['hub_campaigns']==out['hub_campaigns']


def test_missing_event_clock_is_a_direct_fact_without_a_campaign(hub):
    c=hub._conn();c.execute('UPDATE edges SET observed_at=NULL WHERE src=?',(A,));c.commit();c.close()
    out=W.funding_links(hub,address=A,as_of=1000.,verified_only=True)
    direct,=out['hub_funding']
    assert direct['observed_state']=='unknown' and direct['reason']=='observed_time_unknown'
    assert out['hub_campaigns']==[]


def test_infrastructure_and_unknown_catalogue_never_supply_campaigns(hub,monkeypatch):
    monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset({H}))
    out=W.funding_links(hub,address=A)
    assert out['hub_funding']==[] and out['hub_campaigns']==[]
    assert out['excluded_funders'][0]['excluded']=='known_infrastructure'
    monkeypatch.setattr(W,'_known_infrastructure',lambda:None)
    out=W.funding_links(hub,address=A)
    assert out['state']=='partial' and out['hub_campaigns']==[]
    assert out['hub_funding'][0]['infrastructure_state']=='unknown'


@pytest.mark.parametrize('kwargs',[dict(source='seat'),dict(proof_state='claimed'),dict(method='legacy'),dict(proof_kind='receipt')])
def test_direct_hub_relationship_requires_strict_host_method(hub,kwargs):
    c=hub._conn();c.execute('DELETE FROM edges WHERE src=?',(A,));edge(hub,c,A,H,985.,990.,**kwargs);c.commit();c.close()
    out=W.funding_links(hub,address=A)
    assert out['hub_funding']==[] and out['hub_campaigns']==[]


def test_family_keeps_hub_fact_but_never_unions_campaign_members(hub,tmp_path):
    c=hub._conn();edge(hub,c,B,H,986.,991.);c.commit();c.close()
    desk=tmp_path/'desk';(desk/'pumpswap').mkdir(parents=True)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:
        c.executescript('CREATE TABLE ps_watch_events(signature,ordinal,pool,mint,actor,slot,tx_index,is_buy,received_at,quote_atomic,pre_quote_reserve_atomic); CREATE INDEX watch_at ON ps_watch_events(received_at); CREATE TABLE wallet_setup_events(signature,outer_ix,program,payer,wallet,slot,tx_index,block_time,received_at,processed_at);')
        c.executemany('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?,?)', [('a'*64,0,address(20),address(21),A,10,1,1,995.,'1','2'),('b'*64,0,address(20),address(21),B,11,1,0,996.,'1','2')])
    row,=F.materialize_candidates(hub,desk,1000.)['items']
    assert row['coverage']['strict_funding']=='known'
    assert row['evidence']['funding'][0]['src']==A
    assert row['evidence']['hub_campaigns'][0]['ownership_proven'] is False
    assert row['evidence']['sells']==[]  # sibling in same funding minute is not automatically family
    assert row['ownership_proven'] is False
    c=hub._conn();edge(hub,c,C,H,987.,993.);c.commit();c.close()
    assert F.materialize_candidates(hub,desk,1000.)['items']==[row]


@pytest.fixture
def watch_store(tmp_path):
    net=WalletNetwork(tmp_path/'watch.db',clock=lambda:1000.)
    desk=tmp_path/'desk';(desk/'pumpswap').mkdir(parents=True)
    c=net._conn();c.executescript(W.SCHEMA);c.close()  # Race admission, not first-ever WAL initialization.
    return net,desk


def add(store,who,**kwargs):
    net,desk=store
    return W.add(net,desk,address=who,operator_id=kwargs.pop('operator_id','hub60:test:960'),evidence='fixture',source='host',actor='test',now=1000.,**kwargs)


def test_campaign_quota_includes_removed_and_never_reassigns_existing(watch_store):
    assert add(watch_store,A,campaign_limit=1)['state']=='added'
    net,desk=watch_store
    c=net._conn();c.execute('UPDATE watch_list SET removed_at=1001 WHERE address=?',(A,));c.commit();c.close()
    assert add(watch_store,B,campaign_limit=1)['reason']=='campaign_watch_full'
    assert add(watch_store,A,campaign_limit=1)['reason']=='campaign_watch_existing'
    assert add(watch_store,A,campaign_limit=1,operator_id='other')['reason']=='campaign_watch_existing'
    c=net._conn();assert c.execute('SELECT removed_at,operator_id FROM watch_list WHERE address=?',(A,)).fetchone()[:]==(1001.,'hub60:test:960');c.close()
    assert add(watch_store,A)['state']=='added'  # explicit legacy path still reinstates


@pytest.mark.parametrize('limit,operator',[(0,'x'),(-2,'x'),(True,'x'),(1,None),(1,'')])
def test_bad_campaign_quota_refused(watch_store,limit,operator):
    assert add(watch_store,A,campaign_limit=limit,operator_id=operator)['reason']=='bad_arguments'


@pytest.mark.parametrize('limit',[1,20])
def test_campaign_final_slot_is_atomic(watch_store,limit):
    for n in range(30,30+limit-1):assert add(watch_store,address(n),campaign_limit=limit)['state']=='added'
    gate=threading.Barrier(2)
    def attempt(who):gate.wait(timeout=2);return add(watch_store,who,campaign_limit=limit)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        out=list(pool.map(attempt,[A,B]))
    assert sorted(r['state'] for r in out)==['added','refused']
    assert next(r for r in out if r['state']=='refused')['reason']=='campaign_watch_full'


def test_campaign_members_are_unique_and_time_query_uses_partial_index(hub):
    c=hub._conn();edge(hub,c,B,H,986.,991.);edge(hub,c,B,H,987.,992.);c.commit()
    plan=c.execute('EXPLAIN QUERY PLAN '+W.HUB_CAMPAIGN_QUERY,(H,960.,1020.,1000.,1000.,201)).fetchall();c.close()
    assert any('SEARCH edges USING INDEX edges_hub_campaign' in r[3] for r in plan)
    campaign,=W.funding_links(hub,address=A,as_of=1000.,limit=200)['hub_campaigns']
    assert [m['src'] for m in campaign['members']]==sorted({A,B})
    assert next(m for m in campaign['members'] if m['src']==B)['observed_at']==986.
    assert not campaign['members_truncated']


@pytest.mark.parametrize('observed,known',[(985.,0.),(0.,990.),(-1.,990.),(float('inf'),990.),(985.,float('inf'))])
def test_invalid_or_future_clocks_never_supply_hub_evidence(hub,observed,known):
    c=hub._conn();c.execute('DELETE FROM edges WHERE src=?',(A,));edge(hub,c,A,H,observed,known);c.commit();c.close()
    out=W.funding_links(hub,address=A,as_of=1000.)
    assert out['hub_funding']==[] and out['hub_campaigns']==[]


def test_campaign_quota_retains_global_watch_cap(watch_store,monkeypatch):
    monkeypatch.setattr(W,'MAX_LISTED',1)
    assert add(watch_store,A)['state']=='added'
    assert add(watch_store,B,campaign_limit=20)['reason']=='watch_list_full'


@pytest.mark.parametrize('removed',[False,True])
def test_legacy_relabel_never_releases_a_historical_campaign_slot(watch_store,removed):
    assert add(watch_store,A,campaign_limit=1)['state']=='added'
    if removed:
        c=watch_store[0]._conn();c.execute('UPDATE watch_list SET removed_at=1001 WHERE address=?',(A,));c.commit();c.close()
    assert add(watch_store,A,operator_id=None)['state']=='added'
    assert add(watch_store,B,campaign_limit=1)['reason']=='campaign_watch_full'


@pytest.mark.parametrize('independent',['sponsor_only','actor','creator'])
def test_hub_payment_does_not_make_hubs_own_sales_family_sales(hub,tmp_path,independent):
    desk=tmp_path/'desk';(desk/'pumpswap').mkdir(parents=True)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:
        c.executescript('CREATE TABLE ps_watch_events(signature,ordinal,pool,mint,actor,slot,tx_index,is_buy,received_at,quote_atomic,pre_quote_reserve_atomic); CREATE INDEX watch_at ON ps_watch_events(received_at); CREATE TABLE wallet_setup_events(signature,outer_ix,program,payer,wallet,slot,tx_index,block_time,received_at,processed_at);')
        c.executemany('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?,?)', [('a'*64,0,address(20),address(21),A,10,1,1,995.,'1','2'),('b'*64,0,address(20),address(21),H,11,1,0,996.,'1','2')])
        c.execute('INSERT INTO wallet_setup_events VALUES(?,?,?,?,?,?,?,?,?,?)',('c'*64,0,next(iter(F.PROGRAMS)),H,A,9,1,None,990.,991.))
        if independent=='actor':c.execute('INSERT INTO ps_watch_events VALUES(?,?,?,?,?,?,?,?,?,?,?)',('a'*64,1,address(20),address(21),H,10,1,1,995.,'1','2'))
    if independent=='creator':
        c=hub._conn();hub._insert(c,src=H,dst=address(21),edge_type='created_mint',source='host',actor='fixture',observed_at=980.,known_at=990.,proof_kind='research_stream',proof_ref='launch',proof_state='verified',method='wallet-watch-launch-v1');c.commit();c.close()
    row,=F.materialize_candidates(hub,desk,1000.)['items']
    assert row['evidence']['funding'][0]['hub_relation']=='direct_payment_only'
    assert len(row['evidence']['sells'])==(0 if independent=='sponsor_only' else 1)


def test_campaign_watch_and_claim_reserve_two_daily_rows(watch_store,monkeypatch):
    from sniper.office import wallet_network as N
    monkeypatch.setattr(N,'MAX_ROWS_PER_DAY',1)
    assert add(watch_store,A,campaign_limit=1)['reason']=='daily_row_cap'
    monkeypatch.setattr(N,'MAX_ROWS_PER_DAY',2)
    assert add(watch_store,A,campaign_limit=1)['state']=='added'
    c=watch_store[0]._conn()
    assert c.execute('SELECT sum(rows_added) FROM daily').fetchone()[0]==2
    assert c.execute('SELECT campaign,address FROM watch_campaign_members').fetchall()[0][:]==('hub60:test:960',A)
    c.close()


def test_failed_campaign_claim_rolls_back_watch_and_budget(watch_store):
    c=watch_store[0]._conn();c.executescript(W.SCHEMA)
    c.execute("CREATE TRIGGER refuse_claim BEFORE INSERT ON watch_campaign_members BEGIN SELECT RAISE(ABORT,'claim failure'); END");c.commit();c.close()
    with pytest.raises(sqlite3.IntegrityError,match='claim failure'):
        add(watch_store,A,campaign_limit=1)
    c=watch_store[0]._conn()
    assert c.execute('SELECT count(*) FROM watch_list').fetchone()[0]==0
    assert c.execute('SELECT count(*) FROM watch_campaign_members').fetchone()[0]==0
    assert c.execute('SELECT coalesce(sum(rows_added),0) FROM daily').fetchone()[0]==0
    c.close()

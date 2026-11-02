"""A proven payment to shared submission infrastructure is not an operator-family edge."""
import json
import sqlite3

import pytest

from sniper.office import wallet_watch as W, wallet_link_backfill as L
from sniper.office import wallet_campaign as P, wallet_campaign_history as H, wallet_group_leads as G
from sniper.services.memecoin_cluster_rpc import known_funders
from .test_wallet_campaign import source as campaign_fixture, funding, setup
from .test_wallet_group_leads import source as group_fixture, add_edge
from .test_wallet_link_backfill import A, B, SIG

TIP='4vieeGHPYPG2MmyPRcYjdiDmmhN3ww7hsFNap8pVN3Ey'


def link(net,recipient,funder,ref='proof'):
    fact=dict(state='known',kind='verified_funding',verified=True,funder=funder,recipient=recipient,funding_signature=SIG,funded_at=50.,amount_atomic='1000000000',method='confirmed_system_transfer',network_id='solana',provenance='research_rpc',tool='watch_verified_funding',asof=dict(kind='finalized_transaction',block_time_s=50.))
    with sqlite3.connect(net.chain_db) as c:c.execute('INSERT INTO fetches(fetch_id,state,result,shown_at) VALUES(?,?,?,?)',(ref,'known',json.dumps(fact),60.))
    c=net._conn();net._insert(c,src=recipient,dst=funder,edge_type='funded_by',source='host',actor='test',observed_at=50.,known_at=60.,proof_kind='chain_fetch',proof_ref=ref,proof_state='verified',method='wallet-watch-receipt-v1');c.commit();c.close()


def test_submission_tip_is_research_infrastructure_without_changing_frozen_catalogue():
    original=known_funders()
    assert TIP not in original['addresses']
    assert TIP in W._known_infrastructure()
    assert known_funders()==original


@pytest.mark.parametrize('service_anchor',[False,True])
def test_backfill_cannot_expand_to_or_through_tip_receiver(tmp_path,service_anchor):
    net,desk,_=campaign_fixture.__wrapped__(tmp_path)
    anchor,target=(TIP,A) if service_anchor else (A,TIP)
    W.add(net,desk,address=anchor,operator_id=None,evidence='fixture',source='host',actor='test',now=900.)
    link(net,target,anchor)
    result=L.expand_existing_links(net,desk,1000.)
    assert result['added']==0 and result['blocked']==1
    with sqlite3.connect(net.path) as c:
        assert c.execute('SELECT count(*) FROM edges').fetchone()[0]==1
        assert c.execute('SELECT address FROM watch_list').fetchall()==[(anchor,)]


def test_starting_at_tip_receiver_preserves_payment_but_never_merges_payers(tmp_path):
    net,_,_=campaign_fixture.__wrapped__(tmp_path)
    link(net,TIP,A);link(net,B,A,ref='other')
    result=W.funding_links(net,address=TIP,as_of=1000.,verified_only=True)
    assert result['funding_chain']==[] and result['siblings']==[]
    assert result['excluded_funders'][0]['excluded']=='known_infrastructure'
    assert result['excluded_funders'][0]['service_boundary']['role']=='known_submission_tip_receiver'
    with sqlite3.connect(net.path) as c:assert c.execute('SELECT count(*) FROM edges').fetchone()[0]==2


@pytest.mark.parametrize('kind',['funding','setup'])
@pytest.mark.parametrize('service_seed',[False,True])
def test_new_preparation_does_not_link_service_seed_or_member(tmp_path,service_seed,kind):
    fixture=campaign_fixture.__wrapped__(tmp_path)
    seed,member=(TIP,B) if service_seed else (A,TIP)
    if kind=='funding':funding(fixture,seed=seed,who=member)
    else:setup(fixture,payer=seed,wallet=member)
    net,desk,_=fixture;P.materialize_campaigns(net,desk,1000.)
    assert P.read_campaigns(net)['items']==[]
    with sqlite3.connect(net.path) as c:assert c.execute('SELECT count(*) FROM edges').fetchone()[0]==(1 if kind=='funding' else 0)


def test_nonservice_payment_still_expands_and_prepares(tmp_path):
    fixture=campaign_fixture.__wrapped__(tmp_path);net,desk,_=fixture
    W.add(net,desk,address=A,operator_id=None,evidence='fixture',source='host',actor='test',now=900.)
    link(net,B,A)
    assert L.expand_existing_links(net,desk,1000.)['added']==1
    P.materialize_campaigns(net,desk,1000.)
    assert len(P.read_campaigns(net)['items'])==1
    G.scan_group_leads(net,1000.)
    lead,=G.read_group_leads(net)['items'];fact,=lead['evidence']
    assert fact['quality']['service_boundary']['addresses']==[]
    assert fact['quality']['service_boundary']['control_link_eligible'] is None


def test_legacy_group_service_path_is_labelled_on_copy_without_rewriting_payment(tmp_path,monkeypatch):
    fixture=group_fixture.__wrapped__(tmp_path);net,_=fixture
    add_edge(fixture,src=TIP,dst=A,kind='funded_by',source='host',proof_kind='research_stream',proof_state='verified',method='wallet-watch-stream-v1')
    with monkeypatch.context() as old:
        old.setattr(W,'_known_infrastructure',lambda:frozenset())
        G.scan_group_leads(net,1000.)
    c=sqlite3.connect(net.path)
    before=c.execute('SELECT path_json,quality_json FROM wallet_group_lead_evidence').fetchall();c.close()
    item,=G.read_group_leads(net)['items'];fact,=item['evidence']
    assert fact['path']['recipient']==TIP and fact['quality']['strength']=='strict_funding'
    assert fact['quality']['service_boundary']['addresses']==[TIP]
    assert fact['quality']['service_boundary']['control_link_eligible'] is False
    c=sqlite3.connect(net.path)
    assert c.execute('SELECT path_json,quality_json FROM wallet_group_lead_evidence').fetchall()==before
    c.close()


def test_legacy_service_campaign_is_labelled_and_cannot_be_repeat_operator_history(tmp_path,monkeypatch):
    fixture=campaign_fixture.__wrapped__(tmp_path);net,desk,_=fixture
    funding(fixture,seed=TIP,who=A)
    with monkeypatch.context() as old:
        old.setattr(P,'_known_infrastructure',lambda:frozenset())
        P.materialize_campaigns(net,desk,1000.)
    c=sqlite3.connect(net.path);before=c.execute('SELECT doc_json FROM wallet_campaign_versions').fetchall();c.close()
    item,=P.read_campaigns(net)['items']
    assert item['service_boundary']['addresses']==[TIP]
    assert item['prediction_state']=='blocked_known_infrastructure'
    history,=H.operation_history(net)['items']
    assert history['prediction_state']=='blocked_known_infrastructure' and history['prior_state']=='unknown'
    assert 'prior_launches' not in history
    doc=dict(seed=TIP,launches=[dict(mint=A,coverage=dict(creation_verified=True),launch_id='test',source_signature='sig',source_receipt=980.)],buys=[],roles=[])
    assert H._operations(P._label_service_boundaries(doc,W._known_infrastructure(),W._known_tip_receivers()),1000.)==[]
    c=sqlite3.connect(net.path)
    assert c.execute('SELECT doc_json FROM wallet_campaign_versions').fetchall()==before
    c.close()


def test_missing_frozen_catalogue_is_unknown_and_backfill_fails_closed(tmp_path,monkeypatch):
    import sniper.services.memecoin_cluster_rpc as K
    def unavailable():raise OSError('fixture')
    monkeypatch.setattr(K,'known_funders',unavailable)
    assert W._known_infrastructure() is None
    net,desk,_=campaign_fixture.__wrapped__(tmp_path)
    W.add(net,desk,address=A,operator_id=None,evidence='fixture',source='host',actor='test',now=900.)
    link(net,B,A)
    result=L.expand_existing_links(net,desk,1000.)
    assert result['state']=='unknown' and result['added']==0


def test_mixed_legacy_campaign_labels_only_service_member_on_read_copy(tmp_path,monkeypatch):
    fixture=campaign_fixture.__wrapped__(tmp_path);net,desk,_=fixture
    funding(fixture,seed=A,who=TIP);funding(fixture,seed=A,who=B)
    with monkeypatch.context() as old:
        old.setattr(P,'_known_infrastructure',lambda:frozenset())
        P.materialize_campaigns(net,desk,1000.)
    doc,=P.read_campaigns(net)['items']
    assert doc['service_boundary']['addresses']==[]
    assert doc.get('prediction_state')!='blocked_known_infrastructure'
    service=[r for r in doc['roles'] if r['address']==TIP]
    normal=[r for r in doc['roles'] if r['address']==B]
    assert service and all(r['service_boundary']['control_link_eligible'] is False for r in service)
    assert normal and all(r['service_boundary']['control_link_eligible'] is None for r in normal)


def test_tip_receiver_is_not_a_sibling_of_other_wallets_paid_by_same_root(tmp_path):
    net,_,_=campaign_fixture.__wrapped__(tmp_path)
    link(net,B,A);link(net,TIP,A,ref='tip')
    result=W.funding_links(net,address=B,as_of=1000.,verified_only=True)
    assert len(result['funding_chain'])==1
    assert result['siblings']==[]
    assert any(e['service_boundary']['addresses']==[TIP] for e in result['excluded_funders'])


def test_hub_bucket_never_lists_service_receiver_as_economic_member(tmp_path):
    from solders.pubkey import Pubkey
    net,_,_=campaign_fixture.__wrapped__(tmp_path)
    link(net,B,A);link(net,TIP,A,ref='tip')
    for i in range(100):
        member=str(Pubkey.from_bytes(bytes([255])*31+bytes([i])))
        link(net,member,A,ref='member'+str(i))
    result=W.funding_links(net,address=B,as_of=1000.,verified_only=True)
    campaign,=result['hub_campaigns']
    assert campaign['members'] and all(m['src']!=TIP for m in campaign['members'])
    assert campaign['members_truncated'] is True
    with sqlite3.connect(net.path) as c:assert c.execute('SELECT count(*) FROM edges').fetchone()[0]==102


def test_legacy_tip_only_preparation_does_not_become_idle_operator_evidence(tmp_path,monkeypatch):
    fixture=campaign_fixture.__wrapped__(tmp_path);net,desk,_=fixture
    funding(fixture,seed=A,who=TIP)
    with monkeypatch.context() as old:
        old.setattr(P,'_known_infrastructure',lambda:frozenset())
        P.materialize_campaigns(net,desk,1000.)
    doc,=P.read_campaigns(net)['items']
    assert doc['family_link_eligible'] is False
    assert all(r['service_boundary']['addresses']==[TIP] for r in doc['roles'])
    history,=H.operation_history(net)['items']
    assert history['prior_state']=='unknown' and 'prior_preparations_without_observed_launch' not in history

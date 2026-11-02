"""Stored research paths, not inferred organizations or beneficial control."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest
from solders.pubkey import Pubkey
from sniper.office.wallet_network import WalletNetwork


def addr(n):return str(Pubkey.from_bytes(n.to_bytes(32,'big')))
DEV,MINT,FUNDER,OTHER=(addr(n) for n in range(1,5))


@pytest.fixture
def source(tmp_path):
    clock=[1000.];net=WalletNetwork(tmp_path/'graph.db',clock=lambda:clock[0]);net._conn().close()
    return net,clock


def add_edge(fixture,src=DEV,dst=MINT,kind='created_mint',known=990.,**kwargs):
    net,_=fixture;options=dict(source='import',proof_kind='seed',proof_state='none',method='seed');options.update(kwargs)
    c=net._conn();net._insert(c,src=src,dst=dst,edge_type=kind,actor='fixture',observed_at=980.,known_at=known,proof_ref=src+dst,**options);c.commit();c.close()


def scan(source,**kwargs):
    from sniper.office.wallet_group_leads import scan_group_leads
    return scan_group_leads(source[0],source[1][0],**kwargs)


def read(source,**kwargs):
    from sniper.office.wallet_group_leads import read_group_leads
    return read_group_leads(source[0],**kwargs)


def test_import_paths_are_unverified_leads_and_repeat_mints_keep_one_stable_key(source):
    add_edge(source);scan(source)
    lead,=read(source)['items']
    assert lead['kind']=='creation_key' and lead['anchors']==[DEV]
    assert lead['control_state']=='unproven' and lead['evidence'][0]['quality']['strength']=='import_lead'
    ident=lead['lead_id'];source[1][0]=1001.
    add_edge(source,dst=OTHER,known=1001.);scan(source)
    current,=read(source)['items']
    assert current['lead_id']==ident and current['first_known_at']==lead['first_known_at']
    assert {e['path']['mint'] for e in current['evidence']}=={MINT,OTHER}
    assert read(source,as_of=1000.)['items']==[lead]
    assert scan(source)['inserted']==0


def test_strict_funding_is_a_direct_path_not_a_union_of_service_customers(source,monkeypatch):
    from sniper.office import wallet_watch as W
    monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset({FUNDER}))
    for recipient in (DEV,OTHER):add_edge(source,src=recipient,dst=FUNDER,kind='funded_by',source='host',proof_kind='research_stream',proof_state='verified',method='wallet-watch-stream-v1')
    scan(source)
    lead,=read(source)['items']
    assert lead['kind']=='funder_key' and lead['anchors']==[FUNDER]
    assert lead['shared_infrastructure'] is True and lead['control_state']=='unproven'
    assert {p['path']['recipient'] for p in lead['evidence']}=={DEV,OTHER}
    assert all(e['quality']['strength']=='strict_funding' for e in lead['evidence'])
    assert read(source)['organization_count'] is None


def test_bounded_raw_paging_keeps_unsupported_rows_and_restarts_without_dropping_later_leads(source):
    for n in range(10):add_edge(source,src=addr(100+n),dst=addr(200+n),kind='holds')
    add_edge(source)
    for _ in range(30):
        result=scan(source,limit=2)
        if not result['has_more']:break
    assert read(source)['items'][0]['anchors']==[DEV]
    assert scan(source,limit=2)['inserted']==0


def test_future_known_data_is_held_and_only_visible_after_its_actual_projection(source):
    add_edge(source,known=1001.);scan(source)
    assert read(source)['items']==[]
    source[1][0]=1002.;scan(source)
    assert len(read(source)['items'])==1
    assert read(source,as_of=1000.)['items']==[]


def _tokens(source,credit=None):
    c=source[0]._conn()
    c.executescript('''CREATE TABLE watch_token_transfers(signature TEXT,outer_ix INTEGER,inner_ix INTEGER,source_account TEXT,destination_account TEXT,source_owner TEXT,destination_owner TEXT,mint TEXT,program TEXT,amount_atomic TEXT,decimals INTEGER,fee_atomic TEXT,credit_atomic TEXT,authority TEXT,state TEXT,reason TEXT,instruction_json TEXT,fetch_id TEXT,source TEXT,flow_id INTEGER,known_at REAL);
CREATE TABLE watch_flow_timing(source TEXT,flow_id INTEGER,signature TEXT,flow_state TEXT,graph_commit_after REAL,PRIMARY KEY(source,flow_id));''')
    c.execute('INSERT INTO watch_token_transfers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('5'*88,0,0,addr(10),addr(11),DEV,OTHER,MINT,'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb','9007199254740993',6,None,credit,DEV,'unknown','token2022_fee_unknown','{}','proof','tape',1,990.))
    c.execute('INSERT INTO watch_flow_timing VALUES(?,?,?,?,?)',('tape',1,'5'*88,'known',991.));c.commit();c.close()


def test_token_gross_instruction_with_unknown_net_credit_is_a_typed_direct_lead(source):
    _tokens(source);scan(source)
    lead,=read(source)['items'];evidence,=lead['evidence']
    assert lead['kind']=='token_owner_pair' and lead['anchors']==[DEV,OTHER]
    assert evidence['quality']['strength']=='gross_instruction' and evidence['quality']['net_credit_state']=='unknown'
    assert evidence['path']['amount_atomic']=='9007199254740993' and evidence['path']['credit_atomic'] is None
    assert evidence['source_known_at']==991. and lead['control_state']=='unproven'


def test_pending_token_availability_never_becomes_a_verified_transfer(source):
    _tokens(source)
    c=source[0]._conn();c.execute('UPDATE watch_flow_timing SET graph_commit_after=NULL');c.commit();c.close()
    scan(source)
    lead,=read(source)['items']
    assert lead['evidence'][0]['quality']['strength']=='unverified_token_lead'
    assert lead['evidence'][0]['source_known_at'] is None


def test_read_report_never_creates_missing_graph(tmp_path):
    from sniper.office.wallet_group_leads import read_group_leads
    path=tmp_path/'missing.db';net=WalletNetwork(path,clock=lambda:1000.)
    assert read_group_leads(net)['state']=='unknown' and not path.exists()


def test_cli_is_disabled_without_explicit_activation(tmp_path,capsys):
    spec=importlib.util.spec_from_file_location('lead_cli',Path(__file__).resolve().parents[2]/'scripts/office_wallet_group_leads.py')
    cli=importlib.util.module_from_spec(spec);spec.loader.exec_module(cli)
    assert cli.main(['--state-root',str(tmp_path)])==0
    assert 'Disabled' in capsys.readouterr().out and not (tmp_path/'data').exists()


def test_large_workload_batch_is_resumable_not_a_catalogue_ceiling(source):
    for n in range(120):add_edge(source,src=addr(100+n),dst=addr(300+n))
    out=scan(source,limit=1000)
    assert out['state']=='partial' and out['inserted']>0
    while out['has_more']:out=scan(source,limit=1000)
    visible=read(source)
    assert len(visible['items'])==100 and visible['has_more'] is True
    epoch=out['coverage']['imports']
    assert epoch['as_of']==1000. and epoch['watermark']>=120 and epoch['identity']


def test_new_strict_links_are_not_queued_behind_import_backfill(source):
    for n in range(120):add_edge(source,src=addr(100+n),dst=addr(300+n))
    add_edge(source,src=DEV,dst=FUNDER,kind='funded_by',source='host',proof_kind='research_stream',proof_state='verified',method='wallet-watch-stream-v1')
    scan(source,limit=100)
    assert any(l['kind']=='funder_key' and l['anchors']==[FUNDER] for l in read(source)['items'])


def test_group_scan_exposes_sqlite_busy_code_without_exception_text(source):
    holder=source[0]._conn()
    try:
        holder.execute('BEGIN IMMEDIATE')
        result=scan(source)
        assert result['state']=='unknown' and result['reason']=='group_store_unknown'
        assert result['sqlite_errorcode']==sqlite3.SQLITE_BUSY
        assert result['sqlite_errorname']=='SQLITE_BUSY'
        assert set(result)=={'state','reason','inserted','has_more','items','sqlite_errorcode','sqlite_errorname'}
    finally:
        holder.rollback();holder.close()


def test_group_scan_waits_for_brief_writer_contention(source):
    import threading
    ready=threading.Event();release=threading.Event()
    def hold_writer():
        holder=source[0]._conn()
        try:
            holder.execute('BEGIN IMMEDIATE');ready.set()
            release.wait();threading.Event().wait(.03)
        finally:
            holder.rollback();holder.close()
    writer=threading.Thread(target=hold_writer);writer.start()
    try:
        assert ready.wait(1)
        release.set()
        result=scan(source)
        assert result['state']=='partial',result
        assert 'sqlite_errorcode' not in result
    finally:
        release.set();writer.join(1)
        assert not writer.is_alive()


def test_new_import_creation_path_is_a_creator_claim_not_a_signing_developer(source):
    add_edge(source);scan(source)
    lead,=read(source)['items'];evidence,=lead['evidence']
    assert lead['kind']=='creation_key'
    assert evidence['path']['creator_claim']==DEV and 'developer' not in evidence['path']
    assert evidence['quality']['creation_role']=='creator_claim'


def test_legacy_import_read_is_neutral_without_rewriting_immutable_rows_or_truncated_claims(source):
    from sniper.office import wallet_group_leads as G
    c=source[0]._conn();c.executescript(G.SCHEMA)
    c.execute('INSERT INTO wallet_group_leads VALUES(?,?,?,?,?)',(G.VERSION,'legacy','developer_key',json.dumps([DEV]),990.))
    for i in range(101):
        path=json.dumps(dict(developer=DEV,mint=addr(100+i)))
        quality=json.dumps(dict(strength='import_lead',source='import',shared_infrastructure=False))
        c.execute('INSERT INTO wallet_group_lead_evidence VALUES(?,?,?,?,?,?,?,?)',(str(i).zfill(3),'legacy','imports',str(i),980.,990.,path,quality))
    c.commit();before=c.execute('SELECT kind,lead_id FROM wallet_group_leads').fetchall();raw=c.execute('SELECT path_json,quality_json FROM wallet_group_lead_evidence ORDER BY evidence_id').fetchall();c.close()
    lead,=read(source)['items']
    assert lead['kind']=='creation_key' and lead['lead_id']=='legacy' and lead['evidence_truncated'] is True
    assert len(lead['evidence'])==100
    assert all(e['quality']['creation_role']=='creator_claim' and e['path']['creator_claim']==DEV and 'developer' not in e['path'] for e in lead['evidence'])
    assert 'creation_role' not in lead
    c=source[0]._conn()
    assert c.execute('SELECT kind,lead_id FROM wallet_group_leads').fetchall()==before
    assert c.execute('SELECT path_json,quality_json FROM wallet_group_lead_evidence ORDER BY evidence_id').fetchall()==raw
    c.close()


def test_strict_creation_keeps_validated_signing_developer_distinct_from_creator_claim(source,tmp_path):
    from sniper.office import wallet_group_leads as G
    net,_=source;net.chain_db=tmp_path/'chain.db';signature='5'*88
    fact=dict(state='known',tool='watch_verified_creation',provenance='research_rpc',commitment='finalized',method='confirmed_pump_create_v1',mint=MINT,developer=DEV,curve=addr(12),signature=signature,slot=10,block_time=980,asked=dict(signature=signature))
    c=sqlite3.connect(net.chain_db);c.execute('CREATE TABLE fetches(fetch_id TEXT,result TEXT,shown_at REAL,tool TEXT,state TEXT)')
    c.execute('INSERT INTO fetches VALUES(?,?,?,?,?)',('proof',json.dumps(fact),990.,'watch_verified_creation','known'));c.commit();c.close()
    add_edge(source,source='host',proof_kind='chain_fetch',proof_state='verified',method='confirmed_pump_create_v1')
    c=net._conn();c.execute("UPDATE edges SET proof_ref='proof'");c.commit();c.close()
    scan(source)
    lead,=read(source)['items'];evidence,=lead['evidence']
    assert lead['kind']=='creation_key'
    assert evidence['quality']['strength']=='strict_creation' and evidence['quality']['creation_role']=='signing_developer'
    assert evidence['path']['developer']==DEV and evidence['path']['signature']==signature
    assert 'creator_claim' not in evidence['path']

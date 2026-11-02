import json
import sqlite3
from sniper.office.wallet_network import WalletNetwork


def doc(cid,mint,sig,t):
    return dict(campaign_id=cid,seed='bank',roles=[dict(role='funding_recipient',address='dev',known_at=t-5,proof=dict(source_key=cid,kind='funding',event_at=t-6,known_at=t-5))],launches=[dict(launch_id=cid,mint=mint,source_signature=sig,source_receipt=t,known_at=t+.5,emitted_at=t+1,coverage=dict(creation_verified=True))],buys=[dict(mint=mint,actor='buyer1',signature='B'+sig,received_at=t+2),dict(mint=mint,actor='buyer2',signature='B'+sig,received_at=t+2)])


def setup(tmp):
    n=WalletNetwork(tmp/'n.db',clock=lambda:400.)
    with sqlite3.connect(n.path) as c:
        c.executescript('CREATE TABLE wallet_campaigns(campaign_id TEXT PRIMARY KEY,seed TEXT,bucket_start REAL,first_recorded_at REAL); CREATE TABLE wallet_campaign_versions(campaign_id TEXT,revision INTEGER,recorded_at REAL,source_as_of REAL,doc_json TEXT,PRIMARY KEY(campaign_id,revision));')
        for cid,mint,sig,t in [('old','M1','S1',100.),('new','M2','S2',200.)]:
            c.execute('INSERT INTO wallet_campaigns VALUES(?,?,?,?)',(cid,'bank',t-10,t-5))
            c.execute('INSERT INTO wallet_campaign_versions VALUES(?,?,?,?,?)',(cid,1,t+3,t+3,json.dumps(doc(cid,mint,sig,t))))
    return n


def test_prior_sequences_exclude_future_revision_and_atomic_buy_actors_are_one_event(tmp_path):
    from sniper.office.wallet_campaign_history import operation_history
    n=setup(tmp_path);before=operation_history(n,as_of=300.)
    new=next(r for r in before['items'] if r['campaign_id']=='new')
    assert new['prior_launches']==1 and new['ownership_proven'] is False
    old=next(r for r in before['items'] if r['campaign_id']=='old')
    buys=[e for e in old['sequence'] if e['kind']=='buy_tx']
    assert len(buys)==1 and buys[0]['actors']==2
    with sqlite3.connect(n.path) as c:
        changed=doc('old','M1','S1',100.);changed['buys']*=5
        c.execute('INSERT INTO wallet_campaign_versions VALUES(?,?,?,?,?)',('old',2,350.,350.,json.dumps(changed)))
    after=operation_history(n,as_of=400.)
    assert next(r for r in after['items'] if r['campaign_id']=='new')['prior_sequence_counts']==new['prior_sequence_counts']


def test_current_mint_repeated_in_other_campaign_is_not_independent_history(tmp_path):
    from sniper.office.wallet_campaign_history import operation_history
    n=setup(tmp_path)
    with sqlite3.connect(n.path) as c:
        x=doc('old','M2','S2',100.);c.execute('UPDATE wallet_campaign_versions SET doc_json=? WHERE campaign_id=?',(json.dumps(x),'old'))
    item=next(r for r in operation_history(n,as_of=300.)['items'] if r['campaign_id']=='new')
    assert item['prior_launches']==0


def test_missing_history_is_unknown_and_does_not_create_db(tmp_path):
    from sniper.office.wallet_campaign_history import operation_history
    n=WalletNetwork(tmp_path/'absent.db',clock=lambda:400.)
    assert operation_history(n,as_of=300.)['state']=='unknown'
    assert not n.path.exists()


def test_postcommit_launch_clock_is_joined_when_immutable_doc_omits_it(tmp_path):
    from sniper.office.wallet_campaign_history import operation_history
    n=setup(tmp_path)
    with sqlite3.connect(n.path) as c:
        c.execute('CREATE TABLE wallet_campaign_launches(launch_id TEXT PRIMARY KEY,emitted_at REAL)')
        for cid,t in [('old',100.),('new',200.)]:
            row=json.loads(c.execute('SELECT doc_json FROM wallet_campaign_versions WHERE campaign_id=?',(cid,)).fetchone()[0]);row['launches'][0].pop('emitted_at')
            c.execute('UPDATE wallet_campaign_versions SET doc_json=? WHERE campaign_id=?',(json.dumps(row),cid))
            c.execute('INSERT INTO wallet_campaign_launches VALUES(?,?)',(cid,t+1))
    out=operation_history(n,as_of=300.)
    assert next(r for r in out['items'] if r['campaign_id']=='new')['prior_launches']==1

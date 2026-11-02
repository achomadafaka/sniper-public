import json
import sqlite3
import time
import pytest

from solders.keypair import Keypair

from sniper.office import wallet_network as N, wallet_watch as W, wallet_token_flow as TF
from sniper.office.wallet_watch_collect import SCHEMA as COLLECT_SCHEMA


def address(n):
    return str(Keypair.from_seed(n.to_bytes(32, 'little')).pubkey())


def test_research_defaults_do_not_stop_on_old_graph_size_or_daily_rows(tmp_path):
    net=N.WalletNetwork(tmp_path/'graph.db',clock=lambda:100.)
    c=net._conn()
    c.execute('INSERT INTO daily VALUES(?,?,?)',(N._day(100.),100001,34400));c.commit()
    net._size=lambda:(1<<30)+1
    assert net._refusal(c,100.,rows=1) is None
    assert N.CREDITS_PER_DAY is None
    c.close()


def test_default_graph_retention_does_not_delete_old_evidence(tmp_path):
    net=N.WalletNetwork(tmp_path/'graph.db',clock=lambda:100.)
    net.add_edge(src=address(1),dst=address(2),edge_type='follows',actor='test',note_ref='observed')
    c=net._conn();net.prune(c,100.+1000*86400)
    assert c.execute('SELECT count(*) FROM edges').fetchone()[0]==1
    c.close()


def test_busy_graph_writer_defers_without_a_two_second_observation_stall(tmp_path):
    net=N.WalletNetwork(tmp_path/'graph.db',busy_timeout=0);first=net._conn();second=net._conn()
    try:
        first.execute('BEGIN IMMEDIATE');started=time.monotonic()
        with pytest.raises(sqlite3.OperationalError):second.execute('BEGIN IMMEDIATE')
        assert time.monotonic()-started<1.
    finally:first.rollback();first.close();second.close()


def test_graph_initialization_closes_connection_when_schema_is_busy(tmp_path,monkeypatch):
    class Busy:
        closed=False
        def execute(self,*a):raise sqlite3.OperationalError('busy')
        def close(self):self.closed=True
    connection=Busy()
    monkeypatch.setattr(N.sqlite3,'connect',lambda *a,**kw:connection)
    with pytest.raises(sqlite3.OperationalError):N.WalletNetwork(tmp_path/'graph.db')._conn()
    assert connection.closed


def test_watch_registry_and_export_pass_500_without_changing_removed_rows(tmp_path):
    (tmp_path/'pumpswap').mkdir();net=N.WalletNetwork(tmp_path/'graph.db')
    c=net._conn();c.executescript(W.SCHEMA)
    c.executemany('INSERT INTO watch_list VALUES(?,NULL,?,?,?,1,NULL)',[(address(i),'fixture','host','test') for i in range(501)])
    c.execute('UPDATE watch_list SET removed_at=2 WHERE address=?',(address(0),));c.commit();c.close()
    assert W.add(net,tmp_path,address=address(502),operator_id=None,evidence='observed role',source='host',actor='test',now=100.)['state']=='added'
    doc=json.loads(W.watch_file(tmp_path).read_text())
    assert len(doc['wallets'])==501 and address(0) not in doc['wallets']


def test_unlimited_campaign_membership_keeps_durable_claims(tmp_path):
    (tmp_path/'pumpswap').mkdir();net=N.WalletNetwork(tmp_path/'graph.db')
    for i in range(21):
        assert W.add(net,tmp_path,address=address(i),operator_id='campaign',campaign_limit=-1,evidence='observed role',source='host',actor='test',now=100.)['state']=='added'
    with sqlite3.connect(net.path) as c:
        assert c.execute('SELECT count(*) FROM watch_campaign_members').fetchone()[0]==21


def alias_fixture(tmp_path):
    (tmp_path/'pumpswap').mkdir();net=N.WalletNetwork(tmp_path/'graph.db')
    W.add(net,tmp_path,address=address(1),operator_id=None,evidence='role',source='host',actor='test',now=100.)
    c=net._conn();TF.init_storage(c);c.executescript(COLLECT_SCHEMA)
    return net,c


def snapshot(c,*,flow=1,owner=None,state='known',reason=None,post='20',after=99.,account=None,slot=None):
    owner=owner or address(1);account=account or address(3);sig='sig'+str(flow)
    c.execute('INSERT INTO watch_flow_timing(source,flow_id,signature,received_at,capture_processed_at,proof_stored_at,graph_commit_before,graph_commit_after,selector_json,gaps_json,flow_state,slot) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('tape',flow,sig,90.,91.,92.,93.,after,'{}','{}','known',slot if slot is not None else 100+flow))
    c.execute('INSERT INTO watch_token_accounts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(sig,account,address(2),owner,TF.TOKEN22,6,None if reason else '10',post,None if reason else '10',state,reason,'proof','tape',flow,94.))


def test_export_includes_owner_bound_token_alias_even_when_balance_side_missing(tmp_path):
    net,c=alias_fixture(tmp_path)
    snapshot(c,state='unknown',reason='token_balance_side_missing');c.commit()
    W._sync(c,tmp_path,100.)
    doc=json.loads(W.watch_file(tmp_path).read_text())
    assert set(doc['wallets'])=={address(1),address(3)}
    assert doc['owner_wallets']==[address(1)]
    assert doc['token_accounts']==[address(3)]
    assert [r[0] for r in c.execute('SELECT address FROM watch_list WHERE removed_at IS NULL')]==[address(1)]
    c.close()


def test_latest_alias_owner_change_or_closed_account_revokes_old_alias(tmp_path):
    net,c=alias_fixture(tmp_path);snapshot(c);snapshot(c,flow=2,owner=address(4));c.commit()
    W._sync(c,tmp_path,100.)
    assert json.loads(W.watch_file(tmp_path).read_text())['wallets']==[address(1)]
    snapshot(c,flow=3,post=None,state='unknown',reason='token_balance_side_missing');c.commit()
    W._sync(c,tmp_path,100.)
    assert json.loads(W.watch_file(tmp_path).read_text())['wallets']==[address(1)]
    c.close()


def test_alias_needs_available_commit_and_removal_revokes_it(tmp_path):
    net,c=alias_fixture(tmp_path);snapshot(c,after=None);c.commit()
    W._sync(c,tmp_path,100.)
    assert json.loads(W.watch_file(tmp_path).read_text())['wallets']==[address(1)]
    c.execute('UPDATE watch_flow_timing SET graph_commit_after=99.');c.commit()
    W._sync(c,tmp_path,100.);assert address(3) in json.loads(W.watch_file(tmp_path).read_text())['wallets']
    c.close();W.remove(net,tmp_path,address=address(1),now=101.)
    assert json.loads(W.watch_file(tmp_path).read_text())['wallets']==[]


def test_late_older_snapshot_cannot_resurrect_previous_token_account_owner(tmp_path):
    net,c=alias_fixture(tmp_path)
    snapshot(c,owner=address(4),slot=200)
    snapshot(c,flow=2,slot=100);c.commit()
    W._sync(c,tmp_path,100.)
    assert json.loads(W.watch_file(tmp_path).read_text())['wallets']==[address(1)]
    c.close()


def test_same_slot_conflicting_identity_cannot_choose_a_current_alias(tmp_path):
    net,c=alias_fixture(tmp_path)
    snapshot(c,owner=address(4),slot=100)
    snapshot(c,flow=2,slot=100);c.commit()
    W._sync(c,tmp_path,100.)
    assert json.loads(W.watch_file(tmp_path).read_text())['wallets']==[address(1)]
    c.close()


def test_unfinished_newer_identity_prevents_reusing_an_older_alias(tmp_path):
    net,c=alias_fixture(tmp_path)
    snapshot(c,slot=100);snapshot(c,flow=2,slot=200,owner=address(4),after=None);c.commit()
    W._sync(c,tmp_path,100.)
    assert json.loads(W.watch_file(tmp_path).read_text())['wallets']==[address(1)]
    c.close()

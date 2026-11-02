"""Raw observations, atomic graph evidence and honest commit/selector witnesses."""
import json
import sqlite3
import struct

import base58
import pytest

from .test_wallet_flow_consumer import source, insert, wire, rows, NOW, A, B
from .test_wallet_token_flow import X, Y, M, TOKEN
from sniper.office.wallet_stream_proof import confirmed_receipt
from sniper.office.wallet_watch_collect import Collector, movements


def token_wire():
    u=wire();info=u.transaction.transaction;msg=info.transaction.message
    del msg.account_keys[:];msg.account_keys.extend(base58.b58decode(a) for a in [A,B,X,Y,M,TOKEN]);msg.header.num_readonly_unsigned_accounts=2
    del msg.instructions[:];ix=msg.instructions.add();ix.program_id_index=5;ix.accounts=bytes([2,4,3,0]);ix.data=bytes([12])+struct.pack('<Q',2**63+5)+bytes([6])
    meta=info.meta;del meta.pre_balances[:];del meta.post_balances[:]
    meta.pre_balances.extend([100000]*6);meta.post_balances.extend([95000]+[100000]*5)
    for field,amounts in [(meta.pre_token_balances,[2**63+10,1]),(meta.post_token_balances,[5,2**63+6])]:
        for i,owner,n in zip([2,3],[A,B],amounts):
            b=field.add();b.account_index=i;b.owner=owner;b.mint=M;b.program_id=TOKEN;b.ui_token_amount.amount=str(n);b.ui_token_amount.decimals=6
    return u


def test_codec_adds_raw_unmatched_sides_without_changing_legacy_movements():
    u=token_wire();u.transaction.transaction.meta.post_token_balances.pop(0)
    tx=confirmed_receipt(u,commitment='confirmed',block_time=NOW-1)
    assert tx['meta']['preTokenBalances'] is None and tx['meta']['postTokenBalances'] is None
    assert len(tx['meta']['preTokenBalanceObservations'])==2 and len(tx['meta']['postTokenBalanceObservations'])==1
    assert movements(tx,A)[1]['state']=='unknown'


def test_consumer_stores_tokens_timing_and_charges_once_with_shared_proof(source):
    net,chain,clock,desk=source;insert(source,raw=token_wire().SerializeToString())
    (desk/'universe.heartbeat').write_text(json.dumps(dict(ts=NOW,wallet_flow=dict(desired_selectors=2,submitted_selectors=1,submitted_at=NOW-5,selector_acknowledged=True))))
    out=Collector(net,desk).consume_flows()
    assert out['rows']==1
    account,=rows(net.path,'watch_token_accounts');transfer,=rows(net.path,'watch_token_transfers');timing,=rows(net.path,'watch_flow_timing')
    assert account['owner']==A and transfer['amount_atomic']==str(2**63+5)
    assert account['fetch_id']==transfer['fetch_id']
    assert len(rows(chain.path,'fetches'))==1 and not rows(chain.path,'calls')
    assert rows(net.path,'daily')[0]['rows_added']==12
    assert timing['received_at']==NOW-2 and timing['capture_processed_at']==NOW-1
    assert timing['proof_stored_at']<=timing['graph_commit_before']<=timing['graph_commit_after']
    selector=json.loads(timing['selector_json'])
    assert selector['desired_selectors']==2 and selector['submitted_selectors']==1
    assert selector['selector_acknowledged'] is None
    assert json.loads(timing['gaps_json'])['state']=='unknown'
    assert Collector(net,desk).consume_flows()['rows']==0
    assert len(rows(net.path,'watch_token_transfers'))==1
    assert rows(net.path,'daily')[0]['rows_added']==13  # one new seed cursor, no duplicate token facts


def test_receipt_failure_rolls_back_tokens_and_charge_then_retry_uses_same_proof(source):
    net,chain,clock,desk=source;insert(source,raw=token_wire().SerializeToString());collector=Collector(net,desk)
    c=collector._open();c.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON watch_flow_receipts BEGIN SELECT RAISE(ABORT,'test receipt failure'); END");c.close()
    assert collector.consume_flows()['state']=='unknown'
    assert not rows(net.path,'watch_token_accounts') and not rows(net.path,'watch_token_transfers') and not rows(net.path,'watch_flow_timing')
    assert not rows(net.path,'watch_flow_receipts') and rows(net.path,'daily')[0]['rows_added']==1
    with sqlite3.connect(net.path) as c:c.execute('DROP TRIGGER fail_receipt')
    clock[0]+=5
    assert Collector(net,desk).consume_flows()['rows']==1
    assert len(rows(chain.path,'fetches'))==1


def test_new_rows_are_included_in_cap_preflight(source,monkeypatch):
    from sniper.office import wallet_network
    net,chain,clock,desk=source;insert(source,raw=token_wire().SerializeToString())
    monkeypatch.setattr(wallet_network,'MAX_ROWS_PER_DAY',6)
    out=Collector(net,desk).consume_flows()
    assert out['reason']=='daily_row_cap' and out['pending']==1
    assert not rows(net.path,'watch_token_transfers') and not rows(net.path,'watch_token_accounts')
    assert not rows(net.path,'watch_flow_receipts')


@pytest.mark.parametrize('heartbeat',[None,dict(ts=NOW-31,wallet_flow={}),dict(ts=NOW+1,wallet_flow={}),dict(ts=NOW,wallet_flow=dict(desired_selectors=True,submitted_selectors=1))])
def test_missing_stale_future_or_bad_selector_snapshot_stays_unknown(source,heartbeat):
    net,chain,clock,desk=source;insert(source)
    if heartbeat is not None:(desk/'universe.heartbeat').write_text(json.dumps(heartbeat))
    Collector(net,desk).consume_flows()
    timing,=rows(net.path,'watch_flow_timing')
    assert json.loads(timing['selector_json'])['state']=='unknown'


@pytest.mark.parametrize('now',[float('nan'),float('inf'),True,0,None])
def test_selector_snapshot_rejects_unknown_observation_clock(tmp_path,now):
    from sniper.office.wallet_watch_collect import selector_snapshot
    path=tmp_path/'hb';path.write_text(json.dumps(dict(ts=NOW,wallet_flow=dict(desired_selectors=1,submitted_selectors=1,submitted_at=NOW-1))))
    assert selector_snapshot(path,now)['state']=='unknown'


def test_timing_update_failure_never_erases_committed_receipt_or_invents_after(source,monkeypatch):
    net,chain,clock,desk=source;insert(source,raw=token_wire().SerializeToString());collector=Collector(net,desk);original=collector._open
    class Connection:
        def __init__(self):self.conn=original()
        def __getattr__(self,name):return getattr(self.conn,name)
        def __enter__(self):self.conn.__enter__();return self
        def __exit__(self,*args):return self.conn.__exit__(*args)
        def execute(self,sql,*args):
            if sql.startswith('UPDATE watch_flow_timing'):raise sqlite3.OperationalError('test timing update failure')
            return self.conn.execute(sql,*args)
    monkeypatch.setattr(collector,'_open',Connection)
    out=collector.consume_flows()
    assert out['rows']==1 and out['reason']=='stream_timing_store_unknown'
    assert len(rows(net.path,'watch_token_transfers'))==1 and len(rows(net.path,'watch_flow_receipts'))==1
    assert rows(net.path,'watch_flow_timing')[0]['graph_commit_after'] is None


def test_token_parser_unknown_is_durable_without_erasing_existing_native_evidence(source,monkeypatch):
    from sniper.office import wallet_token_flow
    net,chain,clock,desk=source;insert(source)
    monkeypatch.setattr(wallet_token_flow,'token_flow',lambda tx:dict(accounts=[],transfers=[],state='unknown',reason='token_evidence_row_limit'))
    assert Collector(net,desk).consume_flows()['rows']==1
    timing,=rows(net.path,'watch_flow_timing')
    assert timing['flow_state']=='known' and timing['token_state']=='unknown'
    assert timing['token_reason']=='token_evidence_row_limit'
    assert rows(net.path,'watch_movements') and rows(net.path,'edges')


def test_token_fact_causal_context_joins_one_exact_flow_not_an_earlier_signature(source):
    net,chain,clock,desk=source;insert(source,raw=token_wire().SerializeToString())
    Collector(net,desk).consume_flows()
    for table in ['watch_token_accounts','watch_token_transfers']:
        row,=rows(net.path,table)
        assert row['source']==str((desk/'pumpswap/tape.db').resolve()) and row['flow_id']==1
        assert row['known_at']==NOW
        with sqlite3.connect(net.path) as c:
            assert c.execute(f'SELECT t.graph_commit_after FROM {table} a JOIN watch_flow_timing t ON t.source=a.source AND t.flow_id=a.flow_id').fetchone()==(NOW,)

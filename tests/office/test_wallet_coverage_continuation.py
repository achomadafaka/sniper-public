"""Research ceilings defer work; validated metadata owners are observers, never control."""
import base64
import json
import sqlite3
import struct

import base58
import pytest
from solders.keypair import Keypair

from .test_wallet_flow_consumer import source, insert, wire, rows, NOW, A
from .test_wallet_token_follow import rotation,D,E
from .test_wallet_token_flow import X,M,TOKEN,TOKEN22
from sniper.office import wallet_watch as W
from sniper.office.wallet_watch_collect import Collector


@pytest.fixture(autouse=True)
def infrastructure(monkeypatch):monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset())


def test_sol_child_is_watched_after_commit_before_next_retained_token_frame(source):
    net,chain,clock,desk=source;u=wire();u.transaction.transaction.transaction.message.account_keys[1]=base58.b58decode(D)
    insert(source,raw=u.SerializeToString());insert(source,n=4,raw=rotation(D,E,n=4).SerializeToString())
    out=Collector(net,desk).consume_flows()
    assert out['rows']==2 and {r['address'] for r in W.read_list(net)}=={A,D,E}
    assert any(r['source_owner']==D for r in rows(net.path,'watch_token_transfers'))


def test_watched_token_owner_absent_from_message_keys_is_an_observer_not_signer(source):
    net,chain,clock,desk=source;W.add(net,desk,address=D,operator_id=None,evidence='fixture',source='host',actor='crew',now=NOW-5)
    u=rotation(A,E)
    for side in [u.transaction.transaction.meta.pre_token_balances,u.transaction.transaction.meta.post_token_balances]:side[0].owner=D
    assert base58.b58decode(D) not in u.transaction.transaction.transaction.message.account_keys
    insert(source,raw=u.SerializeToString());Collector(net,desk).consume_flows()
    transfer,=rows(net.path,'watch_token_transfers');assert transfer['source_owner']==D
    assert E in {r['address'] for r in W.read_list(net)} and not rows(net.path,'edges')


@pytest.mark.parametrize('encoding',['zlib','archive'])
def test_actual_compressed_and_archived_frames_use_shared_reader(source,encoding):
    from sniper.office.wallet_stream_proof import encode_payload
    from .test_wallet_flow_archive import compact as archive_rows
    net,chain,clock,desk=source;raw=rotation().SerializeToString();insert(source,raw=raw)
    with sqlite3.connect(desk/'pumpswap/tape.db') as c:
        c.execute('UPDATE wallet_flow_transactions SET payload_json=?',(encode_payload(raw),))
        c.execute('CREATE TABLE wallet_flow_meta(id INTEGER PRIMARY KEY,payload_bytes INTEGER,pending_metadata INTEGER)')
        c.execute('INSERT INTO wallet_flow_meta SELECT 1,sum(length(payload_json)),0 FROM wallet_flow_transactions')
    if encoding=='archive':assert archive_rows(desk/'pumpswap/tape.db',desk/'pumpswap/wallet-flow-archive',before=NOW)['archived']==1
    assert Collector(net,desk).consume_flows()['rows']==1
    assert rows(net.path,'watch_token_transfers')


def many_rotations(n=120):
    u=wire();info=u.transaction.transaction;msg=info.transaction.message
    del msg.account_keys[:];keys=[A,X,M,TOKEN];owners=[]
    for i in range(n):
        owner=str(Keypair.from_seed((1000+i).to_bytes(32,'little')).pubkey());account=str(Keypair.from_seed((2000+i).to_bytes(32,'little')).pubkey())
        owners.append(owner);keys.extend([owner,account])
    msg.account_keys.extend(base58.b58decode(k) for k in keys);msg.header.num_readonly_unsigned_accounts=0
    del msg.instructions[:]
    for i in range(n):
        ix=msg.instructions.add();ix.program_id_index=3;ix.accounts=bytes([1,2,5+i*2,0]);ix.data=bytes([12])+struct.pack('<Q',1)+bytes([6])
    meta=info.meta;del meta.pre_balances[:];del meta.post_balances[:]
    meta.pre_balances.extend([100000]*len(keys));meta.post_balances.extend([95000]+[100000]*(len(keys)-1))
    for side,amounts in [(meta.pre_token_balances,[1000]+[0]*n),(meta.post_token_balances,[1000-n]+[1]*n)]:
        for index,owner,amount in zip([1]+[5+i*2 for i in range(n)],[A]+owners,amounts):
            b=side.add();b.account_index=index;b.owner=owner;b.mint=M;b.program_id=TOKEN;b.ui_token_amount.amount=str(amount);b.ui_token_amount.decimals=6
    return u,owners


def test_more_than_twenty_and_hundred_recipients_resume_after_restart_without_drop(source):
    net,chain,clock,desk=source;u,owners=many_rotations();insert(source,raw=u.SerializeToString())
    Collector(net,desk).consume_flows()
    for _ in range(10):Collector(net,desk).consume_flows()
    assert set(owners)<={r['address'] for r in W.read_list(net)}
    assert len(rows(net.path,'watch_campaign_members'))==120
    assert len(rows(net.path,'watch_token_transfers'))==120
    assert len(rows(chain.path,'fetches'))==1


def test_old_supported_flow_is_not_discarded_by_expansion_age(source):
    net,chain,clock,desk=source;insert(source,bt=NOW-1000,raw=rotation().SerializeToString())
    Collector(net,desk).consume_flows()
    assert D in {r['address'] for r in W.read_list(net)}


def test_two_hundred_pending_is_not_a_terminal_quota(source):
    net,chain,clock,desk=source
    for n in range(2,203):insert(source,n=n,bt=None)
    for _ in range(4):Collector(net,desk).consume_flows(limit=100)
    assert len(rows(net.path,'watch_flow_pending'))==201
    assert not rows(net.path,'watch_flow_receipts')


def test_only_token2022_fee_unknown_can_watch_gross_intent_without_net_claim(source):
    net,chain,clock,desk=source;insert(source,raw=rotation(program=TOKEN22).SerializeToString())
    Collector(net,desk).consume_flows()
    t,=rows(net.path,'watch_token_transfers');assert t['reason']=='token2022_fee_unknown' and t['credit_atomic'] is None
    watched={r['address']:r for r in W.read_list(net)};assert D in watched
    assert 'intent' in watched[D]['evidence'] and 'net_unknown' in watched[D]['evidence']
    assert not rows(net.path,'edges')


def test_changed_owner_of_previously_watched_account_revokes_alias_without_watching_new_owner(source):
    net,chain,clock,desk=source;insert(source,raw=rotation().SerializeToString())
    Collector(net,desk,expand=False).consume_flows()
    assert X in json.loads(W.watch_file(desk).read_text())['wallets']
    # The only owner in this frame is now unwatched D; X is still a message account alias.
    u=rotation(n=4)
    for side in [u.transaction.transaction.meta.pre_token_balances,u.transaction.transaction.meta.post_token_balances]:side[0].owner=D
    insert(source,n=4,raw=u.SerializeToString());Collector(net,desk,expand=False).consume_flows()
    assert D not in {r['address'] for r in W.read_list(net)}
    latest=[r for r in rows(net.path,'watch_token_accounts') if r['account']==X][-1]
    assert latest['owner']==D and X not in json.loads(W.watch_file(desk).read_text())['wallets']


def test_generic_hub_labels_do_not_suppress_strict_research_payment(source):
    net,chain,clock,desk=source;u=wire();u.transaction.transaction.transaction.message.account_keys[1]=base58.b58decode(D)
    with net._conn() as c:c.execute('INSERT INTO labels VALUES(?,?,?,?,?,?)',('solana',A,'hub','host','crew',NOW-1))
    insert(source,raw=u.SerializeToString());Collector(net,desk).consume_flows()
    assert D in {r['address'] for r in W.read_list(net)}


def test_default_daily_allocation_is_absent_but_usage_is_accounted(tmp_path):
    from .test_wallet_watch_collect import setup,NOW as HTTP_NOW
    net,chain,clock,calls=setup(tmp_path,lambda params:[],{})
    with net._conn() as c:net._count(c,HTTP_NOW,credits=33333)
    result=Collector(net,tmp_path).tick(max_calls=1)
    assert calls and result['credit_cap_estimate_per_day'] is None
    assert result['credits_remaining_estimate_today'] is None
    assert result['credits_reserved_estimate_today']>=33333


def test_disabled_follow_defers_sol_and_later_enable_cannot_backdate_knowledge(source):
    net,chain,clock,desk=source;u=wire();u.transaction.transaction.transaction.message.account_keys[1]=base58.b58decode(D)
    insert(source,bt=NOW-1000,raw=u.SerializeToString())
    Collector(net,desk,expand=False).consume_flows()
    assert D not in {r['address'] for r in W.read_list(net)}
    clock[0]=NOW-1;Collector(net,desk).consume_flows()
    assert D not in {r['address'] for r in W.read_list(net)}
    clock[0]=NOW+2;out=Collector(net,desk).consume_flows()
    assert D in {r['address'] for r in W.read_list(net)}
    assert out['expansion_pending']['count_lower_bound']==0


def test_committed_timing_witness_recovers_after_busy_failure_at_new_upper_time(source,monkeypatch):
    net,chain,clock,desk=source;insert(source,raw=rotation().SerializeToString())
    collector=Collector(net,desk);monkeypatch.setattr(collector,'_finish_timings',lambda *a:False)
    collector.consume_flows()
    timing,=rows(net.path,'watch_flow_timing');assert timing['graph_commit_after'] is None
    charged=rows(net.path,'daily')[0]['rows_added']
    clock[0]+=7;Collector(net,desk).consume_flows()
    timing,=rows(net.path,'watch_flow_timing')
    assert timing['graph_commit_after']==NOW+7 and timing['graph_commit_after']>timing['graph_commit_before']
    assert len(rows(net.path,'watch_flow_receipts'))==1 and len(rows(net.path,'watch_token_transfers'))==1
    assert rows(net.path,'daily')[0]['rows_added']<=charged+1  # bootstrap cursor only; no second witness charge


def test_historical_alias_context_records_new_owner_after_a_late_older_identity(source):
    net,chain,clock,desk=source
    for n,slot,owner in [(3,200,A),(4,100,D),(5,300,E)]:
        u=rotation(n=n);u.transaction.slot=slot
        for side in [u.transaction.transaction.meta.pre_token_balances,u.transaction.transaction.meta.post_token_balances]:side[0].owner=owner
        insert(source,n=n,raw=u.SerializeToString())
        with sqlite3.connect(desk/'pumpswap/tape.db') as c:c.execute('UPDATE wallet_flow_transactions SET slot=? WHERE flow_id=?',(slot,n-2))
        Collector(net,desk,expand=False).consume_flows()
    x=[r for r in rows(net.path,'watch_token_accounts') if r['account']==X]
    assert [r['owner'] for r in x]==[A,D,E]
    assert {r['address'] for r in W.read_list(net)}=={A}
    assert X not in json.loads(W.watch_file(desk).read_text())['wallets']


def test_deferred_twenty_first_child_is_followed_before_its_next_retained_hop(source):
    net,chain,clock,desk=source;first,owners=many_rotations(21)
    second=rotation(owners[-1],E,n=4,amount=1)
    second.transaction.transaction.transaction.message.account_keys[2]=first.transaction.transaction.transaction.message.account_keys[45]
    for side,amounts in [(second.transaction.transaction.meta.pre_token_balances,[1,0]),(second.transaction.transaction.meta.post_token_balances,[0,1])]:
        for row,amount in zip(side,amounts):row.ui_token_amount.amount=str(amount)
    insert(source,raw=first.SerializeToString());insert(source,n=4,raw=second.SerializeToString())
    for _ in range(12):Collector(net,desk).consume_flows()
    watched={r['address'] for r in W.read_list(net)}
    assert owners[-1] in watched and E in watched

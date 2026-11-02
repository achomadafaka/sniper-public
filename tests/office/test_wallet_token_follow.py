"""Following an observed transfer watches a wallet; it never proves funding or control."""
import json
import sqlite3

import base58
import pytest
from solders.keypair import Keypair
from solders.pubkey import Pubkey

from .test_wallet_flow_consumer import source, insert, rows, NOW, A
from .test_wallet_token_flow_consumer import token_wire
from .test_wallet_token_flow import TOKEN, TOKEN22
from sniper.office.wallet_watch_collect import Collector
from sniper.office import wallet_watch as W

D,E=(str(Keypair.from_seed(bytes([i])*32).pubkey()) for i in [21,22])


def rotation(sender=A,recipient=D,n=3,program=TOKEN,amount=None):
    u=token_wire();info=u.transaction.transaction;msg=info.transaction.message
    info.signature=bytes([n])*64;info.transaction.signatures[0]=info.signature
    msg.account_keys[0]=base58.b58decode(sender);msg.account_keys[1]=base58.b58decode(recipient);msg.account_keys[5]=base58.b58decode(program)
    for side in [info.meta.pre_token_balances,info.meta.post_token_balances]:
        side[0].owner=sender;side[1].owner=recipient
        for b in side:b.program_id=program
    if amount is not None:
        import struct
        msg.instructions[0].data=bytes([12])+struct.pack('<Q',amount)+bytes([6])
    return u


@pytest.fixture(autouse=True)
def known_infrastructure(monkeypatch):
    monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset())


def test_first_rotation_adds_new_watch_before_second_hop_is_decoded(source):
    net,chain,clock,desk=source;insert(source,raw=rotation().SerializeToString())
    # The second frame is already retained locally; watching the new owner must precede decoding it.
    insert(source,n=4,raw=rotation(D,E,n=4).SerializeToString())
    out=Collector(net,desk).consume_flows()
    assert out['rows']==2 and {r['address'] for r in W.read_list(net)}=={A,D,E}
    assert {r['source_owner'] for r in rows(net.path,'watch_token_transfers')}=={A,D}
    assert not rows(net.path,'edges')
    for r in W.read_list(net):
        if r['address']!=A:
            assert r['operator_id'].startswith('token60:') and r['evidence'].startswith('observed_token_transfer ')
    assert set(json.loads(W.watch_file(desk).read_text())['wallets'])=={A,D,E}
    assert len(rows(net.path,'watch_campaign_members'))==2


@pytest.mark.parametrize('bad',['offcurve','missing_owner','zero','same_owner','future','removed','infrastructure','label','disabled'])
def test_ineligible_token_destinations_never_expand(source,bad,monkeypatch):
    net,chain,clock,desk=source;recipient=D;u=rotation();bt=NOW-1
    if bad=='token2022':u=rotation(program=TOKEN22)
    elif bad=='offcurve':recipient=str(Pubkey.find_program_address([b'pool'],Pubkey.from_string(TOKEN))[0]);u=rotation(recipient=recipient)
    elif bad=='missing_owner':
        for side in [u.transaction.transaction.meta.pre_token_balances,u.transaction.transaction.meta.post_token_balances]:side.pop()
    elif bad=='zero':u=rotation(amount=0)
    elif bad=='same_owner':
        for side in [u.transaction.transaction.meta.pre_token_balances,u.transaction.transaction.meta.post_token_balances]:side[1].owner=A
    elif bad=='stale':bt=NOW-121
    elif bad=='future':bt=NOW+1
    elif bad=='removed':
        with net._conn() as c:c.execute('INSERT INTO watch_list VALUES(?,NULL,?,?,?, ?,?)',(D,'removed','host','crew',NOW-5,NOW-3))
    elif bad=='infrastructure':monkeypatch.setattr(W,'_known_infrastructure',lambda:frozenset({D}))
    elif bad=='label':
        with net._conn() as c:c.execute('INSERT INTO labels VALUES(?,?,?,?,?,?)',('solana',D,'infrastructure','host','crew',NOW))
    insert(source,bt=bt,raw=u.SerializeToString())
    Collector(net,desk,expand=bad!='disabled').consume_flows()
    assert {r['address'] for r in W.read_list(net)}=={A}
    if bad=='token2022':
        transfer,=rows(net.path,'watch_token_transfers')
        assert transfer['state']=='unknown' and transfer['credit_atomic'] is None and transfer['fee_atomic'] is None
    if bad in ('offcurve','same_owner'):
        assert rows(net.path,'watch_flow_receipts')[0]['state']=='known'
        assert rows(net.path,'watch_token_transfers')[0]['state']=='known'


def test_campaign_membership_survives_calls_and_removals_without_twenty_limit(source):
    net,chain,clock,desk=source;collector=Collector(net,desk);first=None
    for i in range(21):
        dest=str(Keypair.from_seed(i.to_bytes(32,'little')).pubkey())
        if first is None:first=dest
        insert(source,n=i+3,raw=rotation(recipient=dest,n=i+3).SerializeToString())
        out=collector.consume_flows(limit=1)
    assert out['reason']=='available_stream_only'
    assert len(rows(net.path,'watch_campaign_members'))==21
    W.remove(net,desk,address=first,now=NOW)
    c=collector._open()
    assert collector._expand_token(c,str((desk/'pumpswap/tape.db').resolve()),1,rows(net.path,'watch_flow_receipts')[0]['signature'],NOW) is None
    c.close()
    assert first not in {r['address'] for r in W.read_list(net)}
    assert len(rows(net.path,'watch_campaign_members'))==21


def test_source_proof_must_bind_the_actual_source_flow_and_observed_sender(source,monkeypatch):
    net,chain,clock,desk=source;collector=Collector(net,desk)
    # Save evidence, but defer the post-commit follow to inspect its proof gate.
    monkeypatch.setattr(collector,'_expand_token',lambda *a,**k:None)
    insert(source,raw=rotation().SerializeToString());collector.consume_flows()
    with sqlite3.connect(net.path) as c:c.execute("UPDATE watch_token_transfers SET fetch_id='not-a-source-proof'")
    c=collector._open();signature=rows(net.path,'watch_flow_receipts')[0]['signature']
    assert Collector(net,desk)._expand_token(c,str((desk/'pumpswap/tape.db').resolve()),1,signature,NOW)=='token_expansion_source_proof_unknown'
    c.close();assert {r['address'] for r in W.read_list(net)}=={A}


@pytest.mark.parametrize('cap',['global','daily'])
def test_token_follow_respects_existing_caps_after_evidence_is_durable(source,monkeypatch,cap):
    from sniper.office import wallet_network
    monkeypatch.setattr(W,'MAX_LISTED',500)
    monkeypatch.setattr(wallet_network,'MAX_ROWS_PER_DAY',100000)
    net,chain,clock,desk=source;collector=Collector(net,desk)
    monkeypatch.setattr(collector,'_expand_token',lambda *a,**k:None)
    insert(source,raw=rotation().SerializeToString());collector.consume_flows()
    c=collector._open()
    with c:
        if cap=='global':
            for i in range(499):
                address=str(Keypair.from_seed((1000+i).to_bytes(32,'little')).pubkey())
                c.execute('INSERT INTO watch_list VALUES(?,NULL,?,?,?, ?,NULL)',(address,'fixture','host','crew',NOW))
        else:c.execute('UPDATE daily SET rows_added=?',(wallet_network.MAX_ROWS_PER_DAY-1,))
    signature=rows(net.path,'watch_flow_receipts')[0]['signature']
    why=Collector(net,desk)._expand_token(c,str((desk/'pumpswap/tape.db').resolve()),1,signature,NOW)
    assert why==('watch_list_full' if cap=='global' else 'daily_row_cap')
    assert D not in {r['address'] for r in W.read_list(net)}
    assert len(rows(net.path,'watch_token_transfers'))==1 and len(rows(net.path,'watch_flow_receipts'))==1
    c.close()


def test_uncommitted_token_evidence_cannot_expand(source):
    net,chain,clock,desk=source;collector=Collector(net,desk);c=collector._open()
    c.execute('BEGIN IMMEDIATE')
    assert collector._expand_token(c,'source',1,'signature',NOW)=='token_expansion_commit_unknown'
    c.rollback();c.close()


def test_unknown_infrastructure_is_typed_and_keeps_transfer_evidence(source,monkeypatch):
    net,chain,clock,desk=source;insert(source,raw=rotation().SerializeToString())
    monkeypatch.setattr(W,'_known_infrastructure',lambda:None)
    out=Collector(net,desk).consume_flows()
    assert out['reason']=='token_expansion_infrastructure_unknown'
    assert rows(net.path,'watch_token_transfers') and {r['address'] for r in W.read_list(net)}=={A}


def test_known_explicit_token2022_fee_can_watch_recipient_without_inventory_inference(source):
    import struct
    net,chain,clock,desk=source;u=rotation(program=TOKEN22)
    amount,fee=2**63+5,7
    u.transaction.transaction.transaction.message.instructions[0].data=bytes([26,1])+struct.pack('<QBQ',amount,6,fee)
    insert(source,raw=u.SerializeToString());Collector(net,desk).consume_flows()
    transfer,=rows(net.path,'watch_token_transfers')
    assert transfer['state']=='known' and transfer['fee_atomic']=='7' and transfer['credit_atomic']==str(amount-fee)
    assert {r['address'] for r in W.read_list(net)}=={A,D}
    assert not rows(net.path,'edges') and not rows(chain.path,'calls')

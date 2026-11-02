"""Actual temporary raw capture, research watch/export, market stores and funded intent boundary."""
import json

import pytest
from solders.pubkey import Pubkey

import test_known_group_canary as C
from test_known_group_canary import ledger, knobs
from test_known_group_canary_worker import source, started, push, state, transfer
from sniper.office import wallet_watch as watch
from sniper.office.wallet_network import WalletNetwork
from sniper.services import memecoin_manual_intents as intents


def healthy_market_and_source(s,monkeypatch,*,relay=None,on_funding=None,restart=False):
    from test_desk_ps_metadata_merged_flow import build,send,W
    from test_codex_pumpswap_merged_flow import NOW,MINT,POOL
    monkeypatch.setattr(C,'R',NOW);monkeypatch.setattr(C,'MINT',MINT);monkeypatch.setattr(C,'POOL',POOL)
    monkeypatch.setattr(C,'H',str(Pubkey.from_bytes(bytes([40])*32)))
    s=started(s,monkeypatch);s.clock[0]=NOW-10
    assert s.worker.tick()['state']=='baseline'
    net=WalletNetwork(s.watch,clock=lambda:s.clock[0])
    assert watch.add(net,s.desk,address=C.DEV,operator_id='fixture',evidence='native funding receipt fixture',
        source='host',actor='fixture',now=NOW-9)['state']=='added'
    plan=([(C.H,relay,24,7,NOW-9,1_000_000)] if relay else [])+[(relay or C.H,C.DEV,25,8,NOW-6,10_000_000)]
    s.preparation_statuses=[]
    for sender,recipient,signature,slot,receipt,amount in plan:
        s.clock[0]=receipt+.1;write_submitted_heartbeat(s)
        tx=transfer(sender,recipient,signature=signature,slot=slot,block_time=int(receipt),amount=amount)
        push(s,tx,receipt);out=s.worker.tick();s.preparation_statuses.append(out)
        if on_funding:on_funding(s,tx,receipt,len(s.preparation_statuses))
    if restart:
        s.worker.close()
        s.worker=s.module.KnownGroupTail(s.ledger,capture_path=s.path,desk_dir=s.desk,watch_path=s.watch,clock=lambda:s.clock[0],now_iso_fn=lambda:C.iso(s.clock[0]))
    s.clock[0]=NOW+.1
    creation=C.creation();creation.update(slot=9,blockTime=int(NOW))
    push(s,creation,NOW)
    # Existing producers decode real synthetic PumpSwap logs and persist a parsed mint-account read.
    market,_,calls=build(s.desk,monkeypatch)
    assert len(calls)==1
    send(market,monkeypatch,number=45,slot=15,cap=40_000,received=NOW+3.69,processed=NOW+3.7)
    monkeypatch.setattr(W.time,'time',lambda:s.clock[0])
    from sniper.feeds.yellowstone import geyser_pb2 as pb
    for slot,at in [(15,NOW+3.8),(16,NOW+3.9)]:
        s.clock[0]=at;block=pb.SubscribeUpdate();block.block_meta.slot=slot;block.block_meta.block_time.timestamp=int(NOW+3)
        market.process(block.SerializeToString(),received_at=at)
    s.clock[0]=NOW+3.95;market.commit_files();market.heartbeat(True)
    s.clock[0]=NOW+4.1
    write_submitted_heartbeat(s,preserve_market=True)
    C.enable(s.k,s.ledger)
    s.market=market;s.net=net
    return s


def write_submitted_heartbeat(s,*,preserve_market=False):
    from sniper.office.wallet_selector_identity import selector_digest
    path=s.desk/'universe.heartbeat'
    beat=json.loads(path.read_text()) if preserve_market else {}
    wallets=json.loads(watch.watch_file(s.desk).read_text())['wallets']
    beat.update(ts=s.clock[0],connected=True,wallet_flow=dict(enabled=True,storage_initialized=True,last_commit_state='known',
        pending_gaps={},transaction_callback_errors=0,desired_selectors=len(wallets),submitted_selectors=len(wallets),
        submitted_at=s.clock[0],submitted_selector_digest=selector_digest(wallets),selector_acknowledged=None))
    path.write_text(json.dumps(beat))


def close_market(s):
    for conn in s.market.files._open.values():conn.close()


def test_native_worker_generates_causal_compact_proof_and_durable_funded_intent(source,monkeypatch):
    """Spec32-55,85-109,164-169: raw native source through real market/watch readers to enqueue."""
    s=healthy_market_and_source(source,monkeypatch)
    try:
        out=s.worker.tick()
        assert out['eligible']==1,out
        row=s.ledger.execute('SELECT intent_id FROM memecoin_manual_intents').fetchone()
        assert row
        saved=intents.read_status(s.ledger,row[0]);proof=saved['desk_metadata']['known_group_proof']
        assert saved['signal_source']=='desk' and saved['size_usd']==25 and saved['state']==intents.QUEUED
        assert (saved['tp_pct'],saved['sl_pct'],saved['trail_pct'])==(40.,30.,None)
        assert proof['version']=='known-group-launch-compact-v1'
        assert (proof['creation']['mint'],proof['creation']['developer'],proof['creation']['signature'])==(C.MINT,C.DEV,C.CREATE_SIG)
        assert proof['creation']['raw_ref'].startswith(str(s.path.resolve())+':2:')
        assert proof['creation']['received_at_host']==C.R and proof['entry_deadline_utc']==C.iso(C.R+5)
        assert proof['funding_path'][-1]['recipient']==C.DEV and proof['funding_path'][-1]['known_at_host']<C.R
        assert [member['address'] for member in proof['members']]==[C.DEV]
        assert proof['coverage']['complete'] is False and proof['coverage']['selector_acknowledged'] is None
        assert proof['coverage']['submitted_selector_digest']==json.loads((s.desk/'universe.heartbeat').read_text())['wallet_flow']['submitted_selector_digest']
        assert saved['desk_metadata']['verdict_price']['ref_price_usd']>0
    finally:close_market(s)


def test_prepared_retry_rechecks_actual_frozen_member_watch_and_submitted_export(source,monkeypatch):
    """Spec113-132: transient enqueue failure cannot retain authority after current observation removal."""
    s=healthy_market_and_source(source,monkeypatch)
    real_enqueue=s.k.enqueue_candidate;calls=[]
    def fail_once(*args,**kwargs):
        calls.append(kwargs)
        if len(calls)==1:raise ValueError('synthetic_queue_unavailable')
        return real_enqueue(*args,**kwargs)
    monkeypatch.setattr(s.k,'enqueue_candidate',fail_once)
    try:
        first=s.worker.tick()
        assert first['refusals']['synthetic_queue_unavailable']==1 and len(calls)==1
        _,prepared=state(s)
        assert 'prepared' in prepared['candidates'][C.CREATE_SIG]
        assert s.ledger.execute('SELECT count(*) FROM memecoin_manual_intents').fetchone()[0]==0
        assert watch.remove(s.net,s.desk,address=C.DEV,now=C.R+4.2)['state']=='removed'
        replacement=str(Pubkey.from_bytes(bytes([41])*32))
        assert watch.add(s.net,s.desk,address=replacement,operator_id='fixture',evidence='separate observation fixture',
            source='host',actor='fixture',now=C.R+4.2)['state']=='added'
        s.clock[0]=C.R+4.4;write_submitted_heartbeat(s,preserve_market=True)
        exported=json.loads(watch.watch_file(s.desk).read_text())['wallets']
        assert len(exported)==1 and C.DEV not in exported
        second=s.worker.tick()
        assert second['eligible']==0 and second['refusals']['known_group_member_observation_not_ready']==1,second
        assert len(calls)==1,'prepared retry reached enqueue despite the frozen member being removed'
        assert s.ledger.execute('SELECT count(*) FROM memecoin_manual_intents').fetchone()[0]==0
    finally:close_market(s)


def leave_selector_change_pending(s):
    other=str(Pubkey.from_bytes(bytes([42])*32))
    assert watch.add(s.net,s.desk,address=other,operator_id='fixture',evidence='temporary separate observation',
        source='host',actor='fixture',now=s.clock[0])['state']=='added'
    s.capture.wallet_flow_selectors()
    assert watch.remove(s.net,s.desk,address=other,now=s.clock[0])['state']=='removed'
    s.capture.wallet_flow_selectors()


@pytest.mark.parametrize('case',['selectors_only','ring_pending','ring_persisted','unknown_pending','unknown_persisted','wrong_digest','removed_member'])
def test_exact_selector_markers_remain_diagnostic_while_actual_losses_and_unready_members_refuse(source,monkeypatch,case):
    """Observed policy amendment: only exact selectors_changed is non-loss; every current member/digest gate stays real."""
    from sniper.persistence import db_memecoin as db
    s=healthy_market_and_source(source,monkeypatch)
    try:
        dropped=s.capture.stats['wallet_flow_dropped']
        assert tuple(s.capture.wallet_flow_selectors())==(C.DEV,)
        assert 'selectors_changed' in s.capture.wallet_flow_gaps
        assert s.capture.stats['wallet_flow_dropped']==dropped
        s.capture.wallet_flow_maintenance(s.clock[0]);s.capture.commit_files()
        leave_selector_change_pending(s)
        if case.startswith('ring') or case.startswith('unknown'):
            reason='ring_overflow' if case.startswith('ring') else 'selectors_changed_extra'
            s.capture.wallet_flow_gap(reason,s.clock[0])
            if case.endswith('persisted'):
                s.capture.wallet_flow_maintenance(s.clock[0]);s.capture.commit_files()
                leave_selector_change_pending(s)
        if case=='removed_member':
            assert watch.remove(s.net,s.desk,address=C.DEV,now=s.clock[0])['state']=='removed'
            replacement=str(Pubkey.from_bytes(bytes([43])*32))
            assert watch.add(s.net,s.desk,address=replacement,operator_id='fixture',evidence='different observation',
                source='host',actor='fixture',now=s.clock[0])['state']=='added'
            s.capture.wallet_flow_selectors()
        write_submitted_heartbeat(s,preserve_market=True)
        beat=json.loads((s.desk/'universe.heartbeat').read_text())
        beat['wallet_flow']['pending_gaps']=dict(s.capture.wallet_flow_gaps)
        if case=='wrong_digest':beat['wallet_flow']['submitted_selector_digest']='0'*64
        (s.desk/'universe.heartbeat').write_text(json.dumps(beat))
        page=db.read_known_group_raw_tail(s.path,cursor=0,pending={},anchor=None,limit=64,now_host=s.clock[0])
        diagnostic=dict(pending=beat['wallet_flow']['pending_gaps'].get('selectors_changed'),
            persisted=[gap for gap in page['gaps'] if gap['reason']=='selectors_changed'])
        assert diagnostic['pending'] and diagnostic['persisted']
        out=s.worker.tick()
        if case=='selectors_only':
            assert out['eligible']==1,out
            assert out['selector_changes']==diagnostic
            row=s.ledger.execute('SELECT desk_metadata_json FROM memecoin_manual_intents').fetchone()
            proof=json.loads(row[0])['known_group_proof']
            assert proof['coverage']['selector_changes']==diagnostic
            assert proof['coverage']['complete'] is False and proof['coverage']['selector_acknowledged'] is None
            assert [member['address'] for member in proof['members']]==[C.DEV]
        else:
            assert out['eligible']==0 and out['refusals'],out
            assert s.ledger.execute('SELECT count(*) FROM memecoin_manual_intents').fetchone()[0]==0
    finally:close_market(s)

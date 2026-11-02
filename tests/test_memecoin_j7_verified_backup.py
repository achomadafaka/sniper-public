import json
import collections
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_onchain_lane as O
from sniper.settings import memecoin as cfg
from test_memecoin_entry_identity import _conn
from scripts.research.onchain_wallet_watch import SCHEMA

REAL_VERDICT = O.slot_lane_verdict

SIG = 'jbbK6DttdLJrf2tPyKTBGu5Y9Rm8NtsdZmuDGVcqFJchVfhceXRBJpr8zFnMtK3pjjayKbjpxaj4jFU89Mh8U3s'
MINT = '6kLQP6SpZWY2281u8oWdKBPnTjgoAA6ZJSXZczt2gkwm'
OWNER = '6XpoayPqAE99XfCPARDig3CwU6Mg7pMdrTCCbqVidZMV'
NOW = '2026-10-11T10:53:46.037887+00:00'
UID = 'u1'

@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setattr(cfg, 'MEMECOIN_J7_SOLANA_VERIFIED_BACKUP', True, raising=False)
    c = _conn()
    c.executescript(SCHEMA)
    proof = dict(schema_version=1,proof_version='direct-wallet-paid-v1',network_id='solana',
                 tx_hash=SIG,block_number=455625228,mint=MINT,beneficiary=OWNER,payer=OWNER,
                 acquired_atomic='10009918792696',payment_atomic='486000000',source_user_id=UID)
    valuation = dict(source_initiation=proof,basis='route_owner_quote_out',source_size_usd=486,
                     source_price=.00004855,mint=MINT,owner=OWNER,source_slot=455625228)
    c.execute('INSERT INTO onchain_wallet_detections(signature,source_user_id,handle,address,mint,network_id,source_slot,source_size_usd,source_price,source_valuation_json,detected_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
              (SIG,UID,'wallet',OWNER,MINT,'solana',455625228,486,.00004855,json.dumps(valuation),NOW,NOW))
    c.execute('UPDATE onchain_wallet_detections SET block_time=?',
              (int(datetime.fromisoformat(NOW).timestamp()),))
    e = SimpleNamespace(side='buy',signal_source='follow_activity',delivery_lane='j7_wallets',
        network_id='solana',mint=MINT,source_user_id=UID,fomo_event_id=f'swap:raybot:{SIG}:{OWNER.lower()}:{MINT.lower()}',created_at=NOW)
    monkeypatch.setattr(O,'slot_lane_verdict',lambda *a,**k:(None,None))
    return c,e,valuation

def test_exact_verified_backup_uses_chain_price_and_canonical_identity(setup):
    from sniper.services.memecoin_j7_backup import prepare
    c,e,_=setup
    ev,why=prepare(c,e)
    assert why is None and ev.fomo_event_id=='onchain:'+SIG
    assert ev.their_price==.00004855 and ev.their_size_usd==486
    assert ev.j7_verified_backup and ev.delivery_lane=='j7_verified_backup'

@pytest.mark.parametrize('state',['in_flight','filled','decided','unreadable'])
def test_native_owner_or_policy_never_bypassed(setup,monkeypatch,state):
    from sniper.services.memecoin_j7_backup import prepare
    c,e,_=setup
    monkeypatch.setattr(O,'slot_lane_verdict',lambda *a,**k:(state,None))
    assert prepare(c,e)[1]=='j7_native_'+state

@pytest.mark.parametrize('change',[{'payer':'11111111111111111111111111111111'}, {'payment_atomic':'0'}, {'mint':'11111111111111111111111111111111'}, {'source_user_id':'other'}])
def test_wrong_proof_or_unsolicited_acquisition_refused(setup,change):
    from sniper.services.memecoin_j7_backup import prepare
    c,e,v=setup
    v['source_initiation'].update(change)
    c.execute('UPDATE onchain_wallet_detections SET source_valuation_json=?',(json.dumps(v),))
    assert prepare(c,e)[1]=='receipt_identity_mismatch'

def test_missing_proof_is_unknown_not_a_valid_backup(setup):
    from sniper.services.memecoin_j7_backup import prepare
    c,e,v=setup
    v['source_initiation']=None
    v['unknown_reason']='unsupported_buy_instruction'
    c.execute('UPDATE onchain_wallet_detections SET source_valuation_json=?',(json.dumps(v),))
    assert prepare(c,e)[1]=='missing_receipt_proof'

def test_opaque_id_never_matches_by_wallet_coin_time(setup):
    from sniper.services.memecoin_j7_backup import prepare
    c,e,_=setup
    e.fomo_event_id='swap:opaque-uuid'
    assert prepare(c,e)[1]=='j7_exact_source_unavailable'


@pytest.mark.parametrize('origin', ['swap:33ad450d-1e2a-4025-9ba8-dcf4a6a8baac',
                                  'swap:b9ce8f71-0e62-4134-acae-673f577eeb95'])
def test_uuid_j7_hint_has_no_receipt_lookup_or_trade_authority(setup, origin):
    from sniper.services.memecoin_j7_backup import prepare
    _, event, _ = setup
    event.fomo_event_id = origin
    unreadable = SimpleNamespace(execute=lambda *a: pytest.fail('opaque UUID triggered receipt lookup'))
    assert prepare(unreadable, event) == (None, 'j7_opaque_hint')


@pytest.mark.parametrize('origin,native_state', [
    ('swap:33ad450d-1e2a-4025-9ba8-dcf4a6a8baac', 'pending'),
    ('swap:b9ce8f71-0e62-4134-acae-673f577eeb95', 'filled'),
])
def test_uuid_hint_after_native_entry_is_advisory_through_notifications(setup, monkeypatch,
                                                                       origin, native_state):
    from sniper.persistence.db_memecoin_notifications import NotificationLifecycle
    from sniper.services import memecoin_real_runner as R
    from sniper.services.memecoin_entry_view import entry_view
    c, event, _ = _shyft_native(setup, monkeypatch)
    _native_attempt(c, pending='confirmed' if native_state == 'filled' else 'reserved',
                    terminal='filled' if native_state == 'filled' else None, signed=True)
    if native_state == 'filled':
        position = m.open_position(c, source_user_id=UID, source_handle='wallet', mint=MINT,
            ticker='TEST', entry_price=1, entry_notional=25, entry_fill_source='chain',
            entry_lag_s=0, their_size_usd=486, opened_at=NOW, execution_mode='real',
            network_id='solana', attempt_id='native', origin_event_id='slotlane:' + SIG)
        m.insert_fill(c, position_id=position, kind='entry', price=1, qty=25, notional=25,
            fill_source='chain', fraction=1, created_at=NOW, attempt_id='native',
            origin_event_id='slotlane:' + SIG, network_id='solana', tx_sig='our-native-signature')
    event.fomo_event_id = origin
    c.execute('INSERT INTO memecoin_events(fomo_event_id,source_user_id,mint,side,network_id,'
              'signal_source,delivery_lane,created_at,received_at,received_at_normalized) '
              'VALUES(?,?,?,?,?,?,?,?,?,?)',
              (origin, UID, MINT, 'buy', 'solana', 'follow_activity', 'j7_wallets', NOW, NOW, NOW))
    c.commit()
    hook = R.make_real_hook(None, {}, now_fn=lambda: NOW, trade_size=25, max_age_s=60,
        stats_fn=lambda *a, **k: pytest.fail('opaque UUID reached market reads'))
    assert hook(c, event) == {'status': 'hint_only', 'chain': 'solana', 'reason': 'j7_opaque_hint'}
    assert c.execute('SELECT COUNT(*) FROM memecoin_entry_attempts').fetchone()[0] == 1
    assert c.execute('SELECT status FROM memecoin_pending_entries').fetchone()[0] == (
        'confirmed' if native_state == 'filled' else 'reserved')
    projected = entry_view(c, origin_event_id=origin)
    assert projected['status'] == 'not_evaluated' and projected['attempt_id'] is None
    sent = []
    monkeypatch.setattr(cfg, 'MEMECOIN_TG_SOURCE_CARDS', False)
    monkeypatch.setattr(cfg, 'MEMECOIN_TG_SOURCE_HOLD_S', 0)
    lifecycle = NotificationLifecycle(c, read_view=lambda **kw: entry_view(c, **kw),
        send_fn=lambda text: sent.append(text) or (True, 1), render_fn=lambda view: view['status'],
        eligible_fn=lambda view: True, now_fn=lambda: NOW)
    lifecycle.activate('2026-10-11T10:53:00+00:00')
    lifecycle.observe(projected)
    lifecycle.deliver()
    assert sent == []


@pytest.mark.parametrize('change,reason', [
    ('malformed_uuid', 'j7_exact_source_unavailable'),
    ('malformed_exact', 'j7_exact_source_unavailable'),
    ('missing', 'j7_receipt_not_observed'),
    ('mismatch', 'receipt_identity_mismatch'),
    ('proof', 'missing_receipt_proof'),
    ('db', 'j7_receipt_state_unavailable'),
])
def test_nonadvisory_j7_failures_keep_refusal_projection(setup, monkeypatch, change, reason):
    from sniper.services import memecoin_real_runner as R
    from sniper.services.memecoin_entry_view import entry_view
    c, event, valuation = setup
    if change == 'malformed_uuid': event.fomo_event_id = 'swap:opaque-uuid'
    elif change == 'malformed_exact': event.fomo_event_id = f'swap:raybot:invalid:{OWNER.lower()}:{MINT.lower()}'
    elif change == 'missing': c.execute('DELETE FROM onchain_wallet_detections')
    elif change == 'mismatch': c.execute("UPDATE onchain_wallet_detections SET source_user_id='other'")
    elif change == 'proof':
        valuation['source_initiation'] = None
        c.execute('UPDATE onchain_wallet_detections SET source_valuation_json=?', (json.dumps(valuation),))
    elif change == 'db': c.execute('DROP TABLE onchain_wallet_detections')
    c.commit()
    hook = R.make_real_hook(None, {}, now_fn=lambda: NOW, trade_size=25, max_age_s=60,
        stats_fn=lambda *a, **k: pytest.fail('unverified J7 event reached market reads'))
    result = hook(c, event)
    assert result == {'status': 'source_initiation_unknown', 'chain': 'solana', 'reason': reason}
    attempt_id = c.execute('SELECT attempt_id FROM memecoin_entry_attempts').fetchone()[0]
    assert entry_view(c, attempt_id=attempt_id)['status'] == 'refused'


@pytest.mark.parametrize('field,value',[('mint','Other'),('source_user_id','other')])
def test_detection_identity_must_match(setup,field,value):
    from sniper.services.memecoin_j7_backup import prepare
    c,e,_=setup
    setattr(e,field,value)
    assert prepare(c,e)[1]=='receipt_identity_mismatch'

def test_missing_receipt_stays_unknown(setup):
    from sniper.services.memecoin_j7_backup import prepare
    c,e,_=setup
    c.execute('DELETE FROM onchain_wallet_detections')
    assert prepare(c,e)[1]=='j7_receipt_not_observed'

@pytest.mark.parametrize('bound',[False,True])
def test_j7_fast_only_refusal_does_not_consume_confirmed_proof_but_reservation_does(setup,bound):
    c,e,_=setup
    c.execute("INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,signal_source,network_id,mint,source_user_id,decision_started_at,terminal_status,terminal_reason,delivery_lane,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              ('j7',e.fomo_event_id,'follow_activity','solana',MINT,UID,NOW,'refused','native_fast_lane_unavailable','j7_wallets',NOW))
    if bound:
        m.reserve_pending(c,mint=MINT,source_user_id=UID,strategy='test',notional=25,
                          reserved_at=NOW,network_id='solana',attempt_id='j7')
    assert O._other_lane_attempt_state(c,SIG,NOW,source_user_id=UID)==('attempted' if bound else None)

@pytest.mark.parametrize('bound',[False,True])
def test_j7_native_wait_refusal_does_not_consume_confirmed_proof_but_reservation_does(setup,bound):
    # DexGemsReal 3kjkcUA5 (2026-10-11 21:09Z): J7 arrived while the slot lane's exotic leg was out and
    # was refused j7_native_in_flight, unsigned. The slot lane then ended missing_receipt_proof and the
    # on-chain lane skipped the proven receipt as duplicate_of_feed: nobody took the buy.
    c,e,_=setup
    c.execute("INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,signal_source,network_id,mint,source_user_id,decision_started_at,terminal_status,terminal_reason,delivery_lane,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              ('j7',e.fomo_event_id,'follow_activity','solana',MINT,UID,NOW,'rejected','j7_native_in_flight','j7_wallets',NOW))
    if bound:
        m.reserve_pending(c,mint=MINT,source_user_id=UID,strategy='test',notional=25,
                          reserved_at=NOW,network_id='solana',attempt_id='j7')
    assert O._other_lane_attempt_state(c,SIG,NOW,source_user_id=UID)==('attempted' if bound else None)

def test_proven_receipt_after_j7_native_wait_and_slot_missing_proof_reaches_the_backup(setup,monkeypatch):
    c,e,_=setup
    monkeypatch.setattr(cfg,'MEMECOIN_SOLANA_FAST_LANE_ONLY',True)
    monkeypatch.setattr(O,'slot_lane_verdict',lambda *a,**k:('failed',None))   # missing_receipt_proof
    c.execute("INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,signal_source,network_id,mint,source_user_id,decision_started_at,terminal_status,terminal_reason,delivery_lane,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              ('j7',e.fomo_event_id,'follow_activity','solana',MINT,UID,NOW,'rejected','j7_native_in_flight','j7_wallets',NOW))
    seen=[]
    def hook(conn,ev):
        seen.append(ev)
        return {'status':'test_entry'}
    _,hits=O.sweep_onchain_detections(c,hook,cursor=0,now_fn=lambda:NOW,max_age_s=45.,
                                      approved_ids_fn=lambda *a:{UID},deferred={})
    assert [h['result'] for h in hits]==[{'status':'test_entry'}]
    assert seen[0].j7_verified_backup and seen[0].delivery_lane=='j7_verified_backup'

def test_confirmed_delivery_after_j7_hint_is_marked_as_verified_backup(setup):
    c,e,_=setup
    c.execute('INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,source_user_id,network_id,mint,signal_source,delivery_lane,decision_started_at) VALUES(?,?,?,?,?,?,?,?)',
              ('j7-hint',e.fomo_event_id,UID,'solana',MINT,'follow_activity','j7_wallets',NOW))
    row=c.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    captured=[]
    O._run_hook(c,lambda c,ev: captured.append(ev) or {'status':'test'},row)
    assert captured[0].j7_verified_backup and captured[0].delivery_lane=='j7_verified_backup'

def test_retry_keeps_verified_backup_and_receipt_identity(setup):
    from sniper.services.memecoin_j7_backup import prepare
    c,e,_=setup
    ev,_=prepare(c,e)
    restored=O.event_from_retry_payload(O.retry_payload(ev))
    assert restored.j7_verified_backup and restored.source_initiation==ev.source_initiation

@pytest.mark.parametrize("revival", [False, True])
def test_real_hook_uses_verified_chain_event_and_existing_entry_gates(setup,monkeypatch,revival):
    from sniper.services import memecoin_real_runner as R
    from test_memecoin_entry_identity import _arm,_approve
    c,e,_=setup
    _arm(monkeypatch)
    _approve(c,UID,chains=('solana',))
    monkeypatch.setattr(cfg,'MEMECOIN_SOLANA_FAST_LANE_ONLY',True)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT',30)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP',0)
    monkeypatch.setattr(R,'_maybe_enqueue_retry',lambda *a,**k:None)
    calls=[]
    monkeypatch.setattr(R,'real_entry_hook',lambda c,ev,*a,**k:calls.append((ev,k)) or {'status':'test_entry'})
    ctx={'usdc_fn':lambda:300,'native_fn':lambda:1,'token_safety_fn':lambda *a,**k:(True,'ok'),'execute_fn':lambda *a,**k:None,'kill_path':None}
    h=R.make_real_hook(None,{'solana':ctx},now_fn=lambda:NOW,trade_size=25,max_age_s=60,
        stats_fn=lambda *a,**k:{'price':.00004855,'market_cap':50000,'liquidity':100000})
    if revival:
        from sniper.services.memecoin_j7_backup import prepare
        e,_=prepare(c,e)
        e.revival_reask=True
        monkeypatch.setattr(cfg,'revival_watch_window_s',lambda chain:300)
    res=h(c,e)
    assert res['status']=='test_entry',res
    assert calls[0][1]['max_age_s']==45
    assert calls[0][0].j7_verified_backup
    assert calls[0][0].fomo_event_id=='onchain:'+SIG
    assert calls[0][0].their_price==.00004855

def test_other_named_j7_venue_keeps_exact_identity_binding(setup):
    from sniper.services.memecoin_j7_backup import prepare
    c,e,_=setup
    e.fomo_event_id=e.fomo_event_id.replace('swap:raybot:','swap:other-terminal:')
    assert prepare(c,e)[1] is None

def test_backup_off_keeps_old_j7_duplicate_policy(setup,monkeypatch):
    c,e,_=setup
    monkeypatch.setattr(cfg,'MEMECOIN_J7_SOLANA_VERIFIED_BACKUP',False)
    c.execute("INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,signal_source,network_id,mint,source_user_id,decision_started_at,terminal_status,terminal_reason,delivery_lane,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
              ('j7',e.fomo_event_id,'follow_activity','solana',MINT,UID,NOW,'refused','native_fast_lane_unavailable','j7_wallets',NOW))
    assert O._other_lane_attempt_state(c,SIG,NOW,source_user_id=UID)=='attempted'

@pytest.mark.parametrize('trace',['bad json','{"submission_events":[{"status":"ambiguous"}]}'])
def test_unclear_submission_evidence_still_owns_decision(setup,trace):
    c,e,_=setup
    c.execute("INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,signal_source,network_id,mint,source_user_id,decision_started_at,terminal_status,terminal_reason,delivery_lane,completed_at,trace_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
              ('j7',e.fomo_event_id,'follow_activity','solana',MINT,UID,NOW,'refused','native_fast_lane_unavailable','j7_wallets',NOW,trace))
    assert O._other_lane_attempt_state(c,SIG,NOW,source_user_id=UID)=='attempted'

def test_proof_first_is_offered_to_native_resume_even_without_fallback(setup):
    c,e,_=setup
    row=c.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    offers=[]
    def hook(c,ev): return {'status':'ordinary'}
    hook.receipt_resume=lambda ev: offers.append(ev) or True
    assert O._run_hook(c,hook,row)=={'status':'deferred','reason':'slot_lane_receipt_resume'}
    assert offers[0].source_initiation

def test_sobdog_historical_receipt_now_has_buy_v2_signer_payment_proof():
    from pathlib import Path
    from sniper.services.memecoin_source_valuation import value_source_buy
    from sniper.services.memecoin_source_initiation import solana_receipt_initiation
    tx=json.loads((Path(__file__).parent/'fixtures/memecoin/j7_sobdog_confirmed_20261011.json').read_text())['result']
    owner='6xmMW5JPSEfeRuNdkxixHWsm4Sf57SdXybA3BdwZzrM1'
    facts=value_source_buy(tx,owner,sol_usd=(160,1791712452),now=1791712452)
    assert facts['instruction_name']=='buy_v2' and facts['source_size_usd']>0
    proof=solana_receipt_initiation(tx,owner,facts)
    # The wallet paid 3 USDC (32,662,226 -> 29,662,226 atoms) through a WSOL hop; 26,516,222 was the
    # hop's lamports, not the owner's payment (c9c57f2f8 binds USDC-funded Pump buys to the receipt).
    assert proof['payer']==owner and proof['payment_atomic']=='3000000'
    assert proof['route_payment_atomic']=='2900000' and facts['source_size_usd']==pytest.approx(2.9)
    assert proof['mint']=='D4cYhYCPDM7trS62B7sKTTKeWtCDtG5ECjCtMSthpump'

def test_real_j7_backup_never_goes_through_old_shadow_policy(setup,monkeypatch):
    from sniper.services import memecoin_real_runner as R
    c,e,_=setup
    monkeypatch.setattr(R,'j7_shadow_event',lambda *a:pytest.fail('approved verified backup was shadowed'))
    monkeypatch.setattr(R,'_record_real_decision',lambda *a,**k:None)
    monkeypatch.setattr(R,'_maybe_enqueue_retry',lambda *a,**k:None)
    # Make proof unknown: it must stop before quotes rather than disappear into a shadow copy.
    c.execute('DELETE FROM onchain_wallet_detections')
    h=R.make_real_hook(None,{},now_fn=lambda:NOW,trade_size=25,max_age_s=60)
    assert h(c,e)['reason']=='j7_receipt_not_observed'

@pytest.mark.parametrize('source_state',['in_flight','filled','decided','unreadable'])
def test_real_hook_cannot_quote_a_native_owned_backup(setup,monkeypatch,source_state):
    from sniper.services import memecoin_real_runner as R
    c,e,_=setup
    monkeypatch.setattr(O,'slot_lane_verdict',lambda *a,**k:(source_state,None))
    monkeypatch.setattr(R,'_maybe_enqueue_retry',lambda *a,**k:None)
    h=R.make_real_hook(None,{},now_fn=lambda:NOW,trade_size=25,max_age_s=60,
                       stats_fn=lambda *a,**k:pytest.fail('native-owned buy reached quotes'))
    assert h(c,e)['reason']=='j7_native_'+source_state

def test_malformed_legacy_terminal_row_never_looks_unsigned_and_nonowning(setup):
    c,e,_=setup
    c.execute('PRAGMA ignore_check_constraints=ON')
    c.execute("INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,signal_source,network_id,mint,source_user_id,decision_started_at,terminal_status,terminal_reason,delivery_lane) VALUES(?,?,?,?,?,?,?,?,?,?)",
              ('broken',e.fomo_event_id,'follow_activity','solana',MINT,UID,NOW,'refused',None,'j7_wallets'))
    assert O._other_lane_attempt_state(c,SIG,NOW,source_user_id=UID)=='attempted'

def test_processed_program_proof_cannot_authorize_confirmed_j7_backup(setup):
    from sniper.services.memecoin_j7_backup import prepare
    from sniper.services.memecoin_source_initiation import SOLANA_PROGRAM_PROOF_VERSION
    from sniper.services.memecoin_slot_signal import PUMP_AMM_PROGRAM
    c,e,v=setup
    v['source_initiation'].update(proof_version=SOLANA_PROGRAM_PROOF_VERSION,program_id=PUMP_AMM_PROGRAM)
    c.execute('UPDATE onchain_wallet_detections SET source_valuation_json=?',(json.dumps(v),))
    assert prepare(c,e)[1]=='j7_confirmed_receipt_required'

def test_cached_receipt_cannot_reset_onchain_freshness_or_start_quotes(setup,monkeypatch):
    from sniper.services import memecoin_real_runner as R
    c,e,_=setup
    monkeypatch.setattr(R,'_maybe_enqueue_retry',lambda *a,**k:None)
    h=R.make_real_hook(None,{},now_fn=lambda:'2026-10-11T10:54:32.037887+00:00',trade_size=25,max_age_s=900,
                       stats_fn=lambda *a,**k:pytest.fail('stale backup started a quote'))
    assert h(c,e)['status']=='stale'


@pytest.mark.parametrize('noted_sig,label_expected', [(SIG, False), ('other-signature', True)])
def test_shyft_backup_never_overturns_a_slot_lane_policy_rejection(setup, monkeypatch, noted_sig,
                                                                   label_expected):
    """73834 (2026-10-14): the slot lane refused 'mcap $280178 over ceiling $250000' at processed time;
    90 ms later the Shyft-confirmed backup re-decided the same source buy on a different price
    reference and filled it past MEMECOIN_SOLANA_FAST_LANE_ONLY. A rejection of THIS signature (the
    reachable fixture) is final; a rejection of another signature changes nothing."""
    from sniper.services import memecoin_slot_skips as skips
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setattr(skips, '_LAST', collections.OrderedDict())
    skips.note(noted_sig, 'rejected', 'mcap $280178 over ceiling $250000')
    conn, _, _ = setup
    seen = []
    row = conn.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    O._run_hook(conn, lambda c, ev: seen.append(ev) or {'status': 'test'}, row)
    assert seen[0].j7_verified_backup is label_expected
    assert (getattr(seen[0], 'delivery_lane', None) == 'j7_verified_backup') is label_expected


def test_runner_recheck_refuses_a_backup_the_slot_lane_rejected_after_labelling(setup, monkeypatch):
    """Gamma LOW on 05b043ed6: the slot lane can note 'rejected' AFTER _run_hook labelled the row;
    with no attempt row slot_lane_verdict still reads None, so the runner recheck must also read
    the in-memory decision before any quote."""
    from sniper.services import memecoin_real_runner as R
    from sniper.services import memecoin_slot_skips as skips
    c, e, _ = setup
    monkeypatch.setattr(skips, '_LAST', collections.OrderedDict())
    skips.note(SIG, 'rejected', 'mcap $290574 over ceiling $250000')
    monkeypatch.setattr(R, '_maybe_enqueue_retry', lambda *a, **k: None)
    h = R.make_real_hook(None, {}, now_fn=lambda: NOW, trade_size=25, max_age_s=60,
                         stats_fn=lambda *a, **k: pytest.fail('slot-lane-rejected buy reached quotes'))
    assert h(c, e)['reason'] == 'j7_native_rejected'


@pytest.mark.parametrize('first', ['onchain_fallback', 'j7'])
def test_a_multi_own_buy_source_reaches_both_paths_and_reserves_once(setup, monkeypatch, first):
    """Alpha 2026-10-14: slot_shape_unsupported admits the on-chain fallback, and the J7 recheck
    reads the same 'failed' verdict, so both paths may judge the one source buy. The coin
    reservation (partial UNIQUE(network_id, mint)) and the cross-lane attempt check keep it to one."""
    from sniper.services import memecoin_slot_skips as skips
    from sniper.services.memecoin_j7_backup import prepare
    c, e, _ = setup
    monkeypatch.setattr(O, 'slot_lane_verdict', REAL_VERDICT)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_FAST_LANE_ONLY', True)
    monkeypatch.setattr(skips, '_LAST', collections.OrderedDict())
    skips.note(SIG, 'rejected', 'multi_own_buy:pump_amm+pump_curve')
    proven, why = prepare(c, e)
    assert why is None and proven.j7_verified_backup             # J7 reads 'failed': allowed

    def reserve(attempt_id, origin, source):
        c.execute("INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,signal_source,network_id,"
                  "mint,source_user_id,decision_started_at) VALUES(?,?,?,?,?,?,?)",
                  (attempt_id, origin, source, 'solana', MINT, UID, NOW))
        return m.reserve_pending(c, mint=MINT, source_user_id=UID, strategy='test', notional=25,
                                 reserved_at=NOW, network_id='solana', attempt_id=attempt_id)

    seen = []

    def hook(conn, ev):
        seen.append(ev)
        return {'status': 'reserved' if reserve('onchain', ev.fomo_event_id, 'onchain_watch') else 'dup'}

    def sweep():
        return O.sweep_onchain_detections(c, hook, cursor=0, now_fn=lambda: NOW, max_age_s=45,
                                          approved_ids_fn=lambda conn, net: {UID}, deferred={})[1]

    if first == 'j7':
        assert reserve('j7', e.fomo_event_id, 'follow_activity') is not None
        assert [r['result'] for r in sweep()] == [{'status': 'skipped', 'reason': 'duplicate_of_feed'}]
        assert seen == []
    else:
        assert [r['result'] for r in sweep()] == [{'status': 'reserved'}]
        assert seen[0].native_failure_fallback and seen[0].slot_fallback_class == 'slot_shape_unsupported'
        assert reserve('j7', e.fomo_event_id, 'follow_activity') is None
    assert c.execute("SELECT COUNT(*) FROM memecoin_pending_entries").fetchone()[0] == 1


def _native_attempt(conn, *, pending='failed', reason='slot_lane_build:unsupported',
                    signed=False, terminal='failed'):
    conn.execute("INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,signal_source,"
                 "network_id,mint,source_user_id,decision_started_at,terminal_status,terminal_reason,completed_at) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?)",
                 ('native', 'slotlane:' + SIG, 'slot_lane', 'solana', MINT, UID, NOW, terminal,
                  reason if terminal is not None else None, NOW if terminal is not None else None))
    if pending is not None:
        pending_id = m.reserve_pending(conn, mint=MINT, source_user_id=UID, strategy='test',
            notional=25, reserved_at=NOW, network_id='solana', attempt_id='native',
            signal_source='slot_lane')
        if signed:
            m.set_pending_sig(conn, pending_id, 'our-native-signature', NOW)
        if pending == 'failed':
            assert m.release_pending(conn, pending_id, reason)
        elif pending == 'confirmed':
            conn.execute("UPDATE memecoin_pending_entries SET status='confirmed' WHERE id=?", (pending_id,))
    conn.commit()


def _shyft_native(setup, monkeypatch):
    from sniper.services import memecoin_slot_skips as skips
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', 'shyft')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_FAST_LANE_ONLY', True)
    monkeypatch.setattr(O, 'slot_lane_verdict', REAL_VERDICT)
    monkeypatch.setattr(skips, '_LAST', collections.OrderedDict())
    return setup


@pytest.mark.parametrize('strict_scan', [False, True])
@pytest.mark.parametrize('reason', ['cached_price_beyond_limit', 'kill_switch', 'not_approved',
                                    'mcap $280178 over ceiling $250000', 'source buy $10 below floor $20'])
def test_released_unsigned_native_policy_blocks_same_source_confirmed_backup(setup, monkeypatch, strict_scan, reason):
    """5581/5582: native 8e5128bb aborted cached_price_beyond_limit, then 0945a4a5 filled.
    Reproduce its durable unsigned release and aborted note with the existing confirmed-proof fixture.
    The captured exact 4XUX source/attempt identities are retained in the private incident evidence.
    """
    from sniper.services import memecoin_slot_skips as skips
    from sniper.services.memecoin_j7_backup import native_refusal
    c, _, valuation = _shyft_native(setup, monkeypatch)
    valuation['j7_native_failure_backup'] = strict_scan
    c.execute('UPDATE onchain_wallet_detections SET source_valuation_json=?', (json.dumps(valuation),))
    _native_attempt(c, reason=reason, terminal='aborted')
    skips.note(SIG, 'aborted', reason)  # This is the captured status, not an invented rejected note.
    row = c.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    assert REAL_VERDICT(c, SIG, row['detected_at']) == ('decided', None)
    assert native_refusal(c, O.OnchainEv(row)) == 'j7_native_decided'
    result = O._route(c, lambda *a: pytest.fail('native policy veto reached ordinary entry'), row, {})
    assert result == {'status': 'skipped', 'reason': 'duplicate_of_slot_lane'}


def test_cached_price_policy_is_classified_at_the_existing_shared_boundary():
    from sniper.services.memecoin_slot_lane import policy_refusal
    assert policy_refusal('cached_price_beyond_limit')


def test_unsigned_native_policy_does_not_own_a_different_source_signature(setup, monkeypatch):
    from sniper.services import memecoin_slot_skips as skips
    c, _, _ = _shyft_native(setup, monkeypatch)
    _native_attempt(c, reason='cached_price_beyond_limit', terminal='aborted')
    c.execute("UPDATE memecoin_entry_attempts SET origin_event_id='slotlane:another-source'")
    skips.note('another-source', 'aborted', 'cached_price_beyond_limit')
    row = c.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    seen = []
    assert O._route(c, lambda c, ev: seen.append(ev) or {'status': 'independent'}, row, {}) == {
        'status': 'independent'}
    assert seen[0].j7_verified_backup and seen[0].fomo_event_id == 'onchain:' + SIG


@pytest.mark.parametrize('reason,signed,klass', [
    ('slot_lane_build:unsupported', False, 'build_refused'),
    ('chain_error:InstructionError', True, 'landed_fail'),
    ('unrecognised_mechanical_failure', False, None),
])
def test_unsigned_policy_classification_preserves_existing_mechanical_results(setup, monkeypatch, reason, signed, klass):
    c, _, _ = _shyft_native(setup, monkeypatch)
    _native_attempt(c, reason=reason, signed=signed)
    row = c.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    assert REAL_VERDICT(c, SIG, row['detected_at']) == ('failed', klass)
    seen = []
    assert O._route(c, lambda c, ev: seen.append(ev) or {'status': 'mechanical'}, row, {}) == {'status': 'mechanical'}
    assert seen[0].slot_fallback_class == klass


@pytest.mark.parametrize('pending,signed,expected', [
    ('reserved', False, 'in_flight'), ('reserved', True, 'in_flight'),
    ('confirmed', True, 'filled'), ('failed', True, 'failed'),
])
def test_unsigned_policy_classification_preserves_native_ownership(setup, monkeypatch, pending, signed, expected):
    c, _, _ = _shyft_native(setup, monkeypatch)
    _native_attempt(c, pending=pending, reason='cached_price_beyond_limit', signed=signed)
    assert REAL_VERDICT(c, SIG, NOW) == (expected, None)


@pytest.mark.parametrize('uid', ['account-one-wallet', 'account-two-wallet'])
@pytest.mark.parametrize('lane_name', ['j7_wallets', 'j7_pump'])
@pytest.mark.parametrize('reason,signed,klass', [
    ('slot_lane_build:unsupported', False, 'build_refused'),
    ('chain_error:InstructionError', True, 'landed_fail'),
])
def test_shyft_exact_j7_from_both_accounts_reaches_existing_gates_after_native_failure(
        setup, monkeypatch, uid, lane_name, reason, signed, klass):
    from sniper.services import memecoin_real_runner as R
    from test_memecoin_entry_identity import _arm, _approve
    c, e, valuation = _shyft_native(setup, monkeypatch)
    e.source_user_id = uid
    e.delivery_lane = lane_name
    if lane_name == 'j7_pump':
        e.fomo_event_id = f'pump:{SIG}:{OWNER}:buy:{MINT}'
    valuation['source_initiation']['source_user_id'] = uid
    c.execute('UPDATE onchain_wallet_detections SET source_user_id=?,source_valuation_json=?',
              (uid, json.dumps(valuation)))
    _native_attempt(c, reason=reason, signed=signed)
    _arm(monkeypatch)
    m.add_wallet(c, uid, 'wallet')
    assert m.set_wallet_real_approved(c, uid, True, chains=['solana'], force=True,
        now_iso='2026-10-11T10:53:45+00:00')['ok']
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 30)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP', 0)
    monkeypatch.setattr(R, '_maybe_enqueue_retry', lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(R, 'real_entry_hook',
                        lambda conn, ev, *a, **k: seen.append((ev, k)) or {'status': 'test_entry'})
    ctx = {'usdc_fn': lambda: 300, 'native_fn': lambda: 1,
           'token_safety_fn': lambda *a, **k: (True, 'ok'),
           'execute_fn': lambda *a, **k: pytest.fail('test sent a trade'), 'kill_path': None}
    hook = R.make_real_hook(None, {'solana': ctx}, now_fn=lambda: NOW, trade_size=25,
        max_age_s=60, stats_fn=lambda *a, **k:
        {'price': .00004855, 'market_cap': 50000, 'liquidity': 100000})
    result = hook(c, e)
    assert result['status'] == 'test_entry', result
    proven, kwargs = seen[0]
    assert proven.source_user_id == uid and proven.fomo_event_id == 'onchain:' + SIG
    assert proven.source_initiation == valuation['source_initiation']
    assert proven.j7_verified_backup and proven.slot_fallback_class == klass
    assert kwargs['max_age_s'] == 45


@pytest.mark.parametrize('case,expected', [
    ('absent', 'j7_native_no_mechanical_failure'),
    ('pending', 'j7_native_in_flight'),
    ('ambiguous', 'j7_native_in_flight'),
    ('filled', 'j7_native_filled'),
    ('policy', 'j7_native_decided'),
    ('proof', 'j7_native_no_mechanical_failure'),
    ('disabled_class', 'j7_native_no_mechanical_failure'),
    ('later_policy', 'j7_native_rejected'),
])
def test_shyft_j7_never_quotes_without_released_mechanical_authority(setup, monkeypatch, case, expected):
    from sniper.services import memecoin_real_runner as R, memecoin_slot_skips as skips
    c, e, _ = _shyft_native(setup, monkeypatch)
    if case in ('pending', 'ambiguous', 'filled'):
        _native_attempt(c, pending='confirmed' if case == 'filled' else 'reserved',
                        signed=case != 'pending', terminal=None)
    elif case in ('policy', 'proof'):
        _native_attempt(c, pending=None, terminal='source_initiation_unknown' if case == 'proof' else 'rejected',
                        reason='missing_receipt_proof' if case == 'proof' else 'mcap over ceiling')
    elif case in ('disabled_class', 'later_policy'):
        _native_attempt(c)
        if case == 'disabled_class':
            monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_FALLBACK_CLASSES', frozenset({'landed_fail'}))
        else:
            skips.note(SIG, 'rejected', 'mcap over ceiling')
    monkeypatch.setattr(R, '_maybe_enqueue_retry', lambda *a, **k: None)
    hook = R.make_real_hook(None, {}, now_fn=lambda: NOW, trade_size=25, max_age_s=60,
        stats_fn=lambda *a, **k: pytest.fail('unowned fallback reached market reads'))
    result = hook(c, e)
    assert result['reason'] == expected, result


@pytest.mark.parametrize('replacement', ['policy', 'pending', 'filled', 'disabled_class'])
def test_shyft_verified_retry_rechecks_native_authority_after_labelling(setup, monkeypatch, replacement):
    from sniper.services.memecoin_j7_backup import prepare
    from sniper.services import memecoin_real_runner as R, memecoin_slot_skips as skips
    c, e, _ = _shyft_native(setup, monkeypatch)
    _native_attempt(c)
    proven, why = prepare(c, e)
    assert why is None
    retry = O.event_from_retry_payload(O.retry_payload(proven))
    if replacement == 'policy':
        skips.note(SIG, 'rejected', 'mcap over ceiling')
    elif replacement == 'disabled_class':
        monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_FALLBACK_CLASSES', frozenset({'landed_fail'}))
    else:
        c.execute('UPDATE memecoin_pending_entries SET status=?',
                  ('confirmed' if replacement == 'filled' else 'reserved',))
    monkeypatch.setattr(R, '_maybe_enqueue_retry', lambda *a, **k: None)
    hook = R.make_real_hook(None, {}, now_fn=lambda: NOW, trade_size=25, max_age_s=60,
        stats_fn=lambda *a, **k: pytest.fail('revoked fallback retry reached market reads'))
    expected = {'policy': 'rejected', 'pending': 'in_flight', 'filled': 'filled',
                'disabled_class': 'no_mechanical_failure'}[replacement]
    assert hook.attempt(c, retry)['reason'] == 'j7_native_' + expected


def test_exact_pump_hint_needs_same_wallet_mint_and_uid(setup):
    from sniper.services.memecoin_j7_backup import prepare, is_j7_buy, has_exact_hint
    c, e, _ = setup
    e.delivery_lane = 'j7_pump'
    e.fomo_event_id = f'pump:{SIG}:{OWNER}:buy:{MINT}'
    assert is_j7_buy(e)
    proven, why = prepare(c, e)
    assert why is None
    c.execute('INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,source_user_id,network_id,'
              'mint,signal_source,delivery_lane,decision_started_at) VALUES(?,?,?,?,?,?,?,?)',
              ('pump-hint', e.fomo_event_id, UID, 'solana', MINT, 'follow_activity', 'j7_pump', NOW))
    assert has_exact_hint(c, proven)
    e.fomo_event_id = f'pump:{SIG}:{OWNER.lower()}:buy:{MINT}'
    assert prepare(c, e)[1] == 'receipt_identity_mismatch'
    proven.source_user_id = 'other'
    assert not has_exact_hint(c, proven)


@pytest.mark.parametrize('bound', [False, True])
@pytest.mark.parametrize('terminal,reason', [
    ('source_initiation_unknown', 'j7_receipt_not_observed'),
    ('refused', 'j7_receipt_not_observed'),  # persisted trace status from the real hook
    ('rejected', 'j7_native_in_flight'),
])
@pytest.mark.parametrize('delivery_lane', ['j7_pump', 'j7_wallets'])
def test_unsigned_pump_hint_refusal_does_not_consume_later_confirmed_receipt(
        setup, bound, terminal, reason, delivery_lane):
    c, e, _ = setup
    origin = (f'pump:{SIG}:{OWNER}:buy:{MINT}' if delivery_lane == 'j7_pump'
              else f'swap:raybot:{SIG}:{OWNER.lower()}:{MINT.lower()}')
    c.execute('INSERT INTO memecoin_entry_attempts(attempt_id,origin_event_id,source_user_id,network_id,'
              'mint,signal_source,delivery_lane,decision_started_at,terminal_status,terminal_reason,'
              'completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
              ('pump-hint', origin, UID, 'solana', MINT, 'follow_activity', delivery_lane, NOW,
               terminal, reason, NOW))
    if bound:
        assert m.reserve_pending(c, mint=MINT, source_user_id=UID, strategy='test', notional=25,
                                 reserved_at=NOW, network_id='solana', attempt_id='pump-hint')
    assert O._other_lane_attempt_state(c, SIG, NOW, source_user_id=UID) == ('attempted' if bound else None)



def test_shyft_shape_release_is_mechanical_but_later_policy_wins(setup, monkeypatch):
    from sniper.services.memecoin_j7_backup import prepare
    from sniper.services import memecoin_slot_skips as skips
    c, e, _ = _shyft_native(setup, monkeypatch)
    skips.note(SIG, 'rejected', 'multi_own_buy:pump_amm+pump_curve')
    proven, why = prepare(c, e)
    assert why is None and proven.slot_fallback_class == 'slot_shape_unsupported'
    skips.note(SIG, 'rejected', 'mcap over ceiling')
    assert prepare(c, e)[1] == 'j7_native_rejected'


@pytest.mark.parametrize('origin', ['swap:opaque-uuid', f'swap:raybot:invalid:{OWNER.lower()}:{MINT.lower()}'])
def test_shyft_opaque_or_invalid_hint_cannot_use_a_mechanical_native_failure(setup, monkeypatch, origin):
    from sniper.services import memecoin_real_runner as R
    c, e, _ = _shyft_native(setup, monkeypatch)
    _native_attempt(c)
    e.fomo_event_id = origin
    monkeypatch.setattr(R, '_maybe_enqueue_retry', lambda *a, **k: None)
    hook = R.make_real_hook(None, {}, now_fn=lambda: NOW, trade_size=25, max_age_s=60,
        stats_fn=lambda *a, **k: pytest.fail('opaque J7 hint reached market reads'))
    assert hook(c, e)['reason'] == 'j7_exact_source_unavailable'


def test_shyft_mechanical_j7_fallback_keeps_wallet_approval_gate(setup, monkeypatch):
    from sniper.services import memecoin_real_runner as R
    c, e, _ = _shyft_native(setup, monkeypatch)
    _native_attempt(c)
    m.add_wallet(c, UID, 'wallet')
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_ROSTER_GATE_ENABLED', True)
    monkeypatch.setattr(R, '_maybe_enqueue_retry', lambda *a, **k: None)
    hook = R.make_real_hook(None, {'solana': {}}, now_fn=lambda: NOW, trade_size=25, max_age_s=60,
        stats_fn=lambda *a, **k: pytest.fail('unapproved fallback reached market reads'))
    assert hook(c, e)['status'] == 'not_approved'



def test_new_j7_fallback_does_not_trust_a_stamped_restart_class(setup, monkeypatch):
    from sniper.services.memecoin_j7_backup import native_refusal
    c, e, _ = _shyft_native(setup, monkeypatch)
    row = c.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    proven = O.OnchainEv(row)
    proven.j7_native_failure_backup = True
    proven.slot_fallback_class = 'lane_down'
    proven.native_failure_fallback = True
    assert native_refusal(c, proven) == 'j7_native_no_mechanical_failure'


@pytest.mark.parametrize('later_policy', [False, True])
def test_scan_discovery_native_guard_applies_even_without_auto_backup_label(setup, monkeypatch, later_policy):
    from sniper.services import memecoin_real_runner as R, memecoin_slot_skips as skips
    c, e, valuation = _shyft_native(setup, monkeypatch)
    valuation.update(backfill=True, j7_native_failure_backup=True)
    from datetime import datetime
    c.execute('UPDATE onchain_wallet_detections SET source_valuation_json=?,block_time=?',
              (json.dumps(valuation), int(datetime.fromisoformat(NOW).timestamp()) - 1))
    if later_policy:
        _native_attempt(c)
        skips.note(SIG, 'rejected', 'mcap over ceiling')
    monkeypatch.setattr(R, '_maybe_enqueue_retry', lambda *a, **k: None)
    hook = R.make_real_hook(None, {}, now_fn=lambda: NOW, trade_size=25, max_age_s=60,
        stats_fn=lambda *a, **k: pytest.fail('scan discovery bypassed native precedence'))
    row = c.execute('SELECT * FROM onchain_wallet_detections').fetchone()
    expected = 'j7_native_rejected' if later_policy else 'j7_native_no_mechanical_failure'
    assert O._run_hook(c, hook, row)['reason'] == expected


@pytest.mark.parametrize('scanned', [False, True])
@pytest.mark.parametrize('source_before_arm', [False, True])
def test_new_j7_fallback_preserves_per_chain_arm_boundary(setup, monkeypatch, scanned, source_before_arm):
    from sniper.services import memecoin_real_runner as R
    from test_memecoin_entry_identity import _arm
    c, e, valuation = _shyft_native(setup, monkeypatch)
    _native_attempt(c)
    _arm(monkeypatch)
    m.add_wallet(c, UID, 'wallet')
    arm_at = '2026-10-11T10:53:47+00:00' if source_before_arm else '2026-10-11T10:53:45+00:00'
    assert m.set_wallet_real_approved(c, UID, True, chains=['solana'], force=True, now_iso=arm_at)['ok']
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 30)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_CONSENSUS_ABOVE_MCAP', 0)
    monkeypatch.setattr(R, '_maybe_enqueue_retry', lambda *a, **k: None)
    monkeypatch.setattr(R, '_alert_entry_result', lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(R, 'real_entry_hook',
                        lambda conn, ev, *a, **k: seen.append(ev) or {'status': 'test_entry'})
    ctx = {'usdc_fn': lambda: 300, 'native_fn': lambda: 1,
           'token_safety_fn': lambda *a, **k: (True, 'ok'),
           'execute_fn': lambda *a, **k: pytest.fail('test sent a trade'), 'kill_path': None}
    hook = R.make_real_hook(None, {'solana': ctx}, now_fn=lambda: NOW, trade_size=25,
        max_age_s=60, stats_fn=lambda *a, **k:
        {'price': .00004855, 'market_cap': 50000, 'liquidity': 100000})
    if scanned:
        valuation.update(backfill=True, j7_native_failure_backup=True)
        from datetime import datetime
        c.execute('UPDATE onchain_wallet_detections SET source_valuation_json=?,block_time=?',
                  (json.dumps(valuation), int(datetime.fromisoformat(NOW).timestamp())))
        row = c.execute('SELECT * FROM onchain_wallet_detections').fetchone()
        result = O._run_hook(c, hook, row, slot_class='build_refused')
    else:
        result = hook(c, e)
    assert result['status'] == ('pre_arm_backlog' if source_before_arm else 'test_entry'), result
    assert bool(seen) is not source_before_arm



@pytest.mark.parametrize('age_s,expected', [(2, 'pre_arm_backlog'), (46, 'stale')])
def test_exact_j7_cannot_reage_an_old_source_from_fresh_receipt_arrival(setup, monkeypatch, age_s, expected):
    from sniper.services import memecoin_real_runner as R
    from test_memecoin_entry_identity import _arm
    c, e, _ = _shyft_native(setup, monkeypatch)
    _native_attempt(c)
    source_time = int(datetime.fromisoformat(NOW).timestamp()) - age_s
    c.execute('UPDATE onchain_wallet_detections SET block_time=?', (source_time,))
    e.created_at = datetime.fromtimestamp(source_time, timezone.utc).isoformat()
    _arm(monkeypatch)
    m.add_wallet(c, UID, 'wallet')
    arm_at = '2026-10-11T10:53:45+00:00' if age_s == 2 else '2026-10-11T10:52:00+00:00'
    assert m.set_wallet_real_approved(c, UID, True, chains=['solana'], force=True, now_iso=arm_at)['ok']
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 30)
    monkeypatch.setattr(R, '_maybe_enqueue_retry', lambda *a, **k: None)
    monkeypatch.setattr(R, '_alert_entry_result', lambda *a, **k: None)
    hook = R.make_real_hook(None, {'solana': {}}, now_fn=lambda: NOW, trade_size=25, max_age_s=60,
        stats_fn=lambda *a, **k: pytest.fail('pre-arm or stale exact J7 source reached market reads'))
    assert hook(c, e)['status'] == expected
    assert c.execute('SELECT detected_at FROM onchain_wallet_detections').fetchone()[0] == NOW


@pytest.mark.parametrize('block_time', [None, -1, 0, 1.5, 'invalid',
    int(datetime.fromisoformat(NOW).timestamp()) + 1, 253402300800])
def test_exact_shyft_hint_requires_valid_nonfuture_confirmed_block_time(setup, monkeypatch, block_time):
    from sniper.services.memecoin_j7_backup import prepare
    c, e, _ = _shyft_native(setup, monkeypatch)
    _native_attempt(c)
    c.execute('UPDATE onchain_wallet_detections SET block_time=?', (block_time,))
    assert prepare(c, e)[1] == 'j7_source_time_unavailable'

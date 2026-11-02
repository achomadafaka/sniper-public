"""Acceptance contract: 2026-11-01-known-group-funded-canary-design.md.

Public service seam agreed with implementer; all ledgers/receipts are synthetic.
Test -> spec map is in each test's docstring. No provider or signer is used.
"""
import copy
import hashlib
import importlib
import json
import sqlite3
import struct
from datetime import datetime, timezone

import base58
import pytest
from solders.pubkey import Pubkey

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_manual_intents as intents
from sniper.services import memecoin_manual_exit_intents as exits
from sniper.settings import memecoin as cfg

R = 1793534400.0
REGISTRY = 'eb81b2da4c235e8b8396ef5abb985d0c647d40776ba21c52d175dc546f899ba4'
PUMP = '6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P'
SYSTEM = '11111111111111111111111111111111'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
H, DEV, MINT, POOL, OTHER = [str(Pubkey.from_bytes(bytes([n])*32)) for n in (1,2,3,4,5)]
MINT_ACCOUNT = str(Pubkey.from_bytes(bytes([6])*32))
CREATE_SIG = base58.b58encode(bytes([7])*64).decode()
FUND_SIG = base58.b58encode(bytes([8])*64).decode()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def service(monkeypatch):
    k = importlib.import_module('sniper.services.memecoin_known_group_canary')
    monkeypatch.setattr(k, 'APPROVED_ROOTS', frozenset({H}))
    return k


def creation():
    curve = str(Pubkey.find_program_address([b'bonding-curve', bytes(Pubkey.from_string(MINT))], Pubkey.from_string(PUMP))[0])
    accounts = [str(Pubkey.from_bytes(bytes([n])*32)) for n in range(10,26)]
    accounts[0], accounts[2], accounts[5], accounts[-1] = MINT, curve, DEV, PUMP
    data = hashlib.sha256(b'global:create_v2').digest()[:8] + b''.join(struct.pack('<I',len(s))+s for s in (b'n',b's',b'u')) + bytes(Pubkey.from_string(OTHER)) + b'\0\0'
    keys = [DEV,MINT] + [a for a in accounts if a not in (DEV,MINT,PUMP)] + [MINT_ACCOUNT] + [PUMP,TOKEN]
    return dict(slot=100, transactionIndex=1, blockTime=int(R)-1, meta=dict(err=None), transaction=dict(
        signatures=[CREATE_SIG, base58.b58encode(bytes([9])*64).decode()], message=dict(
            accountKeys=[dict(pubkey=a,signer=a in (DEV,MINT),writable=a not in (PUMP,TOKEN)) for a in keys],
            instructions=[dict(programId=PUMP,accounts=accounts,data=base58.b58encode(data).decode()),
                dict(program='spl-token',programId=TOKEN,accounts=[MINT,MINT_ACCOUNT,DEV],
                    data=base58.b58encode(struct.pack('<BQB',14,600_000_000_000_000,6)).decode(),
                    parsed=dict(type='mintToChecked',info=dict(mint=MINT,account=MINT_ACCOUNT,mintAuthority=DEV,
                        tokenAmount=dict(amount='600000000000000',decimals=6))))])))


def funding():
    return dict(slot=99, transactionIndex=1, blockTime=int(R)-10, meta=dict(err=None, preBalances=[20_005_000,0],postBalances=[10_000_000,10_000_000]),
        transaction=dict(signatures=[FUND_SIG],message=dict(accountKeys=[dict(pubkey=H,signer=True),dict(pubkey=DEV,signer=False)],instructions=[
            dict(program='system',programId=SYSTEM,parsed=dict(type='transfer',info=dict(source=H,destination=DEV,lamports=10_000_000)))])))


def proof():
    return dict(version='known-group-launch-proof-v1',cohort_id='known-group-launch-v1',
        registry=dict(version='GRP-001-v1',sha256=REGISTRY,root=H,approved_at_host=R-100,source='host'),
        creation=dict(transaction=creation(),commitment='confirmed',source='raw_confirmed_tail',flow_id=1,raw_ref='capture:1',
            received_at_host=R,received_at_iso=iso(R),decoder_ready_at_host=R+.1,committed_at_host=R+.2,slot=100,tx_index=1,decimals=6),
        funding_path=[dict(source='host',proof_state='verified',proof_kind='research_stream',method='wallet-watch-stream-v1',proof_ref='fund:1',
            known_at_host=R-1,transaction=funding(),commitment='confirmed',slot=99,tx_index=1)],
        members=[dict(address=DEV,role='developer',proof_ref='capture:1',known_at_host=R+.1)],
        tradable=dict(venue='pumpswap',pool=POOL,price_usd=.0001,observed_at_host=R+.2,sol_usd=150.,sol_age_s=0.,
            pool_state=dict(base_reserve_atomic='300000000000000',quote_reserve_atomic='200000000000',
                virtual_quote_atomic='0',supply_atomic='600000000000000',quote_mint='So11111111111111111111111111111111111111112',
                base_decimals=6,lp_fee_bps=20,protocol_fee_bps=5,creator_fee_bps=5,slot=101,metadata_slot=100,cut_slot=102,
                metadata_known_at=R+.1,reserve_known_at=R+.2,virtual_quote_known_at=R+.2,
                virtual_quote_slot=101,virtual_quote_source='confirmed_pool_account',metadata_source='confirmed_mint_to')),
        coverage=dict(state='observed',connected=True,gap=False,overflow=False,history_basis='observed_since_tail',complete=False),prior_sells=[])


@pytest.fixture
def ledger():
    c=sqlite3.connect(':memory:'); c.row_factory=sqlite3.Row
    m.ensure_memecoin_tables(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def knobs(monkeypatch):
    for name,value in dict(MEMECOIN_DESK_ENABLED=False,MEMECOIN_LARGE_BUYER_ENABLED=False,
        MEMECOIN_DESK_CHAINS=frozenset({'solana'}),MEMECOIN_DESK_CLIP_USD=25.,MEMECOIN_DESK_MAX_CONCURRENT=5,
        MEMECOIN_DESK_DAILY_LOSS_USD=200.,MEMECOIN_DESK_RAIL_BOUNDS={'tp_pct':(15,150),'sl_pct':(10,40),'trail_pct':(8,30)}).items():
        monkeypatch.setattr(cfg,name,value)


def enable(k,c):
    cohort=k.register(c,now_iso=iso(R))
    digest=c.execute('SELECT policy_hash FROM memecoin_desk_cohorts WHERE cohort_id=?',(cohort,)).fetchone()[0]
    k.set_admission(c,enabled=True,confirm=True,policy_hash=digest,now_iso=iso(R))
    return cohort,digest


def record(k,c,p):
    k.record_candidate(c,p,now_host=R+.5,now_iso=iso(R+.5))


def enqueue(k,c,p):
    record(k,c,p)
    return k.enqueue_candidate(c,p,now_host=R+.5,now_iso=iso(R+.5))



def canonical_hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def assert_compact_proof(compact,p,*,decision_cut):
    assert set(compact)=={'version','cohort_id','registry','creation','funding_path','members','tradable','coverage',
        'full_source_sha256','decision_cut_host','entry_deadline_utc','service_catalogue'}
    assert compact['version']=='known-group-launch-compact-v1'
    from sniper.services.memecoin_known_group_canary import _known_services
    catalogue=_known_services()
    assert compact['service_catalogue']=={key:catalogue[key] for key in ('version','sha256','known_funders_version','known_funders_sha256')}
    assert compact['service_catalogue']['version']=='known-group-services-v1'
    assert compact['service_catalogue']['sha256']=='67c789b1242bc56b1470a8fbb8bea28403fb39d1c59833772e79bb4dcc18dd51'
    assert compact['cohort_id']==p['cohort_id'] and compact['registry']==p['registry']
    assert compact['full_source_sha256']==canonical_hash(p)
    assert compact['decision_cut_host']==decision_cut
    assert compact['entry_deadline_utc']==iso(datetime.fromisoformat(p['creation']['received_at_iso']).timestamp()+5)
    raw=p['creation'];tx=raw['transaction'];fact=compact['creation']
    for key in ('source','flow_id','raw_ref','slot','tx_index','decimals','received_at_host','received_at_iso',
                'decoder_ready_at_host','committed_at_host'):
        assert fact[key]==raw[key]
    assert (fact['mint'],fact['developer'],fact['signature'],fact['block_time'],fact['outer_ix'])==(MINT,DEV,tx['transaction']['signatures'][0],tx['blockTime'],0)
    assert fact['curve']==tx['transaction']['message']['instructions'][0]['accounts'][2]
    assert fact['raw_sha256']==canonical_hash(tx) and fact['raw_hash_kind']=='rpc_json_sha256'
    assert len(compact['funding_path'])==len(p['funding_path'])
    for actual,hop in zip(compact['funding_path'],p['funding_path']):
        payment=hop['transaction'];transfer=payment['transaction']['message']['instructions'][0]['parsed']['info']
        assert (actual['funder'],actual['recipient'],actual['amount_atomic'])==(transfer['source'],transfer['destination'],str(transfer['lamports']))
        assert actual['signature']==payment['transaction']['signatures'][0] and actual['block_time']==payment['blockTime']
        for key in ('slot','tx_index','known_at_host','proof_ref','method'):assert actual[key]==hop[key]
        assert actual['raw_sha256']==canonical_hash(payment) and actual['raw_hash_kind']=='rpc_json_sha256'
    assert compact['members']==[{**member,'raw_sha256':fact['raw_sha256'],'raw_hash_kind':'rpc_json_sha256'} for member in p['members']]
    assert compact['coverage']==p['coverage'] and compact['tradable']==p['tradable']
    assert '"transaction":' not in json.dumps(compact,separators=(',',':'))


def test_first_usable_state_builds_immediate_exact_desk_request(monkeypatch):
    """Spec 28-55,85-90: no CROWD delay; fixed rails and immutable original expiry."""
    k=service(monkeypatch);p=proof();before=copy.deepcopy(p)
    out=k.evaluate_candidate(p,now_host=R+.5,now_iso=iso(R+.5))
    assert out['eligible'],out
    req=out['request']
    assert req['signal_source']=='desk' and req['size_usd']==25 and req.get('override',False) is False
    assert req['rails']=={'tp_pct':40.,'sl_pct':30.}
    assert req['desk_metadata']['time_stop_s']==30
    assert req['desk_metadata']['cohort_id']=='known-group-launch-v1'
    assert req['ttl_s']==pytest.approx(4.5)
    from sniper.services.memecoin_desk import verdict_price_pumpswap
    expected_cap=verdict_price_pumpswap(p['tradable']['pool_state'],as_of=R+.5,sol_usd=150.,sol_age_s=0.,clip_usd=25.)
    assert expected_cap is not None and req['desk_metadata']['verdict_price']==expected_cap
    assert expected_cap['supply_tokens']==600_000_000 and expected_cap['ref_price_usd']==pytest.approx(.0001)
    assert_compact_proof(req['desk_metadata']['known_group_proof'],p,decision_cut=R+.5)
    assert p==before


@pytest.mark.parametrize('case',[
    'future_funding','late_funding','dust','setup_only','unsigned_funder','unsigned_developer','unknown_decimals',
    'imported_create','missing_receipt','unknown_order','wrong_registry','unknown_root','prior_sell','gap','overflow','disconnected',
])
def test_unproven_or_unsafe_launch_refuses_by_typed_reason(monkeypatch,case):
    """Spec 32-53,102-109,147-159: no invented causal, identity or coverage proof."""
    k=service(monkeypatch);p=proof();f=p['funding_path'][0];cr=p['creation']
    if case=='future_funding':f['known_at_host']=R+.1
    elif case=='late_funding':f['transaction']['blockTime']=int(R)-62
    elif case=='dust':f['transaction']['transaction']['message']['instructions'][0]['parsed']['info']['lamports']=9_999_999
    elif case=='setup_only':f['transaction']['transaction']['message']['instructions'][0]['parsed']['type']='createAccount'
    elif case=='unsigned_funder':f['transaction']['transaction']['message']['accountKeys'][0]['signer']=False
    elif case=='unsigned_developer':cr['transaction']['transaction']['message']['accountKeys'][0]['signer']=False
    elif case=='unknown_decimals':cr['decimals']=None
    elif case=='imported_create':cr['source']='import'
    elif case=='missing_receipt':cr['received_at_host']=None
    elif case=='unknown_order':
        f['transaction']['slot']=100;f['slot']=100;f['transaction']['blockTime']=int(R)-1;f['tx_index']=None
    elif case=='wrong_registry':p['registry']['sha256']='f'*64
    elif case=='unknown_root':p['registry']['root']=OTHER
    elif case=='prior_sell':p['prior_sells']=[dict(owner=DEV,mint=MINT,signature=FUND_SIG,received_at_host=R+.3)]
    elif case in ('gap','overflow'):p['coverage'][case]=True
    elif case=='disconnected':p['coverage']['connected']=False
    out=k.evaluate_candidate(p,now_host=R+.5,now_iso=iso(R+.5))
    assert out['eligible'] is False and isinstance(out['reason'],str) and out['reason']
    assert not out.get('request')


@pytest.mark.parametrize('now',[R-1,R+5.001])
def test_clock_regression_and_retry_after_original_deadline_refuse(monkeypatch,now):
    """Spec 43-46,102-107: restart or metadata completion never refreshes receipt."""
    k=service(monkeypatch);p=proof();p['creation']['decoder_ready_at_host']=now
    out=k.evaluate_candidate(p,now_host=now,now_iso=iso(now))
    assert not out['eligible'] and out['reason']


def test_policy_default_off_and_exact_confirmation_required(monkeypatch,ledger):
    """Spec 69-78,160-168: a producer cannot activate funding authority."""
    k=service(monkeypatch);cohort=k.register(ledger,now_iso=iso(R));p=proof()
    record(k,ledger,p)
    with pytest.raises(ValueError):k.enqueue_candidate(ledger,p,now_host=R+.5,now_iso=iso(R+.5))
    digest=ledger.execute('SELECT policy_hash FROM memecoin_desk_cohorts WHERE cohort_id=?',(cohort,)).fetchone()[0]
    for confirm,policy_hash in [(False,digest),(True,'f'*64)]:
        with pytest.raises(ValueError):k.set_admission(ledger,enabled=True,confirm=confirm,policy_hash=policy_hash,now_iso=iso(R))
    assert ledger.execute('SELECT count(*) FROM memecoin_manual_intents').fetchone()[0]==0


def test_identical_ticket_replay_is_idempotent_changed_proof_refuses(monkeypatch,ledger):
    """Spec 85-90: full immutable proof participates in ticket replay."""
    k=service(monkeypatch);enable(k,ledger);p=proof();iid=enqueue(k,ledger,p)
    assert k.enqueue_candidate(ledger,p,now_host=R+.6,now_iso=iso(R+.6))==iid
    changed=copy.deepcopy(p);changed['coverage']['complete']=True
    with pytest.raises(ValueError):k.enqueue_candidate(ledger,changed,now_host=R+.6,now_iso=iso(R+.6))
    assert ledger.execute('SELECT count(*) FROM memecoin_manual_intents').fetchone()[0]==1


def test_manual_stop_and_old_cohort_hashes_remain_separate(monkeypatch,ledger):
    """Spec 60-83,154-162: no hidden cohort loss/fill cap; old identity stays exact."""
    k=service(monkeypatch);cohort,_=enable(k,ledger)
    old={'clip_usd':25,'gates':'fixture-v1'}
    legacy=m.register_desk_cohort(ledger,producer='H',policy=old,prompt_version='desk-judge-v6',created_at=iso(R))
    body=json.dumps(dict(producer='H',prompt_version='desk-judge-v6',policy=old,kill_limit_usd=200.),sort_keys=True,separators=(',',':'))
    assert ledger.execute('SELECT policy_hash,kill_limit_usd FROM memecoin_desk_cohorts WHERE cohort_id=?',(legacy,)).fetchone()[:]==(hashlib.sha256(body.encode()).hexdigest(),200.)
    m.record_desk_cohort_cost(ledger,cohort_id=cohort,event_id='cost',usd=250,kind='execution',occurred_at=iso(R),provenance='fixture:exact')
    assert not m.desk_cohort_killed(ledger,cohort)
    assert m.desk_cohort_refusal(ledger,cohort_id=cohort,producer='H') is None
    from sniper.services.memecoin_desk_contract import cohort_policy_hash
    assert cohort_policy_hash(k.POLICY,producer='H',prompt_version=k.PROMPT_VERSION)==ledger.execute('SELECT policy_hash FROM memecoin_desk_cohorts WHERE cohort_id=?',(cohort,)).fetchone()[0]


def test_synthetic_enqueue_runner_admission_freezes_exact_proof_and_rails(monkeypatch,ledger):
    """Spec 85-90,164-169: real enqueue/drain/runner/reservation with fake execution."""
    from sniper.services import memecoin_real_runner as runner, memecoin_arming
    from sniper.util.clock import real_utc_now_iso
    k=service(monkeypatch)
    at=real_utc_now_iso();host=datetime.fromisoformat(at).timestamp();shift=host-R
    p=proof()
    p['creation'].update(received_at_host=host,received_at_iso=at,decoder_ready_at_host=host+.01,committed_at_host=host+.02)
    p['creation']['transaction']['blockTime']+=int(shift)
    p['funding_path'][0]['transaction']['blockTime']+=int(shift)
    p['funding_path'][0]['known_at_host']+=shift
    p['members'][0]['known_at_host']=host+.01
    p['registry']['approved_at_host']+=shift
    p['tradable']['observed_at_host']=host+.02
    p['tradable']['pool_state'].update(metadata_known_at=host+.01,reserve_known_at=host+.02,virtual_quote_known_at=host+.02)
    enable(k,ledger)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_ENABLED',True)
    monkeypatch.setattr(memecoin_arming.cfg,'MEMECOIN_REAL_ENABLED',True)
    monkeypatch.setattr(runner,'_alert_entry_result',lambda *a,**kw:None)
    k.record_candidate(ledger,p,now_host=host+.1,now_iso=iso(host+.1))
    iid=k.enqueue_candidate(ledger,p,now_host=host+.1,now_iso=iso(host+.1))
    pending=[]
    def execute(conn,pending_id,mint,size,now_iso,**kw):
        assert kw['desk_price_cap']['ref_price_usd']==pytest.approx(.0001)
        assert kw['desk_price_cap']['tolerance_pct']==cfg.MEMECOIN_DESK_VERDICT_TOLERANCE_PCT
        row=dict(conn.execute('SELECT * FROM memecoin_pending_entries WHERE id=?',(pending_id,)).fetchone());pending.append(row)
        pid=m.open_position(conn,source_user_id=row['source_user_id'],source_handle='fixture',mint=mint,ticker='KG',entry_price=.0001,
            entry_notional=size,entry_fill_source='chain',entry_lag_s=0.,their_size_usd=0.,opened_at=now_iso,strategy=row['strategy'],
            execution_mode='real',network_id='solana',signal_source=row['signal_source'],funded_contract_json=row['funded_contract_json'],
            origin_event_id=row['origin_event_id'],attempt_id=row['attempt_id'])
        sig=base58.b58encode(bytes([11])*64).decode()
        m.insert_fill(conn,position_id=pid,kind='entry',price=.0001,qty=size/.0001,notional=size,fill_source='chain',
            fraction=1.,created_at=now_iso,tx_sig=sig,attempt_id=row['attempt_id'],origin_event_id=row['origin_event_id'],
            network_id='solana',signal_source='desk')
        conn.execute("UPDATE memecoin_pending_entries SET status='confirmed',tx_sig=? WHERE id=?",(sig,pending_id))
        return dict(status='filled',position_id=pid,tx_sig=sig)
    contexts={'solana':dict(usdc_fn=lambda:300.,sol_fn=lambda:1.,token_safety_fn=lambda *a,**kw:(True,'ok'),execute_fn=execute,kill_path=None)}
    stats=lambda *a,**kw:dict(market_cap=60000.,price=.0001,liquidity_usd=20000.)
    intents.drain(ledger,runner.make_real_buy_fn(contexts,stats_fn=stats),now_fn=real_utc_now_iso)
    status=intents.read_status(ledger,iid)
    assert status['state']==intents.FILLED,status
    pos=m.get_position(ledger,status['position_id'])
    assert pos['source_user_id'] is None and pos['signal_source']=='desk'
    assert pos['funded_contract_json']==pending[0]['funded_contract_json']
    frozen=json.loads(pos['funded_contract_json'])
    assert frozen['policy']['max_hold_s']==30 and frozen['policy']['tp_pct']==40
    assert frozen['policy']['stop_pct']==30 and frozen['policy']['trail_pct']==0
    assert_compact_proof(frozen['source']['desk']['known_group_proof'],p,decision_cut=host+.1)
    assert intents.read_status(ledger,iid)['size_usd']==25


def test_revocation_and_cross_lane_reservation_refuse_atomically(monkeypatch,ledger):
    """Spec 128-132,151-156: a queued ticket cannot bypass durable policy stop."""
    k=service(monkeypatch);cohort,digest=enable(k,ledger);p=proof();iid=enqueue(k,ledger,p)
    meta=json.loads(ledger.execute('SELECT desk_metadata_json FROM memecoin_manual_intents WHERE intent_id=?',(iid,)).fetchone()[0])
    contract=json.dumps({'source':{'desk':{**meta,'ticket_id':iid,'rails':{'tp_pct':40.,'sl_pct':30.}}}})
    args=dict(mint=MINT,source_user_id=None,strategy='control',notional=25,reserved_at=iso(R+.6),network_id='solana',signal_source='desk',funded_contract_json=contract)
    k.set_admission(ledger,enabled=False,confirm=True,policy_hash=digest,now_iso=iso(R+.6))
    assert m.reserve_pending(ledger,**args) is None
    seen=[]
    intents.drain(ledger,lambda *a,**kw:seen.append(kw) or {'status':'refused'},now_fn=lambda:iso(R+.6))
    assert not seen,'revoked queued admission reached buy executor'
    k.set_admission(ledger,enabled=True,confirm=True,policy_hash=digest,now_iso=iso(R+.7))
    assert m.reserve_pending(ledger,mint=MINT,source_user_id='copy-owner',strategy='control',notional=25,reserved_at=iso(R+.7),network_id='solana',signal_source='slot_lane')
    assert m.reserve_pending(ledger,**dict(args,reserved_at=iso(R+.8))) is None


def test_arbitrary_manual_stop_policy_cannot_register(monkeypatch,ledger):
    """Spec 69-72: manual-stop zero sentinel is restricted to exact host policy."""
    k=service(monkeypatch);k.register(ledger,now_iso=iso(R))
    with pytest.raises(ValueError):
        m.register_desk_cohort(ledger,producer='H',policy={'stop_mode':'manual','clip_usd':25},prompt_version='fake',created_at=iso(R),stop_mode='manual')
    assert ledger.execute('SELECT count(*) FROM memecoin_desk_cohorts').fetchone()[0]==1


def sell_fixture(monkeypatch):
    """Saved offline trade bytes; synthetic clocks isolate freshness from capture date."""
    import sys
    from test_wallet_universe_sol_venues import captured_tx, MINT as sold_mint
    from sniper.services.wallet_universe_sol_venues import decode_transaction
    tx=captured_tx('pump_sell')
    swap,=decode_transaction(tx,sold_mint)['swaps']
    monkeypatch.setattr(sys.modules[__name__],'DEV',swap['actor'])
    monkeypatch.setattr(sys.modules[__name__],'MINT',sold_mint)
    tx['blockTime']=int(R)+1
    tx['transactionIndex']=1
    return dict(transaction=tx,commitment='confirmed',source='raw_confirmed_tail',flow_id=2,raw_ref='capture:2',
        received_at_host=R+1,decoder_ready_at_host=R+1.1,committed_at_host=R+1.2,
        slot=tx['slot'],tx_index=1,mint=sold_mint,owner=swap['actor'],signature=tx['transaction']['signatures'][0])


def open_frozen(k,c,p):
    iid=enqueue(k,c,p)
    meta=json.loads(c.execute('SELECT desk_metadata_json FROM memecoin_manual_intents WHERE intent_id=?',(iid,)).fetchone()[0])
    contract=json.dumps({'source':{'desk':{**meta,'ticket_id':iid,'rails':{'tp_pct':40.,'sl_pct':30.}}}})
    pid=m.open_position(c,source_user_id=None,source_handle='fixture',mint=MINT,ticker='KG',entry_price=.0001,entry_notional=25,
        entry_fill_source='chain',entry_lag_s=0,their_size_usd=0,opened_at=iso(R+.6),execution_mode='real',network_id='solana',
        signal_source='desk',funded_contract_json=contract)
    return pid,contract


def test_admission_stop_keeps_fresh_frozen_cohort_exit_and_exact_replay(monkeypatch,ledger):
    """Spec 49-55,118-132: automated transport survives stop; full payload replay binds."""
    k=service(monkeypatch);sell=sell_fixture(monkeypatch);_,digest=enable(k,ledger);p=proof();pid,_=open_frozen(k,ledger,p)
    k.set_admission(ledger,enabled=False,confirm=True,policy_hash=digest,now_iso=iso(R+.9))
    iid=k.nominate_sell(ledger,position_id=pid,sell=sell,now_host=R+1.3,now_iso=iso(R+1.3))
    assert iid
    assert k.nominate_sell(ledger,position_id=pid,sell=sell,now_host=R+1.4,now_iso=iso(R+1.4))==iid
    row=dict(ledger.execute('SELECT * FROM memecoin_manual_exit_intents WHERE intent_id=?',(iid,)).fetchone())
    assert row['action'] not in ('close','partial','close_any_price','source_partial')
    assert row['fraction']==1. and row['actor']!='operator'
    assert json.loads(row['request_json'])
    changed=copy.deepcopy(sell);changed['committed_at_host']+=.01
    with pytest.raises(ValueError):k.nominate_sell(ledger,position_id=pid,sell=changed,now_host=R+1.4,now_iso=iso(R+1.4))
    calls=[]
    def close(conn,pos,reason,at,**kw):
        calls.append((reason,kw));assert reason!='manual' and not kw.get('close_any_price')
        return dict(status='closed')
    exits.drain(ledger,close,lambda *a,**kw:pytest.fail('partial exit called'),now_fn=lambda:iso(R+3),position_fn=m.get_position,notify_fn=lambda *a,**kw:None)
    assert len(calls)==1,'fresh nomination must survive later drain and admission stop'
    assert exits.read_status(ledger,iid)['state']==exits.DONE


@pytest.mark.parametrize('bad',['stale','wrong_mint','late_member','unsigned','transfer_only'])
def test_stale_unbound_or_noncohort_sell_cannot_close(monkeypatch,ledger,bad):
    """Spec 49-53,118-126,164-168: source-bound first sell, frozen causal membership."""
    k=service(monkeypatch);sell=sell_fixture(monkeypatch);enable(k,ledger)
    if bad=='late_member':
        import sys
        monkeypatch.setattr(sys.modules[__name__],'DEV',OTHER)
    p=proof();pid,_=open_frozen(k,ledger,p)
    now=R+1.3
    if bad=='stale':now=R+2.001
    elif bad=='wrong_mint':sell['mint']=OTHER
    elif bad=='late_member':p['members'].append(dict(address=sell['owner'],role='genesis_signer',proof_ref='later-discovery',known_at_host=R+1.2))
    elif bad=='unsigned':sell['transaction']['transaction']['signatures']=[]
    elif bad=='transfer_only':sell['transaction']=funding()
    assert not k.nominate_sell(ledger,position_id=pid,sell=sell,now_host=now,now_iso=iso(now))
    assert ledger.execute('SELECT count(*) FROM memecoin_manual_exit_intents').fetchone()[0]==0


def test_presign_sell_invalidation_and_original_expiry_block_atomic_reservation(monkeypatch,ledger):
    """Spec 128-132: stale enqueue cannot race observed sell into funded reservation."""
    k=service(monkeypatch);sell=sell_fixture(monkeypatch);enable(k,ledger);p=proof();iid=enqueue(k,ledger,p)
    meta=json.loads(ledger.execute('SELECT desk_metadata_json FROM memecoin_manual_intents WHERE intent_id=?',(iid,)).fetchone()[0])
    contract=json.dumps({'source':{'desk':{**meta,'ticket_id':iid,'rails':{'tp_pct':40.,'sl_pct':30.}}}})
    args=dict(mint=MINT,source_user_id=None,strategy='control',notional=25,network_id='solana',signal_source='desk',funded_contract_json=contract)
    assert m.reserve_pending(ledger,**args,reserved_at=iso(R+5.001)) is None
    k.ingest_sell(ledger,sell,now_host=R+1.3,now_iso=iso(R+1.3))
    assert m.reserve_pending(ledger,**args,reserved_at=iso(R+1.4)) is None


def test_existing_signed_close_ownership_prevents_second_broadcast(monkeypatch,ledger):
    """Spec 118-126,151-153: same durable close claims and unknown recovery path."""
    k=service(monkeypatch);sell=sell_fixture(monkeypatch);enable(k,ledger);p=proof();pid,_=open_frozen(k,ledger,p)
    claim=m.claim_position_close(ledger,pid,iso(R+1),'stop')
    assert claim
    assert m.mark_position_close_submitted(ledger,pid,claim,iso(R+1),'already-signed')
    iid=k.nominate_sell(ledger,position_id=pid,sell=sell,now_host=R+1.3,now_iso=iso(R+1.3))
    broadcasts=[]
    def close(conn,pos,reason,at,**kw):
        token=m.claim_position_close(conn,pid,at,reason)
        if token:broadcasts.append(token)
        return dict(status='already_closing',sig=pos['close_sig'])
    if iid:
        exits.drain(ledger,close,lambda *a,**kw:pytest.fail('partial called'),now_fn=lambda:iso(R+1.4),position_fn=m.get_position,notify_fn=lambda *a,**kw:None)
    assert broadcasts==[]
    pos=m.get_position(ledger,pid)
    assert pos['close_claim_id']==claim and pos['close_sig']=='already-signed'


def test_large_raw_receipt_keeps_compact_main_ledger_proof_and_cannot_register_unchecked_compact(monkeypatch,ledger):
    """Spec85-90,94-99,118-124: retain full source off-path; freeze normalized proof across the money seam."""
    k=service(monkeypatch);enable(k,ledger);p=proof()
    small=k.evaluate_candidate(p,now_host=R+.5,now_iso=iso(R+.5))
    assert small['eligible'],small
    marker='synthetic-original-source-log-'
    p['creation']['transaction']['meta']['logMessages']=[marker+'x'*373000]
    assert len(json.dumps(p).encode())>373000
    result=k.evaluate_candidate(p,now_host=R+.5,now_iso=iso(R+.5))
    assert result['eligible'],result
    compact=result['request']['desk_metadata']['known_group_proof']
    assert_compact_proof(compact,p,decision_cut=R+.5)
    assert len(json.dumps(result['request']['desk_metadata']).encode())==len(json.dumps(small['request']['desk_metadata']).encode())
    iid=enqueue(k,ledger,p)
    saved=intents.read_status(ledger,iid)['desk_metadata']['known_group_proof']
    assert saved==compact and marker.encode() not in ledger.serialize()
    with pytest.raises(ValueError):k.record_candidate(ledger,compact,now_host=R+.6,now_iso=iso(R+.6))
    changed=copy.deepcopy(p);changed['creation']['transaction']['meta']['logMessages'][0]+='changed'
    with pytest.raises(ValueError):k.enqueue_candidate(ledger,changed,now_host=R+.6,now_iso=iso(R+.6))

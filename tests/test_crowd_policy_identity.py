from sniper.services import memecoin_origin as O
import sniper.settings.memecoin as cfg

def metadata(**changes):
    return dict(dict(history_basis='observed_tape',cohort_id='large-buyer-crowd-v1',producer='H',opportunity_id='lb:p:w',snapshot_hash='a'*64,time_stop_s=300.,admission_proof=proof(),crowd_proof=dict(crowd_others_60s=1,source_slot=12,source_tx_index=2,source_ordinal=0,source_signature='sig',detect_lag_s=1.)),**changes)

def test_crowd_requires_frozen_one_prior_qualifier():
    assert O.validate_desk_metadata(metadata(min_others_60s=1))==(True,None)
    for value in (None,0,True,2,'1'):
        m=metadata()
        if value is not None:m['min_others_60s']=value
        assert not O.validate_desk_metadata(m)[0]

def test_crowd_defaults_to_prior_qualifier_but_is_disabled():
    assert cfg.MEMECOIN_LARGE_BUYER_MIN_OTHERS==1
    assert cfg.MEMECOIN_LARGE_BUYER_ENABLED is False

def test_pilot_cannot_enter_when_global_token_safety_is_off(monkeypatch):
    monkeypatch.setattr(cfg,'MEMECOIN_LARGE_BUYER_ENABLED',True)
    monkeypatch.setattr(cfg,'MEMECOIN_DESK_ENABLED',False)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_TOKEN_SAFETY_ENABLED',False)
    assert not O.desk_lane_open({'cohort_id':'large-buyer-crowd-v1'})
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_TOKEN_SAFETY_ENABLED',True)
    assert O.desk_lane_open({'cohort_id':'large-buyer-crowd-v1'})

def proof(**changes):
    return dict(mint='mint',decimals=6,metadata_slot=12,metadata_known_at_host=90.,metadata_source='helius_research:get_account_info:json_parsed',token_program='spl-token',entry_due_host=100.,entry_deadline_utc='2026-11-01T00:00:05+00:00',clock_domain='corrected_utc',**changes)

def test_crowd_requires_typed_admission_proof():
    m=metadata(min_others_60s=1,admission_proof=proof())
    assert O.validate_desk_metadata(m)==(True,None)
    assert not O.validate_desk_metadata(metadata(min_others_60s=1,admission_proof=None))[0]
    for field,value in [('decimals',True),('metadata_slot',0),('metadata_known_at_host',float('nan')),('metadata_source','guess'),('entry_deadline_utc','2026-11-01T00:00:05'),('token_program','unknown')]:
        bad=dict(m,admission_proof={**proof(),field:value})
        assert not O.validate_desk_metadata(bad)[0],field

def test_crowd_admission_proof_is_bound_to_mint_and_queue_deadline():
    m=metadata(min_others_60s=1,admission_proof=proof())
    assert O.crowd_admission_refusal(m,mint='other',now_iso='2026-11-01T00:00:00+00:00',now_host=101.)=='crowd_mint_mismatch'
    assert O.crowd_admission_refusal(m,mint='mint',now_iso='2026-11-01T00:00:05+00:00',now_host=101.)=='crowd_entry_expired'
    assert O.crowd_admission_refusal(m,mint='mint',now_iso='2026-11-01T00:00:00+00:00',now_host=89.)=='crowd_metadata_future'
    assert O.crowd_admission_refusal(m,mint='mint',now_iso='2026-11-01T00:00:00+00:00',now_host=101.) is None

def test_pilot_admission_schema_is_idempotent_and_immutable():
    import sqlite3,pytest
    from sniper.persistence import db_memecoin as M
    c=sqlite3.connect(':memory:');M.ensure_memecoin_tables(c);M.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_pilot_admissions VALUES('large-buyer-crowd-v1','lb:p:w','2026-11-01T00:00:00+00:00')")
    with pytest.raises(sqlite3.IntegrityError):c.execute("DELETE FROM memecoin_pilot_admissions")
    with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE memecoin_pilot_admissions SET admitted_at='later'")

def test_runner_refuses_expired_crowd_before_reading_book(monkeypatch):
    from sniper.services import memecoin_real_runner as R
    from sniper.util import clock
    monkeypatch.setattr(cfg,'MEMECOIN_LARGE_BUYER_ENABLED',True)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_TOKEN_SAFETY_ENABLED',True)
    monkeypatch.setattr(cfg,'MEMECOIN_DESK_CHAINS',frozenset({'solana'}))
    monkeypatch.setattr(cfg,'MEMECOIN_DESK_CLIP_USD',25.)
    monkeypatch.setattr(cfg,'MEMECOIN_DESK_RAIL_BOUNDS',{'tp_pct':(15,150),'sl_pct':(10,40),'trail_pct':(8,30)})
    monkeypatch.setattr(clock,'real_utc_now_iso',lambda:'2026-11-01T00:00:10+00:00')
    called=[];monkeypatch.setattr(O,'desk_book_refusal',lambda *a,**kw:called.append(kw))
    result=R._desk_entry_refusal(None,mint='mint',network_id='solana',size_usd=25.,size_pct_equity=None,override=False,desk_ticket_id='lb:p:w',desk_rails={'tp_pct':40.,'sl_pct':30.},desk_metadata=metadata(min_others_60s=1))
    assert result=='crowd_entry_expired' and called==[]

def test_crowd_requires_causal_source_and_detection_proof():
    m=metadata(min_others_60s=1)
    assert O.validate_desk_metadata(m)[0]
    assert not O.validate_desk_metadata({k:v for k,v in m.items() if k!='crowd_proof'})[0]
    for field,value in [('source_slot',0),('source_tx_index',0),('crowd_others_60s',0),('detect_lag_s',5.001),('source_signature','')]:
        bad=dict(m,crowd_proof={**m['crowd_proof'],field:value})
        assert not O.validate_desk_metadata(bad)[0],field

def test_crowd_presign_deadline_uses_monotonic_clock_and_preserves_prior_gate(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_real_leg as L
    monkeypatch.setattr(cfg,'MEMECOIN_LARGE_BUYER_ENABLED',True)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_TOKEN_SAFETY_ENABLED',True)
    tick=[100.];ev=SimpleNamespace(desk=metadata(min_others_60s=1))
    check=L._crowd_presign_fn(ev,lambda:None,now_iso='2026-11-01T00:00:00+00:00',started=100.,clock=lambda:tick[0])
    assert check() is None
    tick[0]=105.;assert check()=='crowd_entry_expired'
    tick[0]=100.;monkeypatch.setattr(cfg,'MEMECOIN_REAL_TOKEN_SAFETY_ENABLED',False)
    assert check()=='desk_disabled'
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_TOKEN_SAFETY_ENABLED',True)
    check=L._crowd_presign_fn(ev,lambda:'existing_age_gate',now_iso='2026-11-01T00:00:00+00:00',started=100.,clock=lambda:100.)
    assert check()=='existing_age_gate'

def test_crowd_deadline_is_not_extended_by_slow_pre_entry_checks(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_real_leg as L
    monkeypatch.setattr(cfg,'MEMECOIN_LARGE_BUYER_ENABLED',True)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_TOKEN_SAFETY_ENABLED',True)
    ev=SimpleNamespace(desk=metadata(min_others_60s=1),_crowd_clock_anchor=('2026-11-01T00:00:00+00:00',100.))
    check=L._crowd_presign_fn(ev,None,now_iso='2026-11-01T00:00:00+00:00',started=110.,clock=lambda:111.)
    assert check()=='crowd_entry_expired'


def test_pilot_capacity_lookups_are_indexed():
    import sqlite3
    from sniper.persistence import db_memecoin as M
    c=sqlite3.connect(':memory:');M.ensure_memecoin_tables(c)
    for sql,params,index in [
        ("SELECT desk_opportunity_id FROM memecoin_pending_entries WHERE desk_cohort_id=? AND status='reserved'",('crowd',),'idx_mc_pending_pilot_capacity'),
        ("SELECT desk_ticket_id FROM memecoin_manual_intents WHERE signal_source='desk' AND CASE WHEN json_valid(desk_metadata_json) THEN json_extract(desk_metadata_json,'$.cohort_id') END=? AND state IN ('queued','claimed','unresolved','filled')",('crowd',),'idx_mc_intent_pilot_capacity')]:
        plan=' '.join(str(row) for row in c.execute('EXPLAIN QUERY PLAN '+sql,params))
        assert index in plan,plan


def test_crowd_refuses_before_frozen_due_time():
    m=metadata(min_others_60s=1)
    assert O.crowd_admission_refusal(m,mint='mint',now_iso='2026-11-01T00:00:00+00:00',now_host=99.)=='crowd_entry_not_due'


def test_crowd_signing_callback_demands_final_builder_check(monkeypatch):
    from types import SimpleNamespace
    from sniper.services.memecoin_real_leg import _crowd_presign_fn
    monkeypatch.setattr(cfg,'MEMECOIN_LARGE_BUYER_ENABLED',True)
    monkeypatch.setattr(cfg,'MEMECOIN_REAL_TOKEN_SAFETY_ENABLED',True)
    callback=_crowd_presign_fn(SimpleNamespace(desk=metadata(min_others_60s=1)),None,now_iso='2026-11-01T00:00:00+00:00',started=0.,clock=lambda:1.)
    assert callback.require_final_check is True and callback() is None


def test_funded_reservation_keeps_full_crowd_proof():
    from sniper.persistence import db_memecoin as M
    meta=metadata(min_others_60s=1)
    assert M._desk_contract_metadata({'source':{'desk':meta}})==meta
    bad={**meta,'admission_proof':None}
    assert M._desk_contract_metadata({'source':{'desk':bad}}) is None


def test_crowd_history_basis_is_explicit_and_survives_reservation():
    from sniper.persistence import db_memecoin as M
    m=metadata(min_others_60s=1)
    m.pop('history_basis',None)
    assert not O.validate_desk_metadata(m)[0]
    m['history_basis']='observed_tape'
    assert O.validate_desk_metadata(m)==(True,None)
    assert M._desk_contract_metadata({'source':{'desk':m}})==m
    for value in (None,True,'complete',''):
        assert not O.validate_desk_metadata(dict(m,history_basis=value))[0]
    legacy={k:m[k] for k in ('cohort_id','producer','opportunity_id','snapshot_hash')}
    legacy.update(cohort_id='large-buyer-v1',history_basis='observed_tape')
    assert not O.validate_desk_metadata(legacy)[0]

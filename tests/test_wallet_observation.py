import sqlite3
import pytest


def event(**over):
    return dict(dict(origin='solana:A:sig:0:M',uid='A',chain='solana',address='Owner',signature='sig',
                     event_index='0',mint='M',action='buy',quantity_atomic='184467440737095516160',
                     quote_atomic='5000000',source_at='2026-10-02T00:00:00+00:00',received_at='2026-10-02T00:00:00+00:00',
                     state='decoded',setup_json='{}',payload_json='{}'),**over)


def test_exact_quantity_and_duplicate_origin(tmp_path):
    from sniper.persistence.db_wallet_observation import open_observation_db,record_observation
    c=open_observation_db(tmp_path/'research.db')
    assert record_observation(c,event())
    assert not record_observation(c,event(received_at='2026-10-02T00:01:00+00:00'))
    assert c.execute('SELECT quantity_atomic,typeof(quantity_atomic) FROM observations').fetchone()==('184467440737095516160','text')
    with pytest.raises(ValueError,match='conflicting'): record_observation(c,event(quantity_atomic='2'))


def test_cursor_and_observation_rollback_together(tmp_path):
    from sniper.persistence.db_wallet_observation import open_observation_db,record_observation,advance_cursor
    c=open_observation_db(tmp_path/'research.db')
    with pytest.raises(RuntimeError):
        with c:
            record_observation(c,event())
            advance_cursor(c,owner='A:Owner',signature='sig',observed_at='now')
            raise RuntimeError()
    assert c.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==0
    assert c.execute('SELECT COUNT(*) FROM cursors').fetchone()[0]==0


def test_manifest_requires_forward_window_proof_and_not_paused():
    from sniper.services.wallet_observation import validate_manifest
    manifest=dict(enabled=True,chain='solana',forward_start='2026-10-02T00:00:00+00:00',
                  selection_at='2026-10-01T00:00:00+00:00',max_wallets=1,
                  owners=[dict(uid='A',address='Owner')])
    assert validate_manifest(manifest,{('solana','Owner'):('A','a')})[0]['uid']=='A'
    with pytest.raises(ValueError): validate_manifest(manifest,{})
    manifest['owners'][0]['paused']=True
    with pytest.raises(ValueError): validate_manifest(manifest,{('solana','Owner'):('A','a')})


def test_fee_only_native_transfer_is_not_a_buy():
    from sniper.services.wallet_observation import decode_observation
    tx={'transaction':{'message':{'accountKeys':['Owner']}},'meta':{'fee':5000,'err':None,
        'preBalances':[100000],'postBalances':[95000],'preTokenBalances':[],'postTokenBalances':[]}}
    rows=decode_observation(tx,dict(uid='A',chain='solana',address='Owner'),received_at='2026-10-02T00:00:00+00:00')
    assert not any(r['action']=='buy' and r['state']=='decoded' for r in rows)


def test_bounded_collection_keeps_restart_backlog(tmp_path):
    from sniper.persistence.db_wallet_observation import open_observation_db
    from sniper.services.wallet_observation import collect_tick
    c=open_observation_db(tmp_path/'o.db'); calls=[]
    owner=dict(uid='A',chain='solana',address='Owner',forward_start='2026-10-01T00:00:00+00:00')
    def rpc(method,params):
        calls.append(method)
        if method=='getSignaturesForAddress': return [dict(signature='s1',slot=1,blockTime=1790900000)]
        return {'transaction':{'signatures':['s1'],'message':{'accountKeys':['Owner']}},'meta':{'err':None},'blockTime':1790900000}
    out=collect_tick(c,[owner],rpc,now_iso='2026-10-02T00:00:00+00:00',max_calls=1)
    assert out['calls']==1 and c.execute('SELECT COUNT(*) FROM backlog').fetchone()[0]==1
    out=collect_tick(c,[owner],rpc,now_iso='2026-10-02T00:01:00+00:00',max_calls=1)
    assert out['calls']==1 and c.execute('SELECT COUNT(*) FROM backlog').fetchone()[0]==0
    assert c.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==1


def test_transport_failure_does_not_advance_frontier(tmp_path):
    from sniper.persistence.db_wallet_observation import open_observation_db
    from sniper.services.wallet_observation import collect_tick
    c=open_observation_db(tmp_path/'o.db')
    def rpc(*a): raise TimeoutError()
    out=collect_tick(c,[dict(uid='A',chain='solana',address='Owner',forward_start='2026-10-01T00:00:00+00:00')],rpc,
                     now_iso='2026-10-02T00:00:00+00:00',max_calls=2)
    assert out['states']['A:Owner']=='timeout'
    assert c.execute('SELECT COUNT(*) FROM cursors').fetchone()[0]==0


def pump_tx(*,signer='Owner'):
    from sniper.services.memecoin_solana_amounts import PUMP_PROGRAM_ID
    def balance(index,owner,amount):
        return dict(accountIndex=index,mint='M',owner=owner,uiTokenAmount={'amount':str(amount)})
    return dict(blockTime=1790971200,slot=1,transaction=dict(signatures=['sig'],message=dict(
        accountKeys=[dict(pubkey='Owner',signer=signer=='Owner'),dict(pubkey='Curve',signer=False),
                     dict(pubkey='Vault',signer=False),dict(pubkey='UserToken',signer=False)],
        instructions=[dict(programId=PUMP_PROGRAM_ID)])),meta=dict(err=None,
        preBalances=[100000,10000,0,0],postBalances=[90000,20000,0,0],
        preTokenBalances=[balance(2,'Curve',100),balance(3,'Owner',0)],
        postTokenBalances=[balance(2,'Curve',90),balance(3,'Owner',10)]))


def test_proven_pump_swap_and_recipient_only_rejection():
    from sniper.services.wallet_observation import decode_observation
    owner=dict(uid='A',chain='solana',address='Owner')
    good=decode_observation(pump_tx(),owner,received_at='2026-10-02T20:00:00+00:00')
    assert good[0]['action']=='buy' and good[0]['quantity_atomic']=='10' and good[0]['quote_atomic']=='10000'
    gift=decode_observation(pump_tx(signer='Other'),owner,received_at='2026-10-02T20:00:00+00:00')
    assert gift[0]['state']=='identity_mismatch'


def test_missing_research_capacity_rejects_collection():
    from sniper.services.wallet_observation import validate_budget
    with pytest.raises(ValueError,match='research capacity'):
        validate_budget({},['https://funded.example/rpc'])


def test_quote_jobs_have_research_origin_and_frozen_policy(tmp_path):
    from sniper.services.wallet_observation import schedule_research_quotes
    from sniper.services.memecoin_signal_copy_quotes import open_measurement_db
    c=open_measurement_db(tmp_path/'quotes.db')
    out=schedule_research_quotes(c,event(),policies=['control'],now_iso='2026-10-02T00:00:00+00:00',
                                budget=dict(remaining_calls=2,max_active=3,max_quotes_day=10))
    assert out['state']=='queued'
    row=c.execute('SELECT cohort,origin_event_id FROM copy_quote_schedule').fetchone()
    assert tuple(row)==('wallet-universe-v1',event()['origin'])
    assert out['policy_ids']


def test_default_cli_makes_no_database_or_provider_calls(tmp_path):
    from scripts.research.wallet_observation import main
    p=tmp_path/'manifest.json';p.write_text('{}')
    assert main(['--manifest',str(p),'--db',str(tmp_path/'o.db')])==0
    assert not (tmp_path/'o.db').exists()


def test_quote_identity_mismatch_is_censored_not_a_fill(tmp_path):
    from sniper.services.wallet_observation import schedule_research_quotes,run_research_quote_tick
    from sniper.services.memecoin_signal_copy_quotes import open_measurement_db
    c=open_measurement_db(tmp_path/'q.db')
    schedule_research_quotes(c,event(),policies=['control'],now_iso='2026-10-02T00:00:00+00:00',
                            budget=dict(remaining_calls=2,max_active=3,max_quotes_day=10))
    assert run_research_quote_tick(c,lambda *a:{'inputMint':'wrong','outputMint':'M','inAmount':'1','outAmount':'2'},
                                  now_iso='2026-10-02T00:00:00+00:00',max_quotes=1)==1
    assert c.execute('SELECT status FROM copy_quote_result').fetchone()['status']=='failed'
    assert c.execute("SELECT COUNT(*) FROM copy_quote_schedule WHERE leg='exit'").fetchone()[0]==0


def test_observation_db_rejects_foreign_ledger_before_mutation(tmp_path):
    from sniper.persistence.db_wallet_observation import open_observation_db
    path=tmp_path/'foreign.db'
    with sqlite3.connect(path) as c: c.execute('CREATE TABLE funded(id INTEGER)')
    with pytest.raises(ValueError,match='foreign'): open_observation_db(path)
    with sqlite3.connect(path) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==0
        assert c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()==[('funded',)]


def test_transaction_clock_cannot_admit_pre_forward_history(tmp_path):
    from sniper.persistence.db_wallet_observation import open_observation_db
    from sniper.services.wallet_observation import collect_tick
    c=open_observation_db(tmp_path/'o.db')
    owner=dict(uid='A',chain='solana',address='Owner',forward_start='2026-10-02T00:00:00+00:00')
    def rpc(method,params):
        if method=='getSignaturesForAddress': return [dict(signature='sig',slot=1,blockTime=None)]
        tx=pump_tx();tx['blockTime']=1;return tx
    collect_tick(c,[owner],rpc,now_iso='2026-10-02T20:00:00+00:00',max_calls=2)
    assert c.execute('SELECT COUNT(*) FROM observations').fetchone()[0]==0


def test_collection_rotates_owners_under_one_call_budget(tmp_path):
    from sniper.persistence.db_wallet_observation import open_observation_db
    from sniper.services.wallet_observation import collect_tick
    c=open_observation_db(tmp_path/'o.db');visited=[]
    owners=[dict(uid=u,chain='solana',address=u,forward_start='2026-10-02T00:00:00+00:00') for u in ['A','B']]
    def rpc(method,params): visited.append(params[0]);return []
    for t in ['01','02']: collect_tick(c,owners,rpc,now_iso=f'2026-10-02T00:00:{t}+00:00',max_calls=1)
    assert visited==['A','B']


def test_research_policy_set_cannot_expand_and_old_buy_cannot_queue(tmp_path):
    from sniper.services.wallet_observation import schedule_research_quotes
    from sniper.services.memecoin_signal_copy_quotes import open_measurement_db
    c=open_measurement_db(tmp_path/'q.db');b=dict(remaining_calls=2,max_active=3,max_quotes_day=10)
    e=event(source_at='2026-10-02T00:00:00+00:00')
    schedule_research_quotes(c,e,policies=['control'],now_iso=e['source_at'],budget=b)
    with pytest.raises(ValueError,match='policy set'):
        schedule_research_quotes(c,e,policies=['control','momentum_a'],now_iso=e['source_at'],budget=b)
    out=schedule_research_quotes(c,event(origin='old',source_at='2026-10-01T00:00:00+00:00'),policies=['control'],
                                now_iso=e['source_at'],budget=b)
    assert out['state']=='stale_source'


def test_missed_quote_horizon_is_censored_without_provider_call(tmp_path):
    from sniper.services.wallet_observation import schedule_research_quotes,run_research_quote_tick
    from sniper.services.memecoin_signal_copy_quotes import open_measurement_db
    c=open_measurement_db(tmp_path/'q.db');e=event(source_at='2026-10-02T00:00:00+00:00')
    schedule_research_quotes(c,e,policies=['control'],now_iso=e['source_at'],
                            budget=dict(remaining_calls=2,max_active=3,max_quotes_day=10))
    calls=[]
    run_research_quote_tick(c,lambda *a:calls.append(a),now_iso='2026-10-02T00:05:00+00:00',max_quotes=1)
    assert calls==[]
    assert c.execute('SELECT status FROM copy_quote_result').fetchone()['status']=='failed'


def test_quote_db_refuses_existing_unowned_measurement_store(tmp_path):
    from sniper.persistence.db_wallet_observation import open_research_quote_db
    from sniper.services.memecoin_signal_copy_quotes import open_measurement_db
    p=tmp_path/'q.db';open_measurement_db(p).close()
    with pytest.raises(ValueError,match='unowned'): open_research_quote_db(p)
    own=tmp_path/'own.db';open_research_quote_db(own).close();open_research_quote_db(own).close()


def test_research_manifest_is_frozen_and_usage_survives_restart(tmp_path):
    from sniper.persistence.db_wallet_observation import open_observation_db,freeze_manifest
    p=tmp_path/'o.db';c=open_observation_db(p)
    c.execute('CREATE TABLE budget_usage(day TEXT PRIMARY KEY,reads INTEGER,quotes INTEGER)');c.commit()
    freeze_manifest(c,{'chain':'solana','owners':[{'uid':'A','address':'Owner'}],
                       'selection_at':'2026-10-01T00:00:00+00:00','forward_start':'2026-10-02T00:00:00+00:00','policies':['control']})
    c.close();c=open_observation_db(p)
    with pytest.raises(ValueError,match='manifest'):
        freeze_manifest(c,{'chain':'solana','owners':[],'policies':['control']})


def test_watch_only_pump_profile_uses_existing_address_identity():
    from sniper.persistence import db_memecoin as m
    from sniper.services.wallet_observation import observation_owners
    c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;m.ensure_memecoin_tables(c)
    from sniper.persistence.db_axiom import ensure_axiom_tables
    ensure_axiom_tables(c);m.ensure_address_registry_tables(c)
    c.execute('CREATE TABLE memecoin_wallet_addresses(network_id TEXT,address TEXT,source_user_id TEXT,handle TEXT,verify_status TEXT)')
    address='9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin';uid='pump:'+address
    m.add_wallet(c,uid,'watch','')
    assert observation_owners(c,{uid})[('solana',address)][0]==uid


def test_research_entry_expiry_uses_original_source_and_preserves_clocks(tmp_path):
    from sniper.services.wallet_observation import schedule_research_quotes,run_research_quote_tick
    from sniper.services.memecoin_signal_copy_quotes import open_measurement_db
    c=open_measurement_db(tmp_path/'q.db');e=event(source_at='2026-10-02T00:00:00+00:00')
    schedule_research_quotes(c,e,policies=['control'],now_iso='2026-10-02T00:01:29+00:00',
                            budget=dict(remaining_calls=2,max_active=3,max_quotes_day=10))
    calls=[]
    run_research_quote_tick(c,lambda *a:calls.append(a),now_iso='2026-10-02T00:01:31+00:00',max_quotes=1)
    assert calls==[]
    row=c.execute('SELECT failure_reason,source_received_at FROM copy_quote_result').fetchone()
    assert tuple(row)==('stale_source',e['received_at'])


def test_quote_pacing_cannot_relabel_a_late_horizon_as_success(tmp_path,monkeypatch):
    from sniper.services.wallet_observation import schedule_research_quotes,run_research_quote_tick
    from sniper.services.memecoin_signal_copy_quotes import open_measurement_db
    from sniper.services.memecoin_solana_amounts import USDC
    import time
    c=open_measurement_db(tmp_path/'q.db');e=event();clock=[0]
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    schedule_research_quotes(c,e,policies=['control'],now_iso=e['source_at'],
                            budget=dict(remaining_calls=2,max_active=3,max_quotes_day=10))
    c.execute("UPDATE copy_quote_schedule SET leg='exit',horizon_s=5")
    monkeypatch.setattr('sniper.services.memecoin_signal_copy_quotes.expected_tokens',lambda *a:'10')
    def quote(im,om,amount):
        clock[0]=3
        return dict(inputMint=im,outputMint=om,inAmount=amount,outAmount='100',routePlan=[])
    run_research_quote_tick(c,quote,now_iso=e['source_at'],max_quotes=1)
    assert c.execute('SELECT status,failure_reason FROM copy_quote_result').fetchone()['failure_reason']=='missed_horizon'

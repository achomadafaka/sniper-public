import json
import sqlite3
import subprocess
from pathlib import Path
import pytest
from test_wallet_execution_evidence import episode
from test_wallet_execution_replay import policy
from test_wallet_execution_worker import source_db
from test_wallet_execution_deploy import mod
from sniper.research.wallet_execution.evidence import episode_gate,study_gate,digest,PROTOCOL
from sniper.research.wallet_execution.publication import require_study,UnsupportedResearch
from sniper.research.wallet_execution.replay import replay
from sniper.persistence.db_wallet_execution import open_store,ingest_episode
from sniper.services.wallet_execution_research import collect_once,build_coverage,read_coverage


def test_forged_study_cannot_publish():
    e=episode(); h='0'*64
    m=dict(protocol=PROTOCOL,wallet='alice',chain='56',origins=[e['origin_id']],episodes_digest=digest([e]),
           trials=[{}],sealed_at=-5000,discovery_end=-6000,validation_start=-4000,validation_end=-3000,
           test_start=-1000,test_end=2000,power=dict(minimum_detectable_cash=1,training_evidence_digest=h,
           min_coin_clusters=1,min_days=1),trial_ledger_digest=h,holdout_read_count=1,outcome_complete=True,
           census_complete=True,multiplicity='holm_coin_day_clustered',independent_review_digest=h,sealed_store_receipt=h)
    assert not study_gate(m,[e]).eligible
    with pytest.raises(UnsupportedResearch): require_study(m,[e])


def test_pre_acquisition_quote_never_triggers_tp():
    e=episode(); e['frames'][120]['available_at']=1
    e['frames'][120]['sells']['1000000'].update(floor_usd=140,expected_usd=141)
    r=replay(e,policy())
    assert r['reason']=='timecap' and r['exit_at']==5


def test_decimals_disagreement_is_rejected():
    e=episode()
    for f in e['frames']: f['buy']['decimals']=0
    assert not episode_gate(e).eligible
    assert replay(e,policy(entry='dip',max_wait_s=0,premium_cap_pct=-90))['state']=='censored'


@pytest.mark.parametrize('position',[0,-1])
def test_malformed_boundary_rejected_without_crash(position):
    e=episode(); e['frames'][position]=None
    assert not episode_gate(e).eligible


def test_nan_feature_clock_rejected():
    e=episode(); e['features']={'mcap':{'available_at':float('nan')}}
    assert not episode_gate(e,decision_at=1).eligible


def test_default_tape_is_canonical_with_override(monkeypatch):
    from scripts.research.wallet_execution_worker import default_tape_path
    monkeypatch.setenv('MEMECOIN_TICKS_DB','/tmp/custom-ticks.db')
    assert default_tape_path()==Path('/tmp/custom-ticks.db')


def test_feed_retention_and_replacement_are_explicit(tmp_path):
    source=source_db(tmp_path); db=open_store(tmp_path/'r.db')
    collect_once(db,source,now='2026-10-04T00:00:00+00:00')
    c=sqlite3.connect(source); c.execute('DELETE FROM memecoin_feed_observations_v2')
    c.execute("INSERT INTO memecoin_feed_observations_v2 VALUES (100,'100','alice','Alice','56','m','buy','2026-10-04T00:00:01+00:00','{}')");c.commit();c.close()
    r=collect_once(db,source,now='2026-10-04T00:00:02+00:00')
    assert 'feed' in r['continuity_gaps'] and r['new_origins']==1
    replacement=tmp_path/'replacement.db';sqlite3.connect(source).backup(sqlite3.connect(replacement)); replacement.replace(source)
    r=collect_once(db,source,now='2026-10-04T00:00:03+00:00')
    assert 'feed' in r['continuity_gaps']


def test_headers_and_gaps_archive_every_page(tmp_path):
    source=source_db(tmp_path);db=open_store(tmp_path/'r.db');tape=tmp_path/'ticks.db'
    c=sqlite3.connect(tape)
    c.executescript('CREATE TABLE pool_ticks(id INTEGER PRIMARY KEY,amount0 TEXT); CREATE TABLE block_times(chain_id TEXT,block_no INTEGER); CREATE TABLE tick_gaps(id INTEGER PRIMARY KEY,reason TEXT);')
    c.commit()
    collect_once(db,source,now='2026-10-04T00:00:00+00:00',tape_path=tape,limit=2)
    for i in range(1,6):
        c.execute('INSERT INTO block_times VALUES (?,?)',('56',i));c.execute('INSERT INTO tick_gaps VALUES (?,?)',(i,'gap'))
    c.commit();c.close()
    for i in range(3):collect_once(db,source,now=f'2026-10-04T00:00:0{i+1}+00:00',tape_path=tape,limit=2)
    assert db.execute("SELECT count(*) FROM we_raw WHERE source LIKE 'evm_heads%'").fetchone()[0]==5
    assert db.execute("SELECT count(*) FROM we_raw WHERE source LIKE 'evm_gaps%'").fetchone()[0]==5


def test_coverage_does_not_decode_historical_episode_paths(tmp_path):
    db=open_store(tmp_path/'r.db');collect_once(db,source_db(tmp_path),now='2026-10-04T00:00:00+00:00')
    e=episode();ingest_episode(db,e);db.commit()
    # After ingest, projection must read metadata only, even if large archived payload is unreadable.
    db.execute("UPDATE we_episodes SET payload='not json'");db.commit()
    cards=build_coverage(db,now='2026-10-04T00:00:01+00:00')
    assert next(c for c in cards['cells'] if c['wallet']=='alice' and c['chain']=='56')['complete_episodes']==1


def test_fresh_source_failure_is_unavailable_with_previous_counts(tmp_path):
    p=tmp_path/'c.json';p.write_text(json.dumps(dict(protocol=PROTOCOL,generated_at='2026-10-04T00:00:00+00:00',source_status='unavailable',cells=[dict(wallet='alice',chain='56',signals=4)])))
    r=read_coverage(p,chain='56',now='2026-10-04T00:00:01+00:00')
    assert r['state']=='unavailable' and r['cells'][0]['signals']==4


def test_rollback_restarts_old_worker_if_switch_fails(monkeypatch,tmp_path):
    m=mod();sha='a'*40;state=tmp_path/'state';units=tmp_path/'units';runtime=state/'.runtime/wallet-research'
    release=runtime/'releases'/sha;(release/'scripts/research').mkdir(parents=True);(release/m.ENTRY).write_text('# test')
    previous=runtime/'releases'/('b'*40);previous.mkdir();(runtime/'current').symlink_to(previous)
    units.mkdir();(units/m.UNIT).write_text(m.MARKER+'\n# old');calls=[]
    monkeypatch.setattr(m,'validate_request',lambda *a:None)
    monkeypatch.setattr(m,'owned_command',lambda *a:True)
    monkeypatch.setattr(Path,'read_bytes',lambda *a:b'python3\0')
    def run(*args,**kwargs):
        calls.append(args);return subprocess.CompletedProcess(args,0,sha if 'rev-parse' in args else '123' if 'show' in args else '','')
    monkeypatch.setattr(m,'run',run)
    monkeypatch.setattr(m.subprocess,'run',lambda args,**k:(calls.append(tuple(args)),subprocess.CompletedProcess(args,0,'',''))[1])
    monkeypatch.setattr(m.os,'replace',lambda *a:(_ for _ in ()).throw(OSError('switch failed')))
    with pytest.raises(OSError):m.deploy(sha,repo=tmp_path,state=state,unit_dir=units)
    assert ('systemctl','--user','start',m.UNIT) in calls


def test_armed_board_cannot_claim_legacy_selector_is_active(tmp_path,monkeypatch):
    from test_memecoin_api_armed_and_screen_targets import _client
    from sniper.settings import memecoin as cfg
    from sniper.services.memecoin_copy_arm import copy_arm_enabled
    monkeypatch.setattr(cfg,'MEMECOIN_COPY_ARM_POLICY',True)
    client,_=_client(tmp_path)
    body=client.get('/api/memecoin/armed?chain=solana').get_json()
    assert body['copy_arm_policy'] is False and body['copy_arm_policy_configured'] is True
    assert not copy_arm_enabled()


def test_changed_feed_origin_is_quarantined_without_stalling_new_buys(tmp_path):
    source=source_db(tmp_path);db=open_store(tmp_path/'r.db')
    collect_once(db,source,now='2026-10-04T00:00:00+00:00',bootstrap='beginning')
    c=sqlite3.connect(source);c.execute("UPDATE memecoin_feed_observations_v2 SET raw_json='changed' WHERE id=3")
    c.execute("INSERT INTO memecoin_feed_observations_v2 VALUES (4,'4','alice','Alice','56','m','buy','2026-10-04T00:00:01+00:00','{}')");c.commit();c.close()
    r=collect_once(db,source,now='2026-10-04T00:00:02+00:00')
    assert r['source_status']=='ok' and r['new_origins']==1 and r['conflicting_origins']==1
    assert db.execute('SELECT count(*) FROM we_source_gaps').fetchone()[0]>0
    assert json.loads(db.execute("SELECT payload FROM we_origins WHERE mint='mint' ORDER BY observed_at LIMIT 1").fetchone()[0])['raw_json']=='{}'
    assert collect_once(db,source,now='2026-10-04T00:00:03+00:00')['source_status']=='ok'


def test_mutable_header_revision_coverage_is_explicitly_unknown(tmp_path):
    source=source_db(tmp_path);db=open_store(tmp_path/'r.db');tape=tmp_path/'tape.db';c=sqlite3.connect(tape)
    c.executescript('CREATE TABLE pool_ticks(id INTEGER PRIMARY KEY); CREATE TABLE block_times(chain_id TEXT,block_no INTEGER,block_hash TEXT); CREATE TABLE tick_gaps(id INTEGER PRIMARY KEY);')
    c.commit();collect_once(db,source,now='2026-10-04T00:00:00+00:00',tape_path=tape)
    c.executemany('INSERT INTO block_times VALUES (?,?,?)',[('56',1,'old'),('56',2,'other')]);c.commit()
    collect_once(db,source,now='2026-10-04T00:00:01+00:00',tape_path=tape)
    c.execute("UPDATE block_times SET block_hash='new' WHERE block_no=1");c.commit();c.close()
    r=collect_once(db,source,now='2026-10-04T00:00:02+00:00',tape_path=tape)
    assert r['header_revision_coverage']=='unknown' and r['archive_is_canonical'] is False
    assert build_coverage(db,now='2026-10-04T00:00:03+00:00')['continuity_gap_count']>0


def test_roster_uses_actual_comma_separated_authority():
    from sniper.services.wallet_execution_research import roster_manifest
    members=roster_manifest([dict(source_user_id='u',handle='alice',real_approved=1,real_chains='robin,bsc,solana')],now='2026-10-04T00:00:00+00:00')['members']
    assert {m['chain'] for m in members}=={'4663','56','solana'}
    assert all(m['mode']=='armed' for m in members)


def test_coverage_accepts_chain_alias_and_keeps_ui_identity(tmp_path):
    p=tmp_path/'c.json';p.write_text(json.dumps(dict(protocol=PROTOCOL,generated_at='2026-10-04T00:00:00+00:00',source_status='ok',cells=[dict(wallet='alice',chain='56',signals=4)])))
    r=read_coverage(p,chain='bsc',now='2026-10-04T00:00:01+00:00')
    assert len(r['cells'])==1 and r['cells'][0]['chain']=='bsc'


def test_a_stop_that_fails_after_stopping_still_restarts_the_old_worker(monkeypatch,tmp_path):
    # Codex W9: `stopped` was set only after the stop returned success, so a stop that stopped the
    # worker and then failed or timed out skipped the rollback and left it down.
    m=mod();sha='a'*40;state=tmp_path/'state';units=tmp_path/'units';runtime=state/'.runtime/wallet-research'
    release=runtime/'releases'/sha;(release/'scripts/research').mkdir(parents=True);(release/m.ENTRY).write_text('# test')
    previous=runtime/'releases'/('b'*40);previous.mkdir();(runtime/'current').symlink_to(previous)
    units.mkdir();(units/m.UNIT).write_text(m.MARKER+'\n# old');calls=[]
    monkeypatch.setattr(m,'validate_request',lambda *a:None)
    monkeypatch.setattr(m,'owned_command',lambda *a:True)
    monkeypatch.setattr(Path,'read_bytes',lambda *a:b'python3\0')
    def run(*args,**kwargs):
        calls.append(args)
        if args[:3]==('systemctl','--user','stop'):
            raise subprocess.CalledProcessError(1,args)
        return subprocess.CompletedProcess(args,0,sha if 'rev-parse' in args else '123' if 'show' in args else '','')
    monkeypatch.setattr(m,'run',run)
    monkeypatch.setattr(m.subprocess,'run',lambda args,**k:(calls.append(tuple(args)),subprocess.CompletedProcess(args,0,'',''))[1])
    with pytest.raises(subprocess.CalledProcessError):m.deploy(sha,repo=tmp_path,state=state,unit_dir=units)
    assert ('systemctl','--user','start',m.UNIT) in calls
    assert (runtime/'current').resolve()==previous.resolve()

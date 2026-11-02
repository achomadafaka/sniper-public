import pytest
from sniper.persistence.db_wallet_execution import open_store,record_origin,ingest_episode
from sniper.research.wallet_execution.study import seal_round,evaluate_round
from test_wallet_execution_evidence import episode
from test_wallet_execution_replay import policy


def plan():
    return {'protocol':'wallet-execution-research-v1','wallet':'alice','chain':'56',
            'discovery_end':-2000.,'validation_start':2000.,'validation_end':4000.,
            'test_start':8000.,'test_end':12000.,'embargo_s':1802.,
            'outcome_tail_s':1860.,
            # Registered maxima of the path after the hold (W8): their sum plus the longest
            # wait+hold path is the shortest tail a round may seal with (1800 + 60 here).
            'latency_bounds':{'entry_delay_s':1.,'exit_delay_s':1.,'settlement_s':1.,'persistence_s':57.},
            'baseline':policy(), 'challengers':[policy(policy_id='dip',entry='dip',max_wait_s=3.,premium_cap_pct=-10.)],
            'power':None,'trial_ledger':[],'source_code_revision':'a'*40}


def test_round_seals_before_validation_and_never_rewrites(tmp_path):
    db=open_store(tmp_path/'r.db'); p=plan(); key=seal_round(db,p,now=0.)
    assert seal_round(db,p,now=0.)==key
    p['challengers']=[policy(policy_id=str(i)) for i in range(9)]
    with pytest.raises(ValueError,match='eight'): seal_round(db,p,now=0.)
    p=plan()
    with pytest.raises(ValueError,match='future'): seal_round(db,p,now=2001.)


def test_round_census_keeps_missing_paths_and_waits_for_future_end(tmp_path):
    db=open_store(tmp_path/'r.db'); key=seal_round(db,plan(),now=0.)
    record_origin(db,{'upstream_event_id':'missing','source_user_id':'alice','network_id':'56','mint':'m',
                      'observed_at':'1970-01-01T02:30:00+00:00'})
    db.commit()
    assert evaluate_round(db,key,now=13000.)['status']=='accruing'   # inside the outcome tail
    r=evaluate_round(db,key,now=13861.)
    assert r['registered_origins']==1 and r['paired_origins']==0
    assert r['excluded_origins']==1 and r['winner'] is None
    assert r['policies'][0]['net_cash_per_registered_origin'] is None


def test_power_missing_is_not_replaced_by_an_old_close_count(tmp_path):
    db=open_store(tmp_path/'r.db'); key=seal_round(db,plan(),now=0.)
    r=evaluate_round(db,key,now=13861.)
    assert r['status']=='blocked' and 'validation_missing' in r['codes']
    assert not r['eligible'] and r['winner'] is None


def test_trial_and_policy_identity_are_unique_before_seal(tmp_path):
    db=open_store(tmp_path/'r.db'); p=plan(); p['challengers']=[p['baseline']]
    with pytest.raises(ValueError,match='policy'): seal_round(db,p,now=0.)


def test_size_study_keeps_missing_pairs_and_separate_prior_context(tmp_path):
    from test_wallet_execution_replay import sizing_episode
    db = open_store(tmp_path/'r.db'); p = plan()
    p['baseline'] = policy(clip_usd=131.25)
    p['challengers'] = [policy(policy_id='full', clip_usd=175.)]
    key = seal_round(db, p, now=0.)
    origin = record_origin(db, {'upstream_event_id': 'sized', 'source_user_id': 'alice', 'network_id': '56',
                               'mint': 'token', 'observed_at': '1970-01-01T02:30:00+00:00'})
    e = sizing_episode(); e['origin_id'] = origin
    for name in ['source_at', 'available_at', 'received_at']: e['source'][name] += 9000.
    for name in ['start', 'end']: e['window'][name] += 9000.
    e['execution']['calibration_before'] += 9000.
    for frame in e['frames']:
        frame['at'] += 9000.; frame['available_at'] += 9000.
    e['features'] = {'market_regime': {'value': 'thin', 'available_at': 8999.},
                     'feed_health': {'value': 'stale', 'available_at': 8999.},
                     'chain_market_regime': {'value': 'normal', 'available_at': 8999.}}
    ingest_episode(db, e)
    record_origin(db, {'upstream_event_id': 'missing', 'source_user_id': 'alice', 'network_id': '56',
                      'mint': 'absent', 'observed_at': '1970-01-01T02:31:00+00:00'})
    db.commit()
    result = evaluate_round(db, key, now=13861.)
    assert result['registered_origins'] == 2 and result['paired_origins'] == 1
    assert result['policies'][1]['paired_mean_delta'] == -9.75
    assert result['policies'][1]['censor_reasons'] == {'episode_missing': 1}
    assert result['policies'][1]['net_cash_per_registered_origin'] is None
    assert result['context_counts']['market_regime'] == {'thin': 1, 'unknown': 1}
    assert result['context_counts']['feed_health'] == {'stale': 1, 'unknown': 1}
    assert result['context_counts']['chain_market_regime'] == {'normal': 1, 'unknown': 1}
    assert not result['eligible'] and result['winner'] is None
    assert evaluate_round(db, key, now=15000.) == result


def test_a_plan_without_an_outcome_tail_cannot_be_sealed(tmp_path):
    # Codex W8: a missing tail read the last origin at test end and froze its outcome as missing.
    db=open_store(tmp_path/'r.db'); p=plan(); p.pop('outcome_tail_s')
    with pytest.raises(ValueError,match='future'): seal_round(db,p,now=0.)
    p['outcome_tail_s']=10.                               # shorter than the longest wait+hold path
    with pytest.raises(ValueError,match='future'): seal_round(db,p,now=0.)


def test_a_study_sealed_without_a_tail_is_never_read(tmp_path):
    from sniper.persistence.db_wallet_execution import encoded
    db=open_store(tmp_path/'r.db'); p=plan(); p.pop('outcome_tail_s')
    with db: db.execute('INSERT INTO we_studies VALUES (?,?)',('legacy',encoded({**p,'sealed_at':0.})))
    r=evaluate_round(db,'legacy',now=99999.)
    assert r['status']=='blocked' and r['codes']==['contract_invalid','outcome_tail_missing']
    assert db.execute('SELECT COUNT(*) FROM we_study_reads').fetchone()[0]==0


def test_a_tail_that_only_covers_the_hold_is_refused(tmp_path):
    # Codex night review W8 residual: hold 1800 + tail 1800 sealed before the last settlement.
    db=open_store(tmp_path/'r.db'); p=plan(); p['outcome_tail_s']=1800.
    with pytest.raises(ValueError,match='future'): seal_round(db,p,now=0.)
    p['outcome_tail_s']=1860.
    assert seal_round(db,p,now=0.)


# ---- Codex follow-up 2026-10-07, W8: the whole path after the hold is registered and enforced ----

def test_a_round_registers_every_delay_after_the_hold_before_it_seals(tmp_path):
    db=open_store(tmp_path/'r.db')
    p=plan(); p.pop('latency_bounds')
    with pytest.raises(ValueError,match='future'): seal_round(db,p,now=0.)
    for bad in (-1., float('nan'), True, None):
        p=plan(); p['latency_bounds']['settlement_s']=bad
        with pytest.raises(ValueError,match='future'): seal_round(db,p,now=0.)
    p=plan(); p['latency_bounds']['entry_delay_s']=61.      # Codex: a 61 s entry delay is allowed
    with pytest.raises(ValueError,match='future'): seal_round(db,p,now=0.)   # 1800+61+1+1+57 > 1860
    p['outcome_tail_s']=1920.
    assert seal_round(db,p,now=0.)


@pytest.mark.parametrize('stored, why', [
    ({'outcome_tail_s':1860.}, 'latency_bounds_unregistered'),        # sealed under the flat +60 s
    ({'outcome_tail_s':1800.}, 'latency_bounds_unregistered'),        # sealed before that
    ({'outcome_tail_s':1800.,'latency_bounds':{'entry_delay_s':1.,'exit_delay_s':1.,'settlement_s':1.,
                                               'persistence_s':57.}}, 'outcome_tail_short'),
])
def test_a_stored_plan_is_validated_before_its_first_read(tmp_path, stored, why):
    """A plan sealed under an older rule is read under the current one, never by its own stored
    numbers: it blocks and writes no receipt (the 22 plans of 2026-10-07 have no bounds)."""
    from sniper.persistence.db_wallet_execution import encoded
    db=open_store(tmp_path/'r.db'); p=plan(); p.pop('latency_bounds'); p.update(stored)
    with db: db.execute('INSERT INTO we_studies VALUES (?,?)',('old',encoded({**p,'sealed_at':0.})))
    r=evaluate_round(db,'old',now=99999.)
    assert r['status']=='blocked' and r['codes']==['contract_invalid',why]
    assert db.execute('SELECT COUNT(*) FROM we_study_reads').fetchone()[0]==0


def test_a_receipt_fixed_under_a_now_invalid_plan_is_never_reused_as_valid(tmp_path):
    from sniper.persistence.db_wallet_execution import encoded
    db=open_store(tmp_path/'r.db'); p=plan(); p.pop('latency_bounds')
    receipt={'study_id':'old','status':'blocked','codes':['validation_missing'],'winner':None}
    with db:
        db.execute('INSERT INTO we_studies VALUES (?,?)',('old',encoded({**p,'sealed_at':0.})))
        db.execute('INSERT INTO we_study_reads VALUES (?,?)',('old',encoded(receipt)))
    r=evaluate_round(db,'old',now=99999.)
    assert r['status']=='invalid' and r['codes']==['validation_missing','contract_invalid',
                                                   'latency_bounds_unregistered']
    assert r['winner'] is None


@pytest.mark.parametrize('delay, why', [(1., 'latency_outside_registered_bound'), (None, 'latency_unknown')])
def test_an_episode_slower_than_its_registered_bound_is_censored(tmp_path, delay, why):
    """Codex's counterexample: an accepted 61 s entry delay settled after read_after and sealed
    episode_missing. An episode outside the plan's registered delays is censored by name instead."""
    from test_wallet_execution_replay import sizing_episode
    db=open_store(tmp_path/'r.db'); p=plan()
    p['latency_bounds']['entry_delay_s']=.5; p['latency_bounds']['persistence_s']=57.5
    p['baseline']=policy(clip_usd=131.25); p['challengers']=[]
    key=seal_round(db,p,now=0.)
    origin=record_origin(db,{'upstream_event_id':'slow','source_user_id':'alice','network_id':'56',
                             'mint':'token','observed_at':'1970-01-01T02:30:00+00:00'})
    e=sizing_episode(); e['origin_id']=origin
    for name in ['source_at','available_at','received_at']: e['source'][name]+=9000.
    for name in ['start','end']: e['window'][name]+=9000.
    e['execution']['calibration_before']+=9000.
    for frame in e['frames']:
        frame['at']+=9000.; frame['available_at']+=9000.
    assert e['execution']['entry_delay_s']==1.                      # over the registered 0.5 s
    ingest_episode(db,e); db.commit()
    if delay is None:                                               # Gamma LOW: unknown is not over-bound
        stored=__import__('json').loads(db.execute('SELECT payload FROM we_episodes').fetchone()[0])
        stored['execution'].pop('entry_delay_s')
        from sniper.persistence.db_wallet_execution import encoded
        with db: db.execute('UPDATE we_episodes SET payload=?',(encoded(stored),))
    r=evaluate_round(db,key,now=13861.)
    assert r['paired_origins']==0
    assert r['policies'][0]['censor_reasons']=={why:1}

import importlib
import sqlite3

import pytest


def module():
    return importlib.import_module('sniper.office.quota_measurement')


@pytest.fixture
def db():
    conn = sqlite3.connect(':memory:')
    module().ensure_schema(conn)
    yield conn
    conn.close()


def wake(conn, **changes):
    args = dict(epoch_id='proof', actor_id='head', generation=1, incarnation='one', session_id='session',
        wake_id='wake1', account_alias='claude-main', outcome='ok', counters=dict(input_tokens=10,
        output_tokens=2, cache_read_input_tokens=3, cache_creation_input_tokens=5), result_id='result1',
        started_at=1000., ended_at=1001., per_wake_max=100, usage_scope='per_wake', sdk_version='test-sdk')
    args.update(changes)
    return module().record_wake(conn, **args)


def test_exact_replay_and_zero_are_known_but_conflicting_identity_refuses(db):
    assert wake(db)['used'] == 17
    assert wake(db)['used'] == 17
    assert module().wake_budget(db, actor_id='head', now=1002., limit=17)['exhausted'] is True
    with pytest.raises(ValueError, match='identity'):
        wake(db, incarnation='two')
    assert module().wake_budget(db, actor_id='head', now=1002., limit=100)['used'] == 17
    assert wake(db, wake_id='zero', result_id='zero', counters={k:0 for k in (
        'input_tokens','output_tokens','cache_read_input_tokens','cache_creation_input_tokens')})['used'] == 0


@pytest.mark.parametrize('changes', [dict(counters=None), dict(counters={'output_tokens':0}),
    dict(usage_scope='unknown'), dict(per_wake_max=None), dict(outcome='timeout', counters=None),
    dict(counters=dict(input_tokens=True, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0))])
def test_unknown_never_becomes_zero_and_parks_day(db, changes):
    assert wake(db, **changes)['state'] == 'unknown'
    assert module().wake_budget(db, actor_id='head', now=1002., limit=100)['state'] == 'unknown'


def test_duplicate_result_is_unknown_not_another_charge(db):
    wake(db)
    assert wake(db, wake_id='reused')['state'] == 'unknown'
    assert module().wake_budget(db, actor_id='head', now=1002., limit=100)['state'] == 'unknown'


def test_unknown_scope_never_inferred_from_two_rising_wakes(db):
    wake(db, usage_scope='unknown')
    assert wake(db, wake_id='second', result_id='second', usage_scope='unknown', counters=dict(
        input_tokens=20, output_tokens=4, cache_read_input_tokens=6, cache_creation_input_tokens=10))['state'] == 'unknown'


def test_utc_day_is_accounted_at_completion_and_clock_regression_refuses(db):
    wake(db, started_at=86399., ended_at=86401.)
    assert module().wake_budget(db, actor_id='head', now=86399., limit=100)['used'] == 0
    assert module().wake_budget(db, actor_id='head', now=86402., limit=100)['used'] == 17
    assert module().wake_budget(db, actor_id='head', now=86400., limit=100)['state'] == 'unknown'
    assert module().wake_budget(db, actor_id='head', now=86402., limit=None)['state'] == 'unknown'


def test_account_windows_are_separate_unknown_is_not_unlimited_and_secrets_are_not_saved(db):
    q=module()
    raw={'five_hour':{'utilization':9,'resets_at':'2026-10-30T13:00:00Z'},
         'seven_day':{'utilization':54,'resets_at':'2026-11-03T20:00:00Z'},'accessToken':'SECRET'}
    row=q.record_account_snapshot(db,snapshot_id='s1',provider='claude',account_alias='claude-main',observed_at=1000.,raw=raw)
    assert row['windows']['five_hour']['remaining_percent']==91
    assert row['windows']['seven_day']['remaining_percent']==46
    assert 'SECRET' not in str(list(db.execute('SELECT payload_json FROM quota_account_snapshots')))
    assert q.record_account_snapshot(db,snapshot_id='s1',provider='claude',account_alias='claude-main',observed_at=1000.,raw=raw)==row
    row=q.record_account_snapshot(db,snapshot_id='s2',provider='codex',account_alias='codex-main',observed_at=1001.,raw=None,error='timeout')
    assert row['state']=='unknown' and row['windows']=={}


def test_retention_is_bounded_and_never_deletes_current_day(db):
    wake(db)
    assert module().prune(db,now=40*86400.,limit=1)['wakes']==1
    wake(db, wake_id='current', result_id='current', started_at=40*86400.,ended_at=40*86400.+1)
    assert module().prune(db,now=40*86400.+2,limit=1)['wakes']==0


def test_cap_is_not_a_zero_default_and_budget_exhaustion_is_typed(db):
    wake(db)
    assert module().wake_budget(db,actor_id='head',now=1002.,limit=10)['reason']=='budget_exhausted'
    assert module().wake_budget(db,actor_id='head',now=1002.,limit=0)['state']=='unknown'


def test_transport_failure_cannot_become_known_from_a_partial_body(db):
    row=module().record_account_snapshot(db,snapshot_id='partial',provider='claude',account_alias='a',observed_at=1000.,
        raw={'five_hour':{'utilization':9,'resets_at':2000},'seven_day':{'utilization':54,'resets_at':3000}},error='timeout')
    assert row['state']=='unknown'


def test_missing_codex_secondary_stays_unknown_and_account_id_is_not_saved(db):
    row=module().record_account_snapshot(db,snapshot_id='codex',provider='codex',account_alias='a',observed_at=1000.,
        raw={'accountId':'DO_NOT_STORE','ordinaryUsageAllowed':True,'rateLimits':{
            'primary':{'usedPercent':37,'windowDurationMins':10080,'resetsAt':2000},'secondary':None}})
    assert row['state']=='partial' and row['unknown_windows']==['codex:secondary']
    assert row['windows']['codex:primary']['remaining_percent']==63
    assert 'DO_NOT_STORE' not in str(row)


def test_existing_transaction_is_never_committed_by_a_receipt(db):
    db.execute('BEGIN')
    wake(db)
    assert db.in_transaction
    db.rollback()
    assert module().wake_budget(db,actor_id='head',now=1002.,limit=100)['used']==0


def test_cache_reads_are_recorded_but_not_counted_against_cap(db):
    row=wake(db,counters=dict(input_tokens=10,output_tokens=2,cache_read_input_tokens=1000000,cache_creation_input_tokens=5))
    assert row['state']=='known' and row['used']==17 and not row['over_cap']
    assert row['counters']['cache_read_input_tokens']==1000000


def test_known_failed_wake_and_cap_breach_keep_real_usage(db):
    row=wake(db,outcome='failed',per_wake_max=10)
    assert row['state']=='known' and row['used']==17 and row['over_cap']
    assert row['reason']=='wake_cap_breach'
    assert module().wake_budget(db,actor_id='head',now=1002.,limit=100)['used']==17


def test_calibration_has_absolute_ceiling_but_no_calibrated_cap(db):
    row=wake(db,calibration=True,per_wake_max=None,absolute_ceiling=10,sdk_version='test-sdk')
    assert row['state']=='known' and row['calibration'] is True
    assert row['over_cap'] is False and row['over_ceiling'] is True
    assert row['reason']=='absolute_ceiling_breach' and row['sdk_version']=='test-sdk'


def test_claude_account_guard_uses_used_percent_and_refuses_expired_snapshots(db):
    q=module()
    def snapshot(sid,at,five=9,week=54,reset=5000):
        q.record_account_snapshot(db,snapshot_id=sid,provider='claude',account_alias='a',observed_at=at,
            raw={'five_hour':{'utilization':five,'resets_at':reset},'seven_day':{'utilization':week,'resets_at':reset+100}})
    snapshot('a',1000)
    assert q.account_guard(db,account_alias='a',now=1001,max_age_s=60)['allowed'] is True
    snapshot('b',1002,five=50)
    row=q.account_guard(db,account_alias='a',now=1003,max_age_s=60)
    assert row['state']=='known' and row['allowed'] is False
    snapshot('c',1004,five=9,week=80)
    assert not q.account_guard(db,account_alias='a',now=1005,max_age_s=60)['allowed']
    assert q.account_guard(db,account_alias='a',now=1100,max_age_s=60)['state']=='unknown'
    snapshot('d',1200,reset=1210)
    assert q.account_guard(db,account_alias='a',now=1211,max_age_s=60)['state']=='unknown'


def test_calibrated_one_point_five_cap_can_be_fractional(db):
    assert wake(db,per_wake_max=25.5)['state']=='known'


def _cap_conn():
    import sqlite3 as _s
    conn=_s.connect(':memory:',isolation_level=None);conn.row_factory=_s.Row
    module().ensure_schema(conn);return conn


def _cap_wake(conn,wake_id,counters,**kw):
    return module().record_wake(conn,**{**dict(epoch_id='e',actor_id='head',generation=1,incarnation='1',session_id='s',wake_id=wake_id,account_alias='a',
                                        outcome='ok',counters=counters,result_id=None,started_at=1.,ended_at=2.,per_wake_max=200000,absolute_ceiling=400000,usage_scope='per_wake',sdk_version='test-sdk'),**kw})


def test_cache_read_is_recorded_but_not_counted():
    """Alpha ruling: cap counts input + output + cache create. Cache read scales with context."""
    doc=_cap_wake(_cap_conn(),'w',dict(input_tokens=10,output_tokens=20,cache_read_input_tokens=900000,cache_creation_input_tokens=30))
    assert doc['state']=='known' and doc['used']==60 and doc['counters']['cache_read_input_tokens']==900000


def test_cap_breach_is_a_known_count_not_unknown():
    """Gamma's read counts breaches apart from unknown usage, and a breach parks by itself."""
    doc=_cap_wake(_cap_conn(),'w',dict(input_tokens=300000,output_tokens=0,cache_read_input_tokens=0,cache_creation_input_tokens=0))
    assert doc['state']=='known' and doc['used']==300000 and doc['reason']=='wake_cap_breach'


def test_malformed_provider_result_id_cannot_erase_a_wake_receipt(db):
    row=wake(db,result_id={'untrusted':'value'})
    assert row['state']=='known' and row['result_id'] is None


@pytest.mark.parametrize('bucket,malformed',[('broken',None),('broken',[]),('',{})])
def test_malformed_declared_account_bucket_keeps_snapshot_partial(db,bucket,malformed):
    row=module().record_account_snapshot(db,snapshot_id='mixed',provider='codex',account_alias='fixture',observed_at=1000.,
        raw={'rateLimitsByLimitId':{'codex':{
            'primary':{'usedPercent':10,'resetsAt':2000},
            'secondary':{'usedPercent':20,'resetsAt':3000}},bucket:malformed}})
    assert row['state']=='partial' and row['unknown_windows']
    assert row['windows']['codex:primary']['remaining_percent']==90

import hashlib
import json
import pytest
from sniper.office import grader,store


def setup(tmp_path,kind='discovery'):
    conn=store.open_store(tmp_path/'office.db')
    registry=tmp_path/'registry.json'
    registry.write_text(json.dumps({'credit_metrics':['entered_coins','brier_skill']}))
    epoch=dict(epoch_kind=kind,metric_registry_hash=hashlib.sha256(registry.read_bytes()).hexdigest(),registered_look_id='look',registered_look_at=200.)
    conn.execute('INSERT INTO epochs VALUES(?,?)',('e',json.dumps(epoch)))
    conn.commit()
    a=dict(agent_id='fired-agent',metric='entered_coins',value=None,sample_n=5,censored_n=2,note='Descriptive only.',epoch_id='e',epoch_kind=kind,look_id=None if kind=='discovery' else 'look',additive=False)
    return conn,registry,a


def release(conn,registry,a,at=100.):
    return grader.release_aggregate(conn,epoch_id='e',kind='descriptive_credit',released_at=at,aggregates=a,source_heads={'grades':'a'*64},authorize_release=lambda *args:True,credit_registry_path=registry)


def test_live_discovery_credit_keeps_null_and_fired_identity(tmp_path):
    conn,registry,a=setup(tmp_path)
    release(conn,registry,a)
    row=json.loads(conn.execute('SELECT payload_json FROM sealed_views').fetchone()[0])
    assert row['aggregates']['value'] is None and row['aggregates']['agent_id']=='fired-agent'
    assert row['aggregates']['additive'] is False


def test_epoch_kind_comes_from_sealer_state_and_look_is_enforced(tmp_path):
    conn,registry,a=setup(tmp_path,'evaluated')
    with pytest.raises(ValueError,match='credit_look_not_released'):
        release(conn,registry,a)
    with pytest.raises(ValueError,match='credit_epoch_kind_mismatch'):
        release(conn,registry,dict(a,epoch_kind='discovery',look_id=None))
    release(conn,registry,a,at=200.)
    with pytest.raises(ValueError,match='unknown_credit_metric'):
        release(conn,registry,dict(a,metric='made_up'),at=201.)


def test_same_look_can_release_each_agent_without_summing_credits(tmp_path):
    conn,registry,a=setup(tmp_path)
    first=release(conn,registry,a)
    release(conn,registry,dict(a,agent_id='second-agent'))
    assert release(conn,registry,a)==first
    assert conn.execute('SELECT COUNT(*) FROM sealed_views').fetchone()[0]==2
    with pytest.raises(ValueError,match='frozen_release_changed'):
        release(conn,registry,dict(a,value=99.))

import json
import pytest
from sniper.services import brain_health_tile as tile


def test_missing_and_valid_fresh_stale_error(tmp_path):
    p=tmp_path/'health.json'
    assert tile.summary(p,1000)['state']=='missing'
    data={'written_ts':1000,'metrics':{'facts_live':5,'share_verified':0.2,'by_status':{'evidence_broken':1}}}
    p.write_text(json.dumps(data))
    assert tile.summary(p,4600)['state']=='ok'
    assert tile.summary(p,1000+37*3600)['state']=='stale'
    data['error']='BrainUnavailable';p.write_text(json.dumps(data))
    assert tile.summary(p,1001)['state']=='error'


@pytest.mark.parametrize('data',[[],None,{}, {'written_ts':'bad'}, {'written_ts':float('nan')},
                                    {'written_ts':2000,'metrics':{}}, {'written_ts':1000,'metrics':[]}])
def test_malformed_or_future_health_is_not_healthy(tmp_path,data):
    p=tmp_path/'health.json';p.write_text(json.dumps(data))
    assert tile.summary(p,1000)['state']=='error'


def test_absent_counts_remain_unknown(tmp_path):
    p=tmp_path/'health.json';p.write_text(json.dumps({'written_ts':1000,'metrics':{}}))
    out=tile.summary(p,1001)
    assert out['facts_live'] is None and out['evidence_broken'] is None

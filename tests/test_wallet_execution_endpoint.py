import json
from datetime import datetime,timezone
from sniper.services.wallet_execution_research import read_coverage


def artifact(path, **extra):
    body={'protocol':'wallet-execution-research-v1','generated_at':'2026-10-04T12:00:00+00:00',
          'source_status':'ok','cells':[{'wallet':'u','chain':'56','signals':3,'complete_episodes':0,'strategy':None},
                                       {'wallet':'u','chain':'solana','signals':2,'complete_episodes':0,'strategy':None}]}
    body.update(extra); path.write_text(json.dumps(body))


def test_read_is_chain_wallet_scoped_and_does_not_create_missing_file(tmp_path):
    p=tmp_path/'coverage.json'
    assert read_coverage(p,chain='56',now='2026-10-04T12:00:00+00:00')['state']=='unavailable'
    assert not p.exists()
    artifact(p)
    r=read_coverage(p,chain='56',wallet='u',now='2026-10-04T12:00:30+00:00')
    assert r['state']=='ok' and len(r['cells'])==1 and r['cells'][0]['signals']==3
    assert r['cells'][0]['strategy'] is None


def test_stale_future_malformed_and_wrong_protocol_never_look_fresh(tmp_path):
    p=tmp_path/'c.json'; artifact(p)
    assert read_coverage(p,chain='56',now='2026-10-04T12:03:00+00:00')['state']=='stale'
    assert read_coverage(p,chain='56',now='2026-10-04T11:59:00+00:00')['state']=='unavailable'
    p.write_text('{wrong'); assert read_coverage(p,chain='56',now='2026-10-04T12:00:00+00:00')['state']=='unavailable'
    artifact(p,protocol='old'); assert read_coverage(p,chain='56',now='2026-10-04T12:00:00+00:00')['state']=='unavailable'


def test_endpoint_rejects_bad_chain_and_uses_local_artifact(monkeypatch,tmp_path):
    from flask import Flask
    from test_memecoin_promotion_endpoint import _client
    from sniper.util import state_paths
    monkeypatch.setattr(state_paths,'state_path',lambda *a: tmp_path.joinpath(*a))
    p=tmp_path/'data/wallet_execution_coverage.json'; p.parent.mkdir(); artifact(p,generated_at=datetime.now(timezone.utc).isoformat())
    client,_=_client()
    r=client.get('/api/memecoin/wallet-map/execution-research?chain=bsc&wallet=u')
    assert r.status_code==200 and len(r.get_json()['cells'])==1
    assert client.get('/api/memecoin/wallet-map/execution-research?chain=nope').status_code==400

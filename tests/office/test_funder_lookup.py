"""Funder lookup on synthetic databases and a fake provider. No network, no key."""
import json
import sqlite3

import pytest

from sniper.office import funder_lookup as F

W = 'So11111111111111111111111111111111111111112'
A = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
B = 'ComputeBudget111111111111111111111111111111'


def dbs(tmp_path, cluster=(), graph=(), runner=()):
    c = sqlite3.connect(tmp_path / 'c.db'); c.execute('CREATE TABLE cluster_funder_cache(owner,funder,status,resolved_at,funder_source,source_detail)')
    c.executemany('INSERT INTO cluster_funder_cache VALUES(?,?,?,?,?,?)', cluster); c.commit(); c.close()
    g = sqlite3.connect(tmp_path / 'g.db'); g.execute('CREATE TABLE memecoin_wallet_funders(wallet,funder,status,funding_sig,funded_at,read_at)')
    g.executemany('INSERT INTO memecoin_wallet_funders VALUES(?,?,?,?,?,?)', graph); g.commit(); g.close()
    r = sqlite3.connect(tmp_path / 'r.db'); r.execute('CREATE TABLE wallet_funders(address,funder,status,resolved_at)')
    r.executemany('INSERT INTO wallet_funders VALUES(?,?,?,?)', runner); r.commit(); r.close()
    return dict(cluster_db=tmp_path / 'c.db', graph_db=tmp_path / 'g.db', runner_db=tmp_path / 'r.db')


class Provider:
    def __init__(self, status=200, body=None, boom=False): self.s, self.b, self.boom, self.calls = status, body, boom, 0
    def __call__(self, wallet):
        self.calls += 1
        if self.boom: raise OSError('down')
        return self.s, self.b


def test_a_held_funder_costs_no_provider_call_and_keeps_the_signature(tmp_path):
    p = Provider()
    r = F.funder_of(W, fetch=p, **dbs(tmp_path, cluster=[(W, A, 'resolved', 1., 'funded_by', json.dumps({'signature': 'sig1', 'slot': 9, 'date': 'd'}))]))
    assert r['state'] == 'resolved' and r['funder'] == A and p.calls == 0 and r['provider_called'] is False
    assert r['evidence'][0]['signature'] == 'sig1' and r['evidence'][0]['source'] == 'cluster_funder_cache:funded_by'


def test_sources_that_disagree_are_a_conflict_with_none_chosen(tmp_path):
    r = F.funder_of(W, **dbs(tmp_path, cluster=[(W, A, 'resolved', 1., 'direct_tx', None)], graph=[(W, B, 'resolved', 'sg', 5., 6.)]))
    assert r['state'] == 'conflict' and r['funders'] == sorted([A, B]) and 'funder' not in r


def test_unresolved_or_unfunded_local_rows_are_not_a_funder_so_the_provider_is_asked(tmp_path):
    p = Provider(200, {'funder': A, 'funderType': 'exchange', 'signature': 's2', 'slot': 3, 'date': 'x'})
    r = F.funder_of(W, fetch=p, **dbs(tmp_path, cluster=[(W, None, 'unresolved', 1., None, None)], graph=[(W, None, 'no_funding_tx', None, None, 1.)]))
    assert (r['state'], r['funder'], r['source'], r['signature'], r['funder_type'], p.calls) == ('resolved', A, 'helius_funded_by', 's2', 'exchange', 1)


@pytest.mark.parametrize('status,body,state,reason', [(404, None, 'absent', 'provider_404'), (200, {'funder': None}, 'absent', 'provider_no_funder'),
                                                      (200, {'funder': W}, 'absent', 'provider_no_funder'), (401, None, 'unknown', 'provider_auth'),
                                                      (429, None, 'unknown', 'provider_quota'), (500, None, 'unknown', 'provider_500'),
                                                      (200, {'funder': 'not an address'}, 'unknown', 'provider_malformed'), (200, [1], 'unknown', 'provider_200')])
def test_provider_answers_are_typed_and_only_a_real_none_is_absent(tmp_path, status, body, state, reason):
    r = F.funder_of(W, fetch=Provider(status, body), **dbs(tmp_path))
    assert (r['state'], r['reason']) == (state, reason) and r.get('quota', False) == (status == 429)


def test_unreachable_provider_and_no_provider_are_unknown_never_absent(tmp_path):
    d = dbs(tmp_path)
    assert F.funder_of(W, fetch=Provider(boom=True), **d)['reason'] == 'provider_unreachable'
    assert F.funder_of(W, **d)['state'] == 'unknown'


def test_unreadable_local_source_is_reported_not_read_as_empty(tmp_path):
    r = F.funder_of(W, cluster_db=tmp_path / 'missing.db', fetch=Provider(404))
    assert r['unreadable_sources'] == ['cluster_funder_cache'] and r['state'] == 'absent'
    assert 'cluster_funder_cache' in F.funder_of(W, cluster_db=tmp_path / 'missing.db')['unreadable_sources']


def test_invalid_wallet_and_self_funding_are_refused(tmp_path):
    assert F.funder_of('abc')['reason'] == 'wallet_invalid'
    assert F.funder_of(W, **dbs(tmp_path, runner=[(W, W, 'resolved', 1.)]))['state'] == 'unknown'          # a wallet is not its own funder

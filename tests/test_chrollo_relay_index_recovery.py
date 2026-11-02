"""Exact chrollo buy: lookup committed order before the fill-hash index, then bounded recovery."""
import copy
import gzip
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from sniper.services import memecoin_relay_initiation as relay

FRAME = json.load(gzip.open(Path(__file__).parent / 'fixtures/memecoin/chrollo_relay_origin_20261005.json.gz', 'rt'))


@pytest.mark.parametrize('mode', ['hash_lag', 'delayed', 'persistent', 'forged', 'healthy', 'pending', 'failed'])
def test_order_lookup_and_delayed_index_keep_the_exact_signed_binding(monkeypatch, mode):
    calls, sleeps, origin_reads = [], [], []
    origin = copy.deepcopy(FRAME['origin'])
    if mode == 'forged': origin['result']['meta']['postTokenBalances'] = origin['result']['meta']['preTokenBalances']
    def fetch(url, **kwargs):
        if 'requests/' not in url:
            origin_reads.append(url)
            return origin
        query = parse_qs(urlsplit(url).query)
        calls.append(query)
        if mode == 'hash_lag' and 'hash' in query: return {'requests': []}
        if mode == 'persistent' or (mode in {'delayed', 'forged'} and sum(sleeps) < 5):
            return {'requests': []}
        if mode == 'pending' and sum(sleeps) < 5: return {'requests': [{'status': 'pending'}]}
        if mode == 'failed': return {'requests': [{'status': 'failure'}]}
        return FRAME['request']
    monkeypatch.setattr(relay.time, 'sleep', sleeps.append)
    verifier = relay.WatcherVerifier(poll_s=0, requests_per_s=0, identities_fn=lambda _: FRAME['identities'], chains=FRAME['chains']['chains'], fetch=fetch)
    item = {'row': {'source_user_id': FRAME['proof']['source_user_id'],
                    'buy': {'source_initiation': FRAME['proof']}}, 'transaction': FRAME['transaction']}
    if mode in {'persistent', 'forged', 'failed'}:
        with pytest.raises(relay.ProofError, match='^' + ('relay_request_not_indexed' if mode == 'persistent' else 'relay_request_failed' if mode == 'failed' else 'origin_payment_unproved') + '$'):
            verifier(item)
    else:
        proof = verifier(item)['row']['buy']['source_initiation']
        assert proof == FRAME['verified'] and proof['origin_amount_atomic'] == '800000000'
    assert all(q.get('orderId') == [FRAME['verified']['order_id']] and 'hash' not in q for q in calls)
    if mode in {'hash_lag', 'healthy', 'failed'}: assert len(calls) == 1 and sleeps == []
    else: assert len(calls) == 7 and sleeps == [.1, .2, .4, .8, 1.6, 3.2]
    assert len(origin_reads) == (0 if mode in {'persistent', 'failed'} else 1)
    assert item['row']['buy']['source_initiation']['proof_version'] == 'relay-envelope-other-target-v1'

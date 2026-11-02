"""Captured Princess buy; incomplete response mutations model provider indexing lag."""
import copy
import gzip
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_relay_initiation as relay

FRAME = json.load(gzip.open(Path(__file__).parent / 'fixtures/memecoin/princess_relay_origin_20261006.json.gz', 'rt'))


@pytest.mark.parametrize('mode', ['healthy', 'order_lag', 'receipt_lag', 'persistent', 'forged'])
def test_signed_princess_buy_recovers_incomplete_evidence_without_relaxing_proof(monkeypatch, mode):
    calls, sleeps = {'request': 0, 'origin': 0}, []
    def fetch(url, **kwargs):
        stage = 'request' if 'requests/' in url else 'origin'
        calls[stage] += 1
        frame = copy.deepcopy(FRAME[stage])
        if stage == 'request' and (mode == 'persistent' or (mode == 'order_lag' and sum(sleeps) < .7)):
            frame['requests'][0].pop('orderData')
        if stage == 'origin':
            if mode == 'receipt_lag' and sum(sleeps) < .7:
                frame['result']['blockTime'] = None
            if mode == 'forged':
                frame['result']['meta']['postTokenBalances'] = frame['result']['meta']['preTokenBalances']
        return frame
    monkeypatch.setattr(relay.time, 'sleep', sleeps.append)
    verifier = relay.WatcherVerifier(poll_s=0, requests_per_s=0, identities_fn=lambda _: FRAME['identities'], chains=FRAME['chains']['chains'], fetch=fetch)
    item = {'row': {'source_user_id': FRAME['proof']['source_user_id'],
                    'buy': {'source_initiation': copy.deepcopy(FRAME['proof'])}},
            'transaction': FRAME['transaction']}
    if mode in {'persistent', 'forged'}:
        with pytest.raises(relay.ProofError, match='^' + ('malformed_origin_evidence' if mode == 'persistent' else 'origin_payment_unproved') + '$'):
            verifier(item)
        assert calls['request'] == (7 if mode == 'persistent' else 1)
    else:
        result = verifier(item)['row']['buy']['source_initiation']
        assert result == FRAME['verified'] and result['origin_amount_atomic'] == '1507000000'
        assert calls == ({'request': 1, 'origin': 1} if mode == 'healthy' else {'request': 4, 'origin': 4})
    assert sleeps == ([] if mode in {'healthy', 'forged'} else [.1, .2, .4] if mode != 'persistent' else [.1, .2, .4, .8, 1.6, 3.2])
    assert item['row']['buy']['source_initiation']['proof_version'] == 'relay-envelope-other-target-v1'

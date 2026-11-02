"""Recorded ANIMA buy: transient rate limits must not become permanent initiation refusals."""
import gzip
import json
from pathlib import Path

import pytest

from sniper.services import memecoin_relay_initiation as relay

FRAME = json.load(gzip.open(Path(__file__).parent / 'fixtures/memecoin/anima_relay_origin_20261005.json.gz', 'rt'))


@pytest.mark.parametrize('limited_stage', ['request', 'origin', 'persistent'])
def test_anima_rate_limit_recovers_only_with_verified_signed_payment(monkeypatch, limited_stage):
    calls, sleeps = [], []
    def fetch(url, **kwargs):
        stage = 'request' if 'requests/' in url else 'origin'
        calls.append(stage)
        if limited_stage == 'persistent' or (stage == limited_stage and calls.count(stage) == 1):
            raise relay.ProofError('origin_rate_limit')
        return FRAME['request'] if stage == 'request' else FRAME['origin']
    monkeypatch.setattr(relay.time, 'sleep', sleeps.append)
    verifier = relay.WatcherVerifier(poll_s=0, requests_per_s=0, identities_fn=lambda _: FRAME['identities'], chains=FRAME['chains']['chains'], fetch=fetch)
    item = {'row': {'source_user_id': FRAME['proof']['source_user_id'],
                    'buy': {'source_initiation': FRAME['proof']}}, 'transaction': FRAME['transaction']}
    if limited_stage == 'persistent':
        with pytest.raises(relay.ProofError, match='^origin_rate_limit:relay_requests$'):
            verifier(item)
        assert calls == ['request'] * 3 and sleeps == [.1, .1]
    else:
        result = verifier(item)['row']['buy']['source_initiation']
        assert result == FRAME['verified']
        assert result['origin_amount_atomic'] == '2500000000'
        assert sleeps == [.1]
    assert item['row']['buy']['source_initiation']['proof_version'] == 'relay-envelope-other-target-v1'



# --- which host refused (Alpha 2026-10-06: origin_rate_limit at 21:15:45Z named no host) ----------

def _item():
    return {'row': {'source_user_id': FRAME['proof']['source_user_id'],
                    'buy': {'source_initiation': FRAME['proof']}}, 'transaction': FRAME['transaction']}


@pytest.mark.parametrize('stage,host', [('chains', 'relay_chains'), ('request', 'relay_requests'),
                                        ('origin', 'solana_rpc')])
def test_a_refusal_names_the_host_that_refused(monkeypatch, stage, host):
    monkeypatch.setattr(relay.time, 'sleep', lambda _s: None)

    def fetch(url, **kwargs):
        got = 'chains' if url.endswith('/chains') else 'request' if 'requests/' in url else 'origin'
        if got == stage:
            raise relay.ProofError('origin_rate_limit')
        return {'chains': FRAME['chains']['chains']} if got == 'chains' else (
            FRAME['request'] if got == 'request' else FRAME['origin'])
    verifier = relay.WatcherVerifier(poll_s=0, requests_per_s=0, identities_fn=lambda _: FRAME['identities'], fetch=fetch)
    with pytest.raises(relay.ProofError, match='^origin_rate_limit:%s$' % host):
        verifier(_item())


def test_chains_sends_the_key_and_a_refusal_does_not_poison_the_cache(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _s: None)
    seen, refuse = [], [True]

    def fetch(url, **kwargs):
        if url.endswith('/chains'):
            seen.append((kwargs.get('headers') or {}).get('x-api-key'))
            if refuse[0]:
                raise relay.ProofError('origin_rate_limit')
            return {'chains': FRAME['chains']['chains']}
        return FRAME['request'] if 'requests/' in url else FRAME['origin']
    verifier = relay.WatcherVerifier(poll_s=0, requests_per_s=0, identities_fn=lambda _: FRAME['identities'], fetch=fetch, api_key='K')
    with pytest.raises(relay.ProofError, match='^origin_rate_limit:relay_chains$'):
        verifier(_item())
    assert verifier.chains is None and set(seen) == {'K'}
    refuse[0] = False
    assert verifier(_item())['row']['buy']['source_initiation'] == FRAME['verified']


def test_a_keyed_dict_error_and_an_http_error_keep_their_own_reasons():
    verifier = relay.WatcherVerifier(poll_s=0, requests_per_s=0, identities_fn=lambda _: [], fetch=lambda *_a, **_k: (_ for _ in ()).throw(
        relay.ProofError('origin_http_503')), chains=[])
    with pytest.raises(relay.ProofError, match='^origin_http_503:relay_requests$'):
        verifier._verify(_item())

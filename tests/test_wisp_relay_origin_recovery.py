"""Exact WISP order plus explicitly mutated partial-provider responses; no trade replay."""
import copy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_relay_initiation as relay
from sniper.services.memecoin_source_initiation import source_initiation_refusal
from sniper.services.memecoin_evm_onchain_watch import relayer_purchase_from_receipt

FRAME = json.load(gzip.open(Path(__file__).parent / 'fixtures/memecoin/wisp_relay_origin_20261004.json.gz', 'rt'))


def run(monkeypatch, *, partial=None, persist=False, forged=False):
    counts = {'request': 0, 'origin': 0}
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    def fetch(url, **kwargs):
        if 'requests/v2?' in url:
            counts['request'] += 1
            value = copy.deepcopy(FRAME['request'])
            if partial == 'order' and (persist or counts['request'] == 1):
                value['requests'][0].pop('orderData')
            return value
        counts['origin'] += 1
        value = copy.deepcopy(FRAME['origin'])
        if partial == 'block_time' and (persist or counts['origin'] == 1):
            value['result']['blockTime'] = None
        if forged:
            value['result']['meta']['postTokenBalances'] = value['result']['meta']['preTokenBalances']
        return value
    verifier = relay.WatcherVerifier(poll_s=0, requests_per_s=0, identities_fn=lambda _: FRAME['identities'], chains=FRAME['chains']['chains'], fetch=fetch)
    item = {'row': {'source_user_id': FRAME['proof']['source_user_id'],
                    'buy': {'source_initiation': copy.deepcopy(FRAME['proof'])}},
            'transaction': FRAME['transaction']}
    return verifier, item, counts


def admitted(result):
    proof = result['row']['buy']['source_initiation']
    event = SimpleNamespace(signal_source='onchain_watch',
        fomo_event_id=f"onchain:56:{proof['tx_hash']}:{proof['mint']}", source_wallet=proof['beneficiary'],
        source_block_number=proof['block_number'], source_user_id=proof['source_user_id'],
        mint=proof['mint'], source_initiation=proof)
    assert source_initiation_refusal(event, '56') is None
    assert proof['origin_amount_atomic'] == '5000000000'


def test_exact_wisp_signed_payment_passes_without_retry(monkeypatch):
    delivery = relayer_purchase_from_receipt(FRAME['receipt'], FRAME['transaction'],
        FRAME['proof']['beneficiary'], chain='56', contracts_verified=True, any_route=True)
    assert delivery['output_atomic'] == int(FRAME['proof']['acquired_atomic'])
    assert delivery['proof_version'] == FRAME['proof']['proof_version']
    verifier, item, counts = run(monkeypatch)
    admitted(verifier(item))
    assert counts == {'request': 1, 'origin': 1}
    assert FRAME['transaction']['hash'] == FRAME['receipt']['transactionHash'] == FRAME['proof']['tx_hash']


@pytest.mark.parametrize('partial', ['order', 'block_time'])
def test_partial_response_recovers_in_existing_background_budget(monkeypatch, partial):
    verifier, item, counts = run(monkeypatch, partial=partial)
    admitted(verifier(item))
    assert counts['request'] == 2


@pytest.mark.parametrize('partial', ['order', 'block_time'])
def test_persistent_malformed_evidence_still_refuses_with_bounded_reads(monkeypatch, partial):
    verifier, item, counts = run(monkeypatch, partial=partial, persist=True)
    with pytest.raises(relay.ProofError, match='malformed_origin_evidence'):
        verifier(item)
    assert counts['request'] == 7
    assert item['row']['buy']['source_initiation']['proof_version'] == 'relay-envelope-other-target-v1'


def test_forged_origin_payment_is_never_retried_or_admitted(monkeypatch):
    verifier, item, counts = run(monkeypatch, forged=True)
    with pytest.raises(relay.ProofError, match='origin_payment_unproved'):
        verifier(item)
    assert counts == {'request': 1, 'origin': 1}

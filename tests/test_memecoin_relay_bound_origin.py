"""Relay B: relay-origin-bound-v1 for GET /requests/v3, which carries no order preimage (2026-10-07).

v2 retires 2026-11-24 and is already rate-limited (8453 3/3 limited, 4663 7/18, 56 3/4 on 10-07).
v3 has no orderData, salt or deadline, so the v1 order-commitment check cannot run. B keeps the
on-chain binding instead: the origin deposit commits the orderId (Solana: raw[16:]; EVM: last
arg), the destination calldata ends with the same orderId, and the signer, amount and balance
deltas come from RPC. It ships default off on both sides: MEMECOIN_RELAY_REQUESTS_VERSION=v2 and
MEMECOIN_RELAY_BOUND_PROOF_ACCEPT=0.
"""
import copy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_relay_initiation as relay
from sniper.services.memecoin_source_initiation import source_initiation_refusal
from sniper.services.memecoin_evm_onchain_watch import relayer_purchase_from_receipt, price_relayer_purchase

FRAME = json.load(gzip.open(Path(__file__).parent / 'fixtures/memecoin/hooks_relay_initiation_20261004.json.gz', 'rt'))
UID = 'a3d6096e-59eb-5684-a9fe-0048e07a0eb4'


def v3_of(v2):
    """The v2 request as GET /requests/v3 returns it (fields per the 2026-10-07 recorded pair in
    fixtures/memecoin/relay_requests_2dcf925a): no orderData, inTxs/outTxs `hash` -> `txHash`,
    metadata.route -> data.route.{quoted,actual}, sender and supersededByRequestId added."""
    r = copy.deepcopy(v2)
    r.pop('orderData', None)
    for side in ('inTxs', 'outTxs'):
        for tx in r['data'][side]:
            tx['txHash'] = tx.pop('hash')
    route = r['data'].pop('metadata', {}).get('route', {})
    r['data']['route'] = {'quoted': route, 'actual': copy.deepcopy(route)}
    r.update(sender=r['user'], supersededByRequestId=None, requestType='Crosschain Swap')
    return r


def inputs():
    request = copy.deepcopy(FRAME['relay']['requests'][0])
    transaction, receipt = copy.deepcopy(FRAME['transaction-public']['result']), copy.deepcopy(FRAME['receipt-public']['result'])
    proof = relayer_purchase_from_receipt(receipt, transaction, request['recipient'], chain='56', contracts_verified=True, any_route=True)
    buy = price_relayer_purchase(proof, decimals_fn=lambda _: 18, price_fn=lambda _: 1)
    buy['source_initiation']['source_user_id'] = UID
    return v3_of(request), copy.deepcopy(FRAME['origin-sol']['result']), buy['source_initiation'], transaction


def bound(request=None, origin=None, proof=None, destination=None, identity=None):
    req, org, prf, dst = inputs()
    return relay.verify_bound_origin(request or req, origin or org, proof or prf, destination or dst,
                                     identities=identity if identity is not None else FRAME['identity'],
                                     chains=FRAME['chains'])


def test_the_recorded_frame_verifies_bound_from_its_v3_shape():
    out = bound()
    req = FRAME['relay']['requests'][0]
    assert out['proof_version'] == relay.BOUND_PROOF_VERSION == 'relay-origin-bound-v1'
    assert out['order_id'] == req['protocol']['orderId']
    assert out['origin_wallet'] == FRAME['identity'][0]['address']
    assert out['origin_amount_atomic'] == '600000000'


def test_v1_and_b_agree_on_the_recorded_frame():
    _req, org, prf, dst = inputs()
    v1 = relay.verify_origin(copy.deepcopy(FRAME['relay']['requests'][0]), org, prf, dst,
                             identities=FRAME['identity'], chains=FRAME['chains'])
    b = bound()
    keys = ('order_id', 'origin_wallet', 'origin_network_id', 'origin_tx', 'origin_slot',
            'origin_amount_atomic', 'origin_identity_user_id')
    assert {k: v1[k] for k in keys} == {k: b[k] for k in keys}


@pytest.mark.parametrize('mutation', ['destination_oid', 'deposit_oid', 'api_oid'])
def test_an_order_id_that_does_not_bind_origin_to_destination_refuses(mutation):
    req, org, prf, dst = inputs()
    if mutation == 'destination_oid':
        dst['input'] = dst['input'][:-64] + '11' * 32
    elif mutation == 'deposit_oid':
        req['protocol']['orderId'] = '0x' + '11' * 32       # API label disagrees with both txs
    else:
        req['protocol']['orderId'] = 'not-hex'
    with pytest.raises(relay.ProofError):
        relay.verify_bound_origin(req, org, prf, dst, identities=FRAME['identity'], chains=FRAME['chains'])


@pytest.mark.parametrize('mutation', ['no_signer', 'foreign_user', 'unverified_identity', 'wrong_uid'])
def test_a_signer_other_than_the_source_wallet_refuses(mutation):
    req, org, prf, dst = inputs()
    identity = copy.deepcopy(FRAME['identity'])
    if mutation == 'no_signer':
        for key in org['transaction']['message']['accountKeys']:
            key['signer'] = False
    elif mutation == 'foreign_user':
        req['user'] = 'F7p3dFrjRTbtRp8FRF6qHLomXbKRBzpvBLjtQcfcgmNe'
    elif mutation == 'unverified_identity':
        identity[0]['status'] = 'candidate'
    else:
        identity[0]['source_user_id'] = 'someone-else'
    with pytest.raises(relay.ProofError):
        relay.verify_bound_origin(req, org, prf, dst, identities=identity, chains=FRAME['chains'])


@pytest.mark.parametrize('mutation', ['mint', 'beneficiary', 'route_mint', 'underfilled', 'no_route'])
def test_a_mint_or_beneficiary_mismatch_with_the_fill_receipt_refuses(mutation):
    req, org, prf, dst = inputs()
    out = req['data']['route']['quoted']['destination']['outputCurrency']
    if mutation == 'mint':
        prf = dict(prf, mint='0x' + '01' * 20)
    elif mutation == 'beneficiary':
        req['recipient'] = '0x' + '01' * 20
    elif mutation == 'route_mint':
        out['currency']['address'] = '0x' + '01' * 20
    elif mutation == 'underfilled':
        out['minimumAmount'] = str(int(prf['acquired_atomic']) + 1)
    else:
        req['data'].pop('route')
    with pytest.raises(relay.ProofError):
        relay.verify_bound_origin(req, org, prf, dst, identities=FRAME['identity'], chains=FRAME['chains'])


def test_v1_code_on_a_v3_shape_and_b_on_a_v2_shape_both_fail_closed():
    req, org, prf, dst = inputs()
    with pytest.raises(relay.ProofError):                       # txHash rename, no orderData
        relay.verify_origin(req, org, prf, dst, identities=FRAME['identity'], chains=FRAME['chains'])
    v2 = copy.deepcopy(FRAME['relay']['requests'][0])
    with pytest.raises(relay.ProofError):                       # `hash`, no data.route
        relay.verify_bound_origin(v2, org, prf, dst, identities=FRAME['identity'], chains=FRAME['chains'])


def _verifier(answer, *, api_key='key', calls=None):
    def fetch(url, **kwargs):
        if calls is not None:
            calls.append((url, kwargs.get('headers') or {}))
        if 'requests/' in url:
            return answer
        if url.endswith('/chains'):
            return {'chains': FRAME['chains']}
        return FRAME['origin-sol']
    return relay.WatcherVerifier(lambda _: FRAME['identity'], chains=FRAME['chains'], fetch=fetch,
                                 api_key=api_key, requests_version='v3')


def _item():
    _, _, prf, dst = inputs()
    return {'row': {'source_user_id': UID, 'buy': {'source_initiation': prf}}, 'transaction': dst}


@pytest.mark.parametrize('version', ['v2', 'v3'])
def test_cold_solana_origin_only_fetches_catalogue_for_legacy_commitments(version):
    calls = []
    request = inputs()[0] if version == 'v3' else FRAME['relay']['requests'][0]

    def fetch(url, **kwargs):
        calls.append(url)
        if url.endswith('/chains'):
            assert version == 'v2', 'bound Solana proofs must not depend on the catalogue'
            assert kwargs['headers']['x-api-key'] == 'key'
            return {'chains': FRAME['chains']}
        if 'requests/' in url:
            return {'requests': [request]}
        return FRAME['origin-sol']

    verifier = relay.WatcherVerifier(lambda _: FRAME['identity'], fetch=fetch,
                                     api_key='key', requests_version=version)
    result = verifier(_item())['row']['buy']['source_initiation']
    assert result['proof_version'] == (relay.BOUND_PROOF_VERSION if version == 'v3' else relay.PROOF_VERSION)
    assert sum(url.endswith('/chains') for url in calls) == (version == 'v2')


def test_bound_evm_origin_still_requires_the_catalogue():
    request = inputs()[0]
    request['protocol']['deposit']['origin']['chainId'] = 8453

    def fetch(url, **kwargs):
        if url.endswith('/chains'):
            raise relay.ProofError('catalogue_required')
        assert 'requests/v3?' in url
        return {'requests': [request]}

    verifier = relay.WatcherVerifier(lambda _: [], fetch=fetch, api_key='key', requests_version='v3')
    with pytest.raises(relay.ProofError, match='catalogue_required'):
        verifier(_item())


def test_the_v3_verifier_asks_v3_with_the_key_and_returns_a_bound_proof(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    calls = []
    req = inputs()[0]
    out = _verifier({'requests': [req]}, calls=calls)(_item())
    assert out['row']['buy']['source_initiation']['proof_version'] == relay.BOUND_PROOF_VERSION
    [(url, headers)] = [c for c in calls if 'requests/' in c[0]]
    assert url.startswith('https://api.relay.link/requests/v3?orderId=0x')
    assert 'includeOrderData' not in url and headers.get('x-api-key') == 'key'


def test_a_superseded_request_refuses(monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    req = dict(inputs()[0], supersededByRequestId='0x' + 'aa' * 32)
    with pytest.raises(relay.ProofError, match='relay_request_superseded'):
        _verifier({'requests': [req]})(_item())


def test_v3_without_a_key_never_asks_and_refuses():
    calls = []
    with pytest.raises(relay.ProofError, match='relay_api_key_missing'):
        _verifier({'requests': [inputs()[0]]}, api_key='', calls=calls)(_item())
    assert not [c for c in calls if 'requests/' in c[0]]


def test_a_401_is_a_typed_http_refusal(monkeypatch):
    import io
    import urllib.error

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{}'))
    monkeypatch.setattr(relay.urllib.request, 'urlopen', refuse)
    with pytest.raises(relay.ProofError, match='origin_http_401'):
        relay.read_json('https://api.relay.link/requests/v3?orderId=0xab', headers={'x-api-key': 'k'})


@pytest.mark.parametrize('status, reason', [
    ('failure', 'relay_request_failed'), ('refund', 'relay_request_failed'),
    ('pending', 'relay_not_settled'), ('depositing', 'relay_not_settled'),
    ('submitted', 'relay_not_settled'), ('waiting', 'relay_not_settled'),
    ('delayed', 'malformed_origin_evidence'), ('something_new', 'malformed_origin_evidence'),
])
def test_every_v3_status_maps_and_an_unknown_one_fails_closed(status, reason, monkeypatch):
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    req = dict(inputs()[0], status=status)
    with pytest.raises(relay.ProofError, match=reason):
        _verifier({'requests': [req]})(_item())


def _ev(proof, chain='56'):
    return SimpleNamespace(signal_source='onchain_watch',
                           fomo_event_id=f"onchain:{chain}:{proof['tx_hash']}:{proof['mint']}",
                           source_wallet=proof['beneficiary'], source_block_number=proof['block_number'],
                           source_user_id=UID, mint=proof['mint'], source_initiation=proof)


def test_the_runner_refuses_a_bound_proof_unless_the_operator_turns_it_on(monkeypatch):
    from sniper.settings import memecoin as cfg
    proof = bound()
    monkeypatch.setattr(cfg, 'MEMECOIN_RELAY_BOUND_PROOF_ACCEPT', False)
    assert source_initiation_refusal(_ev(proof), '56') == 'wallet_initiation_unproved'
    monkeypatch.setattr(cfg, 'MEMECOIN_RELAY_BOUND_PROOF_ACCEPT', True)
    assert source_initiation_refusal(_ev(proof), '56') is None
    assert source_initiation_refusal(_ev(dict(proof, origin_tx='short')), '56') == 'receipt_identity_mismatch'


def test_both_knobs_default_to_today():
    from sniper.settings import memecoin as cfg
    assert cfg.MEMECOIN_RELAY_REQUESTS_VERSION == 'v2'
    assert cfg.MEMECOIN_RELAY_BOUND_PROOF_ACCEPT is False


# ---- the recorded 8453 pair (order 0x2dcf925a…, fill 0x59f0f483…, Solana origin), 2026-10-07 ----
# v2 and v3 answers for the SAME order, the destination tx (Base research host), the Solana origin
# tx (research Helius), Relay /chains, the source's identity row and the live v1 proof's inputs.

_PAIR = Path(__file__).parent / 'fixtures/memecoin/relay_requests_2dcf925a'


def _pair():
    frame = json.load(gzip.open(_PAIR / 'frame.json.gz', 'rt'))
    v2 = json.load(open(_PAIR / 'v2.json'))['requests'][0]
    v3 = json.load(open(_PAIR / 'v3.json'))['requests'][0]
    return frame, v2, v3


def test_the_real_v3_answer_verifies_bound_and_matches_v1_on_its_v2_answer():
    frame, v2, v3 = _pair()
    kw = dict(identities=frame['identity'], chains=frame['chains'])
    v1 = relay.verify_origin(v2, frame['origin'], dict(frame['proof']), frame['destination'], **kw)
    b = relay.verify_bound_origin(v3, frame['origin'], dict(frame['proof']), frame['destination'], **kw)
    assert v1['proof_version'] == 'relay-origin-paid-v1' and b['proof_version'] == 'relay-origin-bound-v1'
    keys = ('order_id', 'origin_wallet', 'origin_network_id', 'origin_tx', 'origin_slot',
            'origin_amount_atomic', 'origin_identity_user_id')
    assert {k: b[k] for k in keys} == {k: v1[k] for k in keys} == {k: frame['verified_v1_live'][k] for k in keys}


def test_the_real_v3_answer_refuses_when_its_fill_does_not_carry_the_order():
    frame, _v2, v3 = _pair()
    dst = dict(frame['destination'], input=frame['destination']['input'][:-64] + '22' * 32)
    v3 = copy.deepcopy(v3)
    v3['data']['outTxs'][0]['data']['data'] = dst['input']   # Relay's copy agrees: only the order differs
    with pytest.raises(relay.ProofError, match='destination_order_mismatch'):
        relay.verify_bound_origin(v3, frame['origin'], dict(frame['proof']), dst,
                                  identities=frame['identity'], chains=frame['chains'])

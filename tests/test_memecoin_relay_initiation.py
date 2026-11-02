"""Signed cross-chain origin proof, never beneficiary-only Relay admission."""
import copy
import gzip
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sniper.services import memecoin_relay_initiation as relay
from sniper.services.memecoin_source_initiation import source_initiation_refusal
from sniper.services.memecoin_evm_onchain_watch import relayer_purchase_from_receipt, price_relayer_purchase

FRAME = json.load(gzip.open(Path(__file__).parent / 'fixtures/memecoin/hooks_relay_initiation_20261004.json.gz', 'rt'))
UID = 'a3d6096e-59eb-5684-a9fe-0048e07a0eb4'


def inputs():
    request = copy.deepcopy(FRAME['relay']['requests'][0])
    transaction, receipt = copy.deepcopy(FRAME['transaction-public']['result']), copy.deepcopy(FRAME['receipt-public']['result'])
    proof = relayer_purchase_from_receipt(receipt, transaction, request['recipient'], chain='56', contracts_verified=True, any_route=True)
    buy = price_relayer_purchase(proof, decimals_fn=lambda _: 18, price_fn=lambda _: 1)
    buy['source_initiation']['source_user_id'] = UID
    return request, FRAME['origin-sol']['result'], buy['source_initiation'], transaction


def verify(request=None, origin=None, identity=None):
    req, tx, proof, destination = inputs()
    return relay.verify_origin(request or req, origin or tx, proof, destination,
                               identities=identity if identity is not None else FRAME['identity'], chains=FRAME['chains'])


@pytest.mark.parametrize("chain", ["1", "56", "8453", "4663", "5042", "9999"])
def test_exact_hooks_initiation_admits_the_same_bound_event(chain):
    proof = dict(verify(), network_id=chain)
    assert proof['proof_version'] == 'relay-origin-paid-v1'
    assert proof['origin_wallet'] == FRAME['identity'][0]['address']
    assert proof['origin_amount_atomic'] == '600000000'
    ev = SimpleNamespace(signal_source='onchain_watch', fomo_event_id=f"onchain:{chain}:{proof['tx_hash']}:{proof['mint']}",
                         source_wallet=proof['beneficiary'], source_block_number=proof['block_number'],
                         source_user_id=UID, mint=proof['mint'], source_initiation=proof)
    assert source_initiation_refusal(ev, chain) is None


@pytest.mark.parametrize('mutation', ['foreign_user','no_signer','failed','no_debit','wrong_deposit_id',
                                    'wrong_program','wrong_recipient','wrong_mint','wrong_destination',
                                    'wrong_chain','wrong_order','unverified_identity','wrong_uid','conflict'])
def test_bait_and_identity_tampering_refuse(mutation):
    request, origin, proof, destination = inputs()
    origin, identity = copy.deepcopy(origin), copy.deepcopy(FRAME['identity'])
    if mutation == 'foreign_user': request['user'] = 'F7p3dFrjRTbtRp8FRF6qHLomXbKRBzpvBLjtQcfcgmNe'
    elif mutation == 'no_signer':
        for key in origin['transaction']['message']['accountKeys']: key['signer'] = False
    elif mutation == 'failed': origin['meta']['err'] = {'InstructionError': [0, 'Custom']}
    elif mutation == 'no_debit': origin['meta']['postTokenBalances'] = origin['meta']['preTokenBalances']
    elif mutation == 'wrong_deposit_id': request['protocol']['orderId'] = '0x' + '01' * 32
    elif mutation == 'wrong_program': origin['transaction']['message']['instructions'][-1]['programId'] = '11111111111111111111111111111111'
    elif mutation == 'wrong_recipient': request['recipient'] = '0x' + '01' * 20
    elif mutation == 'wrong_mint': request['orderData']['output']['payments'][0]['currency'] = '0x' + '01' * 20
    elif mutation == 'wrong_destination': request['data']['outTxs'][0]['hash'] = '0x' + '01' * 32
    elif mutation == 'wrong_chain': request['data']['outTxs'][0]['chainId'] = 8453
    elif mutation == 'wrong_order': request['orderData']['output']['payments'][0]['recipient'] = '0x' + '01' * 20
    elif mutation == 'unverified_identity': identity[0]['status'] = 'candidate'
    elif mutation == 'wrong_uid': identity[0]['source_user_id'] = 'someone-else'
    elif mutation == 'conflict': identity[0]['conflict_at'] = '2026-10-04'
    with pytest.raises(relay.ProofError):
        relay.verify_origin(request, origin, proof, destination, identities=identity, chains=FRAME['chains'])


def test_plain_relay_delivery_remains_refused():
    _, _, proof, _ = inputs()
    ev = SimpleNamespace(signal_source='onchain_watch', fomo_event_id=f"onchain:56:{proof['tx_hash']}:{proof['mint']}", source_initiation=proof)
    assert source_initiation_refusal(ev, '56') == 'wallet_initiation_unproved'


def test_slow_verifier_never_blocks_the_submitter_or_direct_events():
    entered, release, complete = threading.Event(), threading.Event(), threading.Event()
    def slow(row): entered.set(); release.wait(2); return dict(row, verified=True)
    worker = relay.OriginWorker(slow, lambda row: complete.set(), workers=1, capacity=1)
    try:
        start = time.perf_counter()
        assert worker.submit({'tx_hash': '1'})
        assert time.perf_counter() - start < .05
        assert entered.wait(1) and not complete.is_set()
        assert worker.submit({'tx_hash': '2'})
        assert not worker.submit({'tx_hash': '3'})
        release.set()
        assert complete.wait(1)
    finally: release.set(); worker.close()


def test_callback_failure_does_not_kill_worker():
    done = threading.Event()
    def callback(row):
        if row['tx_hash'] == '1': raise ValueError('write failed')
        done.set()
    worker = relay.OriginWorker(lambda row: row, callback, workers=1)
    try:
        worker.submit({'tx_hash': '1'}); worker.submit({'tx_hash': '2'})
        assert done.wait(1)
        assert worker.stats['callback_failed'] == 1
    finally: worker.close()


@pytest.mark.parametrize('mutation', ['destination_trailer', 'solver', 'underfilled'])
def test_destination_order_grafts_and_underfills_refuse(mutation):
    request, origin, proof, destination = inputs()
    if mutation == 'destination_trailer':
        destination['input'] = destination['input'][:-64] + '00' * 32
        request['data']['outTxs'][0]['data']['data'] = destination['input']
    elif mutation == 'solver': proof['payer'] = '0x' + '01' * 20
    else: proof['acquired_atomic'] = '1'
    with pytest.raises(relay.ProofError):
        relay.verify_origin(request, origin, proof, destination, identities=FRAME['identity'], chains=FRAME['chains'])


def test_pending_verification_holds_restart_frontier_until_publication():
    entered, release = threading.Event(), threading.Event()
    def slow(item): entered.set(); release.wait(1); return item
    worker = relay.OriginWorker(slow, lambda _: None, workers=1)
    try:
        worker.submit({'row': {'tx_hash': 'x', 'buy': {'source_initiation': {'block_number': 16}}}})
        assert entered.wait(1)
        assert worker.frontier(80) == 15
        release.set(); worker.queue.join()
        assert worker.frontier(80) == 80
    finally: release.set(); worker.close()


def test_watcher_publication_owns_signal_and_preserves_price_age(monkeypatch):
    from scripts.research import onchain_evm_wallet_watch as watch
    created, calls = [], []
    published = threading.Event()
    class Conn:
        def close(self): pass
    def signal_factory(_):
        owner = threading.get_ident(); created.append(owner)
        def signal(row, outcome, price, age):
            assert threading.get_ident() == owner
            calls.append(age); published.set()
        return signal
    def slow(item): time.sleep(.06); return item
    monkeypatch.setattr(relay, 'WatcherVerifier', lambda *a, **k: slow)
    monkeypatch.setattr(watch, 'get_connection', lambda *_: Conn())
    monkeypatch.setattr(watch, '_commit_detections', lambda *_: None)
    monkeypatch.setattr(watch, '_bsc_lane_signal', signal_factory)
    monkeypatch.setattr(watch, '_signal_bsc_lane', lambda signal, row, outcome, price, age: signal(row, outcome, price, age))
    def forbidden(*_): pytest.fail('socket signal crossed thread ownership')
    worker = watch._relay_origin_worker(forbidden)
    try:
        _, _, proof, transaction = inputs()
        row = dict(tx_hash=proof['tx_hash'], source_user_id=UID, buy={'source_initiation': proof}, valuation={})
        assert watch._defer_relay_origin(worker, row, {'_transaction': transaction}, 600., 5.)
        assert published.wait(1)
        assert created and created[0] != threading.get_ident()
        assert calls[0] >= 5.05
    finally: worker.close()


def test_direct_detection_never_queues_origin_provider_work():
    from scripts.research import onchain_evm_wallet_watch as watch
    class Worker:
        def submit(self, _): pytest.fail('direct event queued provider work')
    assert not watch._defer_relay_origin(Worker(), {'buy': {'source_initiation':
        {'proof_version': 'direct-wallet-paid-v1'}}}, {}, 600, 1)


@pytest.mark.parametrize('error', ['origin_provider_unavailable','origin_http_403'])
def test_provider_failure_stays_unknown_and_does_not_retry_rejection(error):
    calls = []
    def fail(*args, **kwargs): calls.append(1); raise relay.ProofError(error)
    verifier = relay.WatcherVerifier(lambda _: FRAME['identity'], chains=FRAME['chains'], fetch=fail)
    _, _, proof, destination = inputs()
    with pytest.raises(relay.ProofError, match=error):
        verifier({'row': {'source_user_id': UID, 'buy': {'source_initiation': proof}}, 'transaction': destination})
    assert len(calls) == 1


def test_indexing_delay_is_retried_only_off_path(monkeypatch):
    calls = []
    def fetch(url, **kwargs):
        if 'requests/' in url:
            calls.append(url)
            return {'requests': []} if len(calls) == 1 else FRAME['relay']
        return FRAME['origin-sol']
    monkeypatch.setattr(relay.time, 'sleep', lambda _: None)
    verifier = relay.WatcherVerifier(lambda _: FRAME['identity'], chains=FRAME['chains'], fetch=fetch)
    _, _, proof, destination = inputs()
    result = verifier({'row': {'source_user_id': UID, 'buy': {'source_initiation': proof}}, 'transaction': destination})
    assert result['row']['buy']['source_initiation']['proof_version'] == relay.PROOF_VERSION
    assert len(calls) == 2


def evm_frame(native=False):
    """Synthetic origin shape; recorded hooks destination identity, recomputed order commitment."""
    from eth_abi import encode
    from eth_utils import keccak
    request, _, proof, destination = inputs()
    payer = proof['beneficiary']
    currency = '0x' + '00' * 20 if native else '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
    depository = next(c for c in FRAME['chains'] if c['id'] == 8453)['protocol']['v2']['depository']
    amount = '600000000'
    signature, block_hash = '0x' + '77' * 32, '0x' + '88' * 32
    payment = request['orderData']['inputs'][0]['payment']
    payment.update(chainId='base', currency=currency, amount=amount)
    oid = relay.order_id(request['orderData'], FRAME['chains'])
    request['protocol']['orderId'] = oid
    destination['input'] = destination['input'][:-64] + oid[2:]
    request['data']['outTxs'][0]['data']['data'] = destination['input']
    deposit = request['protocol']['deposit']['origin']
    deposit.update(chainId=8453, depositor=payer, depository=depository, currency=currency,
                   amount=amount, transactionId=signature)
    request['user'] = payer
    request['data']['inTxs'] = [dict(hash=signature, chainId=8453, status='success', block=16,
                                    timestamp=request['data']['outTxs'][0]['timestamp'] - 1)]
    data = (keccak(text='depositNative(address,bytes32)')[:4] +
            encode(['address','bytes32'], [payer, bytes.fromhex(oid[2:])])) if native else (
                keccak(text='depositErc20(address,address,uint256,bytes32)')[:4] +
                encode(['address','address','uint256','bytes32'], [payer,currency,int(amount),bytes.fromhex(oid[2:])]))
    tx = dict(hash=signature, blockHash=block_hash, blockNumber='0x10', chainId='0x2105',
              input='0x'+data.hex(), value=hex(int(amount)) if native else '0x0', **{'from':payer,'to':depository})
    receipt = dict(transactionHash=signature, blockHash=block_hash, blockNumber='0x10', status='0x1',
                   logs=[] if native else [dict(address=currency,
                     topics=['0x'+keccak(text='Transfer(address,address,uint256)').hex(),
                             '0x'+payer[2:].rjust(64,'0'),'0x'+depository[2:].rjust(64,'0')], data=hex(int(amount)))],
                   **{'from':payer,'to':depository})
    return request, {'transaction':tx,'receipt':receipt,'chain_id':'0x2105'}, proof, destination


@pytest.mark.parametrize('native',[False,True])
def test_evm_origin_signed_deposit_passes(native):
    request, origin, proof, destination = evm_frame(native)
    result = relay.verify_origin(request, origin, proof, destination, identities=[], chains=FRAME['chains'])
    assert result['origin_network_id'] == '8453'
    assert result['origin_wallet'] == proof['beneficiary']


@pytest.mark.parametrize('mutation',['foreign_sender','wrong_rpc_chain','failed','missing_payment','wrong_id'])
def test_evm_origin_fails_closed(mutation):
    request, origin, proof, destination = evm_frame()
    if mutation == 'foreign_sender': origin['transaction']['from'] = '0x'+'22'*20
    elif mutation == 'wrong_rpc_chain': origin['chain_id'] = '0x38'
    elif mutation == 'failed': origin['receipt']['status'] = '0x0'
    elif mutation == 'missing_payment': origin['receipt']['logs'] = []
    elif mutation == 'wrong_id': origin['transaction']['input'] = origin['transaction']['input'][:-64]+'00'*32
    with pytest.raises(relay.ProofError):
        relay.verify_origin(request, origin, proof, destination, identities=[], chains=FRAME['chains'])


def test_expired_evm_order_refuses_even_with_consistent_commitments():
    from eth_abi import encode
    from eth_utils import keccak
    request, origin, proof, destination = evm_frame()
    request['orderData']['output']['deadline'] = 1
    oid = relay.order_id(request['orderData'], FRAME['chains'])
    request['protocol']['orderId'] = oid
    destination['input'] = destination['input'][:-64] + oid[2:]
    request['data']['outTxs'][0]['data']['data'] = destination['input']
    deposit = request['protocol']['deposit']['origin']
    origin['transaction']['input'] = '0x' + (keccak(text='depositErc20(address,address,uint256,bytes32)')[:4] +
        encode(['address', 'address', 'uint256', 'bytes32'],
               [deposit['depositor'], deposit['currency'], int(deposit['amount']), bytes.fromhex(oid[2:])])).hex()
    with pytest.raises(relay.ProofError, match='origin_time_mismatch'):
        relay.verify_origin(request, origin, proof, destination, identities=[], chains=FRAME['chains'])


def test_research_origin_reads_exclude_every_funded_pool_member(monkeypatch):
    from scripts.research import onchain_evm_wallet_watch as watch
    from sniper.settings import memecoin as cfg
    captured = {}
    class Worker:
        def __init__(self, verifier, publish, *, on_stop=None): pass
    def verifier(*args, **kwargs): captured.update(kwargs)
    monkeypatch.setattr(relay, 'WatcherVerifier', verifier)
    monkeypatch.setattr(relay, 'OriginWorker', Worker)
    monkeypatch.setattr(watch, 'evm_rpc_for_chain', lambda _: 'https://funded-primary.invalid')
    monkeypatch.setattr(cfg, 'MEMECOIN_EVM_RPC_POOL_URLS',
                        {'8453': ['https://funded-primary.invalid', 'https://funded-secondary.invalid/']})
    monkeypatch.setenv('MEMECOIN_RESEARCH_DRPC_RPC_URL_8453', 'https://funded-secondary.invalid')
    watch._relay_origin_worker(None)
    assert '8453' not in captured['evm_rpcs']
    monkeypatch.setenv('MEMECOIN_RESEARCH_DRPC_RPC_URL_8453', 'https://independent-research.invalid')
    watch._relay_origin_worker(None)
    assert captured['evm_rpcs']['8453'] == 'https://independent-research.invalid'


def robinhood_inputs(chain=4663):
    """Coherent RH destination variant of the captured signed-deposit proof."""
    import base58
    request, origin, proof, destination = inputs()
    origin = copy.deepcopy(origin)
    chains = copy.deepcopy(FRAME['chains'])
    if not any(c['id'] == chain for c in chains):
        chains.append(dict(id=chain, vmType='evm', protocol=dict(v2=dict(chainId='robinhood'))))
    protocol = next(c for c in chains if c['id'] == chain)['protocol']['v2']['chainId']
    proof['network_id'] = str(chain)
    destination['chainId'] = hex(chain)
    request['orderData']['output']['chainId'] = protocol
    oid = relay.order_id(request['orderData'], chains)
    request['protocol']['orderId'] = oid
    destination['input'] = destination['input'][:-64] + oid[2:]
    for tx in request['data']['outTxs']:
        tx['chainId'] = chain
        tx['data']['data'] = destination['input']
    for tx in request['protocol']['settlement']['destination']['fills']:
        tx['chainId'] = chain
    for ins in origin['transaction']['message']['instructions']:
        if ins.get('programId') == relay.SOLANA_DEPOSITORY:
            raw = base58.b58decode(ins['data'])
            ins['data'] = base58.b58encode(raw[:-32] + bytes.fromhex(oid[2:])).decode()
    return request, origin, proof, destination, chains


@pytest.mark.parametrize("chain", [1, 56, 8453, 4663, 5042, 9999])
def test_every_evm_chain_verifies_committed_origin_and_preserves_destination_chain(chain):
    req, origin, proof, dest, chains = robinhood_inputs(chain)
    result = relay.verify_origin(req, origin, proof, dest, identities=FRAME['identity'], chains=chains)
    assert result['proof_version'] == 'relay-origin-paid-v1'
    assert result['network_id'] == str(chain) and result['origin_identity_user_id'] == UID


@pytest.mark.parametrize('mutation', ['receipt_chain', 'settlement_chain', 'order_chain', 'signed_order'])
def test_robinhood_origin_cannot_reuse_bsc_destination_binding(mutation):
    req, origin, proof, dest, chains = robinhood_inputs()
    if mutation == 'receipt_chain': req['data']['outTxs'][0]['chainId'] = 56
    elif mutation == 'settlement_chain': req['protocol']['settlement']['destination']['fills'][0]['chainId'] = 56
    elif mutation == 'order_chain': req['orderData']['output']['chainId'] = 'bnb'
    else:
        for ins in origin['transaction']['message']['instructions']:
            if ins.get('programId') == relay.SOLANA_DEPOSITORY:
                import base58
                raw = base58.b58decode(ins['data'])
                ins['data'] = base58.b58encode(raw[:-32] + bytes(32)).decode()
    with pytest.raises(relay.ProofError):
        relay.verify_origin(req, origin, proof, dest, identities=FRAME['identity'], chains=chains)


# ---- 2026-10-06 (Gamma/Alpha): a Relay 429 says which limit fired, at the watchers' log level ----

def test_a_relay_429_logs_its_retry_after_body_and_whether_a_key_was_sent(monkeypatch, caplog):
    import io
    import logging
    import urllib.error
    from sniper.services import memecoin_relay_initiation as ri

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests",
                                     {"Retry-After": "7"}, io.BytesIO(b'{"message":"rate limited per ip"}'))
    monkeypatch.setattr(ri.urllib.request, "urlopen", refuse)
    with caplog.at_level(logging.WARNING):            # the last-resort level the watchers run at
        with pytest.raises(ri.ProofError, match="origin_rate_limit"):
            ri.read_json("https://api.relay.link/requests/v2?orderId=0xab",
                         headers={"x-api-key": "secret-value", "Accept": "application/json"})
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "relay 429" in line and "retry_after=7" in line and "key_sent=1" in line
    assert "rate limited per ip" in line and "secret-value" not in line


# ---- 2026-10-07 (Codex N6): the 429 line never carries a credential from the URL or the body ----

@pytest.mark.parametrize("url, secret", [
    ("https://rpc.example.invalid/v2/SYNTHETIC_PATH_CREDENTIAL", "SYNTHETIC_PATH_CREDENTIAL"),
    ("https://rpc.example.invalid/?api-key=SYNTHETIC_QUERY_CREDENTIAL", "SYNTHETIC_QUERY_CREDENTIAL"),
    ("https://user:SYNTHETIC_BASIC_PASSWORD@rpc.example.invalid/", "SYNTHETIC_BASIC_PASSWORD"),
    ("https://rpc.example.invalid/SYNTHETIC_TOKEN_SEGMENT/", "SYNTHETIC_TOKEN_SEGMENT"),
])
def test_a_429_line_logs_host_and_a_fixed_label_never_the_url_credential(monkeypatch, caplog, url, secret):
    import io
    import logging
    import urllib.error
    from sniper.services import memecoin_relay_initiation as ri

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "2"},
                                     io.BytesIO(("limit for %s exceeded" % secret).encode()))
    monkeypatch.setattr(ri.urllib.request, "urlopen", refuse)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ri.ProofError, match="origin_rate_limit"):
            ri.read_json(url, body={"jsonrpc": "2.0"})
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "host=rpc.example.invalid" in line and "endpoint=path_redacted" in line
    assert secret not in line and "exceeded" in line


def test_a_relay_429_names_its_endpoint_and_redacts_a_long_token_in_the_body(monkeypatch, caplog):
    import io
    import logging
    import urllib.error
    from sniper.services import memecoin_relay_initiation as ri
    token = "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6"

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "9" * 80},
                                     io.BytesIO(('{"message":"key %s over limit"}' % token).encode()))
    monkeypatch.setattr(ri.urllib.request, "urlopen", refuse)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ri.ProofError):
            ri.read_json("https://api.relay.link/requests/v2?orderId=0xab", headers={"x-api-key": "k"})
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "host=api.relay.link" in line and "endpoint=requests/v2" in line
    assert token not in line and "over limit" in line and "9" * 33 not in line


def test_an_unparseable_url_still_logs_and_raises_the_typed_error(monkeypatch, caplog):
    import io
    import logging
    import urllib.error
    from sniper.services import memecoin_relay_initiation as ri

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError("x", 429, "Too Many Requests", {}, io.BytesIO(b""))
    monkeypatch.setattr(ri.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(ri.urllib.request, "Request", lambda *a, **k: object())
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ri.ProofError, match="origin_rate_limit"):
            ri.read_json("http://[::1/v2/SYNTHETIC_IPV6_CREDENTIAL")
    line = " ".join(r.getMessage() for r in caplog.records)
    assert "host=unknown" in line and "SYNTHETIC_IPV6_CREDENTIAL" not in line


# ---- 2026-10-07 (N6 residual): a body that echoes a credential in another encoding ----

def _refuse_echoing(monkeypatch, echoed):
    import io
    import urllib.error
    from sniper.services import memecoin_relay_initiation as ri

    def refuse(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "Too Many Requests", {"Retry-After": "2"},
                                     io.BytesIO(("limit for %s exceeded" % echoed).encode()))
    monkeypatch.setattr(ri.urllib.request, "urlopen", refuse)
    return ri


@pytest.mark.parametrize("url, echoed", [
    # each alphanumeric run is under 24 characters, so only the decoded-form redaction catches it
    ("https://rpc.example.invalid/v2/Syn%2BThetic%2FKey%3DValue_1", "Syn+Thetic/Key=Value_1"),
    ("https://rpc.example.invalid/?api-key=Syn%20Thetic%2BQuery%3D1", "Syn Thetic+Query=1"),
    ("https://rpc.example.invalid/?api-key=Syn+Thetic+Plus", "Syn Thetic Plus"),
    ("https://rpc.example.invalid/v2/Syn+Thetic/Key=Raw", "Syn%2BThetic%2FKey%3DRaw"),
])
def test_a_429_body_echoing_the_url_credential_decoded_or_encoded_is_redacted(
        monkeypatch, caplog, url, echoed):
    import logging
    ri = _refuse_echoing(monkeypatch, echoed)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ri.ProofError, match="origin_rate_limit"):
            ri.read_json(url, body={"jsonrpc": "2.0"})
    line = " ".join(r.getMessage() for r in caplog.records)
    assert echoed not in line and "exceeded" in line, line


@pytest.mark.parametrize("echoed", ["hdr-Secret.Value+With/Chars", "hdr-Secret.Value%2BWith%2FChars"])
def test_a_429_body_echoing_the_api_key_header_is_redacted(monkeypatch, caplog, echoed):
    import logging
    ri = _refuse_echoing(monkeypatch, echoed)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ri.ProofError, match="origin_rate_limit"):
            ri.read_json("https://api.relay.link/requests/v3?orderId=0xab",
                         headers={"x-api-key": "hdr-Secret.Value+With/Chars"})
    line = " ".join(r.getMessage() for r in caplog.records)
    assert echoed not in line and "exceeded" in line and "key_sent=1" in line, line
    assert "endpoint=requests/v3" in line, line

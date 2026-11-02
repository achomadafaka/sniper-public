"""Single credentialed Shyft fallback config covers existing native preparation readers."""
import json

import pytest

from sniper.execution import fast_http, pump_curve_buy as P, pumpswap_buy as PS
from sniper.execution import solana_real_seams as seams
from sniper.settings import memecoin as cfg

READ = 'https://rpc.ams.shyft.to/?api_key=offline-read'
FUNDED = 'https://rpc.fra.shyft.to/?api_key=offline-funded'
LEGACY = 'https://legacy.invalid/?api-key=offline-custody'
USER = '11111111111111111111111111111111'


def _config(monkeypatch, profile, fallback):
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_INFRA_PROVIDER', profile)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_URL', fallback)
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_POOL_URLS', ())
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_READ_RPC_URL', READ)


def _reader(monkeypatch, kind, answer=(200, {'result': {'value': None}})):
    calls = []
    if kind == 'direct':
        def request(url, method, params, http):
            calls.append((url, method, params, http))
            return answer
        monkeypatch.setattr(seams, '_rpc_call', request)
        return P._custody_rpc, calls
    class Client:
        def __init__(self, **kwargs):
            assert kwargs == dict(max_connections=4, max_keepalive=4,
                read_timeout_s=P.POOL_READ_TIMEOUT_S, keepalive_expiry_s=P.POOL_READ_KEEPALIVE_S)
        def request(self, method, url, *, timeout, body):
            request = json.loads(body)
            assert method == 'POST' and request['jsonrpc'] == '2.0' and request['id'] == 1
            calls.append((url, request['method'], request['params'], timeout))
            return *answer, None
    monkeypatch.setattr(fast_http, 'PooledHttpClient', Client)
    return P.keepalive_custody_rpc(), calls


@pytest.mark.parametrize('kind', ['direct', 'keepalive'])
@pytest.mark.parametrize('profile,fallback,expected', [
    ('shyft', FUNDED, FUNDED), ('legacy', LEGACY, LEGACY),
])
def test_native_read_uses_explicit_shyft_endpoint_and_preserves_legacy(monkeypatch, kind,
                                                                   profile, fallback, expected):
    _config(monkeypatch, profile, fallback)
    rpc, calls = _reader(monkeypatch, kind)
    params = ['offline-account', {'encoding': 'base64'}]
    assert rpc('getAccountInfo', params) == {'value': None}
    assert len(calls) == 1
    assert calls[0][:3] == (expected, 'getAccountInfo', params)
    assert cfg.MEMECOIN_SOLANA_RPC_URL == fallback  # selection does not mutate config


@pytest.mark.parametrize('builder', [P.SlotLaneCurveBuilder, PS.SlotLanePumpSwapBuilder])
def test_actual_default_builder_refresh_uses_single_funded_shyft_url(monkeypatch, builder):
    _config(monkeypatch, 'shyft', FUNDED)
    _, calls = _reader(monkeypatch, 'direct')
    _reader(monkeypatch, 'keepalive')
    built = builder(user=USER, jup_call=lambda *a, **kw: (503, {}))
    assert built.refresh(5.0) is False  # unavailable quote stays a miss, not fake readiness
    assert len(calls) == 1 and calls[0][0] == FUNDED
    assert calls[0][1] == 'getAccountInfo'


@pytest.mark.parametrize('kind', ['direct', 'keepalive'])
def test_json_rpc_error_is_raised_once_and_null_result_stays_null(monkeypatch, kind):
    _config(monkeypatch, 'shyft', FUNDED)
    rpc, calls = _reader(monkeypatch, kind, (200, {'error': {'code': -32429}}))
    with pytest.raises(RuntimeError, match='-32429'):
        rpc('getAccountInfo', ['x'])
    assert len(calls) == 1 and calls[0][0] == FUNDED
    rpc, calls = _reader(monkeypatch, kind, (200, {'result': None}))
    assert rpc('getAccountInfo', ['x']) is None
    assert len(calls) == 1 and calls[0][0] == FUNDED


@pytest.mark.parametrize('answer', [(429, {'error': {'code': 429}}), (200, 'not-json')])
def test_keepalive_http_or_malformed_response_remains_an_error(monkeypatch, answer):
    _config(monkeypatch, 'shyft', FUNDED)
    rpc, calls = _reader(monkeypatch, 'keepalive', answer)
    with pytest.raises(RuntimeError, match='http'):
        rpc('getAccountInfo', ['x'])
    assert len(calls) == 1 and calls[0][0] == FUNDED


def test_keepalive_ping_uses_same_role_and_existing_timeout(monkeypatch):
    _config(monkeypatch, 'shyft', FUNDED)
    rpc, calls = _reader(monkeypatch, 'keepalive', (200, {'result': 'ok'}))
    assert rpc.ping() == 'ok'
    assert calls == [(FUNDED, 'getHealth', [], P.POOL_READ_PING_TIMEOUT_S)]


@pytest.mark.parametrize('kind', ['direct', 'keepalive'])
def test_empty_legacy_url_is_not_replaced_by_observation_endpoint(monkeypatch, kind):
    _config(monkeypatch, 'legacy', '')
    rpc, calls = _reader(monkeypatch, kind)
    rpc('getAccountInfo', ['x'])
    assert calls[0][0] == ''


def test_default_reads_take_the_funded_role_head_and_never_the_raw_fallback(monkeypatch):
    """67bdba4ac (Codex second review): a builder without an injected rpc reads the funded role's
    head, solana_funded_rpc_urls()[0], not MEMECOIN_SOLANA_RPC_URL directly. The funded slot-lane
    builders inject their context's custody rpc; this default is for the rest (~7 us per call)."""
    _config(monkeypatch, 'shyft', 'https://ignored.invalid/?api_key=raw')
    monkeypatch.setattr(cfg, 'solana_funded_rpc_urls', lambda: (FUNDED, READ))
    for kind in ('direct', 'keepalive'):
        rpc, calls = _reader(monkeypatch, kind)
        rpc('getMultipleAccounts', [['x'], {'encoding': 'base64'}])
        assert calls[0][0] == FUNDED


def test_single_shyft_url_is_the_same_funded_role_as_original_pool_only_shape(monkeypatch):
    from sniper.services.memecoin_solana_provider import provider_urls
    original = {'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft',
                'MEMECOIN_SOLANA_RPC_POOL_URLS': FUNDED, 'MEMECOIN_SOLANA_RPC_URL': ''}
    stage = {**original, 'MEMECOIN_SOLANA_RPC_POOL_URLS': '', 'MEMECOIN_SOLANA_RPC_URL': FUNDED}
    before = dict(stage)
    assert provider_urls('funded', original) == provider_urls('funded', stage) == (FUNDED,)
    assert stage == before
    _config(monkeypatch, 'shyft', FUNDED)
    assert cfg.solana_funded_rpc_urls() == (FUNDED,)


@pytest.mark.parametrize('kind', ['direct', 'keepalive'])
def test_pool_only_config_supplies_the_default_readers(monkeypatch, kind):
    """The defect this file used to witness (a pool-only config left these readers on an empty URL)
    is closed by 67bdba4ac: they read the funded pool's head."""
    _config(monkeypatch, 'shyft', '')
    monkeypatch.setattr(cfg, 'MEMECOIN_SOLANA_RPC_POOL_URLS', (FUNDED,))
    assert cfg.solana_funded_rpc_urls() == (FUNDED,)
    rpc, calls = _reader(monkeypatch, kind)
    rpc('getAccountInfo', ['x'])
    assert calls[0][0] == FUNDED


@pytest.mark.parametrize('fallback', ['https://api.mainnet-beta.solana.com', LEGACY,
                                     'https://rpc.fra.shyft.to/'])
def test_single_endpoint_shyft_stage_rejects_public_legacy_or_uncredentialed_url(fallback):
    from sniper.services.memecoin_solana_provider import provider_urls
    with pytest.raises(ValueError, match='funded_url_invalid'):
        provider_urls('funded', {'MEMECOIN_SOLANA_INFRA_PROVIDER': 'shyft',
            'MEMECOIN_SOLANA_RPC_POOL_URLS': '', 'MEMECOIN_SOLANA_RPC_URL': fallback})

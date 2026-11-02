"""One quote host is a single point of failure for BOTH funded legs.

2026-08-14. `lite-api.jup.ag` — the rate-limited free tier — banned this host's IP and answered 429
to every request for over an hour, including a trivial SOL/USDC probe. A funded entry and a funded
exit each need an exact quote before they will sign, so the outage took out both at once: live
entries were refused (`no quote` on RATIO and boden) and position 37108 went rails-blind for 700
sweeps at -67%, its stop and catastrophe rails reading a nominal mid they cannot transact at.

`api.jup.ag` served complete quotes throughout, with no API key, tip, or subscription. These tests
bind the failover: same provider, second free host, and a banned host must not sit on the critical
path of every later call.
"""
import pytest

from sniper.execution import solana_real_seams as S


class Clock:
    def __init__(self):
        self.t = 500.0

    def __call__(self):
        return self.t


QUOTE = {"outAmount": "12345", "inAmount": "1000", "routePlan": [{"swapInfo": {}}]}


@pytest.fixture(autouse=True)
def _clear_cooldowns(monkeypatch):
    """Keyless host order, whatever `.env` holds.

    These tests assert lite-api-first, which is the KEYLESS order. Once a funded key existed in
    `.env` they began passing or failing depending on the developer's environment — three failures
    here, green in CI. A test whose verdict depends on a file it never mentions is not a test.
    """
    monkeypatch.setattr(S, "_KEY_VAR", S.JUP_API_KEY_VAR)
    monkeypatch.delenv(S.JUP_API_KEY_VAR, raising=False)
    monkeypatch.delenv(S.CAPTURE_JUP_API_KEY_VAR, raising=False)
    S._jup_host_cooldown.clear()
    yield
    S._jup_host_cooldown.clear()


def _http(responses, calls):
    """responses: {base_prefix: (status, json)}; records every URL tried."""
    def http(method, url, body=None):
        calls.append(url)
        for base, resp in responses.items():
            if url.startswith(base):
                return resp[0], resp[1], b""
        raise AssertionError("unexpected url " + url)
    return http


def test_falls_back_to_the_second_host_on_429():
    calls = []
    http = _http({S.JUP_BASE: (429, {"error": "rate limited"}),
                  S.JUP_FALLBACK_BASE: (200, QUOTE)}, calls)
    got = S._jup_quote("MintA", "MintB", 1000, 300, http)
    assert got == QUOTE, "a 429 on the free tier must not fail the quote outright"
    assert any(c.startswith(S.JUP_BASE) for c in calls)
    assert any(c.startswith(S.JUP_FALLBACK_BASE) for c in calls)


def test_a_banned_host_is_skipped_on_the_next_call():
    """Paying the ban on the primary before every fallback puts the outage back on the hot path."""
    calls = []
    http = _http({S.JUP_BASE: (429, {}), S.JUP_FALLBACK_BASE: (200, QUOTE)}, calls)
    S._jup_quote("MintA", "MintB", 1000, 300, http)
    calls.clear()
    S._jup_quote("MintA", "MintB", 1000, 300, http)
    assert calls, "the second call must still happen"
    assert calls[0].startswith(S.JUP_FALLBACK_BASE), "the cooled host must not be tried first again"


def test_the_cooldown_expires_and_the_primary_returns():
    clock = Clock()
    S._jup_host_cooldown[S.JUP_BASE] = clock() + S.JUP_HOST_COOLDOWN_S
    assert S._jup_hosts(clock)[0] == S.JUP_FALLBACK_BASE
    clock.t += S.JUP_HOST_COOLDOWN_S + 1.0
    assert S._jup_hosts(clock)[0] == S.JUP_BASE, "the preferred host must come back by itself"


def test_a_cooled_host_is_still_tried_when_every_host_is_cooled():
    """Never drop a host entirely — a stale cooldown must not turn into a self-inflicted outage."""
    clock = Clock()
    for b in S.JUP_BASES:
        S._jup_host_cooldown[b] = clock() + S.JUP_HOST_COOLDOWN_S
    assert set(S._jup_hosts(clock)) == set(S.JUP_BASES)


def test_a_non_429_error_does_not_replay_against_every_host():
    """A 400 is an answer about the REQUEST. Replaying it wastes the fallback's budget too."""
    calls = []
    http = _http({S.JUP_BASE: (400, {"error": "bad mint"}),
                  S.JUP_FALLBACK_BASE: (200, QUOTE)}, calls)
    assert S._jup_quote("bogus", "MintB", 1000, 300, http) is None
    assert len(calls) == 1, "a request-level error must stop at the first host"
    assert S.JUP_BASE not in S._jup_host_cooldown, "a 400 must not cool the host"


def test_all_hosts_down_returns_none():
    calls = []
    http = _http({S.JUP_BASE: (429, {}), S.JUP_FALLBACK_BASE: (429, {})}, calls)
    assert S._jup_quote("MintA", "MintB", 1000, 300, http) is None
    assert len(calls) == 2, "every host must be tried before giving up"


def test_a_transport_exception_propagates_and_does_not_fail_over():
    """A network error is not a statement about the HOST.

    `2026-08-05-solana-quote-infra-transient-retry-design` gives rpc_transport and provider_timeout
    exactly one retry after 200 ms. If failover swallowed the exception here, that retry would be
    silently deleted and the typed classifier would never see the failure."""
    calls = []

    def http(method, url, body=None):
        calls.append(url)
        raise OSError("dns failure")

    with pytest.raises(OSError):
        S._jup_call("GET", "/quote?x=1", http=http)
    assert len(calls) == 1, "a transport error must not be replayed against the other host"
    assert not S._jup_host_cooldown, "a transport error must not cool a host"


def test_the_seam_still_returns_none_on_a_transport_error():
    def http(method, url, body=None):
        raise OSError("dns failure")

    assert S._jup_quote("MintA", "MintB", 1000, 300, http) is None


def test_funded_slot_leg_forwards_private_http(monkeypatch):
    from sniper.execution import pump_curve_buy as pcb
    from sniper.execution import solana_real_seams as seams
    private = object()
    calls = []
    monkeypatch.setattr(seams, '_jup_call', lambda method, path, **kw: calls.append((method, path, kw)) or (200, {}))
    assert pcb._funded_jup_call('GET', '/quote', http=private) == (200, {})
    assert calls[0][2]['http'] is private


def _warmer_http_fixture(monkeypatch, respond):
    import httpx
    from sniper.execution.fast_http import PooledHttpClient
    from sniper.execution import raydium_launchlab_buy as LL
    from test_slot_lane_exotic_warm_leg import Clock as WarmClock, QUOTE as MINT
    from test_slot_lane_cpmm import PEPE_EV, _exotic_builder
    from sniper.execution import pump_curve_buy as P
    clock, calls = WarmClock(), []
    def transport(request):
        calls.append((clock.t, request.method, str(request.url)))
        return respond(request, len(calls))
    http = PooledHttpClient(transport=httpx.MockTransport(transport), max_connections=2, max_keepalive=2)
    warm = LL.WarmExoticLegs(clock=clock, http=http)
    b = _exotic_builder(PEPE_EV)
    quote = dict(inputMint=P.USDC_MINT, outputMint=MINT, inAmount='10000000', outAmount='100',
        otherAmountThreshold='90', swapMode='ExactIn', slippageBps=P.DEFAULT_LEG_SLIPPAGE_BPS,
        routePlan=[{'swapInfo': {'label': 'fixture', 'ammKey': PEPE_EV.pool}}])
    monkeypatch.setattr(S, '_jup_hosts', lambda *args, **kw: (S.JUP_BASE, S.JUP_FALLBACK_BASE))
    return warm, b, clock, calls, quote, http


@pytest.mark.parametrize("status", [401, 429])
def test_actual_429_then_fallback_200_stops_before_post(monkeypatch, status):
    import httpx
    state = {}
    def respond(request, n):
        return httpx.Response(status if n == 1 else 200, json={} if n == 1 else state['quote'])
    warm, b, clock, calls, quote, http = _warmer_http_fixture(monkeypatch, respond)
    state['quote'] = quote
    monkeypatch.setattr(S, '_jup_headers', lambda env=None: {'x-api-key': 'fixture'})
    monkeypatch.setattr(S, '_jup_hosts', lambda *args, **kw: (S.JUP_FALLBACK_BASE, S.JUP_BASE))
    try:
        warm.note('raydium_cpmm', quote['outputMint'], 10_000_000, leg=None, builder=b)
        warm.refresh_once()
        # A 401 fails over to the fallback host. A 429 on the keyed (funded) host opens the funded
        # quiet window, so the warm leg's failover is cancelled before dispatch (Codex F8).
        hosts = 2 if status == 401 else 1
        assert [c[1] for c in calls] == ['GET'] * hosts
        assert len(warm._http_starts) == hosts and warm.stats['rate_limited'] == 1
        assert warm.stats['funded_429_skips'] == (1 if status == 429 else 0)
        clock.t += 2
        warm.refresh_once()
        assert len(calls) == hosts
    finally: http.close()


# all429: the first host's funded 429 stops the failover host before dispatch (Codex F8).
@pytest.mark.parametrize('mode,expected', [('all429', 1), ('timeout', 1)])
def test_actual_host_failure_counts_every_started_request(monkeypatch, mode, expected):
    import httpx
    def respond(request, n):
        if mode == 'timeout': raise httpx.ReadTimeout('fixture')
        return httpx.Response(429, json={})
    warm, b, clock, calls, quote, http = _warmer_http_fixture(monkeypatch, respond)
    try:
        warm.note('raydium_cpmm', quote['outputMint'], 10_000_000, leg=None, builder=b)
        warm.refresh_once()
        assert len(calls) == expected and len(warm._http_starts) == expected
        if mode == 'timeout': assert warm.stats['last_error'] == 'FastHttpError'
        assert warm.get('raydium_cpmm', quote['outputMint'], 10_000_000) is None
    finally: http.close()


@pytest.mark.parametrize('field,value', [('inAmount','9999999'), ('inputMint','wrong'),
    ('outputMint','wrong'), ('swapMode','ExactOut'), ('slippageBps',151),
    ('otherAmountThreshold','101'), ('routePlan',[]), ('outAmount','0'), ('outAmount', True),
    ('otherAmountThreshold', 1.5)])
def test_speculative_get_identity_is_validated_before_post(monkeypatch, field, value):
    import httpx
    state = {}
    warm, b, clock, calls, quote, http = _warmer_http_fixture(monkeypatch,
        lambda request, n: httpx.Response(200, json=state['quote']))
    mint = quote['outputMint']
    quote[field] = value
    state['quote'] = quote
    try:
        warm.note('raydium_cpmm', mint, 10_000_000, leg=None, builder=b)
        warm.refresh_once()
        assert [c[1] for c in calls] == ['GET']
        assert warm.stats['refresh_failed'] == 1
    finally: http.close()


@pytest.mark.parametrize('knob, cycle_calls, starts', [(2, 8, 4), (4, 10, 6)])
def test_two_healthy_legs_share_four_actual_http_starts_and_cycle(monkeypatch, knob, cycle_calls, starts):
    """Knob 2 (the old warmer): two legs, four starts per 1.5 s. Above it the cap follows the
    three-clip inventory: three legs, two starts each, all inside one cycle."""
    import httpx
    from sniper.execution import pump_curve_buy as P
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_SLOT_LANE_EXOTIC_WARM_MAX', knob, raising=False)
    state = {}
    def respond(request, n):
        if request.method == 'GET':
            quote = dict(state['quote'], inAmount=request.url.params['amount'])
            return httpx.Response(200, json=quote)
        return httpx.Response(200, json={'swapInstruction': {'programId': P.SYSTEM_PROGRAM, 'data': '', 'accounts': []}})
    warm, b, clock, calls, quote, http = _warmer_http_fixture(monkeypatch, respond)
    state['quote'] = quote
    try:
        warm.set_clip_inventory((10_000_000, 11_000_000, 12_000_000))
        for atoms in (10_000_000, 11_000_000):
            warm.note('raydium_cpmm', quote['outputMint'], atoms, leg=None, builder=b)
        warm.refresh_once()
        assert [c[1] for c in calls] == ['GET', 'POST', 'GET', 'POST']
        warm.note('raydium_cpmm', quote['outputMint'], 12_000_000, leg=None, builder=b)
        warm.refresh_once()
        clock.t += 1.49
        warm.refresh_once()
        assert len(calls) == 4
        clock.t += .01
        warm.refresh_once()
        assert len(calls) == cycle_calls
        assert len(warm._http_starts) == starts
    finally: http.close()


def test_near_boundary_fallback_cannot_start_fifth_request_and_live_cold_is_unlimited(monkeypatch):
    import httpx
    from sniper.execution import pump_curve_buy as P, raydium_launchlab_buy as LL
    state = {}
    def respond(request, n): return httpx.Response(429, json={})
    warm, b, clock, calls, quote, http = _warmer_http_fixture(monkeypatch, respond)
    try:
        for _ in range(3): warm._warm_http('GET', S.JUP_BASE + '/quote')
        clock.t += 1.49
        with pytest.raises(LL.WarmHttpBudget):
            P._funded_jup_call('GET', '/quote', http=warm._warm_http)
        assert len(calls) == 4
        live_calls = []
        monkeypatch.setattr(S, '_http', lambda method, url, **kwargs: live_calls.append(method) or (200, {}, None))
        assert P._funded_jup_call('GET', '/quote') == (200, {})
        assert live_calls == ['GET']
    finally: http.close()

"""REST price evidence cannot become a fabricated research-RPC outage."""
import json
import urllib.error

import pytest

from sniper.execution import evm_real_seams, evm_token_safety, evm_wallet
from sniper.execution import research_rotation_4663 as rr
from sniper.services import memecoin_rh_fast_lane as lane

RPC = "https://research.example/rpc"
EXTERNAL_RPC = "https://override.example/rpc"
PRICE = "https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT"
ROUTE_REASON = ("configured sell venue returned no executable quote "
                "(route=rh_fast_v4_direct; other venues not tested)")


@pytest.fixture
def rotation(monkeypatch):
    # Every request uses a local seam; an absent FMP key prevents unrelated fallback work.
    monkeypatch.delenv("FMP_API_KEY", raising=False)
    return rr.Rotation({rr.BASE: RPC}, clock=lambda: 1000.0, log=lambda _: None)


def http_seam(rotation, answer):
    sent = []

    def call(method, url, headers=None, body=None, timeout=10):
        sent.append((method, url, headers, body, timeout))
        if isinstance(answer, Exception):
            raise answer
        return answer

    return rotation.http_seam(lambda: call), sent


def assert_research_untouched(rotation):
    assert rotation.active() == (1, RPC)
    assert rotation.stats()[1] == [{"slot": 1, "calls": 0, "quota": 0,
                                   "transport": 0, "spent": False, "unusable": False}]


def route_cache(rotation, http):
    paced = lane.PacedHttp(http, min_gap_s=0, clock=lambda: 1000.0)

    def sell_route(_mint):
        # The production no-sell-route fallback uses this exact live USD conversion.
        amount = evm_real_seams.usd_to_quote_atomic("4663", 50, http=paced)
        assert amount == 20_000_000_000_000_000
        return False

    def check(mint, usd=None):
        return evm_token_safety.token_safety_ok_evm(
            mint, code_fn=lambda _: "0x6000", sell_route_fn=sell_route,
            sell_sim_fn=lambda _: pytest.fail("a missing sell route must not simulate"),
            sell_route_label="rh_fast_v4_direct")

    return lane.SafetyCache(lane.background_safety(check, rotation, paced), ttl_s=60, usd=50)


def test_real_binance_quote_conversion_is_not_an_rpc_provider_error(rotation):
    http, sent = http_seam(rotation, (200, {"price": "2500"}, {}))
    assert evm_real_seams.usd_to_quote_atomic("4663", 50, http=http) == 20_000_000_000_000_000
    assert sent == [("GET", PRICE, None, None, 10)]
    assert rotation.provider_errors() == 0
    assert_research_untouched(rotation)


def test_valid_price_and_real_missing_route_cache_the_token_refusal(rotation):
    http, sent = http_seam(rotation, (200, {"price": "2500"}, {}))
    cache = route_cache(rotation, http)
    rec = cache.run_one("0xmint", now=1000)
    assert rec is not None and rec[:2] == (False, ROUTE_REASON)
    assert cache.reason("0xmint", now=1000).startswith("unsafe:" + ROUTE_REASON)
    assert cache.counts["verdicts_unsafe"] == 1
    assert cache.counts["verdict_errors"] == 0
    assert len(sent) == 1 and sent[0][0:2] == ("GET", PRICE)
    assert rotation.provider_errors() == 0
    assert_research_untouched(rotation)


@pytest.mark.parametrize("url,reply", [
    (PRICE, {"price": "2500"}),
    ("https://financialmodelingprep.com/stable/quote?symbol=ETHUSD", [{"price": 2500}]),
    ("https://api.dexscreener.com/latest/dex/tokens/0xmint", {"pairs": []}),
])
def test_bodyless_external_rest_get_preserves_reply_identity_and_request(rotation, url, reply):
    headers = {"x-test": "unchanged"}
    answer = (200, reply, {"response": "unchanged"})
    http, sent = http_seam(rotation, answer)
    assert http("GET", url, headers=headers, timeout=2.5) is answer
    assert sent == [("GET", url, headers, None, 2.5)]
    assert rotation.provider_errors() == 0
    assert_research_untouched(rotation)


@pytest.mark.parametrize("status", [None, 403, 429, 500])
def test_rest_status_failure_stays_unknown_not_a_token_verdict_or_research_quota(rotation, status):
    # Even a plausible price body cannot turn an unsuccessful HTTP response into token evidence.
    http, sent = http_seam(rotation, (status, {"price": "2500"}, {}))
    cache = route_cache(rotation, http)
    assert cache.run_one("0xmint", now=1000) is None
    assert cache.reason("0xmint", now=1000) == "no_safety_verdict"
    assert cache.counts["verdict_errors"] == 1 and cache.counts["verdicts_unsafe"] == 0
    assert rotation.provider_errors() == 1
    assert len(sent) == 1
    assert_research_untouched(rotation)


@pytest.mark.parametrize("error", [
    TimeoutError("timed out"), OSError("connection reset"),
    urllib.error.HTTPError(PRICE, 429, "rate limited", {}, None),
    json.JSONDecodeError("invalid REST JSON", "<html>", 0),
])
def test_rest_unanswered_or_unreadable_failures_are_counted_and_propagated(rotation, error):
    http, sent = http_seam(rotation, error)
    with pytest.raises(type(error)) as caught:
        http("GET", PRICE)
    assert caught.value is error
    assert rotation.provider_errors() == 1
    assert len(sent) == 1
    assert_research_untouched(rotation)


def test_swallowed_price_transport_failure_cannot_cache_an_unsafe_token(rotation):
    http, sent = http_seam(rotation, TimeoutError("timed out"))
    paced = lane.PacedHttp(http, min_gap_s=0, clock=lambda: 1000.0)

    def check(_mint, usd=None):
        assert evm_wallet.eth_usd_price(http=paced) is None
        return False, ROUTE_REASON

    cache = lane.SafetyCache(lane.background_safety(check, rotation, paced), ttl_s=60, usd=50)
    assert cache.run_one("0xmint", now=1000) is None
    assert cache.reason("0xmint", now=1000) == "no_safety_verdict"
    assert cache.counts["verdict_errors"] == 1 and cache.counts["verdicts_unsafe"] == 0
    assert rotation.provider_errors() == 1 and len(sent) == 1
    assert_research_untouched(rotation)


@pytest.mark.parametrize("url", [RPC, EXTERNAL_RPC])
@pytest.mark.parametrize("reply", [{}, None, [], {"result": None}, {"result": "not-hex"},
                                  {"price": "2500"}, {"error": {"code": -32603}}])
def test_post_rpc_partial_malformed_or_rejected_reply_is_still_a_provider_error(rotation, url, reply):
    http, sent = http_seam(rotation, (200, reply, {}))
    body = b'{"jsonrpc":"2.0","id":1,"method":"eth_call","params":[]}'
    assert http("POST", url, body=body)[1] is reply
    assert rotation.provider_errors() == 1
    assert sent[0][0:2] == ("POST", url) and sent[0][3] is body


@pytest.mark.parametrize("url", [RPC, EXTERNAL_RPC])
@pytest.mark.parametrize("reply", [{"result": "0x6000"},
                                  {"error": {"code": 3, "message": "execution reverted"}}])
def test_clean_rpc_result_or_contract_revert_remains_unchanged(rotation, url, reply):
    http, _ = http_seam(rotation, (200, reply, {}))
    assert http("POST", url, body="{}")[1] is reply
    assert rotation.provider_errors() == 0


@pytest.mark.parametrize("method,url,body", [("GET", RPC, None),
                                           ("GET", EXTERNAL_RPC, b'{"method":"eth_call"}'),
                                           ("POST", EXTERNAL_RPC, None),
                                           ("POST", EXTERNAL_RPC, "unparseable")])
def test_unknown_or_rpc_request_shapes_never_bypass_rpc_validation(rotation, method, url, body):
    http, _ = http_seam(rotation, (200, {"price": "2500"}, {}))
    http(method, url, body=body)
    assert rotation.provider_errors() == 1


def test_direct_send_to_external_rpc_keeps_strict_default(rotation):
    rotation.send(EXTERNAL_RPC, lambda _: (200, {"price": "2500"}, {}))
    assert rotation.provider_errors() == 1
    assert_research_untouched(rotation)


@pytest.mark.parametrize("price_reply", [{"price": "2500"}, None, {}, "oops", {"price": "NaN"},
                                        {"price": "Infinity"}, {"price": "0"}])
@pytest.mark.parametrize("quoter_blind", [False, True])
def test_actual_rh_bound_safety_distinguishes_answered_sell_miss_from_blind_read(
        rotation, monkeypatch, price_reply, quoter_blind):
    from sniper.execution.evm_v4_quote import _NATIVE, V4
    from sniper.services.memecoin_evm_leg import _v4_direct_quote_fn

    mint = "0x" + "11" * 20
    taker = "0x" + "22" * 20
    key = {"currency0": _NATIVE, "currency1": mint, "fee": 3000,
           "tick_spacing": 60, "hooks": _NATIVE}
    registry = {mint: {"kind": "v4_native", "key": key, "pool": lane.v4_pool_id(key)}}
    requests, quote_inputs = [], []
    monkeypatch.setattr(evm_token_safety.cfg, "MEMECOIN_EVM_CLIP_EXIT_CHECK_CHAINS", ())

    def wire(method, url, headers=None, body=None, timeout=10):
        if method == "GET":
            assert url == PRICE
            requests.append("price")
            return 200, price_reply, {}
        payload = json.loads(body)
        name = payload["method"]
        if name == "eth_getCode":
            return 200, {"result": "0x6000"}, {}
        assert name == "eth_call"
        call = payload["params"][0]
        if call["data"] == "0x313ce567":
            return 200, {"result": "0x" + format(18, "064x")}, {}
        assert call["to"] == V4["4663"]["quoter"]
        requests.append("sell_quoter")
        if quoter_blind:
            return 200, {"error": {"code": -32603, "message": "provider unavailable"}}, {}
        # A complete, answered amountOut=0 is a measured miss, not missing provider evidence.
        return 200, {"result": "0x" + "00" * 32}, {}

    paced = lane.PacedHttp(rotation.http_seam(lambda: wire), min_gap_s=0,
                           clock=lambda: 1000.0)
    direct = _v4_direct_quote_fn("4663", None, taker)

    def quote(sell, buy, amount, slippage_bps=None):
        quote_inputs.append((sell, buy, amount))
        # Exact registry lookup used by the real safety_on quote seam.
        entry = registry.get(str(sell).lower())
        if entry is None:
            return None
        return lane.route_quote(direct, entry, sell, buy, amount, host=RPC, http=paced,
                                slippage_bps=slippage_bps)

    safety = evm_token_safety.build_evm_token_safety(
        chain_id="4663", rpc_url=RPC, taker=taker, quote_fn=quote, http=paced,
        sell_route_label="rh_fast_v4_direct")
    cache = lane.SafetyCache(lane.background_safety(safety, rotation, paced), ttl_s=60, usd=50)
    rec = cache.run_one(mint, now=1000)
    assert requests.count("sell_quoter") >= 1 and requests.count("price") == 1
    weth = evm_wallet.CHAIN_QUOTE["4663"]["token"]
    expected_quotes = [(mint, weth, 10 ** 18)]
    if price_reply == {"price": "2500"}:
        expected_quotes.append((weth, mint, 2_000_000_000_000_000))
    assert quote_inputs == expected_quotes
    if quoter_blind:
        assert rec is None and cache.reason(mint, now=1000) == "no_safety_verdict"
        assert rotation.provider_errors() > 0 and cache.counts["verdicts_unsafe"] == 0
    else:
        assert rec is not None and rec[:2] == (False, ROUTE_REASON)
        assert cache.reason(mint, now=1000) == "unsafe:" + ROUTE_REASON
        assert rotation.provider_errors() == 0 and cache.counts["verdicts_unsafe"] == 1

"""The default V2 reader uses the same bounded, chain-bound read pool as other venues."""

import json

import pytest

from sniper.execution import evm_rpc_pool as pool
from sniper.execution import evm_v2_exit_quote as Q
from sniper.settings import memecoin as cfg


PRIMARY = "https://primary.example.invalid"
BACKUP = "https://backup.example.invalid"
ROUTER = "0x" + "11" * 20
TOKEN = "0x" + "22" * 20
WETH = "0x" + "33" * 20
TAKER = "0x" + "44" * 20
LIMITED = {"error": {"code": -32005, "message": "Too Many Requests"}}


@pytest.fixture(autouse=True)
def isolated_pool(monkeypatch):
    pool.reset_for_tests()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RPC_POOL_URLS", {"1": (PRIMARY, BACKUP)})
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda chain: PRIMARY)
    pool.mark_identity(PRIMARY, True)
    yield
    pool.reset_for_tests()


def amounts_result(amounts):
    return "0x" + "".join(format(x, "064x") for x in (32, len(amounts), *amounts))


def quote(http, **kwargs):
    return Q.dex_v2_exit_quote(
        "1", TOKEN, WETH, 1000, TAKER, cfg_router=ROUTER,
        connector_tokens=(), tax_buffer_bps=0, slippage_bps=100,
        now_fn=lambda: 1000, http=http, **kwargs,
    )


def test_http_200_quota_reply_recovers_the_real_v2_quote_from_verified_backup():
    calls = []

    def http(method, url, *, body):
        request = json.loads(body)
        calls.append((method, url, request))
        if request["method"] == "eth_chainId":
            return 200, {"result": "0x1"}, None
        if url == PRIMARY:
            return 200, LIMITED, None
        return 200, {"result": amounts_result((1000, 2000))}, None

    result = quote(http)
    assert result is not None
    assert result["buyAmount"] == "2000"
    assert result["minBuyAmount"] == "1980"
    assert result["sellAmount"] == "1000"
    assert result["transaction"]["value"] == "0x0"
    assert [c[1] for c in calls] == [PRIMARY, BACKUP, BACKUP]
    assert [c[2]["method"] for c in calls] == ["eth_call", "eth_chainId", "eth_call"]
    assert calls[0][2] == calls[-1][2]
    assert calls[0][2]["params"] == [
        {"to": ROUTER, "data": Q._encode_get_amounts_out(1000, (TOKEN, WETH))}, "latest",
    ]
    assert pool.available(PRIMARY) is False
    assert pool.identity_state(BACKUP) is True


@pytest.mark.parametrize("failure", ["http429", "http500", "transport", "timeout"])
def test_provider_failure_uses_existing_bounded_read_failover(failure):
    calls = []
    pool.mark_identity(BACKUP, True)

    def http(method, url, *, body):
        request = json.loads(body)
        calls.append((url, request))
        assert request["method"] == "eth_call"
        if url == PRIMARY:
            if failure == "transport":
                raise OSError("read transport unavailable")
            if failure == "timeout":
                raise TimeoutError("read timed out")
            return (429 if failure == "http429" else 500), None, None
        return 200, {"result": "0x1234"}, None

    assert Q._make_call_fn("1", http)(ROUTER, "0x1234") == "0x1234"
    assert [url for url, _ in calls] == [PRIMARY, BACKUP]
    assert calls[0][1] == calls[-1][1]
    assert not pool.available(PRIMARY)


def test_warm_healthy_primary_has_one_exact_read_and_no_backup_or_identity_work():
    calls = []

    def http(method, url, *, body):
        calls.append((method, url, json.loads(body)))
        return 200, {"result": "0x1234"}, None

    assert Q._make_call_fn("1", http)(ROUTER, "0xabcd") == "0x1234"
    assert calls == [("POST", PRIMARY, {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": ROUTER, "data": "0xabcd"}, "latest"],
    })]
    assert pool.identity_state(BACKUP) is None


def test_wrong_chain_backup_never_supplies_a_quote():
    calls = []

    def http(method, url, *, body):
        request = json.loads(body)
        calls.append((url, request["method"]))
        if request["method"] == "eth_chainId":
            return 200, {"result": "0x38"}, None
        assert url == PRIMARY, "wrong-chain endpoint must never receive the quote payload"
        return 200, LIMITED, None

    blind = {}
    assert quote(http, blind_out=blind) is None
    assert blind["reason"] == "rpc_rate_limit"
    assert (BACKUP, "eth_chainId") in calls
    assert (BACKUP, "eth_call") not in calls
    assert pool.identity_state(BACKUP) is False
    assert len(calls) <= 3


def test_contract_revert_is_a_real_answer_without_failover_or_health_penalty():
    calls = []

    def http(method, url, *, body):
        calls.append((url, json.loads(body)["method"]))
        return 200, {"error": {"code": 3, "message": "execution reverted: missing pair"}}, None

    blind = {}
    assert quote(http, blind_out=blind) is None
    assert blind == {}
    assert calls == [(PRIMARY, "eth_call")]
    assert pool.available(PRIMARY)


@pytest.mark.parametrize("payload", [None, [], "invalid", {}, {"error": "provider failed"}])
def test_malformed_or_absent_reply_remains_typed_unavailable(payload):
    def http(method, url, *, body):
        return 200, payload, None

    with pytest.raises(Q.V2ReadUnavailable):
        Q._make_call_fn("1", http)(ROUTER, "0xabcd")


@pytest.mark.parametrize("raw", [None, 0, "0x", "0x00", "not-hex"])
def test_result_without_a_valid_amount_array_never_fabricates_a_quote(raw):
    def http(method, url, *, body):
        return 200, {"result": raw}, None

    blind = {}
    assert quote(http, blind_out=blind) is None
    assert blind == {}


@pytest.mark.parametrize("failure", ["quota_body", "http429", "http500", "transport"])
def test_exhausted_pool_remains_typed_infrastructure_unknown(failure):
    pool.mark_identity(BACKUP, True)
    calls = []

    def http(method, url, *, body):
        calls.append((url, json.loads(body)["method"]))
        if failure == "transport":
            raise OSError("read transport unavailable")
        if failure == "quota_body":
            return 200, LIMITED, None
        return (429 if failure == "http429" else 500), None, None

    blind = {}
    assert quote(http, blind_out=blind) is None
    assert blind["reason"] == ("rpc_rate_limit" if failure in ("quota_body", "http429")
                               else "rpc_transport")
    assert calls == [(PRIMARY, "eth_call"), (BACKUP, "eth_call")]


def test_non_quota_32005_is_unknown_without_inventing_a_rate_limit():
    calls = []

    def http(method, url, *, body):
        calls.append(url)
        return 200, {"error": {"code": -32005, "message": "limit exceeded"}}, None

    blind = {}
    assert quote(http, blind_out=blind) is None
    assert blind["reason"] == "rpc_transport"
    assert calls == [PRIMARY]
    assert pool.available(PRIMARY)


@pytest.mark.parametrize("after_quota", [False, True])
def test_budget_refusal_propagates_unchanged_and_never_penalizes_unsent_endpoint(after_quota):
    pool.mark_identity(BACKUP, True)
    refusal = pool.RequestNotSent("caller read deadline")
    calls = []

    def http(method, url, *, body):
        request = json.loads(body)
        items = request if isinstance(request, list) else [request]
        assert all(item["method"] == "eth_call" for item in items)
        calls.append((url, items[0]["method"]))
        if after_quota and url == PRIMARY:
            return 200, LIMITED, None
        raise refusal

    with pytest.raises(pool.RequestNotSent) as caught:
        Q.dex_v2_exit_quote(
            "1", TOKEN, WETH, 1000, TAKER, cfg_router=ROUTER,
            connector_tokens=(TAKER,), tax_buffer_bps=0, slippage_bps=100,
            now_fn=lambda: 1000, http=http,
        )
    assert caught.value is refusal
    assert calls == ([(PRIMARY, "eth_call"), (BACKUP, "eth_call")]
                     if after_quota else [(PRIMARY, "eth_call")])
    assert pool.available(BACKUP)
    assert pool.available(PRIMARY) is (not after_quota)
    stats = {item["host"]: item for item in pool.snapshot((PRIMARY, BACKUP))}
    unsent = stats["backup.example.invalid" if after_quota else "primary.example.invalid"]
    assert (unsent["ok"], unsent["fail"], unsent["penalties"]) == (0, 0, 0)


def test_optional_exit_valuation_budget_refusal_keeps_original_cut_and_signed_bytes():
    original = Q.dex_v2_exit_quote(
        "1", TOKEN, WETH, 1000, TAKER, cfg_router=ROUTER,
        connector_tokens=(), tax_buffer_bps=1500, slippage_bps=100,
        now_fn=lambda: 1000,
        http=lambda method, url, *, body: (
            200, {"result": amounts_result((850, 2000))}, None,
        ),
    )
    before = json.dumps(original, sort_keys=True)
    calls = []

    def budget_refusal(method, url, *, body):
        calls.append((url, json.loads(body)["method"]))
        raise pool.RequestNotSent("caller read deadline")

    assert Q.exit_value_amount(original, http=budget_refusal, value_tax_bps=400) is None
    assert json.dumps(original, sort_keys=True) == before
    assert calls == [(PRIMARY, "eth_call")]
    assert pool.available(PRIMARY)
    stats = pool.snapshot((PRIMARY,))[0]
    assert (stats["ok"], stats["fail"], stats["penalties"]) == (1, 0, 0)


def test_unconfigured_rpc_remains_a_noop(monkeypatch):
    monkeypatch.setattr(cfg, "evm_rpc_for_chain", lambda chain: "")

    def http(*args, **kwargs):
        pytest.fail("missing RPC must never send a request")

    assert Q._make_call_fn("1", http)(ROUTER, "0xabcd") is None


def test_explicit_unpooled_rpc_keeps_its_single_read_target():
    calls = []
    custom = "https://explicit.example.invalid"

    def http(method, url, *, body):
        calls.append((url, json.loads(body)))
        return 200, {"result": "0x1234"}, None

    assert Q._make_call_fn("1", http, rpc=custom)(ROUTER, "0xabcd") == "0x1234"
    assert len(calls) == 1
    assert calls[0][0] == custom
    assert calls[0][1]["method"] == "eth_call"

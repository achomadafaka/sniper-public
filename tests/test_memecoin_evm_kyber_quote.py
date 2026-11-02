"""A third party's calldata is verified field by field, or it is not a venue.

Twelve 4663 signals since 2026-09-15 refused `no_route_all_venues` on mints whose liquidity we
could not reach; BILL's own buyer filled through KyberSwap. This rung reaches it directly. The
aggregator is a third party, so every field that decides where the money goes is re-derived from
OUR request and compared against the pinned contracts.

No network. The route payloads here are fakes shaped like the live ones.
"""
import pytest

from sniper.execution import evm_kyber_quote as K
from sniper.execution.holding_routes import KYBER_TYPES, PINS

PIN = PINS["kyberswap"]
SELL = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
BUY = "0x" + "44" * 20
TAKER = "0x" + "11" * 20
AMOUNT = 10 ** 16
OUT = 7 * 10 ** 22


def _encode(*, src=SELL, dst=BUY, executor=None, approve=None, receivers=None, amounts=None,
            fees=(), fee_amounts=(), receiver=TAKER, qty=AMOUNT, nested_min=1, flags=512,
            permit=b"", target_data=b"\x01"):
    """Build calldata in the exact shape `holding_routes._swap` decodes."""
    from eth_abi import encode
    executor = executor or PIN["executor"]
    approve = approve if approve is not None else "0x" + "0" * 40
    receivers = (executor,) if receivers is None else receivers
    amounts = (qty,) if amounts is None else amounts
    desc = (src, dst, list(receivers), list(amounts), list(fees), list(fee_amounts),
            receiver, qty, nested_min, flags, permit)
    body = encode([KYBER_TYPES[0]], [(executor, approve, target_data, desc, b"")])
    return PIN["selector"] + body.hex()


def _built(**kw):
    return {"routerAddress": PIN["target"], "data": _encode(**kw)}


def _verify(built=None, **over):
    kw = {"sell_token": SELL, "buy_token": BUY, "amount_atomic": AMOUNT, "taker": TAKER,
          "min_out": 1, "verify_code": False}
    kw.update(over)
    return K.verify_build(built if built is not None else _built(), **kw)


# --- the happy path, so every refusal below means something -------------------------------------

def test_a_well_formed_route_verifies():
    tx = _verify()
    assert tx and tx["to"] == PIN["target"]
    assert tx["data"].startswith(PIN["selector"])


# --- every field that decides where the money goes ----------------------------------------------

def test_a_router_that_is_not_the_pin_is_refused():
    assert _verify({"routerAddress": "0x" + "99" * 20, "data": _encode()}) is None


def test_a_selector_that_is_not_the_pin_is_refused():
    bad = {"routerAddress": PIN["target"], "data": "0xdeadbeef" + _encode()[10:]}
    assert _verify(bad) is None


def test_an_executor_that_is_not_the_pin_is_refused():
    """The executor receives the input tokens. A substituted one is a drain."""
    assert _verify(_built(executor="0x" + "77" * 20)) is None


def test_a_non_zero_approve_target_is_refused():
    assert _verify(_built(approve="0x" + "88" * 20)) is None


def test_a_recipient_that_is_not_us_is_refused():
    """THE PROPERTY THIS FILE EXISTS FOR. The coin must arrive at OUR wallet."""
    assert _verify(_built(receiver="0x" + "99" * 20)) is None


def test_swapped_tokens_are_refused():
    assert _verify(_built(src=BUY, dst=SELL)) is None


def test_an_input_amount_that_is_not_ours_is_refused():
    assert _verify(_built(qty=AMOUNT * 2)) is None


def test_input_recipients_that_are_not_the_executor_are_refused():
    assert _verify(_built(receivers=("0x" + "66" * 20,))) is None


def test_a_nested_minimum_below_ours_is_refused():
    """The aggregator may round the minimum; it may never hand back a MEANINGFULLY weaker one."""
    assert _verify(_built(nested_min=90), min_out=100) is None
    assert _verify(_built(nested_min=100), min_out=100) is not None


def test_a_few_wei_of_rounding_is_tolerated_and_more_is_not():
    """Measured 2026-09-22: a genuine GIGABYTE build came back exactly ONE wei under our floor and
    an exact comparison refused all three live coins. The band is rounding-sized, not a discount.
    Pinned from both sides so widening it needs a deliberate edit."""
    assert _verify(_built(nested_min=999_999_999_998), min_out=10 ** 12) is not None
    assert _verify(_built(nested_min=999_999_999_997), min_out=10 ** 12) is None


@pytest.mark.parametrize("kw", [{"fees": ("0x" + "22" * 20,)}, {"fee_amounts": (5,)},
                                {"permit": b"\x01"}, {"flags": 0}])
def test_unproven_fee_permit_or_flag_shapes_are_refused(kw):
    """These change who is paid and under what authority. An unproven shape is refused, never
    guessed -- the same rule holding_routes._swap has enforced on this router since 09-17."""
    assert _verify(_built(**kw)) is None


def test_undecodable_calldata_is_refused_not_raised():
    assert _verify({"routerAddress": PIN["target"], "data": PIN["selector"] + "00"}) is None


def test_a_missing_build_is_refused():
    assert _verify({}) is None


# --- the gates ----------------------------------------------------------------------------------

def test_the_rung_is_off_by_default(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", False, raising=False)
    called = []
    monkeypatch.setattr(K, "_routes", lambda *a, **k: called.append(1))
    assert K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER) is None
    assert called == [], "a disabled rung must not reach the network"


def test_a_chain_outside_the_allowlist_never_reaches_the_network(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", ("4663",), raising=False)
    called = []
    monkeypatch.setattr(K, "_routes", lambda *a, **k: called.append(1))
    assert K.dex_kyber_quote("56", SELL, BUY, AMOUNT, TAKER) is None
    assert called == []


def _enable(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", ("4663",), raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_SLIPPAGE_PCT", 3.0, raising=False)


def test_a_quote_carries_an_exact_allowance_to_the_router_not_the_executor(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(K, "_routes", lambda *a, **k: {"amountOut": str(OUT), "route": [[1, 2, 3]]})
    # the nested minimum must clear OUR 3% floor, exactly as a live build's does
    monkeypatch.setattr(K, "_http_json",
                        lambda *a, **k: {"data": _built(nested_min=OUT * 9700 // 10000)})
    q = K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER, verify_code=False)
    assert q["buyAmount"] == str(OUT) and q["sellAmount"] == str(AMOUNT)
    allow = q["issues"]["allowance"]
    assert allow["spender"] == PIN["target"], "the executor must never hold our allowance"
    assert allow["amount"] == str(AMOUNT), "exact-amount approval only"
    assert q["route"]["protocol"] == "kyberswap"


# --- BSC (2026-09-23): same router address, different bytecode, so its own pin ----------------

def test_bsc_has_its_own_pin_with_the_same_addresses_and_different_code():
    bsc = K._pin("56")
    assert bsc is not None
    assert bsc["target"] == PIN["target"] and bsc["executor"] == PIN["executor"]
    assert bsc["selector"] == PIN["selector"]
    assert bsc["code_hash"] != PIN["code_hash"]
    assert bsc["executor_code_hash"] != PIN["executor_code_hash"]
    assert K._base("56") == "https://aggregator-api.kyberswap.com/bsc/api/v1"
    assert K._base("4663") == "https://aggregator-api.kyberswap.com/robinhood/api/v1"


def test_a_well_formed_bsc_route_verifies_against_the_bsc_pin():
    tx = _verify(chain="56")                       # same addresses, so the fixture encodes fine
    assert tx and tx["to"] == K._pin("56")["target"]


def test_a_chain_with_no_pin_is_a_miss_and_never_reaches_the_network(monkeypatch):
    # Arc (5042) has no Kyber pin. Base was the example here until it got its own pin 2026-09-25.
    assert K._pin("5042") is None and K._base("5042") is None
    assert _verify(chain="5042") is None
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_CHAINS", ("4663", "5042"), raising=False)
    called = []
    monkeypatch.setattr(K, "_routes", lambda *a, **k: called.append(1))
    assert K.dex_kyber_quote("5042", SELL, BUY, AMOUNT, TAKER, verify_code=False) is None
    assert called == []


# --- Base (2026-09-25): same addresses, its own router bytecode, BSC's executor bytecode --------

def test_base_has_its_own_pin_measured_from_base_bytecode():
    base, bsc = K._pin("8453"), K._pin("56")
    assert base is not None
    assert base["target"] == PIN["target"] and base["executor"] == PIN["executor"]
    assert base["selector"] == PIN["selector"]
    assert base["code_hash"] not in (PIN["code_hash"], bsc["code_hash"])
    assert base["executor_code_hash"] == bsc["executor_code_hash"] != PIN["executor_code_hash"]
    assert K._base("8453") == "https://aggregator-api.kyberswap.com/base/api/v1"


def test_a_well_formed_base_route_verifies_against_the_base_pin():
    tx = _verify(chain="8453")
    assert tx and tx["to"] == K._pin("8453")["target"]


def test_base_code_verification_refuses_another_chains_router_bytecode(monkeypatch):
    base = K._pin("8453")
    monkeypatch.setattr(K, "_code_hash", lambda addr, rpc_url, http=None: {
        base["target"]: base["code_hash"], base["executor"]: base["executor_code_hash"]}[addr])
    assert _verify(chain="8453", verify_code=True, rpc_url="http://rpc") is not None
    for foreign in (PIN["code_hash"], K._pin("56")["code_hash"]):
        monkeypatch.setattr(K, "_code_hash", lambda addr, rpc_url, http=None, f=foreign: {
            base["target"]: f, base["executor"]: base["executor_code_hash"]}[addr])
        assert _verify(chain="8453", verify_code=True, rpc_url="http://rpc") is None


def test_bsc_code_verification_checks_the_bsc_hashes_not_the_robinhood_ones(monkeypatch):
    bsc = K._pin("56")
    seen = []
    def fake_hash(addr, rpc_url, http=None):
        seen.append(addr)
        return {bsc["target"]: bsc["code_hash"], bsc["executor"]: bsc["executor_code_hash"]}[addr]
    monkeypatch.setattr(K, "_code_hash", fake_hash)
    assert _verify(chain="56", verify_code=True, rpc_url="http://rpc") is not None
    assert seen == [bsc["target"], bsc["executor"]]
    # the 4663 hashes on a BSC read must refuse: different bytecode is the whole point of the pin
    monkeypatch.setattr(K, "_code_hash", lambda addr, rpc_url, http=None: PIN["code_hash"])
    assert _verify(chain="56", verify_code=True, rpc_url="http://rpc") is None


def test_a_network_failure_is_typed_blindness_not_a_missing_route(monkeypatch):
    """REPLACES `test_a_network_failure_is_a_miss_not_a_crash` (E5, 2026-09-23). That test pinned
    the swallow this finding is about: a bare None makes the ladder record `blind_reason=None` and
    `classify_quote_miss` publish `no_route`, a verdict on the coin, for a read we never made.

    "Not a crash" is still true and still the property that matters -- the ladder's `_run` has
    always caught a rung's exception and read `failure_type` off it. What changed is that this
    rung now participates in that contract instead of opting out of it.
    """
    _enable(monkeypatch)

    def boom(*_a, **_k):
        raise OSError("aggregator down")
    monkeypatch.setattr(K, "_routes", boom)
    with pytest.raises(K.KyberQuoteUnavailable) as caught:
        K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER)
    assert caught.value.failure_type == "rpc_transport"


# --- a code hash we could not READ is not a code hash that MOVED --------------------------------

def test_a_code_hash_read_that_times_out_is_a_typed_provider_failure(monkeypatch):
    """E5 recheck, 2026-09-23. The pinned-bytecode reads sat inside the broad `except Exception`
    that exists for calldata decoding, so a timeout, a 429 or transport loss on `eth_getCode`
    returned `calldata did not decode` -> None, and the ladder published `no_route`: a verdict
    about the coin produced by a read we never made. This is the same class 33aa3d45 fixed for the
    HTTP legs, left behind on the code-hash leg."""
    _enable(monkeypatch)
    monkeypatch.setattr(K, "_routes", lambda *_a, **_k: {"amountOut": str(OUT)})
    monkeypatch.setattr(K, "_http_json", lambda *_a, **_k: {"data": _built(nested_min=OUT)})

    def unavailable(*_a, **_k):
        raise TimeoutError("code hash read timed out")
    monkeypatch.setattr(K, "_code_hash", unavailable)
    with pytest.raises(K.KyberQuoteUnavailable) as caught:
        K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER, rpc_url="offline://rpc")
    assert caught.value.failure_type == "timeout"


def test_an_empty_code_read_never_claims_the_pinned_bytecode_moved(monkeypatch):
    """A node that answers `0x` has told us NOTHING. `_code_hash` returns None there, and the
    comparison against the pin then refused with "router code hash moved: None" -- a false
    statement about a pinned invariant, indistinguishable in the log from a real compromise. Our
    own custody node does this: it keeps state for minutes and then answers `0x` for an older
    block with no error at all."""
    _enable(monkeypatch)
    monkeypatch.setattr(K, "_routes", lambda *_a, **_k: {"amountOut": str(OUT)})
    monkeypatch.setattr(K, "_http_json", lambda *_a, **_k: {"data": _built(nested_min=OUT)})
    monkeypatch.setattr(K, "_code_hash", lambda *_a, **_k: None)
    with pytest.raises(K.KyberQuoteUnavailable) as caught:
        K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER, rpc_url="offline://rpc")
    assert caught.value.failure_type == "rpc_transport"


def test_a_code_hash_that_was_read_and_differs_is_still_a_refusal(monkeypatch):
    """The other side of the same boundary: a hash we actually READ and that does not match the
    pin is a real refusal and must stay one, silently, with no quote and nothing raised."""
    _enable(monkeypatch)
    monkeypatch.setattr(K, "_routes", lambda *_a, **_k: {"amountOut": str(OUT)})
    monkeypatch.setattr(K, "_http_json", lambda *_a, **_k: {"data": _built(nested_min=OUT)})
    monkeypatch.setattr(K, "_code_hash", lambda *_a, **_k: "0x" + "ab" * 32)
    assert K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER, rpc_url="offline://rpc") is None


@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("read timed out"), "timeout"),
    (__import__("urllib.error", fromlist=["error"]).URLError(TimeoutError("timed out")), "timeout"),
    (__import__("urllib.error", fromlist=["error"]).URLError("connection refused"), "rpc_transport"),
    (OSError("aggregator down"), "rpc_transport"),
    (ValueError("Expecting value: line 1 column 1"), "malformed_response"),
    (AttributeError("'list' object has no attribute 'get'"), "malformed_response"),
    (TypeError("string indices must be integers"), "malformed_response"),
])
def test_each_failure_kind_keeps_its_own_name(monkeypatch, exc, expected):
    """Timeout, rejection, transport loss and an unparseable body are four different facts, and
    only the last two ever shared a bucket. None of them is a statement about the token."""
    _enable(monkeypatch)

    def boom(*_a, **_k):
        raise exc
    monkeypatch.setattr(K, "_routes", boom)
    with pytest.raises(K.KyberQuoteUnavailable) as caught:
        K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER)
    assert caught.value.failure_type == expected


def test_a_rate_limited_aggregator_is_not_a_transport_fault(monkeypatch):
    """429 is the provider refusing US, which the retry lane treats differently from a dead
    socket. It must not disappear into `rpc_transport`."""
    import urllib.error
    _enable(monkeypatch)

    def boom(*_a, **_k):
        raise urllib.error.HTTPError("http://agg", 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(K, "_routes", boom)
    with pytest.raises(K.KyberQuoteUnavailable) as caught:
        K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER)
    assert caught.value.failure_type == "rate_limit"


@pytest.mark.parametrize("status", [403, 500, 502])
def test_any_other_http_status_is_transport_never_route_absence(monkeypatch, status):
    """403 is the User-Agent trap this module's own header exists for, and a 5xx is the
    aggregator falling over. Neither says the coin has no route."""
    import urllib.error
    _enable(monkeypatch)

    def boom(*_a, **_k):
        raise urllib.error.HTTPError("http://agg", status, "nope", {}, None)
    monkeypatch.setattr(K, "_routes", boom)
    with pytest.raises(K.KyberQuoteUnavailable) as caught:
        K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER)
    assert caught.value.failure_type == "rpc_transport"


def test_an_answered_route_absence_is_still_a_plain_miss(monkeypatch):
    """THE OTHER HALF, and the one that keeps this honest. A 200 carrying no routeSummary is the
    aggregator ANSWERING that it found nothing, so it must stay None and keep reading `no_route`.
    Typing blindness is worthless if it also relabels a real answer."""
    _enable(monkeypatch)
    monkeypatch.setattr(K, "_routes", lambda *a, **k: None)
    assert K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER) is None


def test_a_disabled_or_unpinned_rung_still_returns_none_without_raising(monkeypatch):
    """The gate returns are 'this rung does not apply here', not 'we could not read'. A raise
    would turn every chain without a pin into a provider fault."""
    from sniper.settings import memecoin as cfg
    _enable(monkeypatch)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", False, raising=False)
    assert K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER) is None
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_KYBER_AGG_ENABLED", True, raising=False)
    assert K.dex_kyber_quote("999", SELL, BUY, AMOUNT, TAKER) is None
    assert K.dex_kyber_quote("4663", SELL, BUY, 0, TAKER) is None


def test_a_zero_output_route_is_refused(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(K, "_routes", lambda *a, **k: {"amountOut": "0"})
    assert K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER) is None


def test_the_min_out_is_derived_from_our_slippage_not_the_aggregators(monkeypatch):
    """The nested minimum is checked against OUR number, so a generous aggregator cannot weaken
    the floor and a stingy one cannot tighten it silently."""
    _enable(monkeypatch)
    seen = {}
    monkeypatch.setattr(K, "_routes", lambda *a, **k: {"amountOut": str(OUT), "route": [[1]]})

    def fake_verify(built, **kw):
        seen.update(kw)
        return {"to": PIN["target"], "data": "0x", "value": "0x0"}
    monkeypatch.setattr(K, "_http_json", lambda *a, **k: {"data": _built()})
    monkeypatch.setattr(K, "verify_build", fake_verify)
    K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER, slippage_bps=300, verify_code=False)
    assert seen["min_out"] == OUT * 9700 // 10000


def test_a_quote_carries_the_minimum_output_the_exit_intent_binds_on(monkeypatch):
    """RATBRAIN 64081 (4663, 2026-09-23): the sell ladder took the Kyber quote, `bind_quote` found
    no `minBuyAmount`, and the executor skipped the rung silently at 10, 20 and 30% and marked the
    bag stuck. The minimum is the same one verified inside the calldata."""
    _enable(monkeypatch)
    monkeypatch.setattr(K, "_routes", lambda *a, **k: {"amountOut": str(OUT), "route": [[1, 2, 3]]})
    monkeypatch.setattr(K, "_http_json",
                        lambda *a, **k: {"data": _built(nested_min=OUT * 9700 // 10000)})
    q = K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER, verify_code=False, slippage_bps=300)
    assert q["minBuyAmount"] == str(OUT * 9700 // 10000) == q["route"]["min_out"]
    from sniper.services.memecoin_exit_intent import (
        bind_quote, build_exit_intent, quote_binding_complete)
    intent = bind_quote(build_exit_intent({"id": 1}, claim_id="c", action="close", fraction=1.0), q)
    assert quote_binding_complete(intent)


@pytest.mark.parametrize("estimate,expected", [(None, 900_000), ("400000", 900_000),
                                               ("700000", 1_400_000), ("5000000", 3_000_000)])
def test_the_transaction_carries_a_gas_limit_above_the_build_estimate(estimate, expected):
    """RATBRAIN 64081: no `gas` on the transaction, the signer's 500,000 default, and 15 sells out
    of gas at ~489,000 used against a 611,000 simulation. 2.0x the build's estimate since
    2026-09-30 (MEMECOIN_EVM_KYBER_BUY_GAS_MULT; 1.5x before), floor 900k, cap 3M."""
    built = _built()
    if estimate is not None:
        built = dict(built, gas=estimate)
    tx = _verify(built)
    assert tx and int(tx["gas"]) == expected


@pytest.mark.parametrize("estimate,entry_limit", [("1005956", 2_011_912),
                                                    ("517594", 1_035_188)])
def test_kyber_exits_use_full_gas_ceiling_without_changing_entry(monkeypatch, estimate,
                                                                  entry_limit):
    """BLUB 65688 and INUTEL 65700 reverted below 3M despite Kyber's estimate; INUTEL at 2,840,977
    of 3,000,000, then filled at 2.08M. Sells take MEMECOIN_EVM_KYBER_SELL_GAS (5,000,000)."""
    _enable(monkeypatch)
    monkeypatch.setattr(K, "_routes", lambda *a, **k: {"amountOut": str(OUT), "route": [[1]]})
    monkeypatch.setattr(K, "_http_json", lambda *a, **k: {
        "data": dict(_built(nested_min=OUT * 9700 // 10000), gas=estimate)})
    entry = K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER,
                              slippage_bps=300, verify_code=False)
    exit_quote = K.dex_kyber_exit_quote("4663", SELL, BUY, AMOUNT, TAKER,
                                        slippage_bps=300, verify_code=False)
    assert int(entry["transaction"]["gas"]) == entry_limit
    assert int(exit_quote["transaction"]["gas"]) == 5_000_000


@pytest.mark.parametrize("body,blind", [
    (b'{"code":4008,"message":"route not found"}', None),
    (b'{"code":"4008","message":"route not found"}', None),
    (b'{"code":4011,"message":"token not found"}', "rpc_transport"),
    (b'{"message":"bad"}', "rpc_transport"),
])
def test_kyber_4008_is_an_answered_no_route_and_other_400s_stay_blind(monkeypatch, body, blind):
    """DAODUN 68544 (2026-10-02): every exit ladder rung published provider_rpc_transport for
    Kyber's own "HTTP 400 4008 route not found". That is an answer about the pair, the same fact as
    a 200 with no routeSummary. Through the real _http_json, so the code is read off the body."""
    import io
    import urllib.error
    import urllib.request
    _enable(monkeypatch)

    def refuse(*_a, **_k):
        raise urllib.error.HTTPError("http://agg", 400, "Bad Request", {}, io.BytesIO(body))
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    if blind is None:
        assert K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER) is None
    else:
        with pytest.raises(K.KyberQuoteUnavailable) as caught:
            K.dex_kyber_quote("4663", SELL, BUY, AMOUNT, TAKER)
        assert caught.value.failure_type == blind

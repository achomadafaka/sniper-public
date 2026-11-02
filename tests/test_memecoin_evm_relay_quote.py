import io
import json
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError

from eth_abi import decode, encode
from eth_account import Account
import pytest

from sniper.execution import evm_relay_quote as relay


WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
TOKEN = "0xa4fbbc5569bfb8751641c57d19213d5ed5d9318d"
TAKER = "0x898be9c27f8b391c47a3d9f2da17781aa8aa3a1d"
AMOUNT = 10**16
MIN_OUT = 7_500_000


def _tenet_quote():
    fixture = json.loads((Path(__file__).parent / 'fixtures/memecoin/' /
                          'relay_tenet_signed_minimum_20261006.json').read_text())
    request = fixture['request']
    quote = relay.dex_relay_quote(
        '4663', request['sell'], request['buy'], request['amount'], request['taker'],
        http=lambda *_a, **_k: (200, fixture['payload'], {}), slippage_bps=300,
        code_hash_fn=_code_hash)
    return quote, fixture


def test_real_relay_guard_binds_the_wallet_minimum_without_io_or_shared_mutation(monkeypatch):
    from sniper.execution.entry_attribution import bind_quote_metadata, copy_minimum_refusal
    quote, fixture = _tenet_quote()
    original = deepcopy(quote)
    monkeypatch.setattr(relay, '_live_code_hash', lambda *_a, **_k: pytest.fail('entry RPC'))
    bound = relay.bind_relay_output_minimum(quote, guard_verified=True)
    assert bound is not None and quote == original
    args = decode(['address[]', 'uint256[]', '(address,bool,uint256,bytes)[]',
                   'address', 'address', 'bytes'], bytes.fromhex(bound['transaction']['data'][10:]))
    guard = args[2][-2][3]
    assert '0x' + guard[79:99].hex() == fixture['request']['taker']
    assert '0x' + guard[119:139].hex() == fixture['request']['buy']
    assert int.from_bytes(guard[42:58], 'big') == int(bound['minBuyAmount'])
    assert bound['transaction']['data'][-64:] == quote['route']['request_id'][2:]
    priced = bind_quote_metadata(bound, network_id='4663', mint=fixture['request']['buy'],
        quote_observed_at='2026-10-06T20:30:00Z', sell_decimals=18, buy_decimals=18,
        sell_token_usd=2600.)
    source = 26 / (int(bound['buyAmount']) / 1e18)
    assert copy_minimum_refusal(priced, source, 35.) is None
    assert 'copy price' in copy_minimum_refusal(priced, source / 2, 35.)
    tampered = deepcopy(priced)
    tampered['transaction']['data'] = quote['transaction']['data']
    assert copy_minimum_refusal(tampered, source, 35.) is not None
    assert relay.bind_relay_output_minimum(quote, guard_verified=False) is None
    assert '0x' + relay.keccak(bytes.fromhex(fixture['guard']['runtime'][2:])).hex() == \
        relay.RELAY_OUTPUT_GUARD_CODE_HASH


@pytest.mark.parametrize('verified', [False, True])
@pytest.mark.parametrize('is_buy', [False, True])
def test_relay_ladder_binds_only_buys_with_a_startup_verified_guard(monkeypatch, verified, is_buy):
    from sniper.execution import evm_real_seams as seams
    from sniper.settings import memecoin as cfg
    quote, fixture = _tenet_quote()
    request = fixture['request']
    original = deepcopy(quote)
    for name in ('MEMECOIN_EVM_RELAY_ENABLED', 'MEMECOIN_EVM_RELAY_ENTRY_ENABLED',
                 'MEMECOIN_EVM_ENTRY_MEASURED_LADDER'):
        monkeypatch.setattr(cfg, name, True)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 35.)
    monkeypatch.setattr(seams, '_rung_ladder_chains', lambda _: {'4663'})
    monkeypatch.setattr(seams, '_entry_tp_pct', lambda _: 25.)
    calls = []
    def provider(chain, sell, buy, amount, taker, **kwargs):
        calls.append((sell, buy))
        if is_buy and sell == request['buy']:
            return {'buyAmount': str(request['amount'] * 98 // 100)}
        return quote
    fn = seams.venue_quote_with_v2_fallback(
        lambda *_a, **_k: None, chain='4663', quote_token=request['sell'],
        taker=request['taker'], relay_quote_fn=provider, enabled=True,
        v2_quote_fn=lambda *_a, **_k: None, v4_quote_fn=lambda *_a, **_k: None,
        rpc_url='https://rpc', relay_output_guard_verified=verified)
    sell, buy = (request['sell'], request['buy']) if is_buy else (request['buy'], request['sell'])
    result = fn(sell, buy, request['amount'], only_rung='relay')
    assert result is not None
    assert (relay.relay_output_minimum(result) is not None) == (is_buy and verified)
    assert quote['transaction'] == original['transaction']
    assert len(calls) == (2 if is_buy else 1)  # existing forward/reverse calls only


def test_output_guard_code_mismatch_fails_startup_verification(monkeypatch):
    calls = []
    def code(_rpc, address, http=None):
        calls.append(address)
        return '0xbad' if address == relay.RELAY_OUTPUT_GUARD else _code_hash(address)
    monkeypatch.setattr(relay, '_live_code_hash', code)
    with pytest.raises(relay.RelayQuoteError, match='code_mismatch'):
        relay.warm_relay_code_hashes('https://rpc', output_guard=True)
    assert calls[-1] == relay.RELAY_OUTPUT_GUARD


@pytest.mark.parametrize('mutation', ['target', 'selector', 'output', 'recipient', 'failure',
                                    'extra_call', 'zero', 'overflow', 'input', 'cleanup'])
def test_relay_minimum_binding_rejects_unknown_or_mismatched_terminal_calls(mutation):
    quote, _ = _tenet_quote()
    types = ['address[]', 'uint256[]', '(address,bool,uint256,bytes)[]',
             'address', 'address', 'bytes']
    args = list(decode(types, bytes.fromhex(quote['transaction']['data'][10:])))
    calls = list(args[2]); guard = list(calls[-2]); data = bytearray(guard[3])
    if mutation == 'target': guard[0] = TOKEN
    if mutation == 'selector': data[:4] = b'\x00' * 4
    if mutation == 'output': data[119:139] = bytes.fromhex(TOKEN[2:])
    if mutation == 'recipient': data[79:99] = bytes.fromhex(TOKEN[2:])
    if mutation == 'failure': guard[1] = True
    if mutation == 'zero': quote['minBuyAmount'] = '0'
    if mutation == 'overflow': quote['minBuyAmount'] = str(2**128)
    if mutation == 'input': args[1] = [1]
    if mutation == 'cleanup':
        tail = list(calls[-1]); tail[3] = b'\x00' * 4; calls[-1] = tuple(tail)
    guard[3] = bytes(data); calls[-2] = tuple(guard)
    if mutation == 'extra_call': calls.append((TOKEN, False, 0, b'\x00' * 4))
    args[2] = calls
    quote['transaction'] = {**quote['transaction'],
                            'data': '0x' + relay.TRANSFER_AND_MULTICALL + encode(types, args).hex()}
    assert relay.bind_relay_output_minimum(quote, guard_verified=True) is None


def _payload(*, recipient=TAKER, token=WETH, amount=AMOUNT, allow_failure=False,
             include_approval=True):
    cleanup = bytes.fromhex("9bb43718") + encode(
        ["address[]", "address[]", "uint256[]", "bytes"],
        [[TOKEN], [TAKER], [0], b"receipt"],
    )
    calls = [(relay.RELAY_ROUTER, allow_failure, 0, cleanup)]
    data = "0x" + relay.TRANSFER_AND_MULTICALL + encode(
        ["address[]", "uint256[]", "(address,bool,uint256,bytes)[]",
         "address", "address", "bytes"],
        [[token], [amount], calls, recipient, recipient, b"route"],
    ).hex()
    steps = [{"id": "swap", "kind": "transaction", "items": [{"data": {
                "chainId": 4663, "from": TAKER, "to": relay.RELAY_APPROVAL_PROXY,
                "data": data, "value": "0", "gas": "700000",
            }}]}]
    if include_approval:
        approve_data = "0x095ea7b3" + encode(
            ["address", "uint256"], [relay.RELAY_APPROVAL_PROXY, amount]
        ).hex()
        steps.insert(0, {"id": "approve", "kind": "transaction", "items": [{"data": {
            "chainId": 4663, "to": token, "data": approve_data, "value": "0",
        }}]})
    return {
        "steps": steps,
        "details": {
            "operation": "swap", "sender": TAKER, "recipient": recipient,
            "currencyIn": {"amount": str(amount), "currency": {
                "chainId": 4663, "address": token,
            }},
            "currencyOut": {"amount": "8000000", "minimumAmount": str(MIN_OUT),
                            "currency": {"chainId": 4663, "address": TOKEN}},
        },
    }


def _http(payload):
    def call(method, url, *, headers=None, body=None, timeout=None):
        assert method == "POST" and url == relay.RELAY_QUOTE_URL
        request = json.loads(body)
        assert request == {
            "user": TAKER, "recipient": TAKER, "refundTo": TAKER,
            "originChainId": 4663, "destinationChainId": 4663,
            "originCurrency": WETH, "destinationCurrency": TOKEN,
            "amount": str(AMOUNT), "tradeType": "EXACT_INPUT",
            "forceSolverExecution": False, "slippageTolerance": "300",
        }
        return 200, payload, {}
    return call


def _code_hash(address):
    return {
        relay.RELAY_APPROVAL_PROXY: relay.RELAY_APPROVAL_PROXY_CODE_HASH,
        relay.RELAY_ROUTER: relay.RELAY_ROUTER_CODE_HASH,
    }[address.lower()]


def test_relay_quote_returns_an_exact_atomic_executor_quote():
    quote = relay.dex_relay_quote(
        "4663", WETH, TOKEN, AMOUNT, TAKER,
        http=_http(_payload()), slippage_bps=300, code_hash_fn=_code_hash,
    )

    assert quote["sellAmount"] == str(AMOUNT)
    assert quote["buyAmount"] == "8000000"
    assert quote["minBuyAmount"] == str(MIN_OUT)
    assert quote["transaction"]["to"] == relay.RELAY_APPROVAL_PROXY
    assert quote["issues"]["allowance"]["spender"] == relay.RELAY_APPROVAL_PROXY
    assert quote["route"]["protocol"] == "relay_same_chain"


@pytest.mark.parametrize("amount,minimum", [
    (None, "1"), ("bad", "1"), ("0", "1"), ("10", "0"), ("10", "11"),
])
def test_relay_quote_rejects_invalid_output_bounds(amount, minimum):
    payload = _payload()
    payload["details"]["currencyOut"].update(amount=amount, minimumAmount=minimum)
    with pytest.raises(relay.RelayQuoteError, match="malformed_response"):
        relay.dex_relay_quote(
            "4663", WETH, TOKEN, AMOUNT, TAKER, http=_http(payload),
            slippage_bps=300, code_hash_fn=_code_hash,
        )


def test_relay_quote_accepts_a_swap_only_response_after_approval():
    quote = relay.dex_relay_quote(
        "4663", WETH, TOKEN, AMOUNT, TAKER,
        http=_http(_payload(include_approval=False)), slippage_bps=300,
        code_hash_fn=_code_hash,
    )

    assert quote["transaction"]["to"] == relay.RELAY_APPROVAL_PROXY
    assert quote["issues"]["allowance"]["spender"] == relay.RELAY_APPROVAL_PROXY


def test_relay_quote_rejects_a_response_for_another_recipient():
    bad = "0x0000000000000000000000000000000000000001"
    try:
        relay.dex_relay_quote(
            "4663", WETH, TOKEN, AMOUNT, TAKER,
            http=_http(_payload(recipient=bad)), slippage_bps=300,
            code_hash_fn=_code_hash,
        )
    except relay.RelayQuoteError as exc:
        assert exc.failure_type == "malformed_response"
    else:
        raise AssertionError("the recipient mismatch was accepted")


def test_relay_quote_rejects_a_different_outer_transfer_amount():
    try:
        relay.dex_relay_quote(
            "4663", WETH, TOKEN, AMOUNT, TAKER,
            http=_http(_payload(amount=AMOUNT - 1)), slippage_bps=300,
            code_hash_fn=_code_hash,
        )
    except relay.RelayQuoteError as exc:
        assert exc.failure_type == "malformed_response"
    else:
        raise AssertionError("the transfer amount mismatch was accepted")


def test_relay_quote_rejects_a_changed_contract_code_hash():
    try:
        relay.dex_relay_quote(
            "4663", WETH, TOKEN, AMOUNT, TAKER,
            http=_http(_payload()), slippage_bps=300,
            code_hash_fn=lambda _address: "0xdead",
        )
    except relay.RelayQuoteError as exc:
        assert exc.failure_type == "code_mismatch"
    else:
        raise AssertionError("the code mismatch was accepted")


def test_relay_contract_code_read_retries_a_transient_rpc_fault(monkeypatch):
    from sniper.execution import evm_wallet

    calls = {"count": 0, "http": None}
    read_http = object()

    def rpc(_url, _method, _params, *, http=None):
        calls["count"] += 1
        calls["http"] = http
        if calls["count"] == 1:
            raise RuntimeError("HTTP 429")
        return {"result": "0x60"}

    relay._live_code_hash.cache_clear()
    monkeypatch.setattr(evm_wallet, "_rpc", rpc)

    assert relay._live_code_hash("https://rpc", relay.RELAY_ROUTER, http=read_http) == \
        "0x" + relay.keccak(b"\x60").hex()
    assert calls == {"count": 2, "http": read_http}


def test_4663_executor_warms_relay_contract_hashes_at_build(monkeypatch):
    from sniper.execution.evm_real_seams import build_real_evm_executor
    from sniper.settings import memecoin as cfg

    seen = []
    read_http = object()
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT', 35.)
    monkeypatch.setattr(
        relay, "warm_relay_code_hashes",
        lambda rpc_url, *, http=None, output_guard=False: seen.append((rpc_url, http, output_guard)) or True,
        raising=False,
    )

    build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc", http=read_http,
        venue_quote_fn=lambda *_args, **_kwargs: None,
        usdc_fn=lambda: 100.0, token_fn=lambda _mint: 0.0,
        price_fn=lambda: 3000.0, submission_transport=lambda _raw: True,
    )

    assert len(seen) == 1
    assert seen[0][0] == "https://rpc"
    assert callable(seen[0][1])
    assert seen[0][2] is True


def test_4663_relay_warmup_uses_the_entry_quote_http_cache(monkeypatch):
    from sniper.execution.evm_real_seams import build_real_evm_executor
    from sniper.settings import memecoin as cfg

    warm_http, route_http = [], []
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENABLED", True)
    # Relay is exit-only by default since 2026-09-02; this exercises it on the BUY side,
    # which is real restorable behaviour behind this knob rather than a fiction.
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_RELAY_ENTRY_ENABLED", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V2_ENTRY_ENABLED", False)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_V4_ENABLED", False)
    monkeypatch.setattr(
        relay, "warm_relay_code_hashes",
        lambda _rpc_url, *, http=None, output_guard=False: warm_http.append(http),
    )
    monkeypatch.setattr(
        relay, "dex_relay_quote",
        lambda *_args, http=None, **_kwargs: route_http.append(http),
    )

    ex = build_real_evm_executor(
        acct=Account.create(), chain_id="4663", rpc_url="https://rpc", http=lambda *_a, **_k: None,
        venue_quote_fn=lambda *_args, **_kwargs: None,
        usdc_fn=lambda: 100.0, token_fn=lambda _mint: 0.0,
        price_fn=lambda: 3000.0, submission_transport=lambda _raw: True,
    )
    assert ex["entry_quote_probe_fn"](TOKEN, 10.0) == "no_route"
    assert len(warm_http) == len(route_http) == 1
    assert warm_http[0] is route_http[0]


def test_relay_quote_reports_swap_impact_as_route_capacity():
    """A Relay impact limit is a route limit, not a provider fault."""
    def http(*_args, **_kwargs):
        raise HTTPError(
            relay.RELAY_QUOTE_URL, 400, "Bad Request", {},
            io.BytesIO(json.dumps({
                "errorCode": "SWAP_IMPACT_TOO_HIGH",
                "message": "Swap impact is too high: -49.48%",
            }).encode()),
        )

    with pytest.raises(relay.RelayQuoteError) as exc:
        relay.dex_relay_quote(
            "4663", WETH, TOKEN, AMOUNT, TAKER,
            http=http, slippage_bps=300, code_hash_fn=_code_hash,
        )

    assert exc.value.failure_type == "route_capacity"

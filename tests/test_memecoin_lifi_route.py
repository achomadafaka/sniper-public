import copy
from dataclasses import replace

import pytest
from eth_abi import encode

from sniper.settings import memecoin as cfg

try:
    from sniper.execution import lifi_route as route
except ImportError:
    route = None


CUSTODY = "0x1111111111111111111111111111111111111111"
FEE_RECIPIENT = "0x2222222222222222222222222222222222222222"
WETH = "0x0bd7d308f8e1639fab988df18a8011f41eacad73"
TOKEN = "0x447096437c7476413e752df0b1db3e2c9169e341"
OUTER_TYPES = [
    "bytes32", "string", "string", "address", "uint256",
    "(address,address,address,address,uint256,bytes,bool)[]",
]


def _request(input_atomic=1_000_000, input_token=WETH, output_token=TOKEN):
    return {
        "allowBridges": [],
        "allowExchanges": ["nordstern"],
        "fee": "0",
        "fromAddress": CUSTODY,
        "fromAmount": str(input_atomic),
        "fromChain": "4663",
        "fromToken": input_token,
        "integrator": "",
        "maxPriceImpact": "0.3",
        "referrer": "",
        "simulation": True,
        "toAddress": CUSTODY,
        "toChain": "4663",
        "toToken": output_token,
    }


def _response(input_atomic=1_000_000, output_min=90_000, input_token=WETH,
              output_token=TOKEN):
    manifest, _digest = cfg.load_lifi_manifest()
    fee = input_atomic * 25 // 10_000
    net = input_atomic - fee
    fee_call = bytes.fromhex(manifest["steps"][0]["selector"][2:]) + encode(
        ["address", "(address,uint256)[]"], [input_token, [(FEE_RECIPIENT, fee)]],
    )
    executor = bytes.fromhex(manifest["steps"][1]["executor"][2:])
    swap_call = b"".join((
        bytes.fromhex(manifest["steps"][1]["selector"][2:]),
        b"\x00\x01",
        executor,
        net.to_bytes(16, "big"),
        output_min.to_bytes(16, "big"),
        b"\x01",
        executor,
        bytes.fromhex(manifest["top_level"]["target"][2:]),
        bytes.fromhex(input_token[2:]),
        bytes.fromhex(output_token[2:]),
        b"\xff",
    ))
    swaps = [
        (
            manifest["steps"][0]["target"], manifest["steps"][0]["target"],
            input_token, input_token, input_atomic, fee_call, True,
        ),
        (
            manifest["steps"][1]["target"], manifest["steps"][1]["target"],
            input_token, output_token, net, swap_call, False,
        ),
    ]
    outer = bytes.fromhex(manifest["top_level"]["selector"][2:]) + encode(
        OUTER_TYPES,
        [bytes.fromhex("01" * 32), "", "", CUSTODY, output_min, swaps],
    )
    return {
        "action": {
            "fromAddress": CUSTODY,
            "fromAmount": str(input_atomic),
            "fromChainId": 4663,
            "fromToken": {"address": input_token},
            "toAddress": CUSTODY,
            "toChainId": 4663,
            "toToken": {"address": output_token},
        },
        "estimate": {
            "feeCosts": [{
                "amount": str(fee),
                "name": "LI.FI Fixed Fee",
                "percentage": "0.0025",
                "token": {"address": input_token},
            }],
            "gasCosts": [{"type": "SEND", "limit": "818279"}],
            "toAmount": str(output_min + 1_000),
            "toAmountMin": str(output_min),
        },
        "includedSteps": [
            {
                "action": {
                    "fromAmount": str(input_atomic),
                    "fromToken": {"address": input_token},
                    "toToken": {"address": input_token},
                },
                "estimate": {"toAmount": str(net), "toAmountMin": str(net)},
                "tool": "feeCollection",
                "type": "protocol",
            },
            {
                "action": {
                    "fromAmount": str(net),
                    "fromToken": {"address": input_token},
                    "toToken": {"address": output_token},
                },
                "estimate": {
                    "toAmount": str(output_min + 1_000),
                    "toAmountMin": str(output_min),
                },
                "tool": "nordstern",
                "type": "swap",
            },
        ],
        "tool": "nordstern",
        "transactionRequest": {
            "chainId": 4663,
            "data": "0x" + outer.hex(),
            "from": CUSTODY,
            "gasLimit": "0xc7c67",
            "to": manifest["top_level"]["target"],
            "value": "0x0",
        },
        "type": "lifi",
    }


def _decoded(input_atomic, output_min_atomic):
    return route.DecodedQuote(
        calldata_sha256="0" * 64,
        decoded_semantics_sha256="0" * 64,
        fee_atomic=0,
        input_atomic=input_atomic,
        output_min_atomic=output_min_atomic,
        path=(WETH, TOKEN),
        request_sha256="0" * 64,
        response_sha256="0" * 64,
        spender=CUSTODY,
        swap_input_atomic=input_atomic,
        target=CUSTODY,
        tool="nordstern",
    )


def _replace_swap_field(response, swap_index, field_index, value):
    raw = bytes.fromhex(response["transactionRequest"]["data"][2:])
    outer = list(route.decode_exact(OUTER_TYPES, raw[4:]))
    swaps = list(outer[-1])
    swap = list(swaps[swap_index])
    swap[field_index] = value
    swaps[swap_index] = tuple(swap)
    outer[-1] = swaps
    response["transactionRequest"]["data"] = "0x5fd9ae2e" + encode(
        OUTER_TYPES, outer,
    ).hex()


def _replace_guard_field(response, start, end, value):
    raw = bytes.fromhex(response["transactionRequest"]["data"][2:])
    outer = list(route.decode_exact(OUTER_TYPES, raw[4:]))
    swaps = list(outer[-1])
    swap = list(swaps[1])
    call_data = bytearray(swap[5])
    call_data[start:end] = value
    swap[5] = bytes(call_data)
    swaps[1] = tuple(swap)
    outer[-1] = swaps
    response["transactionRequest"]["data"] = "0x5fd9ae2e" + encode(
        OUTER_TYPES, outer,
    ).hex()


@pytest.mark.parametrize(
    ("input_atomic", "expected"),
    [
        (1, 0),
        (399, 0),
        (400, 1),
        (1_000_000_000_000_000, 2_500_000_000_000),
        (6_528_997_907_204_926_094_418, 16_322_494_768_012_315_236),
        (525_403_522_853_228_280_622_757, 1_313_508_807_133_070_701_556),
    ],
)
def test_fee_uses_each_exact_input(input_atomic, expected):
    assert route.fee_atomic(input_atomic) == expected


def test_decoder_accepts_audited_nordstern_guard():
    request = _request()
    response = _response()
    manifest, _digest = cfg.load_lifi_manifest()

    decoded = route.decode_quote(response, request, manifest)

    assert decoded.input_atomic == 1_000_000
    assert decoded.swap_input_atomic == 997_500
    assert decoded.output_min_atomic == 90_000
    assert decoded.output_expected_atomic == 91_000
    assert decoded.transaction["gasLimit"] == 818_279


def test_decoder_rejects_a_gas_limit_mismatch():
    response = _response()
    response["estimate"]["gasCosts"][0]["limit"] = "818280"
    manifest, _digest = cfg.load_lifi_manifest()

    with pytest.raises(route.LifiQuoteError):
        route.decode_quote(response, _request(), manifest)


def test_decoder_accepts_audited_sell_amounts():
    input_atomic = 6_528_997_907_204_926_094_418
    output_min = 844_566_365_458_452
    request = _request(input_atomic, TOKEN, WETH)
    response = _response(input_atomic, output_min, TOKEN, WETH)
    manifest, _digest = cfg.load_lifi_manifest()

    decoded = route.decode_quote(response, request, manifest)

    assert decoded.fee_atomic == 16_322_494_768_012_315_236
    assert decoded.swap_input_atomic == 6_512_675_412_436_913_779_182
    assert decoded.output_min_atomic == output_min
    assert decoded.path == (TOKEN, TOKEN, WETH)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda response: response.update(tool="fly"),
        lambda response: response["includedSteps"].reverse(),
        lambda response: response["includedSteps"].append(copy.deepcopy(response["includedSteps"][1])),
        lambda response: response["includedSteps"][0]["estimate"].update(toAmount="997499"),
        lambda response: response["includedSteps"][0]["action"]["toToken"].update(address=TOKEN),
        lambda response: response["transactionRequest"].update(to=CUSTODY),
        lambda response: response["transactionRequest"].update(value="0x1"),
        lambda response: response["transactionRequest"].update(chainId=8453),
        lambda response: response["estimate"].update(toAmountMin="89999"),
        lambda response: response["estimate"]["feeCosts"][0].update(percentage="0.003"),
        lambda response: response.update(typedData={"unsafe": True}),
    ],
)
def test_decoder_rejects_wrong_response_semantics(mutate):
    response = _response()
    mutate(response)
    manifest, _digest = cfg.load_lifi_manifest()

    with pytest.raises(route.LifiQuoteError):
        route.decode_quote(response, _request(), manifest)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda request: request.update(fromChain="8453"),
        lambda request: request.update(allowExchanges=[]),
        lambda request: request.update(allowExchanges=["all"]),
        lambda request: request.update(allowBridges=["across"]),
        lambda request: request.update(fee="1"),
        lambda request: request.update(simulation=False),
        lambda request: request.update(toAddress=FEE_RECIPIENT),
    ],
)
def test_decoder_rejects_wrong_request_authority(mutate):
    request = _request()
    mutate(request)
    manifest, _digest = cfg.load_lifi_manifest()

    with pytest.raises(route.LifiQuoteError):
        route.decode_quote(_response(), request, manifest)


def test_decoder_rejects_trailing_calldata():
    response = _response()
    response["transactionRequest"]["data"] += "00"
    manifest, _digest = cfg.load_lifi_manifest()

    with pytest.raises(route.LifiQuoteError):
        route.decode_quote(response, _request(), manifest)


def test_decoder_rejects_unapproved_nested_selector():
    response = _response()
    _replace_swap_field(response, 1, 5, bytes.fromhex("0193b9fc"))
    manifest, _digest = cfg.load_lifi_manifest()

    with pytest.raises(route.LifiQuoteError):
        route.decode_quote(response, _request(), manifest)


def test_decoder_accepts_provider_route_data_after_the_nordstern_guard():
    response = _response()
    _replace_guard_field(response, 140, 140, bytes(range(183)))
    manifest, _digest = cfg.load_lifi_manifest()

    decoded = route.decode_quote(response, _request(), manifest)

    assert decoded.input_atomic == 1_000_000
    assert decoded.output_min_atomic == 90_000


def test_decoder_binds_nordstern_minimum_to_outer_minimum():
    response = _response(output_min=90_000)
    _replace_guard_field(response, 42, 58, (89_999).to_bytes(16, "big"))
    manifest, _digest = cfg.load_lifi_manifest()

    with pytest.raises(route.LifiQuoteError):
        route.decode_quote(response, _request(), manifest)


def test_decoder_accepts_provider_rounding_in_a_large_nordstern_minimum():
    outer_min = 50_886_415_966_357_185_498_686
    nested_min = 50_886_415_966_357_184_100_864
    response = _response(output_min=outer_min)
    _replace_guard_field(response, 42, 58, nested_min.to_bytes(16, "big"))
    manifest, _digest = cfg.load_lifi_manifest()

    decoded = route.decode_quote(response, _request(), manifest)

    assert decoded.output_min_atomic == outer_min


def test_decoder_accepts_a_nordstern_minimum_one_wei_stricter_than_the_quote():
    """2026-09-11 ~21:30Z: LI.FI began signing a nested minimum 1 wei ABOVE its own toAmountMin
    (616829105856365015459849 vs ...848). A stricter nested minimum only demands more output, so
    it is safe; refusing it read every 4663 LI.FI quote as malformed and refused the deploy."""
    outer_min = 616_829_105_856_365_015_459_848
    response = _response(output_min=outer_min)
    _replace_guard_field(response, 42, 58, (outer_min + 1).to_bytes(16, "big"))
    manifest, _digest = cfg.load_lifi_manifest()

    decoded = route.decode_quote(response, _request(), manifest)

    assert decoded.output_min_atomic == outer_min


def test_decoder_rejects_a_nordstern_minimum_far_above_the_quote():
    outer_min = 616_829_105_856_365_015_459_848
    response = _response(output_min=outer_min)
    _replace_guard_field(response, 42, 58, (outer_min * 2).to_bytes(16, "big"))
    manifest, _digest = cfg.load_lifi_manifest()

    with pytest.raises(route.LifiQuoteError):
        route.decode_quote(response, _request(), manifest)


@pytest.mark.parametrize(
    ("start", "end", "value"),
    [
        (6, 26, bytes.fromhex(CUSTODY[2:])),
        (26, 42, (997_499).to_bytes(16, "big")),
        (42, 58, bytes(16)),
        (59, 79, bytes.fromhex(CUSTODY[2:])),
        (79, 99, bytes.fromhex(CUSTODY[2:])),
        (99, 119, bytes.fromhex(TOKEN[2:])),
        (119, 139, bytes.fromhex(WETH[2:])),
        (139, 140, b""),
    ],
)
def test_decoder_rejects_changed_nordstern_custody_fields(start, end, value):
    response = _response()
    _replace_guard_field(response, start, end, value)
    manifest, _digest = cfg.load_lifi_manifest()

    with pytest.raises(route.LifiQuoteError):
        route.decode_quote(response, _request(), manifest)


@pytest.mark.parametrize(
    ("swap_index", "field_index", "value"),
    [
        (0, 0, CUSTODY),
        (0, 1, CUSTODY),
        (1, 0, CUSTODY),
        (1, 1, CUSTODY),
        (1, 2, TOKEN),
        (1, 3, WETH),
    ],
)
def test_decoder_rejects_wrong_nested_authority(swap_index, field_index, value):
    response = _response()
    _replace_swap_field(response, swap_index, field_index, value)
    manifest, _digest = cfg.load_lifi_manifest()

    with pytest.raises(route.LifiQuoteError):
        route.decode_quote(response, _request(), manifest)


def test_roundtrip_gate_uses_exact_atomic_boundary():
    buy = _decoded(input_atomic=1_000_000, output_min_atomic=997_500)
    sell_at_cap = _decoded(input_atomic=997_500, output_min_atomic=995_000)
    sell_over_cap = replace(sell_at_cap, output_min_atomic=994_999)

    assert route.economic_roundtrip(1_000_000, buy, sell_at_cap, 50).allowed
    refused = route.economic_roundtrip(1_000_000, buy, sell_over_cap, 50)
    assert not refused.allowed
    assert refused.code == "roundtrip_cap"


def test_roundtrip_gate_rejects_broken_final_sell_amount():
    buy = _decoded(input_atomic=1_000_000, output_min_atomic=997_500)
    sell = _decoded(input_atomic=997_499, output_min_atomic=995_000)

    result = route.economic_roundtrip(1_000_000, buy, sell, 50)

    assert not result.allowed
    assert result.code == "atomic_continuity"

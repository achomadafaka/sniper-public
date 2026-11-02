from __future__ import annotations

from eth_abi import encode

from sniper.research.memecoin_opportunity.pons_4663 import (
    PONS_FACTORY,
    PONS_WETH,
    TRACKED_POOL_LIMIT,
    TOKEN_LAUNCHED,
    V3_SWAP,
    decode_pons_4663_log,
    is_known_irrelevant_pons_swap,
    remember_pool,
)
from sniper.research.memecoin_opportunity.types import EventKind, LifecycleState, VenueId
from sniper.research.memecoin_opportunity.state import launch_state
from sniper.services.memecoin_opportunity_live import make_pons_4663_quote_provider


TOKEN = "0x1111111111111111111111111111111111111111"
DEPLOYER = "0x2222222222222222222222222222222222222222"
DEX_FACTORY = "0x3333333333333333333333333333333333333333"
PAIR = "0x4444444444444444444444444444444444444444"
POOL = "0x5555555555555555555555555555555555555555"
SENDER = "0x6666666666666666666666666666666666666666"
RECIPIENT = "0x7777777777777777777777777777777777777777"


def _topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


def _launch_log(*, address: str = PONS_FACTORY, pair: str = PONS_WETH):
    return {
        "address": address,
        "topics": [TOKEN_LAUNCHED, _topic(TOKEN), _topic(DEPLOYER), _topic(DEX_FACTORY)],
        "data": "0x" + encode(
            ["address", "address", "uint256", "uint256", "uint256", "uint256", "uint256"],
            [pair, POOL, 1, 9, 44, 12345, 10**18],
        ).hex(),
    }


def _swap_log(*, address: str = POOL, amount0: int = -500, amount1: int = 200):
    return {
        "address": address,
        "topics": [V3_SWAP, _topic(SENDER), _topic(RECIPIENT)],
        "data": "0x" + encode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            [amount0, amount1, 2**96, 1_000_000, -25],
        ).hex(),
    }


def test_decodes_factory_launch_as_active_pool_without_curve_facts():
    decoded = decode_pons_4663_log(_launch_log(), pool_tokens={})

    assert decoded.blocker is None
    assert decoded.event_kind is EventKind.LAUNCH
    assert decoded.lifecycle is LifecycleState.ACTIVE_POOL
    assert decoded.token_id == TOKEN
    assert decoded.creator_id == DEPLOYER
    assert decoded.pool_id == POOL
    assert decoded.curve_id is None
    assert decoded.virtual_token_reserves is None
    assert decoded.facts == {
        "pair_token": PONS_WETH,
        "quote_mint": PONS_WETH,
        "dex_factory": DEX_FACTORY,
        "dex_id": 1,
        "launch_config_id": 9,
        "position_id": 44,
        "restrictions_end_block": 12345,
        "initial_buy_amount": 10**18,
        "token_is_token0": False,
    }
    state = launch_state(
        "launch", VenueId.PONS_V1_ROBINHOOD_4663,
        decoded,
        "2026-07-29T12:00:00Z",
    )
    assert state.eligible is True
    assert state.blocker_codes == ()
    assert state.pool_id == POOL
    assert state.quote_mint == PONS_WETH
    assert state.v3_token_is_token0 is False


def test_pons_state_rejects_an_unapproved_pair_token():
    decoded = decode_pons_4663_log(_launch_log(pair=PAIR), pool_tokens={})

    state = launch_state(
        "launch", VenueId.PONS_V1_ROBINHOOD_4663,
        decoded,
        "2026-07-29T12:00:00Z",
    )

    assert state.eligible is False
    assert state.blocker_codes == ("UNSUPPORTED_QUOTE_ASSET",)


def test_decodes_only_registered_pool_swaps_with_signed_direction():
    pools = {POOL: (TOKEN, PAIR)}

    buy = decode_pons_4663_log(_swap_log(), pool_tokens=pools)
    sell = decode_pons_4663_log(
        _swap_log(amount0=700, amount1=-250), pool_tokens=pools
    )

    assert buy.event_kind is EventKind.BUY
    assert buy.token_id == TOKEN
    assert buy.actor_id == RECIPIENT
    assert buy.recipient_id == RECIPIENT
    assert (buy.token_delta_atomic, buy.quote_delta_atomic) == (-500, 200)
    assert buy.facts["sqrt_price_x96"] == 2**96
    assert buy.facts["liquidity"] == 1_000_000
    assert buy.facts["tick"] == -25
    assert buy.facts["quote_mint"] == PAIR
    assert buy.facts["swap_sender"] == SENDER
    assert sell.event_kind is EventKind.SELL
    assert (sell.token_delta_atomic, sell.quote_delta_atomic) == (700, -250)


def test_rejects_legacy_factory_and_unregistered_swap_pool():
    legacy = decode_pons_4663_log(
        _launch_log(address="0x0000000000000000000000000000000000000001"),
        pool_tokens={},
    )
    unknown_pool = decode_pons_4663_log(_swap_log(address=PAIR), pool_tokens={})

    assert legacy.blocker.value == "UNKNOWN_PROGRAM_OR_CONTRACT_VERSION"
    assert unknown_pool.blocker.value == "UNKNOWN_PROGRAM_OR_CONTRACT_VERSION"


def test_classifies_only_valid_zero_flow_swap_as_known_irrelevant():
    assert is_known_irrelevant_pons_swap(_swap_log(amount0=0, amount1=1)) is True
    assert is_known_irrelevant_pons_swap(_swap_log(amount0=-500, amount1=200)) is False
    assert is_known_irrelevant_pons_swap({
        **_swap_log(amount0=0, amount1=1),
        "data": "0x01",
    }) is False
    assert is_known_irrelevant_pons_swap({
        **_swap_log(amount0=0, amount1=1),
        "topics": [TOKEN_LAUNCHED],
    }) is False
    assert is_known_irrelevant_pons_swap({
        **_swap_log(amount0=0, amount1=1),
        "topics": [V3_SWAP, "0x01", _topic(RECIPIENT)],
    }) is False
    assert is_known_irrelevant_pons_swap({
        **_swap_log(amount0=0, amount1=1),
        "topics": [V3_SWAP, "0x" + "g" * 64, _topic(RECIPIENT)],
    }) is False
    assert is_known_irrelevant_pons_swap({
        **_swap_log(amount0=0, amount1=1),
        "topics": [V3_SWAP, "0x01" + "00" * 31, _topic(RECIPIENT)],
    }) is False


def test_registered_pool_filter_is_bounded_to_newest_pools():
    pools = {}
    for index in range(TRACKED_POOL_LIMIT + 2):
        remember_pool(
            pools,
            f"0x{index:040x}",
            TOKEN,
            PAIR,
        )

    assert len(pools) == TRACKED_POOL_LIMIT
    assert f"0x{0:040x}" not in pools
    assert f"0x{1:040x}" not in pools
    assert f"0x{TRACKED_POOL_LIMIT + 1:040x}" in pools


def test_pons_quoter_binds_pool_fee_block_and_exact_acquired_quantity():
    calls = []
    fee = 3_000
    buy_out = 8_000_000
    sell_out = 24_000_000_000_000_000
    quoter = "0x8888888888888888888888888888888888888888"
    factory = "0x9999999999999999999999999999999999999999"

    def rpc(method, params):
        calls.append((method, params))
        if method == "eth_blockNumber":
            return "0x123"
        target = params[0]["to"].lower()
        data = params[0]["data"]
        if target == POOL:
            return "0x" + encode(["uint24"], [fee]).hex()
        if target == quoter and data.startswith("0xc45a0155"):
            return "0x" + encode(["address"], [factory]).hex()
        if target == factory:
            return "0x" + encode(["address"], [POOL]).hex()
        assert target == quoter
        amount_in = int(data[2 + 8 + 64 * 2:2 + 8 + 64 * 3], 16)
        amount_out = buy_out if amount_in == 25_000_000_000_000_000 else sell_out
        return "0x" + encode(
            ["uint256", "uint160", "uint32", "uint256"],
            [amount_out, 2**96, 1, 100_000],
        ).hex()

    decoded = decode_pons_4663_log(_launch_log(), pool_tokens={})
    state = launch_state(
        "launch", VenueId.PONS_V1_ROBINHOOD_4663,
        decoded,
        "2026-07-29T12:00:00Z",
    )
    provider = make_pons_4663_quote_provider(
        rpc,
        quoter=quoter,
    )

    result = provider.round_trip(
        state,
        type("Quote", (), {
            "quote_in_atomic": 25_000_000_000_000_000,
            "state_fingerprint": "state-1",
        })(),
    )

    assert result.entry_quote_ok is True
    assert result.acquired_quantity_sell_ok is True
    assert result.tokens_out_atomic == buy_out
    assert result.immediate_exit_quote_atomic == sell_out
    assert result.block_identity == "0x123"
    assert result.provider == "pons_v3_quoter_v1"
    assert result.route_binding == {
        "venue_id": "pons_v1_4663",
        "pool_id": POOL,
        "factory_id": factory,
        "fee": fee,
        "quote_mint": PONS_WETH,
        "token_id": TOKEN,
        "token_is_token0": False,
        "block_identity": "0x123",
        "state_fingerprint": "state-1",
    }
    quoter_calls = [
        item for item in calls
        if item[1]
        and item[1][0].get("to") == quoter
        and not item[1][0]["data"].startswith("0xc45a0155")
    ]
    assert len(quoter_calls) == 2


def test_pons_quoter_fails_closed_on_pool_or_quote_identity():
    provider = make_pons_4663_quote_provider(
        lambda *_args: (_ for _ in ()).throw(AssertionError("RPC must not run")),
        quoter="0x8888888888888888888888888888888888888888",
    )
    decoded = decode_pons_4663_log(_launch_log(), pool_tokens={})
    state = launch_state(
        "launch", VenueId.PONS_V1_ROBINHOOD_4663,
        decoded,
        "2026-07-29T12:00:00Z",
    )

    result = provider.round_trip(
        state.__class__(**{**state.__dict__, "pool_id": None}),
        type("Quote", (), {"quote_in_atomic": 1, "state_fingerprint": "s"})(),
    )

    assert result.entry_quote_ok is False
    assert result.blocker_code == "PONS_POOL_IDENTITY_INVALID"


def test_pons_entry_and_exit_quotes_keep_the_registered_pool_route():
    fee = 500
    token_out = 7_000_000
    quote_out = 27_000_000_000_000_000
    quoter = "0x8888888888888888888888888888888888888888"
    factory = "0x9999999999999999999999999999999999999999"

    def rpc(method, params):
        if method == "eth_blockNumber":
            return "0x456"
        target = params[0]["to"].lower()
        data = params[0]["data"]
        if target == POOL:
            return "0x" + encode(["uint24"], [fee]).hex()
        if target == TOKEN and data.startswith("0x313ce567"):
            return "0x" + encode(["uint8"], [18]).hex()
        if target == quoter and data.startswith("0xc45a0155"):
            return "0x" + encode(["address"], [factory]).hex()
        if target == factory:
            return "0x" + encode(["address"], [POOL]).hex()
        amount_in = int(data[2 + 8 + 64 * 2:2 + 8 + 64 * 3], 16)
        amount_out = token_out if amount_in != token_out else quote_out
        return "0x" + encode(
            ["uint256", "uint160", "uint32", "uint256"],
            [amount_out, 2**96, 1, 100_000],
        ).hex()

    decoded = decode_pons_4663_log(_launch_log(), pool_tokens={})
    state = launch_state(
        "launch", VenueId.PONS_V1_ROBINHOOD_4663,
        decoded,
        "2026-07-29T12:00:00Z",
    )
    provider = make_pons_4663_quote_provider(
        rpc,
        quoter=quoter,
    )

    entry = provider.entry(state, 25_000_000_000_000_000, "state-2")
    exit_quote = provider.exit(
        token_id=TOKEN,
        quantity_atomic=token_out,
        token_decimals=18,
        quote_decimals=18,
        route_binding=entry["route_binding"],
    )

    assert entry["expected_out_atomic"] == token_out
    assert entry["quote_in_asset"] == PONS_WETH
    assert entry["quote_out_decimals"] == 18
    assert entry["route_binding"]["pool_id"] == POOL
    assert exit_quote["expected_out_atomic"] == quote_out
    assert exit_quote["quote_out_asset"] == PONS_WETH
    assert exit_quote["route_binding"]["pool_id"] == POOL


def test_pons_quoter_rejects_a_pool_outside_its_factory():
    quoter = "0x8888888888888888888888888888888888888888"
    factory = "0x9999999999999999999999999999999999999999"

    def rpc(method, params):
        if method == "eth_blockNumber":
            return "0x456"
        target = params[0]["to"].lower()
        data = params[0]["data"]
        if target == POOL:
            return "0x" + encode(["uint24"], [500]).hex()
        if target == quoter and data.startswith("0xc45a0155"):
            return "0x" + encode(["address"], [factory]).hex()
        if target == factory:
            return "0x" + encode(
                ["address"], ["0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"]
            ).hex()
        raise AssertionError("quote call must not run")

    state = launch_state(
        "launch",
        VenueId.PONS_V1_ROBINHOOD_4663,
        decode_pons_4663_log(_launch_log(), pool_tokens={}),
        "2026-07-29T12:00:00Z",
    )
    result = make_pons_4663_quote_provider(rpc, quoter=quoter).round_trip(
        state,
        type("Quote", (), {"quote_in_atomic": 1, "state_fingerprint": "s"})(),
    )

    assert result.entry_quote_ok is False
    assert result.blocker_code == "PONS_POOL_NOT_CANONICAL"

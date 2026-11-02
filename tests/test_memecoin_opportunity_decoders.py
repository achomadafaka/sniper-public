from __future__ import annotations

import base64
import struct

from eth_abi import encode
from eth_utils import keccak

import sniper.research.memecoin_opportunity.bags_solana as bags_solana
from sniper.research.memecoin_opportunity.bags_4663 import (
    BAGS_FACTORY,
    decode_bags_4663_log,
    enrich_bags_4663_state,
    is_untracked_bags_4663_curve_log,
)
from sniper.research.memecoin_opportunity.bags_solana import (
    BAGS_DBC_PROGRAM,
    decode_bags_solana_event,
    is_known_irrelevant_bags_solana_event,
)
from sniper.research.memecoin_opportunity.decoders import DecodeBlocker, DecodedVenueEvent
from sniper.research.memecoin_opportunity.pons_4663 import PONS_WETH
from sniper.research.memecoin_opportunity.pumpfun import (
    PUMPFUN_PROGRAM,
    decode_pumpfun_event,
    is_known_irrelevant_pumpfun_event,
)
from sniper.research.memecoin_opportunity.types import EventKind, LifecycleState


_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, rem = divmod(n, 58)
        out = _ALPHABET[rem] + out
    return "1" * (len(raw) - len(raw.lstrip(b"\0"))) + (out or "")


def _pub(seed: int) -> tuple[bytes, str]:
    raw = bytes([seed]) * 32
    return raw, _b58(raw)


def _s(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<I", len(raw)) + raw


def _anchor_log(discriminator: bytes, payload: bytes) -> str:
    return "Program data: " + base64.b64encode(discriminator + payload).decode()


def test_pumpfun_decodes_create_trade_and_completion_events():
    mint_raw, mint = _pub(1)
    curve_raw, curve = _pub(2)
    user_raw, user = _pub(3)
    creator_raw, creator = _pub(4)
    token_program_raw, _ = _pub(5)
    quote_mint_raw, _ = _pub(6)
    create_payload = b"".join(
        (
            _s("Alpha"),
            _s("ALP"),
            _s("https://example.invalid/a"),
            mint_raw,
            curve_raw,
            user_raw,
            creator_raw,
            struct.pack("<qQQQQ", 1_700_000_000, 1_000_000, 500_000, 800_000, 1_000_000),
            token_program_raw,
            b"\0\0",
            quote_mint_raw,
            struct.pack("<Q", 500_000),
        )
    )
    created = decode_pumpfun_event(
        PUMPFUN_PROGRAM,
        _anchor_log(bytes([27, 114, 169, 77, 222, 235, 99, 118]), create_payload),
    )
    assert created.blocker is None
    assert created.event_kind is EventKind.LAUNCH
    assert (created.token_id, created.curve_id, created.actor_id, created.creator_id) == (
        mint,
        curve,
        user,
        creator,
    )
    assert created.virtual_token_reserves == 1_000_000
    assert created.virtual_quote_reserves == 500_000

    fee_recipient_raw, _ = _pub(7)
    trade_payload = b"".join(
        (
            mint_raw,
            struct.pack("<QQ?", 25_000, 100_000, True),
            user_raw,
            struct.pack("<qQQQQ", 1_700_000_001, 525_000, 900_000, 25_000, 700_000),
            fee_recipient_raw,
            struct.pack("<QQ", 100, 250),
            creator_raw,
            struct.pack("<QQ?QQQq", 50, 125, True, 0, 0, 25_000, 1_700_000_001),
            _s("buy"),
            b"\0",
            struct.pack("<QQQQ", 0, 0, 0, 0),
            struct.pack("<I", 0),
        )
    )
    bought = decode_pumpfun_event(
        PUMPFUN_PROGRAM,
        _anchor_log(bytes([189, 219, 127, 211, 78, 230, 97, 238]), trade_payload),
    )
    assert bought.event_kind is EventKind.BUY
    assert bought.quote_delta_atomic == 25_000
    assert bought.token_delta_atomic == -100_000
    assert bought.actor_id == user

    complete_payload = user_raw + mint_raw + curve_raw + struct.pack("<q", 1_700_000_002) + quote_mint_raw
    complete = decode_pumpfun_event(
        PUMPFUN_PROGRAM,
        _anchor_log(bytes([95, 114, 97, 156, 212, 46, 152, 8]), complete_payload),
    )
    assert complete.event_kind is EventKind.MIGRATION
    assert complete.lifecycle is LifecycleState.MIGRATED


def test_pumpfun_rejects_foreign_program_and_unknown_discriminator():
    assert decode_pumpfun_event("foreign", "Program data: AA==").blocker is DecodeBlocker.UNKNOWN_VERSION
    unknown = _anchor_log(b"12345678", b"")
    assert decode_pumpfun_event(PUMPFUN_PROGRAM, unknown).blocker is DecodeBlocker.UNKNOWN_VERSION


def test_pumpfun_recognizes_official_non_trading_event_as_irrelevant():
    close_accumulator = _anchor_log(
        bytes([146, 159, 189, 172, 146, 88, 56, 244]),
        b"",
    )
    assert is_known_irrelevant_pumpfun_event(close_accumulator) is True
    assert is_known_irrelevant_pumpfun_event(_anchor_log(b"12345678", b"")) is False


def test_bags_solana_decodes_initialize_swap_and_curve_complete():
    pool_raw, pool = _pub(10)
    config_raw, _ = _pub(11)
    creator_raw, creator = _pub(12)
    mint_raw, mint = _pub(13)
    initialized = decode_bags_solana_event(
        BAGS_DBC_PROGRAM,
        _anchor_log(
            bytes([228, 50, 246, 85, 203, 66, 134, 37]),
            pool_raw + config_raw + creator_raw + mint_raw + struct.pack("<BQ", 0, 1_700_000_000),
        ),
    )
    assert initialized.event_kind is EventKind.LAUNCH
    assert (initialized.token_id, initialized.curve_id, initialized.creator_id) == (mint, pool, creator)

    swap_payload = b"".join(
        (
            pool_raw,
            config_raw,
            b"\x01\x00",
            struct.pack("<QQ", 25_000, 90_000),
            struct.pack("<QQ", 25_000, 100_000),
            (123456789).to_bytes(16, "little"),
            struct.pack("<QQQ", 200, 20, 0),
            struct.pack("<QQ", 25_000, 1_700_000_001),
        )
    )
    swap = decode_bags_solana_event(
        BAGS_DBC_PROGRAM,
        _anchor_log(bytes([27, 60, 21, 213, 138, 170, 187, 147]), swap_payload),
        token_by_pool={pool: mint},
    )
    assert swap.event_kind is EventKind.BUY
    assert swap.quote_delta_atomic == 25_000
    assert swap.token_delta_atomic == -100_000
    assert swap.fee_atomic == 220

    completed = decode_bags_solana_event(
        BAGS_DBC_PROGRAM,
        _anchor_log(
            bytes([229, 231, 86, 84, 156, 134, 75, 24]),
            pool_raw + config_raw + struct.pack("<QQ", 800_000, 200_000),
        ),
        token_by_pool={pool: mint},
    )
    assert completed.event_kind is EventKind.MIGRATION
    assert completed.lifecycle is LifecycleState.MIGRATED


def test_bags_solana_recognizes_official_protocol_fee_claim_as_irrelevant():
    claim_protocol_fee_v2 = _anchor_log(
        bytes.fromhex("bb854209cda1540d"),
        b"",
    )

    classify = getattr(
        bags_solana,
        "is_known_irrelevant_bags_solana_event",
        lambda _log_line: False,
    )
    assert classify(claim_protocol_fee_v2) is True
    assert classify(_anchor_log(b"12345678", b"")) is False


def test_bags_solana_ignores_duplicate_evt_swap2_companion():
    # DBC emits EvtSwap and EvtSwap2 for the same ordinary swap. EvtSwap is
    # canonical here; accepting EvtSwap2 would double-count one trade.
    evt_swap2 = _anchor_log(bytes.fromhex("bd4233a826507599"), b"")

    assert is_known_irrelevant_bags_solana_event(evt_swap2) is True


def _topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


def _address_topic(address: str) -> str:
    return "0x" + address[2:].lower().rjust(64, "0")


def test_bags_4663_decodes_factory_and_curve_events():
    token = "0x1111111111111111111111111111111111111111"
    curve = "0x2222222222222222222222222222222222222222"
    creator = "0x3333333333333333333333333333333333333333"
    fee_share = "0x4444444444444444444444444444444444444444"
    partner = "0x5555555555555555555555555555555555555555"
    pool_id = bytes.fromhex("aa" * 32)
    created_log = {
        "address": BAGS_FACTORY,
        "topics": [
            _topic("TokenCreated(address,address,address,address,address,bytes32,string,string,string)"),
            _address_topic(token),
            _address_topic(curve),
            _address_topic(creator),
        ],
        "data": "0x" + encode(
            ["address", "address", "bytes32", "string", "string", "string"],
            [fee_share, partner, pool_id, "Alpha", "ALP", "ipfs://alpha"],
        ).hex(),
    }
    created = decode_bags_4663_log(created_log)
    assert created.event_kind is EventKind.LAUNCH
    assert (created.token_id, created.curve_id, created.creator_id) == (token, curve, creator)
    assert created.fee_share_id == fee_share
    assert created.pool_id == "0x" + pool_id.hex()
    assert created.facts["quote_mint"] == PONS_WETH

    bought_log = {
        "address": curve.upper(),
        "topics": [
            _topic("TokensBought(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256)"),
            _address_topic(creator),
            _address_topic(creator),
        ],
        "data": "0x" + encode(
            ["uint256"] * 10,
            [25_000, 24_500, 100_000, 500, 250, 250, 0, 5, 900_000, 525_000],
        ).hex(),
    }
    bought = decode_bags_4663_log(bought_log, token_by_contract={curve: token})
    assert bought.event_kind is EventKind.BUY
    assert bought.quote_delta_atomic == 25_000
    assert bought.token_delta_atomic == -100_000
    assert bought.virtual_token_reserves == 900_000
    assert bought.virtual_quote_reserves == 525_000
    assert bought.facts["quote_mint"] == PONS_WETH


def test_bags_4663_unknown_contract_or_topic_blocks_without_guessing():
    result = decode_bags_4663_log({"address": "0x" + "99" * 20, "topics": ["0x" + "00" * 32], "data": "0x"})
    assert result.blocker is DecodeBlocker.UNKNOWN_VERSION


def test_bags_4663_midstream_curve_log_is_ignored_until_factory_identity_arrives():
    curve = "0x2222222222222222222222222222222222222222"
    log = {"address": curve, "topics": [_topic(
        "TokensBought(address,address,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256,uint256)"
    )]}
    assert is_untracked_bags_4663_curve_log(log, {}) is True
    assert is_untracked_bags_4663_curve_log(log, {curve: "0x" + "11" * 20}) is False


def test_bags_4663_token_created_topic_requires_the_known_factory():
    topic = _topic(
        "TokenCreated(address,address,address,address,address,bytes32,string,string,string)"
    )
    foreign = {"address": "0x" + "99" * 20, "topics": [topic]}
    factory = {"address": BAGS_FACTORY, "topics": [topic]}

    assert is_untracked_bags_4663_curve_log(foreign, {}) is True
    assert is_untracked_bags_4663_curve_log(factory, {}) is False


def test_bags_4663_lens_enrichment_adds_exact_curve_state():
    created = DecodedVenueEvent(
        event_kind=EventKind.LAUNCH,
        lifecycle=LifecycleState.PRE_MIGRATION,
        token_id="0x" + "11" * 20,
        curve_id="0x" + "22" * 20,
        creator_id="0x" + "33" * 20,
        decoder_version="fixture-v1",
    )
    enriched = enrich_bags_4663_state(created, {
        "exists": True,
        "migrated": False,
        "curve": created.curve_id,
        "fee_share": "0x" + "44" * 20,
        "pool_id": "0x" + "aa" * 32,
        "threshold_quote": 10_000,
        "real_quote_reserves": 3_700,
        "real_token_reserves": 630_000,
        "virtual_token_reserves": 900_000,
        "virtual_quote_reserves": 550_000,
        "price_quote_per_token": 611,
        "progress_bps": 3_700,
        "total_raised": 3_700,
    })

    assert enriched.virtual_token_reserves == 900_000
    assert enriched.virtual_quote_reserves == 550_000
    assert enriched.real_token_reserves == 630_000
    assert enriched.real_quote_reserves == 3_700
    assert enriched.progress_bps == 3_700

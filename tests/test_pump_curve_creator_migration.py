"""A creator migration must not invalidate a proven non-cashback launch copy."""
import base64
import json
import struct
from dataclasses import replace
from pathlib import Path

import pytest

import test_pump_curve_buy as T
import test_slot_lane_slippage_guard as S
from sniper.execution import pump_curve_buy as C
from sniper.services import memecoin_slot_slippage as G

V3 = bytes.fromhex("e1f7501ed5b38488")  # Official Pump IDL, buy_exact_quote_in_v3.


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("cashback", [False, True, None, 0])
def test_only_proven_non_cashback_launches_remove_the_creator_dependency(cashback, native):
    builder = T._builder(T._Clock())
    message, detail = builder.build(T._buy(curve_cashback_enabled=cashback), 5.0, T.SNAP,
                                    **({"funding_lamports": 28_000_000} if native else {}))
    assert message is not None, detail
    ix = next(ix for ix in message.instructions
              if str(message.account_keys[ix.program_id_index]) == C.PUMP_PROGRAM)
    data = bytes(ix.data)
    expected = V3 if cashback is False else C.BUY_EXACT_SOL_IN_DISCRIMINATOR
    assert data[:8] == expected
    assert detail["entry_slippage"]["instruction_discriminator"] == expected.hex()
    assert data[24:] == b"\x00"  # V3 partial_fill must never skip the output floor.
    assert struct.unpack_from("<Q", data, 16)[0] == builder.last_build["min_tokens_out"]
    if cashback is False:
        event, user = T._event(), builder.user
        keys = [str(message.account_keys[i]) for i in ix.accounts]
        curve = C.curve_route_target(event["mint"])
        assert keys == [
            str(C._pda([b"global"], C.PUMP_PROGRAM)), event["mint"], C.WSOL_MINT,
            T.LIVE["accounts"][8][0], C.TOKEN_PROGRAM, curve,
            str(C.ata(curve, event["mint"], T.LIVE["accounts"][8][0])),
            str(C.ata(curve, C.WSOL_MINT, C.TOKEN_PROGRAM)), str(user),
            str(C.ata(user, event["mint"], T.LIVE["accounts"][8][0])),
            str(C.ata(user, C.WSOL_MINT, C.TOKEN_PROGRAM)),
            str(C._pda([b"user_volume_accumulator", bytes(C._pk(user))], C.PUMP_PROGRAM)),
            "8Wf5TiAheLUqBrKXeYg2JtAFFMWtKdG2BSFgqUcPVwTt", builder._buyback,
            C.SYSTEM_PROGRAM, str(C._pda([b"__event_authority"], C.PUMP_PROGRAM)), C.PUMP_PROGRAM,
        ]
        assert detail["cu_class"] is None  # Legacy measurements do not cover a new layout.


def test_v3_keeps_the_existing_floor_and_never_calls_a_provider():
    builder = T._builder(T._Clock())
    builder.rpc = builder.jup_call = lambda *a, **k: pytest.fail("entry-path provider call")
    legacy, old = builder.build(T._buy(), 5.0, T.SNAP)
    old_floor = builder.last_build["min_tokens_out"]
    message, detail = builder.build(T._buy(curve_cashback_enabled=False), 5.0, T.SNAP)
    assert message is not None, detail
    assert builder.last_build["min_tokens_out"] == old_floor
    assert detail["pump_price_limit"] == old["pump_price_limit"]
    assert len(message.account_keys) <= len(legacy.account_keys)


def test_position_74106_source_build_avoids_both_mutable_creator_vaults():
    from solders.pubkey import Pubkey
    from sniper.services.memecoin_slot_signal import decode_owner_buy
    fixture = json.loads((Path(__file__).parent / "fixtures/memecoin/pump_creator_migration_74106.json").read_text())
    source = fixture["source"]
    frame = {"params": {"result": {"context": {"slot": source["slot"]}, "value": {
        "signature": source["signature"], "logs": source["logs"], "err": None}}}}
    buy = decode_owner_buy(frame, fixture["owner"], bytes(Pubkey.from_string(fixture["owner"])),
                           "pump:" + fixture["owner"], recv_monotonic=1.0, recv_utc="")
    assert buy.curve_cashback_enabled is False
    builder = T._builder(T._Clock())
    message, detail = builder.build(buy, 5.0, T.SNAP)
    assert message is not None, detail
    keys = {str(key) for key in message.account_keys}
    assert fixture["failed"]["supplied_creator_vault"] not in keys
    assert fixture["failed"]["expected_creator_vault"] not in keys
    _, legacy = builder.build(replace(buy, curve_cashback_enabled=None), 5.0, T.SNAP)
    assert detail["pump_price_limit"] == legacy["pump_price_limit"]


def test_v3_slippage_reference_still_binds_the_source_identity():
    _tape, buy, validity = S._world(bad=False)
    signed = dict(validity["entry_slippage"], instruction_discriminator=V3.hex())
    assert G._reference(buy, signed)[0] == (signed["pool"],)
    with pytest.raises(ValueError, match="source_identity"):
        G._reference(buy, dict(signed, source_sig="someone-else"))


@pytest.mark.parametrize("completing", [False, True])
def test_v3_output_bound_covers_synthetic_migration(completing):
    _tape, _buy, validity = S._world(bad=False)
    signed = dict(validity["entry_slippage"], instruction_discriminator=V3.hex())
    raw = bytearray(166)
    raw[:8] = bytes.fromhex("17b7f83760d8ac60")
    real = 1 if completing else 500_000
    struct.pack_into("<5Q", raw, 8, 1_000_000, 1_000_000, real, 1_000_000, 2_000_000)
    signed["spend_atomic"] = 100
    accounts = {signed["pool"]: {"owner": C.PUMP_PROGRAM,
                                 "data": [base64.b64encode(raw).decode(), "base64"]}}
    assert G._upper_bound(accounts, signed) == (2_000_000 if completing else 99)


def test_v3_does_not_skip_the_independent_market_cap(monkeypatch):
    monkeypatch.setattr(S.importlib.import_module("sniper.settings.memecoin"),
                        "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True)
    tape, buy, validity = S._open_world(bad=True)
    validity["entry_slippage"]["instruction_discriminator"] = V3.hex()
    validity["entry_slippage"]["min_tokens_atomic"] = 1
    result = S._guard(tape, head=(100, 10.0))(buy, validity)
    assert result["reason"] == "intervening_market_cap", result

"""Jupiter route_v2, decoded from the pinned on-chain IDL (sniper/execution/jupiter_route.py)."""
import json
from pathlib import Path

import base58
import pytest

from sniper.execution import jupiter_route as J

FX = json.loads((Path(__file__).parent / "fixtures/memecoin/rabbit_jupiter_amm_20261016.json").read_text())


def _route_ix(rec):
    msg = rec["transaction"]["message"]
    keys = msg["accountKeys"] + rec["transaction"]["loadedAddresses"]["writable"] + rec["transaction"]["loadedAddresses"]["readonly"]
    (ix,) = [ix for ix in msg["instructions"] if keys[ix["programIdIndex"]] == J.JUPITER_PROGRAM]
    return base58.b58decode(ix["data"]), [keys[i] for i in ix["accounts"]]


@pytest.mark.parametrize("i", range(3))
def test_recorded_route_v2_frames_decode_exactly_to_one_pumpswap_leg(i):
    rec = FX["full"][i]
    row = next(r for r in FX["rows"] if r["signature"] == rec["signature"])
    data, accounts = _route_ix(rec)
    decoded = J.decode_route_v2(data)
    assert decoded["quoted_out_amount"] == row["quoted_out"] and decoded["slippage_bps"] == row["slippage_bps"]
    assert len(J.pump_steps(decoded)) == 1
    assert accounts[J.DESTINATION_ACCOUNT] == row["dest"] and accounts[J.DESTINATION_MINT] == row["mint"]


def test_trailing_bytes_or_another_discriminator_fail_closed():
    data, _ = _route_ix(FX["full"][0])
    assert J.decode_route_v2(data + b"\x00") is None
    assert J.decode_route_v2(b"\x00" * 8 + data[8:]) is None


def test_a_changed_idl_disables_the_decoder(monkeypatch, tmp_path):
    changed = tmp_path / "idl.json"
    changed.write_bytes(J.IDL_PATH.read_bytes().replace(b'"route_v2"', b'"route_v9"', 1))
    monkeypatch.setattr(J, "IDL_PATH", changed)
    J._types.cache_clear()
    try:
        assert J.decode_route_v2(_route_ix(FX["full"][0])[0]) is None
    finally:
        J._types.cache_clear()


@pytest.mark.parametrize("steps, share", [
    ((("TesseraVV2", 10000, 0, 1), ("PumpSwapBuyV3", 10000, 1, 2)), (10000 * 10000, None)),
    ((("TesseraVV2", 333, 0, 2), ("Obsidian", 9667, 0, 2), ("PumpSwapBuyV4", 10000, 2, 3)), (10000 * 10000, None)),
    ((("BisonFiMarketBacked", 10000, 0, 1), ("PumpSwapBuyV4", 9232, 1, 3), ("MeteoraDlmmSwapV2", 768, 1, 3)),
     (10000 * 9232, None)),
    # a step from the input straight into the coin: only the SOL leg's 8500 reaches PumpSwap
    ((("Kipseli", 8500, 0, 1), ("MeteoraDlmmSwapV2", 1500, 0, 2), ("PumpSwapBuyV4", 10000, 1, 2)), (8500 * 10000, None)),
    ((("Whirlpool", 10000, 0, 1), ("MeteoraDlmm", 10000, 1, 2), ("PumpSwapBuyV4", 10000, 2, 3)),
     (None, "sol_not_from_input")),                                                     # three hops
    ((("TesseraVV2", 10000, 0, 1), ("PumpSwapBuyV2", 10000, 1, 2)), (None, "pump_step_unmodelled")),
    ((("TesseraVV2", 10000, 0, 1), ("PumpSwapBuy", 10000, 1, 2)), (None, "pump_step_unmodelled")),
    ((("TesseraVV2", 10000, 0, 1), ("PumpSwapBuyV3WithCashbackClaim", 10000, 1, 2)), (None, "pump_step_unmodelled")),
    ((("TesseraVV2", 9000, 0, 1), ("PumpSwapBuyV4", 10000, 1, 2)), (None, "bps_sum")),
    ((("TesseraVV2", 10000, 0, 1), ("PumpSwapBuyV4", 5000, 1, 2), ("PumpSwapBuyV3", 5000, 1, 2)), (None, "pump_count")),
    ((("PumpSwapBuyV4", 10000, 0, 1),), (None, "sol_not_from_input")),               # SOL-paid: refused
    ((("Whirlpool", 10000, 0, 1),), (None, "pump_count")),
])
def test_pump_share_follows_the_route_token_indexes(steps, share):
    assert J.pump_share({"steps": steps}) == share

"""Two token instructions the processed Shyft validator refused on armed J7 buys (2026-10-14..16:
6 of 325 armed buys, every one a DFlow v1 route; memecoin_slot_lane_skips metadata_invalid).

- SetAuthority (tag 6) with authority type CloseAccount (3): DFlow hands the owner's coin account's
  close authority to its program. No token moves; the AccountOwner type (2) would change who holds
  the coin, so it stays refused, as does any type this route was not seen to use.
- Token-2022 HarvestWithheldTokensToMint ([26, 4]): moves withheld transfer fees to the mint; no
  account's amount changes.
Both are accepted as instructions with no transfer leg (like SyncNative). Burn (8) and
WithdrawExcessLamports (38) stay refused: a burn reduces a balance the valuation could read as a
payment, and 38 was seen only in a cleanup transaction, never in a buy."""
import copy
import json
import struct
from pathlib import Path

import base58
import pytest

from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_shyft_confirmed as conf
from sniper.services import memecoin_shyft_signal as g
from test_memecoin_shyft_route_metadata import empty_skips, wire  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures/memecoin"
CLOSE = json.loads((FIXTURES / "yansanity_5Hbh7u_set_close_authority.json").read_text())
HARVEST = json.loads((FIXTURES / "yansanity_4EmYCX_harvest_withheld.json").read_text())
OWNER = "CHmurt9dTTbbDj6uZELTENx2ZvKszLs6wd73iDmGHW35"          # yansanity, co-signer after Fomo's executor
OWNERS = {OWNER: ("uid", "yansanity")}
COIN = "A8AdxR4WqCMvE9ZdzSGKtczGpUhYJ7EjcQXNF2YY5smj"
TOKEN, TOKEN_2022 = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA", "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


@pytest.fixture(autouse=True)
def routed_venues_on(monkeypatch):
    from sniper.execution import meteora_damm_v2_buy, meteora_dbc_buy, raydium_cpmm_buy, raydium_launchlab_buy
    for module in (raydium_cpmm_buy, raydium_launchlab_buy, meteora_damm_v2_buy, meteora_dbc_buy):
        monkeypatch.setattr(module, "lane_mode", lambda: "live")


def metadata(fix, why=None):
    return conf.transaction_metadata(wire(fix), OWNERS, head_slot=fix["slot"], recv_monotonic=0,
                                     head_monotonic=0, why=why)


def routed(fix):
    return g.routed_buys(wire(fix), OWNERS, recv_monotonic=0, recv_utc="u", head_slot=fix["slot"],
                         head_mono=0, native_buys=())


def _token_ix(fix, tag):
    keys = fix["transaction"]["message"]["accountKeys"]
    for group in fix["meta"]["innerInstructions"]:
        for ix in group["instructions"]:
            data = base58.b58decode(ix["data"])
            if keys[ix["programIdIndex"]] in (TOKEN, TOKEN_2022) and data[:1] == bytes([tag]):
                return ix, data
    raise AssertionError("no tag %d in the fixture" % tag)


def _patched(fix, tag, data):
    fix = copy.deepcopy(fix)
    ix, _old = _token_ix(fix, tag)
    ix["data"] = base58.b58encode(bytes(data)).decode()
    return fix


@pytest.mark.parametrize("fix, spent, got", [(CLOSE, 40_000_000, 718_374_022_027),
                                             (HARVEST, 200_000_000, 2_842_572_918_235)])
def test_the_refused_dflow_receipts_now_yield_metadata_and_the_routed_buy(fix, spent, got):
    why = []
    assert metadata(fix, why=why) is not None and why == []
    buy, = routed(fix)
    assert (buy.owner, buy.mint, buy.usdc_spent_atomic, buy.token_delta_atomic) == (OWNER, COIN, spent, got)
    assert buy.signature == fix["transaction"]["signatures"][0]


def test_the_receipts_carry_exactly_the_instructions_named():
    _ix, data = _token_ix(CLOSE, 6)
    assert data[:3] == bytes([6, 3, 1]) and len(data) == 35            # CloseAccount authority, Some(key)
    _ix, data = _token_ix(HARVEST, 26)
    assert data == bytes([26, 4])                                       # HarvestWithheldTokensToMint


@pytest.mark.parametrize("authority_type", [0, 1, 2, 4, 9])
def test_set_authority_of_any_other_type_stays_refused(authority_type):
    _ix, data = _token_ix(CLOSE, 6)
    data = bytearray(data)
    data[1] = authority_type
    why = []
    assert metadata(_patched(CLOSE, 6, data), why=why) is None and why == ["token_instruction_unsupported:6"]


@pytest.mark.parametrize("data", [bytes([6, 3]), bytes([6, 3, 0, 0]), bytes([6, 3, 1]) + b"\x01" * 31,
                                  bytes([6, 3, 2]) + b"\x01" * 32])
def test_a_malformed_set_authority_is_refused_by_name(data):
    why = []
    assert metadata(_patched(CLOSE, 6, data), why=why) is None and why == ["token_set_authority_invalid"]


def test_a_close_authority_removal_is_accepted_too():
    """Option None (3 bytes): no new authority; still no token moves."""
    assert metadata(_patched(CLOSE, 6, bytes([6, 3, 0]))) is not None


def test_harvest_only_on_token_2022_and_only_its_own_shape():
    keys = [TOKEN] + [OWNER] * 3
    with pytest.raises(ValueError, match="token_instruction_unsupported:26"):
        conf._instruction(pb.CompiledInstruction(program_id_index=0, accounts=bytes([1, 2]),
                                                 data=bytes([26, 4])), keys)
    why = []
    assert metadata(_patched(HARVEST, 26, bytes([26, 4, 0])), why=why) is None
    assert why == ["token_harvest_invalid"]


@pytest.mark.parametrize("data", [bytes([8]) + struct.pack("<Q", 5), bytes([38])])
def test_burn_and_withdraw_excess_lamports_stay_refused(data):
    keys = [TOKEN] + [OWNER] * 3
    with pytest.raises(ValueError, match="token_instruction_unsupported:%d" % data[0]):
        conf._instruction(pb.CompiledInstruction(program_id_index=0, accounts=bytes([1, 2, 3]), data=data), keys)

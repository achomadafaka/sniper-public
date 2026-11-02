"""Research CPMM decoder: real census bytes, explicit synthetic transaction envelopes."""
import base64
import copy
import json
import struct
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_cpmm as C

FIXTURE = json.loads((Path(__file__).parent / "fixtures/desk_cpmm_transaction.json").read_text())


def frame():
    return copy.deepcopy(FIXTURE["frame"])


def binding():
    pool = frame()["account_keys"][3]
    return C.pool_binding(pool, FIXTURE["binding_accounts"],
                          FIXTURE["binding_known_at"], FIXTURE["binding_context_slot"])


def test_confirmed_direct_swap_binds_actor_pool_units_and_effective_pre_state():
    f, b = frame(), binding()
    result = C.decode_transaction(f, {b["pool"]: b})
    assert result["gaps"] == [] and result["status"] == "ok"
    row, = result["rows"]
    assert row["pool"] == b["pool"] and row["mint"] == b["mint"]
    assert row["actor"] == f["account_keys"][0] and row["actor_status"] == "proven"
    assert row["is_buy"] is True and row["quote_mint"] == C.WSOL
    assert row["base_decimals"] == 6 and row["quote_decimals"] == 9
    assert row["outer_ix"] == 0 and row["inner_ix"] is None and row["ordinal"] == 1
    assert row["received_at"] == 1000 and row["observed_at"] == 990
    assert row["reserve_basis"] == "event_effective_pre"
    assert int(row["pre_quote_atomic"]) > 0 and int(row["pre_base_atomic"]) > 0
    assert row["post_quote_atomic"] is None and row["post_price_sol"] is None
    assert row["price_before_sol"] > 0
    assert isinstance(row["token_atomic"], str) and isinstance(row["quote_atomic"], str)


def test_routed_invocation_keeps_real_instruction_location_and_ignores_router_lookalike():
    f, b = frame(), binding()
    router = "11111111111111111111111111111111"
    f["account_keys"].append(router)
    swap = f["instructions"][0]
    f["instructions"] = [{"program_id_index": 14, "accounts": [], "data_b64": ""}]
    f["inner_instructions"] = [{"index": 0, "instructions": [dict(swap, stack_height=2)]}]
    f["logs"] = [f"Program {router} invoke [1]", "Program data: " + base64.b64encode(b"not cpmm").decode(),
                 f"Program {C.PROGRAM} invoke [2]", frame()["logs"][1],
                 f"Program {C.PROGRAM} success", f"Program {router} success"]
    result = C.decode_transaction(f, {b["pool"]: b})
    row, = result["rows"]
    assert result["gaps"] == [] and row["outer_ix"] == 0 and row["inner_ix"] == 0
    assert row["ordinal"] == 3 and row["actor"] == f["account_keys"][0]


def test_two_swaps_in_one_transaction_have_distinct_durable_identity():
    f, b = frame(), binding()
    f["instructions"] *= 2
    f["logs"] *= 2
    result = C.decode_transaction(f, {b["pool"]: b})
    assert result["status"] == "ok" and len(result["rows"]) == 2
    assert {r["outer_ix"] for r in result["rows"]} == {0, 1}
    assert len({r["event_id"] for r in result["rows"]}) == 2


@pytest.mark.parametrize("change,reason", [
    ({"commitment": "processed"}, "not_confirmed"),
    ({"err": "instruction failed"}, "failed_transaction"),
    ({"logs": ["Log truncated"]}, "incomplete_logs"),
])
def test_unconfirmed_failed_or_truncated_frames_never_become_swaps(change, reason):
    f = dict(frame(), **change)
    result = C.decode_transaction(f, {})
    assert result["rows"] == [] and result["reason"] == reason


def test_unknown_pool_is_partial_and_a_later_binding_cannot_rewrite_authority():
    f, b = frame(), binding()
    for bindings in ({}, {b["pool"]: dict(b, known_at=1001)}):
        result = C.decode_transaction(f, bindings)
        row, = result["rows"]
        assert result["status"] == "partial" and row["price_before_sol"] is None
        assert row["binding_status"] == "unknown" and b["pool"] in result["unresolved_pools"]
        assert result["gaps"][0]["cause"] == "pool_unbound"


def test_payer_without_token_owner_proof_is_unknown_not_an_owner_vote():
    f, b = frame(), binding()
    f["pre_token_balances"] = f["post_token_balances"] = []
    result = C.decode_transaction(f, {b["pool"]: b})
    row, = result["rows"]
    assert row["actor"] is None and row["actor_status"] == "unknown"
    assert result["status"] == "partial" and result["gaps"][0]["cause"] == "actor_unknown"


def test_unknown_transaction_index_does_not_turn_into_known_zero():
    f, b = frame(), binding()
    f["tx_index"] = None
    row, = C.decode_transaction(f, {b["pool"]: b})["rows"]
    assert row["tx_index"] is None and row["order_status"] == "unknown"
    f["tx_index"] = 0
    row, = C.decode_transaction(f, {b["pool"]: b})["rows"]
    assert row["tx_index"] == 0 and row["order_status"] == "proven"


@pytest.mark.parametrize("corruption", ["pool_owner", "mint_owner", "decimals", "extensions", "pool_mint"])
def test_pool_binding_requires_program_mint_decimal_and_extension_provenance(corruption):
    f = frame()
    accounts = copy.deepcopy(FIXTURE["binding_accounts"])
    pool, base = f["account_keys"][3], f["account_keys"][11]
    if corruption == "pool_owner":
        accounts[pool]["owner"] = C.WSOL
    elif corruption == "mint_owner":
        accounts[base]["owner"] = C.WSOL
    elif corruption in ("decimals", "extensions"):
        raw = bytearray(base64.b64decode(accounts[base]["data"][0]))
        if corruption == "decimals":
            raw[44] = 9
        else:
            raw += b"unsupported"
        accounts[base]["data"][0] = base64.b64encode(raw).decode()
    else:
        raw = bytearray(base64.b64decode(accounts[pool]["data"][0]))
        raw[200:232] = bytes(32)
        accounts[pool]["data"][0] = base64.b64encode(raw).decode()
    with pytest.raises(C.DecodeLoss):
        C.pool_binding(pool, accounts, 999, f["slot"] - 1)


def test_malformed_event_length_and_bool_are_rejected():
    raw = base64.b64decode(frame()["logs"][1][14:])
    for bad in (raw[:-1], raw + b"x", raw[:88] + b"\x02" + raw[89:]):
        with pytest.raises(C.DecodeLoss):
            C.parse_event(bad)


def test_a_sell_is_oriented_by_wsol_mints_not_assumed_instruction_direction():
    f, b = frame(), binding()
    raw = bytearray(base64.b64decode(f["logs"][1][14:]))
    # Synthetic reverse of the frozen event; preserve valid positive atomic fee/amount fields.
    struct.pack_into("<6Q", raw, 40, 1_000_000_000_000, 100_000_000_000, 1_000_000, 99_999, 0, 0)
    raw[89:121], raw[121:153] = bytes(raw[121:153]), bytes(raw[89:121])
    struct.pack_into("<2Q", raw, 153, 0, 0)
    f["logs"][1] = "Program data: " + base64.b64encode(raw).decode()
    keys = f["account_keys"]
    for left, right in ((4, 5), (6, 7), (8, 9), (10, 11)):
        keys[left], keys[right] = keys[right], keys[left]
    f["instructions"][0]["data_b64"] = base64.b64encode(C.SWAP_INPUT + struct.pack("<2Q", 1_000_000, 0)).decode()
    f["pre_token_balances"] = f["post_token_balances"] = []
    row, = C.decode_transaction(f, {b["pool"]: b})["rows"]
    assert row["is_buy"] is False and row["quote_mint"] == C.WSOL
    assert row["quote_atomic"] == "99999" and row["token_atomic"] == "1000000"

@pytest.mark.parametrize('change', [
    {'slot': True}, {'slot': -1}, {'received_at': float('nan')},
    {'signature': ''}, {'signer_count': -1}, {'signer_count': 999},
])
def test_invalid_frame_identity_and_clock_cannot_authorize_rows(change):
    f, b = dict(frame(), **change), binding()
    assert C.decode_transaction(f, {b['pool']: b})['rows'] == []


def test_negative_account_index_cannot_alias_the_last_key():
    f, b = frame(), binding()
    f['instructions'][0]['program_id_index'] = -1
    assert C.decode_transaction(f, {b['pool']: b})['rows'] == []


def test_missing_clock_correction_is_unknown_and_nonfinite_is_never_persistable():
    f, b = frame(), binding()
    f['observed_at'] = float('nan')
    row, = C.decode_transaction(f, {b['pool']: b})['rows']
    assert row['observed_at'] is None and row['clock_offset_s'] is None


def test_absent_success_metadata_is_unknown_not_a_successful_receipt():
    f,b=frame(),binding();del f['err']
    result=C.decode_transaction(f,{b['pool']:b})
    assert result['rows']==[] and result['status']=='unknown'


def test_unknown_instruction_layout_never_guesses_pool_at_index_three():
    f,b=frame(),binding()
    f['instructions'][0]['data_b64']=base64.b64encode(b'unknown!'+bytes(16)).decode()
    result=C.decode_transaction(f,{b['pool']:b})
    assert result['rows']==[] and result['gaps']==[{'cause':'pool_state_change','pool':None}]


def test_transaction_wide_row_discard_has_global_gap_for_earlier_valid_pools():
    f,b=frame(),binding()
    second=copy.deepcopy(f['instructions'][0])
    f['account_keys'].append('11111111111111111111111111111111')
    second['accounts'][3]=len(f['account_keys'])-1
    f['instructions'].append(second)
    f['logs'] += [f'Program {C.PROGRAM} invoke [1]',f['logs'][1],f'Program {C.PROGRAM} success']
    result=C.decode_transaction(f,{b['pool']:b})
    assert result['rows']==[] and result['status']=='unknown'
    assert any(g['pool'] is None for g in result['gaps'])

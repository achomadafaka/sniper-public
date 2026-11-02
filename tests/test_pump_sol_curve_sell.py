"""Offline SOL sell arithmetic: frozen program events, exact units, no provider clients."""
import base64
import json
import struct
from pathlib import Path

import pytest

from sniper.execution import pump_native_exit as N

FIXTURES = Path(__file__).parent / "fixtures" / "memecoin"
STATE = {"virtual_sol_reserves": 37_903_103_425,
         "virtual_token_reserves": 849_270_827_451_215,
         "real_sol_reserves": 7_903_103_425}


def quote(state=STATE, amount=41_876_174_451, slip=3, protocol=95, creator=30):
    return N.curve_sell_quote(state, amount, slip, protocol_bps=protocol, creator_bps=creator)


def test_processed_sell_pair_has_separate_ceil_fees_and_a_net_floor():
    got, reason = quote()
    assert reason is None
    assert got == {"gross_out": 1_868_848, "protocol_fee": 17_755, "creator_fee": 5_607,
                   "expected_out": 1_845_486, "min_out": 1_790_121, "slippage_bps": 300}


def test_gross_equal_to_real_reserves_passes_but_one_lamport_short_refuses():
    assert quote(dict(STATE, real_sol_reserves=1_868_848))[0]["gross_out"] == 1_868_848
    assert quote(dict(STATE, real_sol_reserves=1_868_847)) == (None, "curve_real_sol_short")
    # Checking net instead of gross would let this insolvent state through.
    assert quote(dict(STATE, real_sol_reserves=1_845_486)) == (None, "curve_real_sol_short")


def test_post_buy_state_funds_the_sell_that_pre_buy_state_cannot():
    from sniper.execution.pump_curve_buy import curve_buy_net, expected_tokens_out
    pre = {"virtual_sol_reserves": 1000, "virtual_token_reserves": 1000,
           "real_sol_reserves": 1, "real_token_reserves": 1000,
           "fee_basis_points": 95, "creator_fee_basis_points": 30}
    assert curve_buy_net(102, pre) == 100
    assert expected_tokens_out(102, pre) == 90
    assert quote(pre, 90) == (None, "curve_real_sol_short")
    post = dict(pre, virtual_sol_reserves=1100, virtual_token_reserves=910,
                real_sol_reserves=101, real_token_reserves=910)
    got, reason = quote(post, 90)
    assert reason is None
    assert got == {"gross_out": 99, "protocol_fee": 1, "creator_fee": 1,
                   "expected_out": 97, "min_out": 94, "slippage_bps": 300}
    assert pre["real_sol_reserves"] == 1


def test_every_existing_exit_rung_signs_a_positive_floor_below_net():
    from sniper.settings.memecoin import (MEMECOIN_EXIT_SLIPPAGE_LADDER,
                                          MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL)
    for rung in MEMECOIN_EXIT_SLIPPAGE_LADDER + MEMECOIN_EXIT_SLIPPAGE_LADDER_MANUAL:
        got, reason = quote(slip=rung)
        assert reason is None
        assert 0 < got["min_out"] < got["expected_out"], rung


@pytest.mark.parametrize("amount", [0, -1, True, 1.5, "5", 2**64])
def test_invalid_atomic_input_cannot_be_rounded_or_truncated(amount):
    assert quote(amount=amount) == (None, "curve_amount_invalid")


@pytest.mark.parametrize("slip", [0, -1, 100, float("nan"), float("inf"), None, True])
def test_invalid_slippage_never_produces_a_signed_floor(slip):
    assert quote(slip=slip) == (None, "curve_slippage_invalid")


@pytest.mark.parametrize("field,value", [("virtual_sol_reserves", 0),
    ("virtual_token_reserves", 0), ("real_sol_reserves", -1),
    ("virtual_sol_reserves", None), ("virtual_token_reserves", True),
    ("real_sol_reserves", "10000000000000000000"), ("virtual_sol_reserves", 2**64)])
def test_unknown_or_malformed_reserves_are_not_zero_or_valid(field, value):
    assert quote(dict(STATE, **{field: value})) == (None, "curve_state_unreadable")


@pytest.mark.parametrize("protocol,creator", [(-1, 30), (95, 10_000),
    (10_000, 30), (True, 30), (95, None), (95, 1.5)])
def test_unmodelled_fee_rates_refuse(protocol, creator):
    assert quote(protocol=protocol, creator=creator) == (None, "curve_fee_unmodelled")


def test_zero_creator_fee_and_nonpositive_net_are_distinct():
    got, reason = quote(creator=0)
    assert reason is None and got["creator_fee"] == 0 and got["expected_out"] == 1_851_093
    tiny = {"virtual_sol_reserves": 4, "virtual_token_reserves": 1, "real_sol_reserves": 4}
    assert quote(tiny, 1) == (None, "curve_net_zero")  # gross2, separate ceil fees1+1
    assert quote(tiny, 1, protocol=0, creator=0, slip=99.99) == (None, "curve_min_zero")


def test_frozen_continuous_tape_pairs_match_gross_and_all_four_reserve_transitions():
    fixture = json.loads((FIXTURES / "pump_sol_curve_sell_pairs.json").read_text())
    assert len(fixture["pairs"]) == 2000
    drained = dust = 0
    for row in fixture["pairs"]:
        pre, post, q, gross = row["pre"], row["post"], row["tokens_in"], row["reported_gross_lamports"]
        state = {"virtual_sol_reserves": pre["virtual_quote"],
                 "virtual_token_reserves": pre["virtual_token"], "real_sol_reserves": pre["real_quote"]}
        got, reason = quote(state, q, protocol=row["fee_bps"], creator=row["creator_fee_bps"])
        if gross == 3:  # one frozen 3-lamport sell pays ceil fees1+1; net1 cannot fund a 3% floor
            assert (got, reason) == (None, "curve_min_zero")
            dust += 1
        else:
            assert reason is None, row["signature"]
            assert got["gross_out"] == gross, row["signature"]
        assert post == {"virtual_quote": pre["virtual_quote"] - gross,
                        "virtual_token": pre["virtual_token"] + q,
                        "real_quote": pre["real_quote"] - gross,
                        "real_token": pre["real_token"] + q}, row["signature"]
        drained += gross == pre["real_quote"]
    assert drained > 0 and dust == 1


def test_confirmed_sol_pairs_match_raw_pre_state_and_raw_logged_fee_amounts():
    from sniper.execution.pump_curve_buy import parse_trade_event
    fixture = json.loads((FIXTURES / "pump_sol_curve_sells.json").read_text())
    assert len(fixture["rows"]) == 17
    sol_rows = non_sol_rows = 0
    creator_rates = set()
    for row in fixture["rows"]:
        raw = base64.b64decode(row["pre_state"]["account_b64"])
        if raw[83:115] != bytes(32):
            non_sol_rows += 1
            continue
        sol_rows += 1
        events = [parse_trade_event(line) for line in row["sell"]["log_messages"]
                  if line.startswith("Program data: ") and base64.b64decode(line[14:])[:8]
                  == bytes.fromhex("bddb7fd34ee661ee")]
        event, = [e for e in events if e and not e["is_buy"] and e["mint"] == row["mint"]]
        assert event == row["trade_event"]
        vt, vs, _rt, rs = struct.unpack_from("<4Q", raw, 8)
        assert event["quote_mint"] == N.SYSTEM_PROGRAM
        gross = event["sol_amount"]
        assert gross > 0 and gross == event["quote_amount"]
        state = {"virtual_sol_reserves": vs, "virtual_token_reserves": vt, "real_sol_reserves": rs}
        got, reason = quote(state, event["token_amount"], protocol=event["fee_basis_points"],
                            creator=event["creator_fee_basis_points"])
        assert reason is None, row["sell"]["signature"]
        assert (got["gross_out"], got["protocol_fee"], got["creator_fee"]) == (
            gross, event["fee"], event["creator_fee"]), row["sell"]["signature"]
        assert (event["virtual_sol_reserves"], event["virtual_token_reserves"],
                event["real_sol_reserves"]) == (vs - gross, vt + event["token_amount"], rs - gross)
        creator_rates.add(event["creator_fee_basis_points"])
    assert (sol_rows, non_sol_rows) == (17, 0)
    assert len(fixture["quote_token_rows"]) == 22
    assert all(base64.b64decode(row["pre_state"]["account_b64"])[83:115] != bytes(32)
               for row in fixture["quote_token_rows"])
    assert creator_rates == {0, 30}  # 300 bps belongs to quote-token curves, not this SOL subset.

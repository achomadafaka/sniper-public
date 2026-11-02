"""Position 73924's signed FLASHX -> native-SOL Pump V3 curve route, without execution metadata."""
import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import base58
import pytest

from sniper.execution import pump_curve_buy as curve
from sniper.feeds.yellowstone import geyser_pb2 as pb
from sniper.services import memecoin_rabbit_signal as rabbit
from sniper.services import memecoin_shyft_prepare as prep


REC = json.loads((Path(__file__).parent / "fixtures/memecoin/rabbit_flashx_curve_73924.json").read_text())
EVENT = curve.parse_trade_event(REC["trade_event_log"])
RESERVES = ("virtual_sol_reserves", "virtual_token_reserves", "real_sol_reserves", "real_token_reserves")
ROUTE = "flashx_curve_v3"


def _frame():
    update = pb.SubscribeUpdate()
    update.transaction.slot = REC["slot"]
    info, source = update.transaction.transaction, REC["transaction"]
    info.signature = base58.b58decode(source["signatures"][0])
    info.transaction.signatures.extend(base58.b58decode(sig) for sig in source["signatures"])
    msg, raw = info.transaction.message, source["message"]
    for src, dst in (("numRequiredSignatures", "num_required_signatures"),
                     ("numReadonlySignedAccounts", "num_readonly_signed_accounts"),
                     ("numReadonlyUnsignedAccounts", "num_readonly_unsigned_accounts")):
        setattr(msg.header, dst, raw["header"][src])
    msg.account_keys.extend(base58.b58decode(key) for key in raw["accountKeys"])
    msg.recent_blockhash = base58.b58decode(raw["recentBlockhash"])
    msg.versioned = True
    for src, dst in (("priorityFee", "priority_fee"), ("computeUnitLimit", "compute_unit_limit"),
                     ("loadedAccountsDataSizeLimit", "loaded_accounts_data_size_limit"),
                     ("heapSize", "heap_size")):
        value = raw["transactionConfig"][src]
        if value is not None:
            setattr(msg.config, dst, value)
    for ix in raw["instructions"]:
        msg.instructions.add(program_id_index=ix["programIdIndex"], accounts=bytes(ix["accounts"]),
                             data=base58.b58decode(ix["data"]))
    return update


def _candidate(update=None):
    stats = {}
    result = prep.preparation_candidate(update or _frame(), {REC["owner"]: ("uid", "notdecu")},
                                        recv_monotonic=1.0, generation=1, stats=stats)
    assert result is not None, stats
    return result


def _parts():
    update = _frame()
    candidate = _candidate(update)
    found = rabbit.source_buy(candidate, prep.preparation_target(candidate))
    assert found == (REC["mint"], {"spend": REC["spend_lamports"],
                                 "min_tokens_out": REC["minimum_tokens_out"]}, ROUTE)
    return update, candidate, found


def _state(*, post):
    state = {key: EVENT[key] for key in (*RESERVES, "fee_basis_points", "creator_fee_basis_points", "creator")}
    state.update(slot=REC["slot"], token_program=curve.TOKEN_2022_PROGRAM,
                 holder_atomic=EVENT["token_amount"] if post else 0,
                 complete=False, is_cashback_coin=False, is_holder_reward=False,
                 quote_mint=curve.SYSTEM_PROGRAM)
    if not post:
        for key in RESERVES:
            state[key] += (EVENT["token_amount"] if "token" in key else -EVENT["sol_amount"])
    return state


def test_exact_source_message_resolves_without_inner_instructions_or_meta():
    update = _frame()
    assert not update.transaction.transaction.HasField("meta")
    assert not update.transaction.transaction.transaction.message.address_table_lookups
    candidate = _candidate(update)
    assert candidate["version"] == "v1"
    assert prep.preparation_target(candidate) == {
        "venue": "pump_curve", "mint": REC["mint"],
        "pool": "7VsPE7ceJ1s6PPdg63swPTxN7TrsRmS1Rnqssx2JtpRT"}
    assert rabbit.owner_signature_basis(update, REC["owner"]) == "leader_sigverify_v1"


def test_router_holder_and_token_program_bind_to_the_same_owner_ata():
    update, candidate, found = _parts()
    assert rabbit.bind_holder(candidate, found, rabbit.writable_keys(update)) == (REC["holder"], None)
    assert rabbit.bind_holder(candidate, found, frozenset()) == (None, "holder_unbound")
    assert rabbit._leg_token_program(candidate, REC["mint"], REC["owner"], REC["holder"]) == curve.TOKEN_2022_PROGRAM


@pytest.mark.parametrize("post", [False, True])
def test_existing_read_guard_reproduces_source_fill_and_payment_exactly(post):
    _, _, found = _parts()
    basis, fill = rabbit.read_verdict(_state(post=post), found[1])
    assert basis == (rabbit.POST_TRADE if post else rabbit.PRE_TRADE)
    for key in (*RESERVES, "sol_amount", "token_amount", "fee", "creator_fee"):
        assert fill[key] == EVENT[key], key
    assert sum(fill[key] for key in ("sol_amount", "fee", "creator_fee")) == REC["spend_lamports"]


@pytest.mark.parametrize("post", [False, True])
def test_router_output_floor_cannot_be_ignored_by_pre_or_post_read(post):
    _, _, found = _parts()
    amount = dict(found[1], min_tokens_out=EVENT["token_amount"] + 1)
    assert rabbit.read_verdict(_state(post=post), amount)[0] is None


def test_a_later_buy_in_the_post_state_cannot_be_applied_as_the_source():
    _, _, found = _parts()
    state = _state(post=True)
    later = rabbit.source_fill(state, spend=10**9)
    moved = dict(state, **{key: later[key] for key in RESERVES})
    assert rabbit.read_verdict(moved, found[1]) == (None, "read_after_source_unproven")


def test_only_the_bound_setup_may_also_name_the_coin_holder():
    update, candidate, found = _parts()
    moved = dict(candidate, instructions=candidate["instructions"] + (
        (curve.TOKEN_2022_PROGRAM, (REC["holder"], curve.WSOL_MINT, REC["owner"]), b"\x03"),))
    assert rabbit.bind_holder(moved, found, rabbit.writable_keys(update)) == (None, "holder_moved_in_frame")


@pytest.mark.parametrize("damage", ["truncated", "trailing", "tail", "count", "actor", "quote",
                                    "token_program", "curve", "holder", "setup_owner", "setup_mint",
                                    "no_setup", "duplicate_setup", "duplicate_route", "legacy"])
def test_other_flashx_shapes_remain_unsupported(damage):
    update = _frame()
    msg = update.transaction.transaction.transaction.message
    route, setup = msg.instructions[2], msg.instructions[1]
    if damage == "truncated":
        route.data = route.data[:-1]
    elif damage == "trailing":
        route.data += b"\x00"
    elif damage == "tail":
        route.data = route.data[:-1] + bytes([route.data[-1] ^ 1])
    elif damage == "count":
        route.accounts = route.accounts[:-1]
    elif damage in ("actor", "quote", "token_program", "curve", "holder"):
        accounts = bytearray(route.accounts)
        index = {"actor": 14, "quote": 8, "token_program": 9, "curve": 11, "holder": 15}[damage]
        accounts[index] = route.accounts[2]
        route.accounts = bytes(accounts)
    elif damage in ("setup_owner", "setup_mint"):
        accounts = bytearray(setup.accounts)
        accounts[0 if damage == "setup_owner" else 3] = route.accounts[2]
        setup.accounts = bytes(accounts)
    elif damage == "no_setup":
        del msg.instructions[1]
    elif damage == "duplicate_setup":
        msg.instructions.add().CopyFrom(setup)
    elif damage == "duplicate_route":
        msg.instructions.add().CopyFrom(route)
    elif damage == "legacy":
        msg.ClearField("config")
        msg.versioned = False
    stats = {}
    assert prep.preparation_candidate(update, {REC["owner"]: ("uid", "h")}, recv_monotonic=1.0,
                                      generation=1, stats=stats) is None
    assert stats.get("unsupported_target") == 1


@pytest.mark.parametrize("index", [2, 13, 16, 17, 19, 24, 25, 26, 27])
def test_receipt_bound_payees_and_canonical_quote_accounts_cannot_change(index):
    update = _frame()
    route = update.transaction.transaction.transaction.message.instructions[2]
    accounts = bytearray(route.accounts)
    accounts[index] = route.accounts[3]  # another valid key (System), never a parser error
    route.accounts = bytes(accounts)
    assert prep.preparation_candidate(update, {REC["owner"]: ("uid", "h")}, recv_monotonic=1.0,
                                      generation=1) is None


@pytest.mark.parametrize("field,value", [("is_cashback_coin", True), ("is_cashback_coin", None),
                                        ("complete", True), ("complete", None),
                                        ("is_holder_reward", True), ("is_holder_reward", None),
                                        ("quote_mint", curve.USDC_MINT), ("quote_mint", None)])
def test_v3_unmodelled_or_unknown_curve_mode_refuses(field, value):
    update, candidate, _ = _parts()
    handler = rabbit.CurveHandler()
    handler.bind(SimpleNamespace(_route=lambda *args: None, _audit=lambda *args: None))
    plan, reason, _ = handler.match(candidate, update)
    assert reason is None
    assert handler.verdict(plan, dict(_state(post=False), **{field: value}), candidate)[0] is None


def test_v3_crossing_real_reserves_refuses_instead_of_modelling_synthetic_migration():
    update, candidate, _ = _parts()
    handler = rabbit.CurveHandler()
    handler.bind(SimpleNamespace(_route=lambda *args: None, _audit=lambda *args: None))
    plan, reason, _ = handler.match(candidate, update)
    assert reason is None
    state = dict(_state(post=False), real_token_reserves=EVENT["token_amount"])
    assert handler.verdict(plan, state, candidate)[0] is None


@pytest.mark.parametrize("bad_discriminator", [False, True])
@pytest.mark.parametrize("length,cashback,reward", [(82, 0, 0), (151, 0, 0), (166, 0, 0),
                                                  (166, 1, 0), (166, 0, 1), (166, 2, 0)])
def test_existing_single_rpc_exposes_only_complete_v3_mode_layout(length, cashback, reward, bad_discriminator):
    from test_memecoin_rabbit_signal_stage2 import fake_rpc

    calls = []
    rpc = fake_rpc(REC["mint"], _state(post=False), calls=calls, slot=REC["slot"])
    raw = bytearray(base64.b64decode(rpc.accounts["curve"]["data"][0]))
    raw.extend(bytes(166 - len(raw)))
    # Official BondingCurve IDL 7616681e: cashback82, quote83..115, holder-reward124.
    raw[:8] = bytes.fromhex("17b7f83760d8ac60")
    if bad_discriminator:
        raw[0] ^= 1
    raw[82], raw[124] = cashback, reward
    raw[83:115] = base58.b58decode(curve.WSOL_MINT)
    rpc.accounts["curve"]["data"][0] = base64.b64encode(raw[:length]).decode()
    state = rabbit.read_curve(REC["mint"], rpc, holder=REC["holder"], owner=REC["owner"])
    if length == 82:                       # no recorded curve has this length: the fee reader refuses it
        assert state == {"reason": "mayhem_or_fee_unmodelled"}
        return
    known = length >= 166 and not bad_discriminator
    assert "reason" not in state
    assert state["complete"] is False
    assert state["is_cashback_coin"] is (bool(cashback) if known and cashback in (0, 1) else None)
    assert state["is_holder_reward"] is (bool(reward) if known else None)
    assert state["quote_mint"] == (curve.WSOL_MINT if known else None)
    assert len(calls) == 1 and len(calls[0][1][0]) == 4


def test_curve_handler_keeps_read_freshness_and_owner_payment_proof():
    update, candidate, _ = _parts()
    handler = rabbit.CurveHandler()
    handler.bind(SimpleNamespace(_route=lambda *args: None, _audit=lambda *args: None))
    plan, reason, route = handler.match(candidate, update)
    assert reason is None and route == ROUTE
    state = _state(post=False)
    assert handler.verdict(copy.deepcopy(plan), dict(state, slot=None), candidate)[0] is None
    assert handler.verdict(copy.deepcopy(plan), dict(state, slot=REC["slot"] - 100), candidate)[0] is None
    fill, reason = handler.verdict(plan, state, candidate)
    assert reason is None
    buy = handler.build(candidate, plan, state, fill, source_user_id="uid", handle="notdecu",
                        signature_basis=rabbit.owner_signature_basis(update, REC["owner"]),
                        read_ms=1.0, recv_epoch=1792251618.0, feed="rabbit:rabbitstream.fra.shyft.to",
                        head=None, head_mono=None, now_mono=1.01)
    proof = buy.source_initiation
    assert proof["tx_hash"] == REC["transaction"]["signatures"][0]
    assert proof["beneficiary"] == proof["payer"] == REC["owner"]
    assert proof["payment_atomic"] == str(REC["spend_lamports"])
    assert proof["acquired_atomic"] == str(EVENT["token_amount"])
    assert proof["signature_basis"] == "leader_sigverify_v1"
    assert proof["payment_basis"] == "pre_execution_estimate" and proof["route"] == ROUTE

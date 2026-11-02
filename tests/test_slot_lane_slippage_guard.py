"""Only a coherent, source-or-newer frozen pool can prove the signed minimum impossible."""
import base64
import copy
import importlib
import json
import struct
from pathlib import Path
from types import SimpleNamespace

import base58
import pytest
from solders.pubkey import Pubkey

from sniper.execution import pump_curve_buy as curve
from sniper.execution import pumpswap_buy as amm
from sniper.execution.solana_native_entry_quote import accounts_for
from sniper.services import memecoin_rabbit_tape as tape_module

FIX = Path(__file__).parent / "fixtures"
CURVE_FRAME = next(frame for frame in json.loads(
    (FIX / "memecoin/rabbit_curve_buys_20261016.json").read_text())["direct"]
    if curve.parse_trade_event(frame["trade_event_log"])["mayhem_mode"] is False)
AMM_FRAMES = json.loads((FIX / "pumpswap_buy_frames.json").read_text())


def _put(tape, key, owner, raw, slot=100):
    tape.on_account(key, owner, 10**12, bytes(raw), slot, 1)


def _world(venue="pump_curve", *, bad=True):
    tape = tape_module.PoolTape()
    tape.reset("connect")
    if venue == "pump_curve":
        event = curve.parse_trade_event(CURVE_FRAME["trade_event_log"])
        mint, program = event["mint"], curve.PUMP_PROGRAM
        pool = accounts_for(mint)["curve"]
        spend = 30_000_000
        expected = curve.expected_tokens_out(spend, event)
        floor = expected * 7 // 10
        raw = bytearray(166)
        raw[:8] = bytes.fromhex("17b7f83760d8ac60")
        vtoken = event["virtual_token_reserves"] // 2 if bad else event["virtual_token_reserves"]
        vsol = event["virtual_sol_reserves"] * (2 if bad else 1)
        real_token = event["real_token_reserves"] - (event["virtual_token_reserves"] - vtoken)
        real_sol = event["real_sol_reserves"] + vsol - event["virtual_sol_reserves"]
        struct.pack_into("<5Q", raw, 8, vtoken, vsol, real_token, real_sol, 10**15)
        raw[49:81] = bytes(Pubkey.from_string(event["creator"]))
        # Full known V3 prefix: not complete/Mayhem/cashback/holder reward, native SOL quote.
        _put(tape, pool, program, raw)
        buy = SimpleNamespace(venue=venue, mint=mint, pool=None, slot=100, signature="source",
                              owner=event["user"], token_program=curve.TOKEN_PROGRAM,
                              trade_event_log=CURVE_FRAME["trade_event_log"])
        metadata = dict(venue=venue, program=program, mint=mint, pool=pool,
                        quote_mint=curve.WSOL_MINT, spend_atomic=spend,
                        min_tokens_atomic=floor, base_token_program=curve.TOKEN_PROGRAM,
                        quote_token_program=curve.TOKEN_PROGRAM, source_sig="source", source_slot=100,
                        instruction_discriminator=curve.BUY_EXACT_SOL_IN_DISCRIMINATOR.hex())
    else:
        event = amm.parse_buy_event(AMM_FRAMES["direct_buy_exact_quote_in"]["buy_event_log"])
        facts = amm.parse_pool_account(AMM_FRAMES["pool_account_b64"])
        mint, pool = facts["base_mint"], event["pool"]
        base, quote = amm.post_trade_reserves(event)
        spend = 30_000_000
        floor = amm.expected_base_out(spend, base, quote, facts["virtual_quote"], amm.total_fee_bps(event)) * 7 // 10
        _put(tape, pool, amm.AMM_PROGRAM, base64.b64decode(AMM_FRAMES["pool_account_b64"]))
        for key, token, amount in ((facts["pool_base_token_account"], mint, base // 10 if bad else base),
                                    (facts["pool_quote_token_account"], facts["quote_mint"], quote)):
            raw = bytearray(165)
            raw[:32], raw[32:64] = bytes(Pubkey.from_string(token)), bytes(Pubkey.from_string(pool))
            struct.pack_into("<Q", raw, 64, amount)
            raw[108] = 1
            _put(tape, key, curve.TOKEN_PROGRAM, raw)
        buy = SimpleNamespace(venue=venue, mint=None, pool=pool, slot=100, signature="source",
                              owner=event["user"], trade_event_log=AMM_FRAMES["direct_buy_exact_quote_in"]["buy_event_log"])
        metadata = dict(venue=venue, program=amm.AMM_PROGRAM, mint=mint, pool=pool,
                        quote_mint=facts["quote_mint"], spend_atomic=spend,
                        min_tokens_atomic=floor, base_token_program=curve.TOKEN_PROGRAM,
                        quote_token_program=curve.TOKEN_PROGRAM, source_sig="source", source_slot=100,
                        pool_facts=facts)
    tape.on_slot(99, 98, tape_module.SLOT_CREATED_BANK)
    tape.on_slot(100, 99, tape_module.SLOT_CREATED_BANK)
    tape.on_slot(101, 100, tape_module.SLOT_CREATED_BANK)
    return tape, buy, {"entry_slippage": metadata}


def _guard(tape, *, head=(101, 10.0), clock=lambda: 10.0):
    module = importlib.import_module("sniper.services.memecoin_slot_slippage")
    return module.make_guard(tape, head_fn=lambda: head, clock=clock)


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
@pytest.mark.parametrize("bad", [False, True])
def test_market_cap_ignores_ticket_size_and_output_floor(venue, bad, monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_COPY_PREMIUM_PCT", 35)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", 35)
    tape, buy, validity = _open_world(venue, bad=bad)
    signed = validity["entry_slippage"]
    signed["min_tokens_atomic"] = 1  # Movement must refuse even when slippage cannot.
    first = _guard(tape, head=(100, 10.0))(buy, validity)
    assert first["market_movement"]["state"] == ("refuse" if bad else "pass"), first
    assert first["state"] == ("refuse" if bad else "pass"), first
    signed["spend_atomic"] *= 100
    second = _guard(tape, head=(100, 10.0))(buy, validity)
    assert second["market_movement"] == first["market_movement"]


def test_unproved_movement_still_proceeds_without_inventing_a_price(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    tape, buy, validity = _world(bad=False)
    tape.disconnect("gap")
    result = _guard(tape)(buy, validity)
    assert result["state"] == "unknown"
    assert result["market_movement"]["state"] == "unknown"
    assert result["market_movement"]["movement_pct"] is None


def test_missing_market_anchor_does_not_erase_the_existing_slippage_veto(monkeypatch):
    from sniper.settings import memecoin as cfg
    module = importlib.import_module("sniper.services.memecoin_slot_slippage")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    def unavailable(*_):
        raise ValueError("source price unknown")
    monkeypatch.setattr(module, "_market_reserves", unavailable)
    tape, buy, validity = _world(bad=True)
    result = _guard(tape)(buy, validity)
    assert result["state"] == "refuse"
    assert result["reason"] == "signed_minimum_unreachable"
    assert result["market_movement"]["state"] == "unknown"


def test_cached_virtual_quote_cannot_fabricate_market_movement(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    tape, buy, validity = _open_world("pump_amm", bad=False)
    validity["entry_slippage"]["pool_facts"] = {
        **validity["entry_slippage"]["pool_facts"], "virtual_quote": 10**18}
    result = _guard(tape, head=(100, 10.0))(buy, validity)
    assert result["market_movement"]["movement_pct"] == 0
    assert result["market_movement"]["state"] == "pass"


def test_source_transaction_post_cut_is_required_for_market_proof(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_COPY_MARKET_MOVEMENT_ONLY", True, raising=False)
    tape, buy, validity = _world(bad=False)  # Event alone has no exact-source account cut.
    result = _guard(tape)(buy, validity)
    assert result["state"] == "pass"
    assert result["market_movement"]["state"] == "unknown"


def _quote_world(*, bad=True, quote_program=curve.TOKEN_2022_PROGRAM, open_bank=False):
    frames = json.loads((FIX / "memecoin/pump_quote_curve_frames.json").read_text())["frames"]
    frame = next(frame for frame in frames
                 if bytes.fromhex(frame["pump_ix_data_hex"])[:8] == curve.BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR)
    line = next(line for line in frame["logs"] if line.startswith("Program data: ")
                and base64.b64decode(line[14:])[:8] == curve.TRADE_EVENT_DISCRIMINATOR)
    event = curve.parse_trade_event(line)
    tape, buy, validity = _world()
    old_key = validity["entry_slippage"]["pool"]
    _, _, _, account = tape._acc.pop(old_key)[-1]
    pool = accounts_for(event["mint"])["curve"]
    raw = bytearray(125)
    raw[:8] = bytes.fromhex("17b7f83760d8ac60")
    vtoken = event["virtual_token_reserves"] // (2 if bad else 1)
    vquote = event["virtual_quote_reserves"] * (2 if bad else 1)
    real = min(vtoken, event["real_token_reserves"])
    struct.pack_into("<5Q", raw, 8, vtoken, vquote, real, event["real_quote_reserves"], 10**15)
    raw[49:81] = bytes(Pubkey.from_string(event["creator"]))
    raw[83:115] = bytes(Pubkey.from_string(event["quote_mint"]))
    account = dict(account, data=bytes(raw))
    tape._acc[pool] = ((100, 1, False, account),)
    spend = struct.unpack_from("<Q", bytes.fromhex(frame["pump_ix_data_hex"]), 8)[0]
    buy.mint, buy.owner, buy.token_program = event["mint"], event["user"], curve.TOKEN_2022_PROGRAM
    buy.trade_event_log = line
    signed = validity["entry_slippage"]
    signed.update(mint=event["mint"], pool=pool, quote_mint=event["quote_mint"],
                  base_token_program=buy.token_program, quote_token_program=quote_program,
                  instruction_discriminator=curve.BUY_EXACT_QUOTE_IN_V2_DISCRIMINATOR.hex(),
                  spend_atomic=spend, min_tokens_atomic=curve.quote_curve_tokens_out(spend, event) * 7 // 10)
    assert event["virtual_sol_reserves"] == 0 < event["virtual_quote_reserves"]
    if open_bank:
        tape.head = 100
        tape._parent.pop(101)
        tape._has_child.discard(100)
        tape._banks, tape._bank_tainted = {100: 0}, set()
        buy.signature = signed["source_sig"] = base58.b58encode(bytes(range(64))).decode()
        account = dict(account, bank_id=0, txn_signature=bytes(range(64)), is_startup=False)
        tape._acc[pool] = ((100, 1, False, account),)
    return tape, buy, validity


@pytest.mark.parametrize("quote_program", [curve.TOKEN_PROGRAM, curve.TOKEN_2022_PROGRAM])
@pytest.mark.parametrize("open_bank", [False, True])
@pytest.mark.parametrize("bad", [False, True])
def test_quote_v2_uses_the_exact_quote_bound_with_existing_bank_proof(quote_program, open_bank, bad):
    tape, buy, validity = _quote_world(bad=bad, quote_program=quote_program, open_bank=open_bank)
    guard = _guard(tape, head=(100 if open_bank else 101, 10.0))
    result = guard(buy, validity)
    assert result["state"] == ("refuse" if bad else "pass"), result
    raw = tape._acc[validity["entry_slippage"]["pool"]][-1][3]["data"]
    vtoken, vquote, real = struct.unpack_from("<3Q", raw, 8)
    signed = validity["entry_slippage"]
    assert result["upper_bound_tokens_atomic"] == min(vtoken * signed["spend_atomic"] //
                                                       (vquote + signed["spend_atomic"]), real)
    signed["min_tokens_atomic"] = result["upper_bound_tokens_atomic"]
    assert guard(buy, validity)["state"] == "pass"
    signed["min_tokens_atomic"] += 1
    assert guard(buy, validity)["state"] == "refuse"


@pytest.mark.parametrize("change", ["quote_mismatch", "sol_alias", "wrong_program", "wrong_own_ix",
    "future_v3", "wrong_pool", "wrong_mint", "raw_quote", "short_quote", "zero_quote",
    "complete", "startup", "bank_conflict", "source_missing"])
def test_quote_v2_incomplete_or_mismatched_proof_stays_unknown(change):
    tape, buy, validity = _quote_world(open_bank=True)
    signed = validity["entry_slippage"]
    key = signed["pool"]
    entry = tape._acc[key][-1]
    account, raw = dict(entry[3]), bytearray(entry[3]["data"])
    if change == "quote_mismatch": signed["quote_mint"] = curve.USDC_MINT
    elif change == "sol_alias": signed["quote_mint"] = curve.WSOL_MINT
    elif change == "wrong_program": signed["quote_token_program"] = curve.SYSTEM_PROGRAM
    elif change == "wrong_own_ix": signed["instruction_discriminator"] = curve.BUY_EXACT_SOL_IN_DISCRIMINATOR.hex()
    elif change == "future_v3": signed["instruction_discriminator"] = "e1f7501ed5b38488"
    elif change == "wrong_pool": signed["pool"] = accounts_for(curve.USDC_MINT)["curve"]
    elif change == "wrong_mint": buy.mint = curve.USDC_MINT
    elif change == "raw_quote": raw[83:115] = bytes(Pubkey.from_string(curve.USDC_MINT))
    elif change == "short_quote": raw = raw[:83]
    elif change == "zero_quote": struct.pack_into("<Q", raw, 16, 0)
    elif change == "complete": raw[48] = 1
    elif change == "startup": account["is_startup"] = True
    elif change == "bank_conflict": tape._bank_tainted.add(100)
    elif change == "source_missing": account["txn_signature"] = b"x" * 64
    account["data"] = bytes(raw)
    tape._acc[key] = ((entry[0], entry[1], entry[2], account),)
    assert _guard(tape, head=(100, 10.0))(buy, validity)["state"] == "unknown"


def _amm_quote_world(quote_key="SPCXxcqX", *, bad=False, open_bank=False):
    from test_slot_lane_pump_nonsol import BUYS, _facts
    frame = BUYS[quote_key]
    facts = _facts(frame).get(frame["pool"])
    event = amm.parse_buy_event(frame["buy_event_log"])
    base, quote = amm.post_trade_reserves(event)
    spend = amm.buy_event_gross_quote(event) // 10
    tape = tape_module.PoolTape()
    tape.reset("connect")
    _put(tape, frame["pool"], amm.AMM_PROGRAM, base64.b64decode(frame["pool_account_b64"]))
    for key, mint, program, amount in (
            (facts["pool_base_token_account"], facts["base_mint"], facts["base_token_program"],
             base // 10 if bad else base),
            (facts["pool_quote_token_account"], facts["quote_mint"], facts["quote_token_program"], quote)):
        raw = bytearray(165)
        raw[:32], raw[32:64] = bytes(Pubkey.from_string(mint)), bytes(Pubkey.from_string(frame["pool"]))
        struct.pack_into("<Q", raw, 64, amount)
        raw[108] = 1
        _put(tape, key, program, raw)
    tape.on_slot(99, 98, tape_module.SLOT_CREATED_BANK)
    tape.on_slot(100, 99, tape_module.SLOT_CREATED_BANK)
    tape.on_slot(101, 100, tape_module.SLOT_CREATED_BANK)
    buy = SimpleNamespace(venue="pump_amm", mint=None, pool=frame["pool"], slot=100,
        signature=frame["signature"], owner=event["user"], trade_event_log=frame["buy_event_log"])
    signed = dict(venue="pump_amm", program=amm.AMM_PROGRAM, mint=facts["base_mint"],
        pool=frame["pool"], quote_mint=facts["quote_mint"], pool_facts=facts,
        base_token_program=facts["base_token_program"], quote_token_program=facts["quote_token_program"],
        source_sig=buy.signature, source_slot=100, spend_atomic=spend,
        funding_prefix_disjoint=True,
        min_tokens_atomic=amm.expected_base_out(spend, base, quote, facts["virtual_quote"],
                                              amm.total_fee_bps(event)) * 7 // 10)
    if open_bank:
        tape.head = 100
        tape._parent.pop(101)
        tape._has_child.discard(100)
        tape._banks, tape._bank_tainted = {100: 0}, set()
        for key, ring in tuple(tape._acc.items()):
            slot, version, seeded, account = ring[-1]
            account = dict(account, bank_id=0, txn_signature=base58.b58decode(buy.signature), is_startup=False)
            tape._acc[key] = ((slot, version, seeded, account),)
    return tape, buy, {"entry_slippage": signed}


@pytest.mark.parametrize("quote_key", ["EPjFWdd5", "SPCXxcqX"])
@pytest.mark.parametrize("open_bank", [False, True])
@pytest.mark.parametrize("bad", [False, True])
def test_quote_token_amm_uses_bound_quote_atoms_and_the_existing_bank_proof(quote_key, open_bank, bad):
    tape, buy, validity = _amm_quote_world(quote_key, bad=bad, open_bank=open_bank)
    guard = _guard(tape, head=(100 if open_bank else 101, 10.0))
    result = guard(buy, validity)
    assert result["state"] == ("refuse" if bad else "pass"), result
    signed, facts = validity["entry_slippage"], validity["entry_slippage"]["pool_facts"]
    amounts = [struct.unpack_from("<Q", tape._acc[facts[key]][-1][3]["data"], 64)[0]
               for key in ("pool_base_token_account", "pool_quote_token_account")]
    assert result["upper_bound_tokens_atomic"] == amounts[0] * signed["spend_atomic"] // (
        amounts[1] + facts["virtual_quote"] + signed["spend_atomic"])
    signed["min_tokens_atomic"] = result["upper_bound_tokens_atomic"]
    assert guard(buy, validity)["state"] == "pass"
    signed["min_tokens_atomic"] += 1
    assert guard(buy, validity)["state"] == "refuse"


@pytest.mark.parametrize("proof", [None, False, 0, 1, "true"])
def test_quote_token_amm_requires_a_boolean_disjoint_funding_prefix(proof):
    tape, buy, validity = _amm_quote_world(bad=True)
    signed = validity["entry_slippage"]
    if proof is None:
        signed.pop("funding_prefix_disjoint")
    else:
        signed["funding_prefix_disjoint"] = proof
    assert _guard(tape)(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("change", ["actor", "source", "slot", "quote", "base_program", "quote_program",
    "pool_owner", "vault_owner", "vault_mint", "vault_pool", "bank_conflict", "source_missing"])
def test_quote_token_amm_keeps_source_vault_and_bank_mismatches_unknown(change):
    tape, buy, validity = _amm_quote_world(bad=True, open_bank=True)
    signed = validity["entry_slippage"]
    key = signed["pool_facts"]["pool_quote_token_account"]
    if change == "actor": buy.owner = curve.USDC_MINT
    elif change == "source": signed["source_sig"] = "wrong"
    elif change == "slot": signed["source_slot"] = 99
    elif change == "quote": signed["quote_mint"] = curve.USDC_MINT
    elif change == "base_program": signed["base_token_program"] = curve.SYSTEM_PROGRAM
    elif change == "quote_program": signed["quote_token_program"] = curve.TOKEN_PROGRAM
    elif change == "bank_conflict": tape._bank_tainted.add(100)
    else:
        if change == "pool_owner": key = signed["pool"]
        slot, version, seeded, account = tape._acc[key][-1]
        account, raw = dict(account), bytearray(account["data"])
        if change in ("pool_owner", "vault_owner"): account["owner"] = curve.SYSTEM_PROGRAM
        elif change == "vault_mint": raw[:32] = bytes(Pubkey.from_string(curve.USDC_MINT))
        elif change == "vault_pool": raw[32:64] = bytes(Pubkey.from_string(curve.USDC_MINT))
        elif change == "source_missing": account["txn_signature"] = b"x" * 64
        account["data"] = bytes(raw)
        tape._acc[key] = ((slot, version, seeded, account),)
    assert _guard(tape, head=(100, 10.0))(buy, validity)["state"] == "unknown"


def test_quote_token_amm_still_rechecks_tape_changes_during_modeling(monkeypatch):
    from sniper.services import memecoin_slot_slippage as module
    tape, buy, validity = _amm_quote_world(bad=True, open_bank=True)
    model = module._upper_bound
    def changed(accounts, signed):
        upper = model(accounts, signed)
        tape.provenance_revision += 1
        return upper
    monkeypatch.setattr(module, "_upper_bound", changed)
    assert _guard(tape, head=(100, 10.0))(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("token_quote", [False, True])
@pytest.mark.parametrize("open_bank", [False, True])
def test_a_proven_completed_curve_with_no_inventory_cannot_fill_the_signed_buy(token_quote, open_bank):
    tape, buy, validity = _quote_world(open_bank=open_bank) if token_quote else _world()
    signed = validity["entry_slippage"]
    if not token_quote and open_bank:
        tape.head = 100
        tape._parent.pop(101)
        tape._has_child.discard(100)
        tape._banks, tape._bank_tainted = {100: 0}, set()
        buy.signature = signed["source_sig"] = base58.b58encode(bytes(range(64))).decode()
    key = signed["pool"]
    entry = tape._acc[key][-1]
    account, raw = dict(entry[3]), bytearray(entry[3]["data"])
    raw[48] = 1
    struct.pack_into("<Q", raw, 24, 0)
    account["data"] = bytes(raw)
    if open_bank:
        account.update(bank_id=0, txn_signature=bytes(range(64)), is_startup=False)
    tape._acc[key] = ((entry[0], entry[1], entry[2], account),)
    result = _guard(tape, head=(100 if open_bank else 101, 10.0))(buy, validity)
    assert result["state"] == "refuse", result
    assert result["reason"] == "signed_minimum_unreachable"
    assert result["upper_bound_tokens_atomic"] == 0


def test_recorded_quote_exact_in_is_below_the_fee_free_bound_in_quote_units():
    tape, buy, validity = _quote_world(bad=False)
    event = curve.parse_trade_event(buy.trade_event_log)
    # Exact ordinary source receipt, reconstructed only for this recorded regression proof.
    signed = validity["entry_slippage"]
    def pretrade(raw):
        struct.pack_into("<3Q", raw, 8, event["virtual_token_reserves"] + event["token_amount"],
                         event["virtual_quote_reserves"] - event["quote_amount"],
                         event["real_token_reserves"] + event["token_amount"])
        return raw
    _curve_raw(tape, signed, pretrade)
    signed["min_tokens_atomic"] = event["token_amount"]
    result = _guard(tape)(buy, validity)
    assert result["state"] == "pass", result
    assert result["upper_bound_tokens_atomic"] >= event["token_amount"]


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
def test_a_frozen_source_slot_can_prove_the_exact_signed_minimum_impossible(venue):
    tape, buy, validity = _world(venue)
    result = _guard(tape)(buy, validity)
    assert result["state"] == "refuse"
    assert result["basis"] == "fee_free_upper_bound"
    assert 0 <= result["upper_bound_tokens_atomic"] < validity["entry_slippage"]["min_tokens_atomic"]
    assert result["state_slot"] == buy.slot
    assert result["source_slot"] == buy.slot
    assert result["spend_atomic"] == validity["entry_slippage"]["spend_atomic"]


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
def test_a_bound_above_or_equal_to_the_floor_is_not_an_executable_quote_or_veto(venue):
    tape, buy, validity = _world(venue, bad=False)
    guard = _guard(tape)
    first = guard(buy, validity)
    assert first["state"] == "pass"
    validity["entry_slippage"]["min_tokens_atomic"] = first["upper_bound_tokens_atomic"]
    assert guard(buy, validity)["state"] == "pass"


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
@pytest.mark.parametrize("change", ["gap", "unfrozen", "old_parent", "fork", "late_write", "unseen", "stale_head", "future_head", "tape_lag"])
def test_unknown_or_unordered_state_never_refuses_an_existing_send(venue, change):
    tape, buy, validity = _world(venue)
    head = (101, 10.0)
    if change == "gap":
        tape.reset("connect")
    elif change == "unfrozen":
        tape.head = 100
    elif change == "old_parent":
        buy.slot = validity["entry_slippage"]["source_slot"] = 101
    elif change == "fork":
        tape._parent[101] = 99
    elif change == "late_write":
        key = validity["entry_slippage"]["pool"]
        previous = tape._acc[key][-1][3]
        tape.on_account(key, previous["owner"], previous["lamports"], previous["data"], 100, 2)
    elif change == "unseen":
        tape._acc.pop(validity["entry_slippage"]["pool"])
    elif change == "stale_head":
        head = (101, 8.9)
    elif change == "future_head":
        head = (101, 10.1)
    elif change == "tape_lag":
        head = (104, 10.0)
    assert _guard(tape, head=head)(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("field,value", [("mint", "wrong"), ("pool", "wrong"), ("program", "wrong"),
    ("quote_mint", curve.USDC_MINT), ("spend_atomic", 0), ("spend_atomic", True),
    ("min_tokens_atomic", 0), ("source_sig", "different"), ("source_slot", 99),
    ("base_token_program", "unknown")])
def test_invalid_or_mismatched_signed_identity_is_unknown(field, value):
    tape, buy, validity = _world()
    validity["entry_slippage"][field] = value
    assert _guard(tape)(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
@pytest.mark.parametrize("change", ["short", "owner", "discriminator", "mode_or_vault_mint", "zero_reserve"])
def test_partial_or_unsupported_account_bytes_are_unknown(venue, change):
    tape, buy, validity = _world(venue)
    key = validity["entry_slippage"]["pool"]
    if venue == "pump_amm" and change in ("mode_or_vault_mint", "zero_reserve"):
        key = validity["entry_slippage"]["pool_facts"]["pool_base_token_account"]
    slot, version, seeded, account = tape._acc[key][-1]
    account = copy.deepcopy(account)
    raw = bytearray(account["data"])
    if change == "short":
        raw = raw[:12]
    elif change == "owner":
        account["owner"] = curve.SYSTEM_PROGRAM
    elif change == "discriminator":
        raw[0] ^= 1
    elif change == "mode_or_vault_mint":
        if venue == "pump_curve":
            raw[82] = 2  # not a serialized Anchor bool
        else:
            raw[:32] = bytes(Pubkey.from_string(curve.USDC_MINT))
    elif change == "zero_reserve":
        struct.pack_into("<Q", raw, 8 if venue == "pump_curve" else 64, 0)
    account["data"] = bytes(raw)
    tape._acc[key] = ((slot, version, seeded, account),)
    assert _guard(tape)(buy, validity)["state"] == "unknown"


def test_the_guard_does_not_take_a_writer_lock_or_mutate_the_shared_cache():
    tape, buy, validity = _world()
    before = copy.deepcopy((tape.stats, tape._acc))
    with tape._write_lock:
        assert _guard(tape)(buy, validity)["state"] == "refuse"
    assert (tape.stats, tape._acc) == before


def test_a_reconnect_during_parsing_cannot_return_a_veto(monkeypatch):
    tape, buy, validity = _world()
    guard = _guard(tape)
    original = tape._lookup
    def reset_after_lookup(*args, **kwargs):
        result = original(*args, **kwargs)
        tape.reset("connect")
        return result
    monkeypatch.setattr(tape, "_lookup", reset_after_lookup)
    assert guard(buy, validity)["state"] == "unknown"


def test_a_missing_optional_tape_is_unknown():
    _, buy, validity = _world()
    assert _guard(None)(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
def test_token_2022_does_not_require_a_mint_read_for_the_fee_free_program_output_bound(venue):
    tape, buy, validity = _world(venue)
    validity["entry_slippage"]["base_token_program"] = curve.TOKEN_2022_PROGRAM
    if venue == "pump_curve":
        buy.token_program = curve.TOKEN_2022_PROGRAM
    else:
        key = validity["entry_slippage"]["pool_facts"]["pool_base_token_account"]
        tape._acc[key][-1][3]["owner"] = curve.TOKEN_2022_PROGRAM
    assert _guard(tape)(buy, validity)["state"] == "refuse"


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
def test_full_input_gross_bound_never_undershoots_the_existing_net_after_fee_models(venue):
    from sniper.services.memecoin_slot_slippage import _upper_bound
    tape, buy, validity = _world(venue, bad=False)
    signed = validity["entry_slippage"]
    accounts, _ = tape.lookup(list(tape._acc), 101)
    for spend in (1, 2, 7, 1_000_000, 30_000_000, 2**63):
        signed["spend_atomic"] = spend
        upper = _upper_bound(accounts, signed)
        for fee in (0, 1, 125, 1500):
            if venue == "pump_curve":
                event = curve.parse_trade_event(buy.trade_event_log)
                event.update(fee_basis_points=fee, creator_fee_basis_points=fee,
                             cashback_fee_basis_points=0)
                modeled = curve.expected_tokens_out(spend, event)
            else:
                event = amm.parse_buy_event(buy.trade_event_log)
                base, quote = amm.post_trade_reserves(event)
                modeled = amm.expected_base_out(spend, base, quote, signed["pool_facts"]["virtual_quote"], fee)
            assert upper >= modeled


def test_an_account_replacement_during_modeling_is_unknown(monkeypatch):
    from sniper.services import memecoin_slot_slippage as guard_module
    tape, buy, validity = _world()
    model = guard_module._upper_bound
    def replace_after_model(accounts, signed):
        upper = model(accounts, signed)
        tape._acc[signed["pool"]] = tuple(list(tape._acc[signed["pool"]]))
        return upper
    monkeypatch.setattr(guard_module, "_upper_bound", replace_after_model)
    assert _guard(tape)(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
def test_a_selected_rpc_seed_cannot_prove_continuous_reserve_coverage(venue):
    tape, buy, validity = _world(venue)
    key = validity["entry_slippage"]["pool"]
    if venue == "pump_amm":
        key = validity["entry_slippage"]["pool_facts"]["pool_base_token_account"]
    slot, _, _, account = tape._acc[key][-1]
    tape._acc[key] = ((slot, -1, True, account),)
    result = _guard(tape)(buy, validity)
    assert result == {"state": "unknown", "reason": "seeded_reserve_uncovered"}


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
def test_a_newer_stream_entry_is_eligible_even_with_an_older_seed_in_the_ring(venue):
    tape, buy, validity = _world(venue)
    key = validity["entry_slippage"]["pool"]
    slot, _, _, account = tape._acc[key][-1]
    tape._acc[key] = ((slot - 1, -1, True, copy.deepcopy(account)), tape._acc[key][-1])
    assert _guard(tape)(buy, validity)["state"] == "refuse"


@pytest.mark.parametrize("kind", ["no_coverage", "pre_source"])
def test_ineligible_fast_misses_do_not_parse_or_derive_the_source(monkeypatch, kind):
    from sniper.services import memecoin_slot_slippage as guard_module
    tape, buy, validity = _world()
    if kind == "no_coverage":
        tape.connected = False
    else:
        tape.head = 100  # only slot99 is frozen, before source S100
    called = []
    def forbidden(*_args):
        called.append(True)
        raise AssertionError("fast miss must not parse source or derive PDAs")
    monkeypatch.setattr(guard_module, "_reference", forbidden)
    assert _guard(tape)(buy, validity)["state"] == "unknown"
    assert called == []


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
def test_a_tape_one_slot_behind_the_observed_head_cannot_veto_a_recovered_pool(venue):
    tape, buy, validity = _world(venue)
    assert _guard(tape, head=(102, 10.0))(buy, validity) == {"state": "unknown", "reason": "tape_lag"}


def _curve_raw(tape, signed, change):
    key = signed["pool"]
    slot, version, seeded, account = tape._acc[key][-1]
    account = copy.deepcopy(account)
    account["data"] = bytes(change(bytearray(account["data"])))
    tape._acc[key] = ((slot, version, seeded, account),)


def _source_modes(buy, *, mayhem=False, cashback=0, holder=0, ix_name=None):
    """Change real serialized event fields, so production's strict decoder remains exercised."""
    event = curve.parse_trade_event(buy.trade_event_log)
    raw = bytearray(base64.b64decode(buy.trade_event_log.split("Program data: ", 1)[1]))
    name = event["ix_name"].encode()
    at = raw.index(struct.pack("<I", len(name)) + name)
    if ix_name is not None:
        raw[at:at + 4 + len(name)] = struct.pack("<I", len(ix_name)) + ix_name.encode()
        name = ix_name.encode()
    after_name = at + 4 + len(name)
    raw[after_name] = int(mayhem)
    struct.pack_into("<Q", raw, after_name + 1, cashback)
    holder_at = after_name + 1 + 4 * 8 + 4 + len(event["shareholders"]) * 34 + 32 + 3 * 8
    struct.pack_into("<Q", raw, holder_at, holder)
    buy.trade_event_log = "Program data: " + base64.b64encode(raw).decode()


@pytest.mark.parametrize("flags", [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 1)])
def test_known_curve_fee_modes_preserve_the_signed_floor_boundary(flags):
    tape, buy, validity = _world()
    signed = validity["entry_slippage"]
    def modes(raw):
        raw[81], raw[82], raw[124] = flags
        return raw
    _curve_raw(tape, signed, modes)
    _source_modes(buy, mayhem=bool(flags[0]), cashback=30 * flags[1], holder=30 * flags[2])
    event = curve.parse_trade_event(buy.trade_event_log)
    assert (event["mayhem_mode"], event["cashback_fee_basis_points"], event["holder_rewards_bps"]) == (
        bool(flags[0]), 30 * flags[1], 30 * flags[2])
    guard = _guard(tape)
    result = guard(buy, validity)
    assert result["state"] == "refuse"
    upper = result["upper_bound_tokens_atomic"]
    signed["min_tokens_atomic"] = upper
    assert guard(buy, validity)["state"] == "pass"
    signed["min_tokens_atomic"] = upper + 1
    assert guard(buy, validity)["state"] == "refuse"


@pytest.mark.parametrize("length", [49, 81, 82, 83, 115, 123, 124, 125, 126, 151, 166])
def test_complete_required_curve_prefixes_do_not_require_unrelated_trailing_allocations(length):
    tape, buy, validity = _world()
    _curve_raw(tape, validity["entry_slippage"], lambda raw: raw[:length])
    assert _guard(tape)(buy, validity)["state"] == "refuse"


@pytest.mark.parametrize("length", [48, 50, 80, 84, 114, 116, 122])
def test_truncated_required_curve_fields_are_unknown(length):
    tape, buy, validity = _world()
    _curve_raw(tape, validity["entry_slippage"], lambda raw: raw[:length])
    assert _guard(tape)(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("offset", [48, 81, 82, 123, 124])
def test_invalid_serialized_curve_booleans_are_unknown(offset):
    tape, buy, validity = _world()
    def malformed(raw):
        raw[offset] = 2
        return raw
    _curve_raw(tape, validity["entry_slippage"], malformed)
    assert _guard(tape)(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("marker", [None, "0000000000000000"])
def test_a_missing_or_future_own_instruction_cannot_borrow_the_legacy_inventory_bound(marker):
    tape, buy, validity = _world()
    validity["entry_slippage"]["instruction_discriminator"] = marker
    assert _guard(tape)(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("real_tokens", [0, 1])
def test_zero_real_quote_is_valid_and_legacy_real_inventory_still_caps_a_v3_source(real_tokens):
    tape, buy, validity = _world()
    def reserves(raw):
        struct.pack_into("<2Q", raw, 24, real_tokens, 0)
        return raw
    _curve_raw(tape, validity["entry_slippage"], reserves)
    _source_modes(buy, mayhem=True, ix_name="buy_exact_quote_in_v3")
    result = _guard(tape)(buy, validity)
    assert result["state"] == "refuse"
    assert result["upper_bound_tokens_atomic"] == real_tokens


@pytest.mark.parametrize("index", [0, 15, 18, 28])
def test_recorded_125_and_151_byte_curves_have_sufficient_bound_state(index):
    frames = json.loads((FIX / "memecoin/pump_fee_config_frames.json").read_text())
    event = curve.parse_trade_event(frames["curve_frames"][index]["line"])
    raw = base64.b64decode(frames["curve_accounts_b64"][event["mint"]])
    assert len(raw) in (125, 151)
    tape, buy, validity = _world()
    signed = validity["entry_slippage"]
    slot, version, seeded, account = tape._acc.pop(signed["pool"])[-1]
    account = dict(account, data=raw)
    signed.update(mint=event["mint"], pool=accounts_for(event["mint"])["curve"], min_tokens_atomic=2**64 - 1)
    buy.mint, buy.owner = event["mint"], event["user"]
    buy.trade_event_log = frames["curve_frames"][index]["line"]
    # Establish the fixture's covered snapshot, rather than injecting a late frozen-bank write.
    tape._acc[signed["pool"]] = ((slot, version, seeded, account),)
    assert _guard(tape)(buy, validity)["state"] == "refuse"


def test_ordinary_recorded_exact_sol_buys_respect_the_gross_bound_including_fee_modes():
    from sniper.services.memecoin_slot_slippage import _upper_bound
    records = json.loads((FIX / "memecoin/rabbit_curve_buys_20261016.json").read_text())["direct"]
    records.append(json.loads((FIX / "pump_curve_buy_frames.json").read_text())["direct_buy_exact_sol_in"])
    modes = set()
    for frame in records:
        event = curve.parse_trade_event(frame["trade_event_log"])
        data = bytes.fromhex(frame["data_hex"])
        assert data[:8] == curve.BUY_EXACT_SOL_IN_DISCRIMINATOR
        assert Pubkey.from_string(event["user"]).is_on_curve()
        modes.add((event["mayhem_mode"], event["cashback_fee_basis_points"] > 0,
                   event["holder_rewards_bps"] > 0))
        spend = struct.unpack_from("<Q", data, 8)[0]
        raw = bytearray(125)
        raw[:8] = bytes.fromhex("17b7f83760d8ac60")
        # These ordinary exact-in events expose post-trade reserves. Never apply this inverse
        # reconstruction to the privileged Mayhem agent's special exact-out source events.
        struct.pack_into("<4Q", raw, 8, event["virtual_token_reserves"] + event["token_amount"],
                         event["virtual_sol_reserves"] - event["sol_amount"],
                         event["real_token_reserves"] + event["token_amount"],
                         event["real_sol_reserves"] - event["sol_amount"])
        raw[81], raw[82], raw[124] = map(int, (event["mayhem_mode"],
            event["cashback_fee_basis_points"] > 0, event["holder_rewards_bps"] > 0))
        signed = dict(venue="pump_curve", pool="pool", spend_atomic=spend)
        accounts = {"pool": {"owner": curve.PUMP_PROGRAM,
                            "data": [base64.b64encode(raw).decode(), "base64"]}}
        assert _upper_bound(accounts, signed) >= event["token_amount"]
    assert {(True, False, False), (False, True, False), (False, False, True)} <= modes


def test_counterexample_mayhem_agent_is_an_off_curve_pda_not_an_ordinary_exact_in_signer():
    frames = json.loads((FIX / "memecoin/pump_fee_config_frames.json").read_text())["curve_frames"]
    from solders.keypair import Keypair
    agent = "BwWK17cbHxwWBKZkUYvzxLcNQ1YVyaFezduWbtm2de6s"
    assert not Pubkey.from_string(agent).is_on_curve()
    assert Keypair.from_seed(bytes(32)).pubkey().is_on_curve()
    for index in (0, 2, 22):
        event = curve.parse_trade_event(frames[index]["line"])
        assert event["user"] == agent and event["ix_name"] == "buy"
        assert event["fee_basis_points"] == event["creator_fee_basis_points"] == 0


@pytest.mark.parametrize("effective_quote", [0, 2**64, 2**64 - 1])
def test_amm_effective_quote_reserves_stay_in_the_program_u64_domain(effective_quote):
    tape, buy, validity = _world("pump_amm")
    signed = validity["entry_slippage"]
    key = signed["pool"]
    slot, version, seeded, account = tape._acc[key][-1]
    account = copy.deepcopy(account)
    raw = bytearray(account["data"])
    quote = struct.unpack_from("<Q", tape._acc[signed["pool_facts"]["pool_quote_token_account"]][-1][3]["data"], 64)[0]
    raw[245:261] = (effective_quote - quote).to_bytes(16, "little", signed=True)
    account["data"] = bytes(raw)
    tape._acc[key] = ((slot, version, seeded, account),)
    result = _guard(tape)(buy, validity)
    assert result["state"] == ("refuse" if 0 < effective_quote < 2**64 else "unknown")


SOURCE_TX = bytes(range(64))
LATER_TX = bytes(reversed(range(64)))


def _open_world(venue="pump_curve", *, bad=True, candidate=True):
    """Source and later atomic transaction cuts in the SAME still-open bank (bank ID zero)."""
    tape, buy, validity = _world(venue, bad=False)
    later, _, _ = _world(venue, bad=bad)
    tape.head = 100
    tape._parent.pop(101)
    tape._has_child.discard(100)
    tape._banks, tape._bank_tainted, tape.provenance_revision = {100: 0}, set(), 0
    buy.signature = validity["entry_slippage"]["source_sig"] = base58.b58encode(SOURCE_TX).decode()
    for index, key in enumerate(tape._acc):
        first = dict(tape._acc[key][-1][3], txn_signature=SOURCE_TX, bank_id=0, is_startup=False)
        second = dict(later._acc[key][-1][3], txn_signature=LATER_TX, bank_id=0, is_startup=False)
        tape._acc[key] = ((100, index, False, first),)
        if candidate:
            tape._acc[key] += ((100, index + 10, False, second),)
    return tape, buy, validity


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
@pytest.mark.parametrize("bad", [False, True])
def test_complete_same_slot_transaction_cut_uses_newer_state_without_waiting_for_a_child(venue, bad):
    tape, buy, validity = _open_world(venue, bad=bad)
    guard = _guard(tape, head=(99, 10.0))  # this tape can lead the independently observed lane head
    result = guard(buy, validity)
    assert result["state"] == ("refuse" if bad else "pass")
    assert result["witness"] == "open_transaction"
    assert result["state_slot"] == buy.slot == tape.head
    assert result["state_bank_id"] == 0
    assert result["state_tx_signature_hex"] == LATER_TX.hex()
    assert guard.stats["open_slot_checks"] == guard.stats["open_slot_hits"] == 1
    assert sum(guard.stats["open_slot_misses"].values()) == 0


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
def test_source_group_is_valid_but_does_not_invent_a_later_unfillable_state(venue):
    tape, buy, validity = _open_world(venue, candidate=False)
    result = _guard(tape, head=(100, 10.0))(buy, validity)
    assert result["state"] == "pass"
    assert result["state_tx_signature_hex"] == SOURCE_TX.hex()


def _streamed_open_world(venue, *, bad=True, omit_source_key=None):
    template, buy, validity = _open_world(venue, bad=bad)
    tape = tape_module.PoolTape()
    tape.reset("connect")
    tape.on_slot(99, 98, tape_module.SLOT_CREATED_BANK, bank_id=0)
    tape.on_slot(100, 99, tape_module.SLOT_CREATED_BANK, bank_id=0)
    for key, ring in template._acc.items():
        if key != omit_source_key:
            account = ring[0][3]
            tape.on_account(key, account["owner"], account["lamports"], account["data"], 100, 1,
                            txn_signature=SOURCE_TX, bank_id=0)
    for version in range(2, tape.RING + 3):
        for key, ring in template._acc.items():
            account = ring[-1][3]
            tape.on_account(key, account["owner"], account["lamports"], account["data"], 100, version,
                            txn_signature=bytes([version]) * 64, bank_id=0)
    return tape, buy, validity


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
@pytest.mark.parametrize("bad", [False, True])
def test_source_proof_survives_raw_ring_rotation_without_waiting(venue, bad):
    tape, buy, validity = _streamed_open_world(venue, bad=bad)
    assert all(len(ring) == tape.RING and all(entry[3]["txn_signature"] != SOURCE_TX
               for entry in ring) for ring in tape._acc.values())
    result = _guard(tape, head=(100, 10.0))(buy, validity)
    assert result["state"] == ("refuse" if bad else "pass")
    assert result["source_anchor_basis"] == "retained_group"
    assert result["witness"] == "open_transaction"


def test_retained_partial_source_cannot_certify_a_complete_newer_amm_group():
    _, _, validity = _open_world("pump_amm")
    missing = validity["entry_slippage"]["pool_facts"]["pool_quote_token_account"]
    tape, buy, validity = _streamed_open_world("pump_amm", omit_source_key=missing)
    result = _guard(tape, head=(100, 10.0))(buy, validity)
    assert result["state"] == "unknown"
    assert result["reason"] == "open_source_unproved"
    assert result["source_anchor_basis"] == "unproved"
    assert result["source_anchor_missing_key"] == missing


def test_retained_source_group_changed_during_modeling_is_unknown(monkeypatch):
    tape, buy, validity = _streamed_open_world("pump_amm")
    module = importlib.import_module("sniper.services.memecoin_slot_slippage")
    assert _guard(tape, head=(100, 10.0))(buy, validity)["state"] == "refuse"
    original = module._upper_bound
    def invalidate(accounts, signed):
        tape._provenance_groups.clear()
        return original(accounts, signed)
    monkeypatch.setattr(module, "_upper_bound", invalidate)
    assert _guard(tape, head=(100, 10.0))(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("change", ["bank", "startup", "seed", "signature", "slot", "newer_version", "bool_version"])
def test_retained_anchor_keeps_every_source_provenance_and_order_check(change):
    tape, buy, validity = _streamed_open_world("pump_amm")
    guard = _guard(tape, head=(100, 10.0))
    assert guard(buy, validity)["state"] == "refuse"
    group_key = (100, 0, SOURCE_TX)
    group = tape._provenance_groups[group_key]
    key, entry = group[0]
    slot, version, seeded, account = entry
    account = dict(account)
    if change == "bank": account["bank_id"] = 1
    elif change == "startup": account["is_startup"] = True
    elif change == "seed": seeded = True
    elif change == "signature": account["txn_signature"] = b"x" * 64
    elif change == "slot": slot = 99
    elif change == "newer_version": version = 999
    elif change == "bool_version": version = True
    tape._provenance_groups[group_key] = ((key, (slot, version, seeded, account)),) + group[1:]
    assert guard(buy, validity)["state"] == "unknown"


def test_newer_partial_update_cannot_reuse_complete_retained_amm_state():
    tape, buy, validity = _streamed_open_world("pump_amm")
    guard = _guard(tape, head=(100, 10.0))
    assert guard(buy, validity)["state"] == "refuse"
    key = validity["entry_slippage"]["pool_facts"]["pool_base_token_account"]
    account = tape._acc[key][-1][3]
    tape.on_account(key, account["owner"], account["lamports"], account["data"], 100, 7,
                    txn_signature=b"p" * 64, bank_id=0)
    assert guard(buy, validity)["state"] == "unknown"


def test_replayed_changed_source_payload_taints_even_after_raw_ring_rotation():
    tape, buy, validity = _streamed_open_world("pump_amm")
    guard = _guard(tape, head=(100, 10.0))
    assert guard(buy, validity)["state"] == "refuse"
    key, entry = tape._provenance_groups[(100, 0, SOURCE_TX)][0]
    account = entry[3]
    tape.on_account(key, account["owner"], account["lamports"] + 1, account["data"], 100, entry[1],
                    txn_signature=SOURCE_TX, bank_id=0)
    assert guard(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
def test_open_transaction_bound_equality_passes_and_one_atom_above_refuses(venue):
    tape, buy, validity = _open_world(venue)
    guard = _guard(tape, head=(100, 10.0))
    upper = guard(buy, validity)["upper_bound_tokens_atomic"]
    validity["entry_slippage"]["min_tokens_atomic"] = upper
    assert guard(buy, validity)["state"] == "pass"
    validity["entry_slippage"]["min_tokens_atomic"] = upper + 1
    assert guard(buy, validity)["state"] == "refuse"


@pytest.mark.parametrize("change", ["bank_missing", "bank_bool", "bank_other", "dead_or_conflict",
    "sig_missing", "sig_short", "sig_mutable", "bank_provenance_missing", "bank_provenance_bool",
    "bank_provenance_other", "startup", "startup_unknown", "seed", "source_missing", "source_evicted",
    "source_bank_other", "source_startup", "source_seed", "source_version_newer", "source_sig_invalid"])
def test_unproved_open_slot_provenance_never_vetoes(change):
    tape, buy, validity = _open_world("pump_amm")
    key = validity["entry_slippage"]["pool"]
    source, latest = tape._acc[key]
    account, first = dict(latest[3]), dict(source[3])
    if change == "bank_missing": tape._banks.clear()
    elif change == "bank_bool": tape._banks[100] = True
    elif change == "bank_other": tape._banks[100] = 1
    elif change == "dead_or_conflict": tape._bank_tainted.add(100)
    elif change == "sig_missing": account.pop("txn_signature")
    elif change == "sig_short": account["txn_signature"] = b"short"
    elif change == "sig_mutable": account["txn_signature"] = bytearray(LATER_TX)
    elif change == "bank_provenance_missing": account.pop("bank_id")
    elif change == "bank_provenance_bool": account["bank_id"] = False
    elif change == "bank_provenance_other": account["bank_id"] = 1
    elif change == "startup": account["is_startup"] = True
    elif change == "startup_unknown": account.pop("is_startup")
    elif change == "seed": latest = (latest[0], latest[1], True, latest[3])
    elif change == "source_missing": first["txn_signature"] = b"x" * 64
    elif change == "source_evicted": source = None
    elif change == "source_bank_other": first["bank_id"] = 1
    elif change == "source_startup": first["is_startup"] = True
    elif change == "source_seed": source = (source[0], source[1], True, source[3])
    elif change == "source_version_newer": source = (source[0], latest[1] + 1, False, source[3])
    elif change == "source_sig_invalid": buy.signature = validity["entry_slippage"]["source_sig"] = "bad"
    tape._acc[key] = (() if source is None else ((source[0], source[1], source[2], first),)) + (
        (latest[0], latest[1], latest[2], account),)
    guard = _guard(tape, head=(100, 10.0))
    assert guard(buy, validity)["state"] == "unknown"
    assert guard.stats["open_slot_hits"] == 0
    assert guard.stats["open_slot_checks"] == sum(guard.stats["open_slot_misses"].values()) == 1


@pytest.mark.parametrize("change", ["mixed_signature", "newer_partial", "future_account_slot", "old_vq"])
def test_a_newer_partial_or_mixed_group_cannot_reuse_a_historical_complete_amm_group(change):
    tape, buy, validity = _open_world("pump_amm")
    signed = validity["entry_slippage"]
    key = signed["pool_facts"]["pool_base_token_account"]
    if change == "old_vq":
        tape._acc[signed["pool"]] = tape._acc[signed["pool"]][:1]
    else:
        slot, version, seeded, account = tape._acc[key][-1]
        account = dict(account, txn_signature=b"u" * 64)
        entry = (slot + (change == "future_account_slot"), version + 10, seeded, account)
        tape._acc[key] = tape._acc[key] + (entry,) if change == "newer_partial" else (tape._acc[key][0], entry)
    assert _guard(tape, head=(100, 10.0))(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
def test_newer_open_writes_make_an_older_frozen_veto_unknown_even_when_source_bank_is_frozen(venue):
    tape, buy, validity = _world(venue)
    # Parent100 is already frozen and bad; a later bank's partial write might have recovered it.
    key = validity["entry_slippage"]["pool"]
    old = tape._acc[key][-1][3]
    account = dict(old, txn_signature=LATER_TX, bank_id=1, is_startup=False)
    tape._acc[key] += ((101, 20, False, account),)
    tape._banks, tape._bank_tainted, tape.provenance_revision = {100: 0, 101: 1}, set(), 0
    assert _guard(tape)(buy, validity)["state"] == "unknown"


@pytest.mark.parametrize("change", ["reset", "conflict", "later_write", "new_head"])
def test_an_open_witness_invalidated_during_modeling_is_unknown(monkeypatch, change):
    from sniper.services import memecoin_slot_slippage as module
    tape, buy, validity = _open_world("pump_amm")
    model = module._upper_bound
    def during_model(accounts, signed):
        result = model(accounts, signed)
        if change == "reset": tape.reset("connect")
        elif change == "conflict":
            tape._bank_tainted.add(100)
            tape.provenance_revision += 1
        elif change == "later_write":
            key = signed["pool"]
            tape._acc[key] = tuple(list(tape._acc[key]))
        elif change == "new_head": tape.head = 101
        return result
    monkeypatch.setattr(module, "_upper_bound", during_model)
    assert _guard(tape, head=(100, 10.0))(buy, validity)["state"] == "unknown"


def test_open_cache_miss_remains_cheap_and_does_not_parse_the_source(monkeypatch):
    from sniper.services import memecoin_slot_slippage as module
    tape, buy, validity = _open_world("pump_amm")
    tape._acc.pop(validity["entry_slippage"]["pool"])
    calls = []
    def forbidden(*_args):
        calls.append(True)
        raise AssertionError("missing open group must not parse source")
    monkeypatch.setattr(module, "_reference", forbidden)
    assert _guard(tape, head=(100, 10.0))(buy, validity)["state"] == "unknown"
    assert calls == []


def test_open_group_reader_takes_no_writer_lock_and_does_not_mutate_shared_tape():
    tape, buy, validity = _open_world("pump_amm")
    before = copy.deepcopy((tape._acc, tape.stats, tape._banks, tape._bank_tainted))
    with tape._write_lock:
        assert _guard(tape, head=(100, 10.0))(buy, validity)["state"] == "refuse"
    assert (tape._acc, tape.stats, tape._banks, tape._bank_tainted) == before


@pytest.mark.parametrize("venue", ["pump_curve", "pump_amm"])
@pytest.mark.parametrize("tainted_slot", [100, 101])
def test_frozen_veto_rejects_guard_only_ambiguity_in_its_ancestry_without_disabling_preparation(venue, tainted_slot):
    tape, buy, validity = _world(venue)
    tape.on_slot(102, 101, tape_module.SLOT_CREATED_BANK)
    tape.on_slot(103, 102, tape_module.SLOT_CREATED_BANK)
    tape._bank_tainted.add(tainted_slot)
    key = validity["entry_slippage"]["pool"]
    accounts, parent = tape.lookup([key], 103)
    assert parent == 102 and accounts[key] is not None and not tape._tainted
    result = _guard(tape, head=(103, 10.0))(buy, validity)
    assert result["state"] == "unknown"


def test_guard_still_discards_metadata_ambiguity_arriving_during_frozen_modeling(monkeypatch):
    from sniper.services import memecoin_slot_slippage as module
    tape, buy, validity = _world()
    original = module._upper_bound
    def metadata_conflict(accounts, signed):
        upper = original(accounts, signed)
        tape._bank_tainted.add(100)
        tape.provenance_revision += 1
        return upper
    monkeypatch.setattr(module, "_upper_bound", metadata_conflict)
    assert _guard(tape)(buy, validity)["state"] == "unknown"
    assert not tape._tainted

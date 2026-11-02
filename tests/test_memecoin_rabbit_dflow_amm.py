"""DFlow -> pump_amm pre-execution decoder (Alpha GO 2026-10-17; memecoin_rabbit_dflow_amm).

Fixtures: tests/fixtures/memecoin/dflow_pump_amm_frames.json, real armed-wallet source transactions
(getTransaction json, maxSupportedTransactionVersion 1, research key) turned into the pre-execution
candidate Lambda's seam delivers (account keys, writable flags, signers, top-level instructions; no
logs, no balances), plus each pool's account (its virtual_quote). The read state is built from the
source's OWN executed BuyEvent pre-reserves and fees, so the model must reproduce what the pool paid.
"""
import base64
import json
import struct
from pathlib import Path

import base58
import pytest

from sniper.execution import pump_fees
from sniper.execution.pump_curve_buy import TOKEN_2022_PROGRAM, TOKEN_PROGRAM, WSOL_MINT
from sniper.execution.pumpswap_buy import BUY_EVENT_DISCRIMINATOR, parse_buy_event
from sniper.services import memecoin_rabbit_dflow_amm as dfa
from sniper.settings import memecoin as cfg

FIX = json.loads((Path(__file__).parent / "fixtures/memecoin/dflow_pump_amm_frames.json").read_text())
AMM = dfa.AMM_PROGRAM


@pytest.fixture(autouse=True)
def knob_on(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM", True)


def candidate(tx, *, slot=None, drop_writable=None, owner_index=1):
    """The pre-execution candidate of one executed transaction: what a frame carries."""
    msg, meta = tx["transaction"]["message"], tx["meta"]
    loaded_w, loaded_r = meta["loadedAddresses"]["writable"], meta["loadedAddresses"]["readonly"]
    keys = msg["accountKeys"] + loaded_w + loaded_r
    h, ns, nstatic = msg["header"], msg["header"]["numRequiredSignatures"], len(msg["accountKeys"])

    def writable(i):
        if i < ns:
            return i < ns - h["numReadonlySignedAccounts"]
        if i < nstatic:
            return i < nstatic - h["numReadonlyUnsignedAccounts"]
        return i < nstatic + len(loaded_w)
    flags = [writable(i) and keys[i] != drop_writable for i in range(len(keys))]
    ixs = tuple((keys[ix["programIdIndex"]], tuple(keys[a] for a in ix["accounts"]), base58.b58decode(ix["data"]))
                for ix in msg["instructions"])
    return dict(signature=tx["transaction"]["signatures"][0], owner=keys[owner_index],
                slot=tx["slot"] if slot is None else slot, recv_monotonic=1.0, generation=1, feed="preconf:test",
                account_keys=tuple(keys), instructions=ixs, version="v1" if tx.get("version") == 1 else "v0",
                signers=tuple(keys[:ns]), writable=tuple(flags), config=None)


def executed_event(tx):
    """The source's executed PumpSwap BuyEvent (its CPI event in the inner instructions)."""
    keys = (tx["transaction"]["message"]["accountKeys"] + tx["meta"]["loadedAddresses"]["writable"]
            + tx["meta"]["loadedAddresses"]["readonly"])
    for group in tx["meta"]["innerInstructions"]:
        for ix in group["instructions"]:
            raw = base58.b58decode(ix["data"])
            if keys[ix["programIdIndex"]] == AMM and raw[8:16] == BUY_EVENT_DISCRIMINATOR:
                return parse_buy_event("Program data: " + base64.b64encode(raw[8:]).decode())
    raise AssertionError("no BuyEvent")


def _token_account(mint, owner, amount, program):
    from solders.pubkey import Pubkey
    raw = bytes(Pubkey.from_string(mint)) + bytes(Pubkey.from_string(owner)) + struct.pack("<Q", amount) + bytes(93)
    return {"owner": program, "data": [base64.b64encode(raw).decode(), "base64"]}


def _fee_config(lp, protocol, creator):
    raw = (pump_fees.FEE_CONFIG_DISCRIMINATOR + b"\x01" + bytes(32) + struct.pack("<3Q", lp, protocol, creator)
           + struct.pack("<I", 1) + struct.pack("<QQ", 0, 0) + struct.pack("<3Q", lp, protocol, creator))
    return {"owner": "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ", "data": [base64.b64encode(raw).decode(), "base64"]}


def trade_time_virtual_quote(ev):
    """The pool's virtual quote AT the trade: the value for which buy_v2's own charge
    ceil((vault quote + virtual) x out / (base - out)) equals the executed net quote. The captured pool
    account is today's, and the virtual quote is a live field (10.0 SOL at one trade, 17.5 SOL now)."""
    base, quote, out = int(ev["pool_base_token_reserves"]), int(ev["pool_quote_token_reserves"]), int(ev["base_amount_out"])
    net = int(ev["quote_amount_in"])
    guess = base * net // out - net - quote
    hits = [vq for vq in range(guess - 5000, guess + 5000) if -(-(quote + vq) * out // (base - out)) == net]
    assert hits, "no virtual quote reproduces the executed charge"
    return hits[len(hits) // 2]


def _pool_at_trade(pool_account, vq):
    from sniper.execution.pumpswap_buy import POOL_VIRTUAL_QUOTE_OFFSET
    raw = bytearray(base64.b64decode(pool_account["data"][0]))
    raw[POOL_VIRTUAL_QUOTE_OFFSET:POOL_VIRTUAL_QUOTE_OFFSET + 16] = (
        vq.to_bytes(16, "little", signed=True))
    return {"owner": pool_account["owner"], "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}


def state_for(tx, plan, *, read_slot, held=0, fee=None):
    """The one read, as it would have answered just before the source: the pool as it stood at the
    trade, the vaults at the source's own pre-trade reserves, the fee tier the source paid."""
    ev = executed_event(tx)
    program = FIX["mints"][plan["mint"]]["owner"]
    lp, proto, creator = fee or (int(ev["lp_fee_basis_points"]), int(ev["protocol_fee_basis_points"]),
                                 int(ev["coin_creator_fee_basis_points"]))
    pool = _pool_at_trade(FIX["pools"][plan["pool"]], trade_time_virtual_quote(ev))
    accounts = {plan["pool"]: pool, plan["mint"]: FIX["mints"][plan["mint"]],
                plan["fee_config"]: _fee_config(lp, proto, creator),
                plan["quote_vault"]: _token_account(WSOL_MINT, plan["pool"], int(ev["pool_quote_token_reserves"]), TOKEN_PROGRAM),
                plan["base_vaults"][program]: _token_account(plan["mint"], plan["pool"], int(ev["pool_base_token_reserves"]), program),
                plan["holders"][program]: (_token_account(plan["mint"], plan["owner"], held, program) if held else None)}
    other = TOKEN_2022_PROGRAM if program == TOKEN_PROGRAM else TOKEN_PROGRAM
    accounts.setdefault(plan["base_vaults"][other], None)
    accounts.setdefault(plan["holders"][other], None)
    return {"slot": read_slot, "accounts": accounts}


def handler(alerts=None):
    sink = [] if alerts is None else alerts
    return dfa.DflowAmmHandler(read_fn=lambda m, p: None, alert_fn=lambda t, x: sink.append(x))


@pytest.mark.parametrize("name", sorted(FIX["pure"]))
def test_a_pure_dflow_pump_amm_frame_fires_with_the_fill_the_pool_paid(name):
    tx = FIX["pure"][name]
    h, cand = handler(), candidate(tx)
    plan, why = h.match(cand)
    assert why is None, why
    ev = executed_event(tx)
    assert plan["pool"] == ev["pool"] and plan["sol_in"] > 0
    fill, why = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 1), cand)
    assert why is None, why
    # sol_in is DFlow's PLANNED SOL (its first leg's output lands within -0.9%..+0.05% of it), so the
    # modelled out sits within a few bps of what the pool paid, not to the unit.
    assert abs(fill["base_out"] - int(ev["base_amount_out"])) * 10_000 <= 20 * int(ev["base_amount_out"])
    buy = h.build(cand, plan, None, fill, source_user_id="uid", handle="h", signature_basis="ed25519_local",
                  feed="preconf:test", read_ms=3.0, recv_epoch=1792200000.0, now_mono=2.0)
    assert (buy.venue, buy.mint, buy.pool, buy.token_amount) == ("pump_amm", plan["mint"], ev["pool"], fill["base_out"])
    assert buy.proof_provenance == "rabbit_pre_execution" and buy.rabbit["route"] == dfa.ROUTE
    assert buy.source_initiation["program_id"] == AMM and buy.source_initiation["payer"] == cand["owner"]
    back = parse_buy_event(buy.trade_event_log)
    assert int(back["base_amount_out"]) == fill["base_out"] and back["pool"] == ev["pool"]
    assert int(back["pool_base_token_reserves"]) == int(ev["pool_base_token_reserves"])


@pytest.mark.parametrize("name", sorted(FIX["split"]))
def test_a_split_route_with_a_dlmm_coin_leg_refuses(name):
    assert handler().match(candidate(FIX["split"][name])) == (None, "dflow_not_single_pump_amm")


@pytest.mark.parametrize("name", sorted(FIX["noncanonical"]))
def test_a_non_canonical_pool_refuses(name):
    assert handler().match(candidate(FIX["noncanonical"][name])) == (None, "pool_unbound")


def _pure():
    name = sorted(FIX["pure"])[0]
    return FIX["pure"][name]


def test_a_read_at_or_after_the_frame_slot_with_coin_already_held_refuses():
    tx = _pure()
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    for read_slot in (tx["slot"], tx["slot"] + 1):
        assert h.verdict(plan, state_for(tx, plan, read_slot=read_slot, held=10 ** 9), cand) == (
            None, "read_not_before_source")
    fill, why = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"], held=0), cand)
    assert why is None                                # same slot, nothing held, nothing else names it
    fill, why = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 1, held=10 ** 9), cand)
    assert why is None and fill["held"] == 10 ** 9   # before the slot: a repeat holder still fires


def test_a_read_with_no_context_slot_is_refused_as_stale():
    tx = _pure()
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    for held in (0, 5):
        assert h.verdict(plan, state_for(tx, plan, read_slot=None, held=held), cand) == (None, "read_stale")


def test_a_read_more_than_two_slots_before_the_frame_is_stale(monkeypatch):
    """Gamma LOW-1: a lagging node's read misses the net sells of S-k..S-1 (a looser cap)."""
    tx = _pure()
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    assert h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 2), cand)[1] is None
    assert h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 3), cand) == (None, "read_stale")
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_READ_MAX_LAG_SLOTS", 5)
    assert h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 5), cand)[1] is None


def test_an_unreadable_fee_tier_fails_closed():
    tx = _pure()
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    state = state_for(tx, plan, read_slot=tx["slot"] - 1)
    state["accounts"][plan["fee_config"]] = {"owner": "x", "data": [base64.b64encode(b"junk").decode(), "base64"]}
    assert h.verdict(plan, state, cand) == (None, "fee_tier_unknown")
    state["accounts"][plan["fee_config"]] = None
    assert h.verdict(plan, state, cand) == (None, "fee_tier_unknown")


def test_a_usdc_account_not_writable_refuses_as_not_usdc():
    from sniper.execution.pump_native_exit import ata
    tx = _pure()
    owner = candidate(tx)["owner"]
    cand = candidate(tx, drop_writable=str(ata(owner, dfa.USDC_MINT, TOKEN_PROGRAM)))
    assert handler().match(cand) == (None, "input_not_usdc")


def test_identity_refusals():
    tx = _pure()
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    state = state_for(tx, plan, read_slot=tx["slot"] - 1)
    state["accounts"][plan["pool"]] = dict(state["accounts"][plan["pool"]], owner="11111111111111111111111111111111")
    assert h.verdict(plan, state, cand) == (None, "pool_not_amm")


def test_knob_off_is_no_handler(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM", False)
    assert handler().match(candidate(_pure())) is None


def test_a_frame_without_dflow_is_not_this_handlers():
    cand = dict(candidate(_pure()), instructions=())
    assert handler().match(cand) is None


def _fired(alerts):
    tx = _pure()
    h, cand = handler(alerts), candidate(tx)
    plan, _ = h.match(cand)
    fill, _ = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 1), cand)
    h.build(cand, plan, None, fill, source_user_id="uid", handle="h", signature_basis="ed25519_local",
            feed="preconf:test", read_ms=3.0, recv_epoch=1792200000.0, now_mono=2.0)
    return h, cand, executed_event(tx)


def test_the_landed_join_is_quiet_when_the_model_reproduces_the_pool():
    alerts = []
    h, cand, ev = _fired(alerts)
    out = h.observe_processed(cand["signature"], dfa.encode_buy_event(dict(ev)))
    assert out["fee_bps_match"] and out["miss_bps"] < 0.01 and alerts == [] and h.stats["math_mismatch"] == 0
    assert h.observe_processed(cand["signature"], dfa.encode_buy_event(dict(ev))) is None    # joined once


def test_the_landed_join_pages_on_a_wrong_out_or_a_wrong_fee_tier():
    alerts = []
    h, cand, ev = _fired(alerts)
    out = h.observe_processed(cand["signature"], dfa.encode_buy_event(
        dict(ev, base_amount_out=int(ev["base_amount_out"]) * 9 // 10)))
    assert out["miss_bps"] > dfa.MODEL_TOLERANCE_BPS and len(alerts) == 1 and "executed" in alerts[0]
    h, cand, ev = _fired(alerts)
    out = h.observe_processed(cand["signature"], dfa.encode_buy_event(
        dict(ev, protocol_fee_basis_points=int(ev["protocol_fee_basis_points"]) + 88)))
    assert not out["fee_bps_match"] and len(alerts) == 2 and "fee tier" in alerts[1]


def test_the_proof_passes_the_pre_execution_check_only_for_this_route(monkeypatch):
    from types import SimpleNamespace
    from sniper.services import memecoin_source_initiation as si
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_SIGNAL", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_BUY", True)
    tx = _pure()
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    fill, _ = h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"] - 1), cand)
    buy = h.build(cand, plan, None, fill, source_user_id="uid", handle="h", signature_basis="ed25519_local",
                  feed="preconf:test", read_ms=3.0, recv_epoch=1792200000.0, now_mono=2.0)
    ev = SimpleNamespace(signal_source="slot_lane", source_timestamp_trust="rabbit_pre_execution",
                         source_wallet=cand["owner"], mint=plan["mint"], source_block_number=cand["slot"],
                         source_user_id="uid", fomo_event_id="slotlane:" + cand["signature"])
    assert si._pre_execution_refusal(ev, buy.source_initiation) is None
    assert si._pre_execution_refusal(ev, dict(buy.source_initiation, route="other")) == "wallet_initiation_unproved"
    monkeypatch.setattr(cfg, "MEMECOIN_SLOT_LANE_RABBIT_DFLOW_AMM", False)
    assert si._pre_execution_refusal(ev, buy.source_initiation) == "wallet_initiation_unproved"


def test_another_instruction_naming_the_holder_refuses_a_same_slot_read():
    tx = _pure()
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    program = FIX["mints"][plan["mint"]]["owner"]
    holder = plan["holders"][program]
    extra = (TOKEN_PROGRAM, (holder, "x" * 32, cand["owner"]), b"\x03" + struct.pack("<Q", 1))
    moved = dict(cand, instructions=cand["instructions"] + (extra,))
    assert h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"]), moved) == (None, "read_not_before_source")
    ata_create = ("ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL", (cand["owner"], holder, cand["owner"]), b"\x01")
    created = dict(cand, instructions=cand["instructions"] + (ata_create,))
    assert h.verdict(plan, state_for(tx, plan, read_slot=tx["slot"]), created)[1] is None


def test_an_owner_who_is_not_a_signer_refuses():
    cand = candidate(_pure())
    assert handler().match(dict(cand, signers=cand["signers"][:1])) == (None, "owner_not_signer")


def test_an_unwritable_coin_account_refuses():
    from sniper.execution.pump_native_exit import ata
    tx = _pure()
    plan, _ = handler().match(candidate(tx))
    program = FIX["mints"][plan["mint"]]["owner"]
    cand = candidate(tx, drop_writable=str(ata(candidate(tx)["owner"], plan["mint"], program)))
    assert handler().match(cand) == (None, "holder_unbound")


def test_an_implausible_impact_and_a_vault_mismatch_refuse():
    tx = _pure()
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    program = FIX["mints"][plan["mint"]]["owner"]
    state = state_for(tx, plan, read_slot=tx["slot"] - 1)
    # A near-empty quote side (1 lamport, no virtual quote): the source would buy almost the whole base.
    state["accounts"][plan["pool"]] = _pool_at_trade(FIX["pools"][plan["pool"]], 0)
    state["accounts"][plan["quote_vault"]] = _token_account(WSOL_MINT, plan["pool"], 1, TOKEN_PROGRAM)
    assert h.verdict(plan, state, cand) == (None, "impact_implausible")
    state = state_for(tx, plan, read_slot=tx["slot"] - 1)
    raw = bytearray(base64.b64decode(state["accounts"][plan["pool"]]["data"][0]))
    raw[139:171] = bytes(32)                                   # pool_base_token_account
    state["accounts"][plan["pool"]] = {"owner": AMM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}
    assert h.verdict(plan, state, cand) == (None, "vault_identity")


def test_a_fee_config_with_no_tier_for_this_market_cap_fails_closed():
    tx = _pure()
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    state = state_for(tx, plan, read_slot=tx["slot"] - 1)
    raw = (pump_fees.FEE_CONFIG_DISCRIMINATOR + b"\x01" + bytes(32) + struct.pack("<3Q", 20, 5, 0)
           + struct.pack("<I", 1) + struct.pack("<QQ", 0, 1) + struct.pack("<3Q", 20, 5, 0))   # threshold 2**64
    state["accounts"][plan["fee_config"]] = {"owner": "pfeeUxB6jkeY1Hxd7CsFCAjcbHA9rWtchMGdZ6VojVZ",
                                             "data": [base64.b64encode(raw).decode(), "base64"]}
    assert h.verdict(plan, state, cand) == (None, "fee_tier_unknown")


def test_two_mints_each_with_its_canonical_pool_named_is_ambiguous():
    tx = _pure()
    cand = candidate(tx)
    other = "So11111111111111111111111111111111111111112"
    extra = (other, dfa.canonical_pool(other))
    ixs = tuple((p, a + extra, d) if p == "DF1ow4tspfHX9JwWJsAb9epbkA8hmpSEAtxXy1V27QBH" else (p, a, d)
                for p, a, d in cand["instructions"])
    assert handler().match(dict(cand, instructions=ixs)) == (None, "pool_unbound")


def test_a_pool_not_created_by_pumps_migration_refuses():
    tx = _pure()
    h, cand = handler(), candidate(tx)
    plan, _ = h.match(cand)
    state = state_for(tx, plan, read_slot=tx["slot"] - 1)
    raw = bytearray(base64.b64decode(state["accounts"][plan["pool"]]["data"][0]))
    raw[11:43] = bytes(32)                                     # creator
    state["accounts"][plan["pool"]] = {"owner": AMM, "data": [base64.b64encode(bytes(raw)).decode(), "base64"]}
    assert h.verdict(plan, state, cand) == (None, "pool_identity")

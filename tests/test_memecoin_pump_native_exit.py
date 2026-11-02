"""pump.fun sell_v2, derived not copied, and the composite exit it feeds the executor.

fries, position 55293, 2026-09-09: Jupiter could not build a sell on a curve quoted in McDonald's
xStock. The two transactions that did sell drove the pump.fun program directly with sell_v2. The
constants below are that curve's live accounts, read from chain the same day; every derivation
in the module must reproduce them, and the assembled transaction simulated clean for the full bag.
"""
import base64
import json
import struct

import pytest

from sniper.execution import pump_native_exit as N

FRIES = "CM8TMGgwXF8ULuUiqyjF5643YNSZUY79mPBw6SnUpump"
MCDX = "XsqE9cRRpzxcGKDXj1BJ7Xmg4GRhZoyY1KpmGSxAWT2"
US = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"
CREATOR = "8AcLrZBbigiG197ibhsoaLyyWSRgiJoBGzgkrfM4qmfw"
FEE_RECIPIENT = "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"
BUYBACK = "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"
LIVE = {  # observed in the successful on-chain sells of 2026-09-09 (2r4LUSf3…, 37ngcKkC…)
    "global": "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf",
    "curve": "3cKeL2HSgqhu8SY61uZizo8RPhtXSqX6X6tn5c1jNUpB",
    "assoc_base_curve": "56GY1uCq1PnPU5VvZrMqMxzaDSZRrBgDjnDduYYgxqEj",
    "assoc_quote_curve": "9NbgjNCXiksfzCB7zPN1b9ireQFjdBNXXCovHDGruQyE",
    "creator_vault": "HHioMJ8aquC5G1aQfAhTEULYMXib3ygLvwrfkrDDTzkH",
    "assoc_creator_vault": "2vZnddTgjsKbTDkuLsZwXjLDGXdobCM3LrWWKNTH83YA",
    "sharing_config": "GwE2ZNwQvPHt96Qkwid1k5QA2qmStRcsiNhE7e7wq7t5",
    "event_authority": "Ce6TQqeHC9p8KetsN6JsjHK7UTZk7nasjjnr7XxXp9F1",
    "our_fries_ata": "EJvjZXdc9JcB5UTXt2BavBqQ2hh2GMWD6ecMHn2mXJyh",
    "our_mcdx_ata": "8Z8stsSXQ8nQpUJycXGZPuSrDAyBZSdctqGnr6boSHAy",
}


def _accounts():
    return N.sell_v2_accounts(user=US, base_mint=FRIES, quote_mint=MCDX,
                              base_token_program=N.TOKEN_2022_PROGRAM, quote_token_program=N.TOKEN_2022_PROGRAM,
                              creator=CREATOR, fee_recipient=FEE_RECIPIENT, buyback_fee_recipient=BUYBACK)


def test_every_derived_account_matches_the_live_sell():
    a = [str(k) for k, _s, _w in _accounts()]
    assert len(a) == 26
    assert a[0] == LIVE["global"]
    assert a[10] == LIVE["curve"] and a[11] == LIVE["assoc_base_curve"] and a[12] == LIVE["assoc_quote_curve"]
    assert a[14] == LIVE["our_fries_ata"] and a[15] == LIVE["our_mcdx_ata"]
    assert a[16] == LIVE["creator_vault"] and a[17] == LIVE["assoc_creator_vault"]
    assert a[18] == LIVE["sharing_config"], "sharing_config lives under the FEE program, seeded by the mint"
    assert a[24] == LIVE["event_authority"]
    assert a[22] == N.FEE_PROGRAM and a[23] == N.SYSTEM_PROGRAM and a[25] == N.PUMP_PROGRAM


def test_only_the_user_signs_and_the_curve_side_is_writable():
    acc = _accounts()
    assert [i for i, (_k, s, _w) in enumerate(acc) if s] == [13]
    writable = {i for i, (_k, _s, w) in enumerate(acc) if w}
    assert writable == {6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 19, 20}


def test_data_is_discriminator_amount_floor():
    d = N.sell_v2_data(5707019249939, 11527488)
    assert d[:8] == bytes.fromhex("5df6823ce7e940b2")
    assert struct.unpack("<QQ", d[8:]) == (5707019249939, 11527488)
    with pytest.raises(ValueError):
        N.sell_v2_data(0, 1)


def test_jupiter_instruction_json_round_trips():
    ix = N.instruction_from_json({"programId": N.SYSTEM_PROGRAM, "data": base64.b64encode(b"\x01\x02").decode(),
                                  "accounts": [{"pubkey": US, "isSigner": True, "isWritable": True}]})
    assert str(ix.program_id) == N.SYSTEM_PROGRAM and bytes(ix.data) == b"\x01\x02"
    assert ix.accounts[0].is_signer and ix.accounts[0].is_writable


def _fake_rpc(sim_err=None):
    """Enough chain for discovery + build: curve, global, curve token accounts, mint owners,
    blockhash, and a simulate answer."""
    from solders.pubkey import Pubkey
    craw = b"\x00" * 48 + b"\x00" + bytes(Pubkey.from_string(CREATOR)) + b"\x00" * 70
    graw = bytearray(1054); graw[41:73] = bytes(Pubkey.from_string(FEE_RECIPIENT)); graw[933:965] = bytes(Pubkey.from_string(BUYBACK))
    calls = []

    def rpc(method, params):
        calls.append(method)
        if method == "getAccountInfo":
            key = params[0]
            if key == LIVE["curve"]:
                return {"value": {"data": [base64.b64encode(craw).decode(), "base64"]}}
            if key == LIVE["global"]:
                return {"value": {"data": [base64.b64encode(bytes(graw)).decode(), "base64"]}}
            return {"value": None}
        if method == "getTokenAccountsByOwner":
            return {"value": [{"account": {"data": {"parsed": {"info": {"mint": MCDX}}}}},
                              {"account": {"data": {"parsed": {"info": {"mint": FRIES}}}}}]}
        if method == "getMultipleAccounts":
            return {"value": [{"owner": N.TOKEN_2022_PROGRAM}, {"owner": N.TOKEN_2022_PROGRAM}]}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": "11111111111111111111111111111111", "lastValidBlockHeight": 123}}
        if method == "simulateTransaction":
            return {"value": {"err": sim_err, "logs": ["Program x failed: boom"] if sim_err else []}}
        raise AssertionError(method)
    rpc.calls = calls
    return rpc


def _fake_jup():
    def jup(method, path, body=None):
        if path.startswith("/quote?inputMint=%s" % FRIES):
            return 200, {"inAmount": "5707019249939", "outAmount": "11527488", "otherAmountThreshold": "10374739",
                         "priceImpactPct": "1.2", "routePlan": [{"swapInfo": {"label": "Pump.fun", "ammKey": LIVE["curve"]}}]}
        if path.startswith("/quote?inputMint=%s" % MCDX):
            return 200, {"inAmount": "10374739", "outAmount": "26900000", "otherAmountThreshold": "26093000",
                         "routePlan": [{"swapInfo": {"label": "Meteora DLMM", "ammKey": "MeteoraPool11111111111111111111111111111111"}}]}
        if path == "/swap-instructions":
            return 200, {"swapInstruction": {"programId": N.SYSTEM_PROGRAM, "data": base64.b64encode(b"\x00").decode(),
                                             "accounts": [{"pubkey": US, "isSigner": True, "isWritable": True}]},
                         "setupInstructions": [], "cleanupInstruction": None, "addressLookupTableAddresses": []}
        raise AssertionError(path)
    return jup


def test_the_composite_quote_settles_in_usdc_and_carries_a_prebuilt_unsigned_tx():
    q = N.build_native_exit(FRIES, 5707019249939, 10.0, user=US, rpc=_fake_rpc(), jup_call=_fake_jup())
    assert q["_venue"] == "pump_native"
    assert q["outputMint"] == N.USDC_MINT and q["otherAmountThreshold"] == "26093000", "the intent's USD floor is the tail's"
    assert q["inAmount"] == "5707019249939"
    assert q["_curve_leg"]["min_out"] == 10374739 and q["_curve_leg"]["quote_mint"] == MCDX
    assert q["routePlan"][0]["swapInfo"]["ammKey"] == LIVE["curve"], "route id names the curve"
    tx = q["_native_swap"]["swapTransaction"]
    from solders.transaction import VersionedTransaction
    vt = VersionedTransaction.from_bytes(base64.b64decode(tx))
    progs = [str(vt.message.account_keys[i.program_id_index]) for i in vt.message.instructions]
    assert progs[2] == N.PUMP_PROGRAM, "compute budget x2, then sell_v2, then the tail"
    assert q["_native_swap"]["lastValidBlockHeight"] == 123


def test_a_failed_simulation_returns_nothing_to_sign():
    assert N.build_native_exit(FRIES, 5707019249939, 10.0, user=US, rpc=_fake_rpc(sim_err={"InstructionError": [2, "Custom"]}), jup_call=_fake_jup()) is None


def test_discovery_reads_the_quote_mint_from_the_curve_not_from_a_guess():
    d = N.discover_curve(FRIES, rpc=_fake_rpc())
    assert d["quote_mint"] == MCDX and d["creator"] == CREATOR and d["fee_recipient"] == FEE_RECIPIENT
    assert d["buyback_fee_recipient"] == BUYBACK and d["curve"] == LIVE["curve"]


@pytest.mark.parametrize("fresh_stage", ["curve", "global", "token_accounts", "mint_owners", "lookup", "simulation"])
def test_recent_confirmed_state_builds_the_same_exit_without_extra_rpc(fresh_stage):
    """Each read can lag at finalized independently, including a sell's lookup table and simulation."""
    from solders.pubkey import Pubkey
    table = str(Pubkey.from_bytes(bytes([7]) * 32))
    meta = struct.pack("<IQQB", 1, 2**64 - 1, 10, 0) + b"\x00" * 35
    table_data = base64.b64encode(meta + bytes(Pubkey.from_string(LIVE["assoc_quote_curve"]))).decode()
    jup = _fake_jup()

    def with_table(method, path, body=None):
        status, result = jup(method, path, body=body)
        if path == "/swap-instructions":
            result["addressLookupTableAddresses"] = [table]
        return status, result

    def read_at(stage_to_confirm):
        chain, calls = _fake_rpc(), []

        def rpc(method, params):
            calls.append(method)
            stage = {"getTokenAccountsByOwner": "token_accounts", "getMultipleAccounts": "mint_owners",
                     "simulateTransaction": "simulation"}.get(method)
            if method == "getAccountInfo":
                stage = "curve" if params[0] == LIVE["curve"] else "global" if params[0] == LIVE["global"] else "lookup"
            if stage == stage_to_confirm and params[-1].get("commitment") != "confirmed":
                return {"value": {"err": "AccountNotFound", "logs": []} if stage == "simulation" else None}
            if stage == "lookup":
                return {"value": {"data": [table_data, "base64"]}}
            return chain(method, params)
        return rpc, calls

    old_state, old_calls = read_at(None)
    expected = N.build_native_exit(FRIES, 5707019249939, 10.0, user=US, rpc=old_state, jup_call=with_table)
    fresh_state, fresh_calls = read_at(fresh_stage)
    actual = N.build_native_exit(FRIES, 5707019249939, 10.0, user=US, rpc=fresh_state, jup_call=with_table)
    assert actual is not None, fresh_stage
    assert actual == expected, "account freshness must not change the amount, floors or unsigned payload"
    assert actual["inAmount"] == "5707019249939" and actual["otherAmountThreshold"] == "26093000"
    assert actual["_curve_leg"]["min_out"] == 10374739
    assert fresh_calls == old_calls == ["getAccountInfo", "getAccountInfo", "getTokenAccountsByOwner",
                                       "getMultipleAccounts", "getLatestBlockhash", "getAccountInfo",
                                       "simulateTransaction"]


def _fake_jup_with_fat_tail(fat_when=("",), quote_fail_when=()):
    """Jupiter whose payout -> USDC tail is fat (45 fresh accounts, no lookup table) for the tail
    shapes in `fat_when` and one account otherwise. `quote_fail_when` names shapes whose tail
    quote answers 400. Records every tail shape it was asked for, in order."""
    from solders.pubkey import Pubkey
    state = {"shape": None, "tail_shapes": [], "swap_builds": 0}

    def jup(method, path, body=None):
        if path.startswith("/quote?inputMint=%s" % FRIES):
            return 200, {"inAmount": "5707019249939", "outAmount": "11527488", "otherAmountThreshold": "10374739",
                         "priceImpactPct": "1.2", "routePlan": [{"swapInfo": {"label": "Pump.fun", "ammKey": LIVE["curve"]}}]}
        if path.startswith("/quote?inputMint=%s" % MCDX):
            shape = path.split("slippageBps=1000", 1)[1]
            state["shape"] = shape
            state["tail_shapes"].append(shape)
            if shape in quote_fail_when:
                return 400, {"error": "no route"}
            return 200, {"inAmount": "10374739", "outAmount": "26900000", "otherAmountThreshold": "26093000",
                         "routePlan": [{"swapInfo": {"label": "Meteora DLMM", "ammKey": "MeteoraPool11111111111111111111111111111111"}}]}
        if path == "/swap-instructions":
            state["swap_builds"] += 1
            n = 45 if state["shape"] in fat_when else 1
            accounts = [{"pubkey": str(Pubkey.new_unique()), "isSigner": False, "isWritable": True} for _ in range(n)]
            return 200, {"swapInstruction": {"programId": N.SYSTEM_PROGRAM, "data": base64.b64encode(b"\x00").decode(),
                                             "accounts": accounts},
                         "setupInstructions": [], "cleanupInstruction": None, "addressLookupTableAddresses": []}
        raise AssertionError(path)
    jup.state = state
    return jup


def test_a_fat_tail_is_rebuilt_capped_until_the_transaction_fits_a_packet():
    """ONLY, 2026-09-22 (ref e4952cc0): Jupiter's best HIMS tail assembled at 1,392 bytes, the
    20-account tail at 1,029. The builder must try the cap itself so the entry guard and the real
    exit both see a sell that fits, and it must not spend the extra calls on a curve that fits."""
    jup = _fake_jup_with_fat_tail()
    q = N.build_native_exit(FRIES, 5707019249939, 10.0, user=US, rpc=_fake_rpc(), jup_call=jup,
                            packet_limit=1232, tail_max_accounts=20)
    assert q is not None and q["_native_swap"]["bytes"] <= 1232
    assert q["_native_swap"]["tail_shape"] == "&maxAccounts=20"
    assert jup.state["tail_shapes"] == ["", "&maxAccounts=20"] and jup.state["swap_builds"] == 2
    assert len(base64.b64decode(q["_native_swap"]["swapTransaction"])) == q["_native_swap"]["bytes"]
    # a curve whose best tail already fits pays no extra call
    slim = _fake_jup_with_fat_tail(fat_when=())
    q2 = N.build_native_exit(FRIES, 5707019249939, 10.0, user=US, rpc=_fake_rpc(), jup_call=slim,
                             packet_limit=1232, tail_max_accounts=20)
    assert q2["_native_swap"]["tail_shape"] == "best" and slim.state["tail_shapes"] == [""]


def test_when_no_tail_shape_fits_the_smallest_build_is_returned_so_the_guard_measures_too_big():
    """A capped quote that does not exist is skipped, not fatal; the direct shape is still tried;
    and when every shape is over the limit the caller gets the smallest one and refuses on its
    real size, exactly the pre-existing `too_big:N>M` verdict."""
    jup = _fake_jup_with_fat_tail(fat_when=("", "&onlyDirectRoutes=true"), quote_fail_when=("&maxAccounts=20",))
    q = N.build_native_exit(FRIES, 5707019249939, 10.0, user=US, rpc=_fake_rpc(), jup_call=jup,
                            packet_limit=1232, tail_max_accounts=20)
    assert jup.state["tail_shapes"] == ["", "&maxAccounts=20", "&onlyDirectRoutes=true"]
    assert q is not None and q["_native_swap"]["bytes"] > 1232
    # the entry guard's own measurement reads that size and still says too_big
    from sniper.execution.solana_swap_executor import _payload_bytes
    assert _payload_bytes(q["_native_swap"]["swapTransaction"]) == q["_native_swap"]["bytes"]
    # a cap of 0 skips the capped shape entirely
    jup0 = _fake_jup_with_fat_tail(fat_when=("",))
    q0 = N.build_native_exit(FRIES, 5707019249939, 10.0, user=US, rpc=_fake_rpc(), jup_call=jup0,
                             packet_limit=1232, tail_max_accounts=0)
    assert jup0.state["tail_shapes"] == ["", "&onlyDirectRoutes=true"] and q0["_native_swap"]["bytes"] <= 1232

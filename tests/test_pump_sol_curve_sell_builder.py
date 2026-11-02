"""Offline normal-SOL sell ABI and complete atomic USDC-tail checks."""
import base64
import copy
import json
import struct
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from solders.pubkey import Pubkey
from solders.transaction import VersionedTransaction

from sniper.execution import pump_native_exit as N

USER = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"
MINT = "CM8TMGgwXF8ULuUiqyjF5643YNSZUY79mPBw6SnUpump"
CREATOR = "8AcLrZBbigiG197ibhsoaLyyWSRgiJoBGzgkrfM4qmfw"
FEE = "62qc2CNXwrYqQScmEdiZFFAnJR262PxWEuNQtxfafNgV"
BUYBACK = "5eHhjP8JaYkz83CWwvGU2uMUXefd3AazWGx4gpcuEEYD"
WSOL = "So11111111111111111111111111111111111111112"
AMOUNT = 41_876_174_451
MINIMUM = 1_790_121


def state():
    return {"mint": MINT, "curve": str(N._pda([b"bonding-curve", bytes(N._pk(MINT))], N.PUMP_PROGRAM)),
            "virtual_sol_reserves": 37_903_103_425, "virtual_token_reserves": 849_270_827_451_215,
            "real_sol_reserves": 7_903_103_425, "creator": CREATOR,
            "token_program": N.TOKEN_PROGRAM, "complete": False, "mayhem_mode": False,
            "is_cashback_coin": False, "is_holder_reward": False, "quote_mint": N.SYSTEM_PROGRAM,
            "curve_layout_size": 166, "creator_fee_bps": 0, "creator_fee_configurable": False,
            "fee_basis_points": 95, "creator_fee_basis_points": 30,
            "fee_recipient": FEE, "buyback_fee_recipient": BUYBACK}


def accounts(s=None):
    s = s or state()
    raw = bytearray(166)
    raw[:8] = bytes.fromhex("17b7f83760d8ac60")
    struct.pack_into("<4Q", raw, 8, s["virtual_token_reserves"], s["virtual_sol_reserves"],
                     100_000_000_000, s["real_sol_reserves"])
    raw[49:81] = bytes(N._pk(s["creator"]))
    raw[83:115] = bytes(N._pk(s["quote_mint"]))
    struct.pack_into("<Q", raw, 115, s["creator_fee_bps"])
    for offset, field in [(48, "complete"), (81, "mayhem_mode"),
                          (82, "is_cashback_coin"), (124, "is_holder_reward")]:
        raw[offset] = int(s[field])
    mint = bytearray(82); mint[44:46] = bytes([6, 1])
    config = bytearray(109)
    config[:8] = bytes.fromhex("8f3492bbdb7b4c9b")
    struct.pack_into("<3QI", config, 41, 0, 95, 30, 1)
    struct.pack_into("<QQ3Q", config, 69, 0, 0, 0, s["fee_basis_points"], s["creator_fee_basis_points"])
    global_raw = bytearray(1088)
    global_raw[:8] = bytes.fromhex("a7e8e8b1c86c727f"); global_raw[8] = 1
    global_raw[41:73] = bytes(N._pk(s["fee_recipient"]))
    global_raw[933:965] = bytes(N._pk(s["buyback_fee_recipient"]))
    global_raw[1045] = int(s["creator_fee_configurable"])
    return [account(raw, N.PUMP_PROGRAM), account(mint, s["token_program"]),
            account(config, N.FEE_PROGRAM), account(global_raw, N.PUMP_PROGRAM)]


def account(raw, owner):
    return {"owner": owner, "data": [base64.b64encode(raw).decode(), "base64"]}


class Offline:
    def __init__(self, values=None, tail=None, sim=None, fat=False):
        self.values = values if values is not None else accounts()
        self.tail = tail or {}; self.sim = {"err": None} if sim is None else sim
        self.calls = []; self.bodies = []; self.fat = fat

    def rpc(self, method, params):
        self.calls.append((method, params))
        if method == "getMultipleAccounts":
            assert params == [[state()["curve"], MINT,
                str(N._pda([b"fee_config", N.FEE_CONFIG_SEED], N.FEE_PROGRAM)),
                str(N._pda([b"global"], N.PUMP_PROGRAM))], {"encoding": "base64", "commitment": "confirmed"}]
            return {"context": {"slot": 123}, "value": self.values}
        if method == "getLatestBlockhash":
            return {"value": {"blockhash": "11111111111111111111111111111111", "lastValidBlockHeight": 124}}
        if method == "simulateTransaction":
            assert params[1] == {"encoding": "base64", "sigVerify": False,
                                 "replaceRecentBlockhash": True, "commitment": "confirmed"}
            return {"value": self.sim}
        raise AssertionError(method)

    def jup(self, method, path, body=None):
        if method == "GET":
            args = parse_qs(urlsplit(path).query)
            assert args["inputMint"] == [WSOL] and args["outputMint"] == [N.USDC_MINT]
            self.calls.append((method, path))
            return 200, {"inputMint": WSOL, "outputMint": N.USDC_MINT, "inAmount": args["amount"][0],
                         "outAmount": "25000000", "otherAmountThreshold": "24250000",
                         "swapMode": "ExactIn", "routePlan": [], **self.tail}
        assert path == "/swap-instructions"
        self.bodies.append(json.loads(body))
        extras = [str(Pubkey.new_unique()) for _ in range(45)] if self.fat else []
        ix = {"programId": N.SYSTEM_PROGRAM, "data": "AA==", "accounts": [
            {"pubkey": key, "isSigner": False, "isWritable": True} for key in extras]}
        return 200, {"setupInstructions": [ix], "swapInstruction": ix,
                     "cleanupInstruction": ix, "addressLookupTableAddresses": []}


def build(io=None, *, supplied=None, **kwargs):
    io = io or Offline()
    return N.native_curve_sell(MINT, AMOUNT, 3, user=USER, rpc=io.rpc, jup_call=io.jup,
                               state=supplied, **kwargs)


def sell_instruction(q):
    tx = VersionedTransaction.from_bytes(base64.b64decode(q["_native_swap"]["swapTransaction"]))
    ix = tx.message.instructions[2]
    assert str(tx.message.account_keys[ix.program_id_index]) == N.PUMP_PROGRAM
    return tx, ix


def test_complete_native_sell_uses_one_snapshot_exact_floor_and_simulation():
    io = Offline(); got = build(io)
    assert got["_curve_leg"] == {"kind": "sol", "quote_mint": WSOL, "expected_out": 1_845_486,
                                  "min_out": MINIMUM, "curve": state()["curve"]}
    assert got["inAmount"] == str(AMOUNT) and got["otherAmountThreshold"] == "24250000"
    assert got["_native_swap"]["simulated"] is True
    tx, ix = sell_instruction(got)
    assert bytes(ix.data) == bytes.fromhex("33e685a4017f83ad") + struct.pack("<QQ", AMOUNT, MINIMUM)
    assert len(ix.accounts) == 16
    assert [str(tx.message.account_keys[i]) for i in ix.accounts] == [
        str(N._pda([b"global"], N.PUMP_PROGRAM)), FEE, MINT, state()["curve"],
        str(N.ata(state()["curve"], MINT, N.TOKEN_PROGRAM)), str(N.ata(USER, MINT, N.TOKEN_PROGRAM)),
        USER, N.SYSTEM_PROGRAM, str(N._pda([b"creator-vault", bytes(N._pk(CREATOR))], N.PUMP_PROGRAM)),
        N.TOKEN_PROGRAM, str(N._pda([b"__event_authority"], N.PUMP_PROGRAM)), N.PUMP_PROGRAM,
        str(N._pda([b"fee_config", N.FEE_CONFIG_SEED], N.FEE_PROGRAM)), N.FEE_PROGRAM,
        str(N._pda([b"bonding-curve-v2", bytes(N._pk(MINT))], N.PUMP_PROGRAM)), BUYBACK]
    assert len(tx.message.instructions) == 6  # two compute, sell, tail setup/swap/cleanup
    assert [c[0] for c in io.calls] == ["getMultipleAccounts", "getLatestBlockhash", "GET", "simulateTransaction"]
    assert io.bodies[0]["wrapAndUnwrapSol"] is True
    assert io.bodies[0]["quoteResponse"]["inAmount"] == str(MINIMUM)
    keys = tx.message.account_keys
    assert [int(i) for i in ix.accounts if int(i) < tx.message.header.num_required_signatures] == [keys.index(N._pk(USER))]
    writable = []
    for position, index in enumerate(ix.accounts):
        is_signer = index < tx.message.header.num_required_signatures
        if is_signer:
            allowed = index < tx.message.header.num_required_signatures - tx.message.header.num_readonly_signed_accounts
        else:
            allowed = index < len(keys) - tx.message.header.num_readonly_unsigned_accounts
        if allowed: writable.append(position)
    assert writable == [1, 3, 4, 5, 6, 8, 15]


def test_cached_state_is_probe_only_and_does_not_reread_or_mutate():
    s = state(); original = copy.deepcopy(s); io = Offline()
    got = build(io, supplied=s, simulate=False)
    assert got["_native_swap"]["simulated"] is False
    assert [c[0] for c in io.calls] == ["getLatestBlockhash", "GET"]
    assert s == original
    io = Offline()
    assert build(io, supplied=s) is None and io.calls == []


@pytest.mark.parametrize("field,value", [("complete", True), ("mayhem_mode", True),
    ("is_cashback_coin", True), ("is_holder_reward", True), ("quote_mint", WSOL),
    ("curve_layout_size", 165), ("curve_layout_size", 166.0), ("creator_fee_bps", 10_000), ("creator_fee_bps", True),
    ("creator_fee_configurable", None), ("fee_basis_points", None),
    ("token_program", N.SYSTEM_PROGRAM), ("mint", WSOL), ("curve", USER),
    ("real_sol_reserves", 1_845_486)])
def test_bad_or_unsupported_cached_state_never_builds(field, value):
    io = Offline()
    assert build(io, supplied={**state(), field: value}, simulate=False) is None
    assert io.calls == []


@pytest.mark.parametrize("field", list(state()))
def test_missing_cached_fields_never_mean_false_or_zero(field):
    s = state(); del s[field]; io = Offline()
    assert build(io, supplied=s, simulate=False) is None
    assert io.calls == []


def test_raw_creator_override_requires_global_permission_and_default_creator_is_free():
    for cached in (False, True):
        s = dict(state(), creator_fee_bps=300)
        assert build(Offline(accounts(s)), supplied=s if cached else None, simulate=not cached) is None
        s["creator_fee_configurable"] = True
        got = build(Offline(accounts(s)), supplied=s if cached else None, simulate=not cached)
        assert got["_curve_leg"]["expected_out"] == 1_795_027
        s["creator"] = N.SYSTEM_PROGRAM
        got = build(Offline(accounts(s)), supplied=s if cached else None, simulate=not cached)
        assert got["_curve_leg"]["expected_out"] == 1_851_093


@pytest.mark.parametrize("index,mode", [(0, "owner"), (0, "disc"), (0, "length"), (0, "bool"),
    (1, "owner"), (1, "length"), (1, "mint_uninitialized"), (2, "owner"), (2, "disc"),
    (2, "tier"), (3, "owner"), (3, "disc"), (3, "length"), (3, "bool")])
def test_snapshot_account_identity_layout_and_fee_refusals(index, mode):
    values = accounts(); acc = values[index]; raw = bytearray(base64.b64decode(acc["data"][0]))
    if mode == "owner": acc["owner"] = N.SYSTEM_PROGRAM
    if mode == "disc": raw[0] ^= 1
    if mode == "length": raw = raw[:81] if index == 1 else raw[:-100]
    if mode == "bool": raw[81 if index == 0 else 1045] = 2
    if mode == "mint_uninitialized": raw[45] = 0
    if mode == "tier": struct.pack_into("<Q", raw, 69, 1)
    acc["data"][0] = base64.b64encode(raw).decode()
    io = Offline(values)
    assert build(io) is None
    assert [c[0] for c in io.calls] == ["getMultipleAccounts"]


@pytest.mark.parametrize("tail", [{"inputMint": MINT}, {"outputMint": WSOL}, {"inAmount": "1"},
    {"swapMode": "ExactOut"}, {"swapMode": None}, {"otherAmountThreshold": "0"},
    {"outAmount": "0"}, {"otherAmountThreshold": "25000001"}, {"inAmount": True}])
def test_tail_must_bind_exact_signed_minimum_identity_and_positive_floor(tail):
    io = Offline(tail=tail)
    assert build(io) is None and io.bodies == []
    assert "simulateTransaction" not in [c[0] for c in io.calls]


@pytest.mark.parametrize("sim", [{"err": {"InstructionError": [2, "Custom"]}}, {}, {"err": False}])
def test_failed_or_malformed_simulation_never_returns_payload(sim):
    assert build(Offline(sim=sim)) is None


def test_oversize_refuses_before_simulation_instead_of_returning_executable_payload():
    io = Offline(fat=True)
    assert build(io, packet_limit=1232, tail_max_accounts=20) is None
    assert len(io.bodies) == 3
    assert "simulateTransaction" not in [c[0] for c in io.calls]


@pytest.mark.parametrize("method", ["getMultipleAccounts", "getLatestBlockhash", "simulateTransaction"])
def test_rpc_failure_is_contained_without_any_payload(method):
    io = Offline(); original = io.rpc
    def rpc(name, params):
        if name == method: raise TimeoutError("offline timeout")
        return original(name, params)
    assert N.native_curve_sell(MINT, AMOUNT, 3, user=USER, rpc=rpc, jup_call=io.jup) is None


@pytest.mark.parametrize("values", [[], [None] * 4, accounts()[:3]])
def test_partial_or_missing_snapshot_refuses_before_build(values):
    io = Offline(values)
    assert build(io) is None and not io.bodies
    assert [c[0] for c in io.calls] == ["getMultipleAccounts"]


@pytest.mark.parametrize("failure", ["rate", "server", "build_rejected", "build_timeout", "missing_blockhash", "missing_lookup"])
def test_tail_provider_failures_never_leave_a_payload(failure):
    io = Offline(); rpc_orig, jup_orig = io.rpc, io.jup
    def rpc(method, params):
        if failure == "missing_blockhash" and method == "getLatestBlockhash": return {"value": {}}
        if failure == "missing_lookup" and method == "getAccountInfo": return {"value": None}
        return rpc_orig(method, params)
    def jup(method, path, body=None):
        if failure in ("rate", "server") and method == "GET":
            io.calls.append((method, path)); return (429 if failure == "rate" else 503), {}
        if method == "POST" and failure == "build_rejected": return 400, {}
        if method == "POST" and failure == "build_timeout": raise TimeoutError("offline")
        status, result = jup_orig(method, path, body)
        if method == "POST" and failure == "missing_lookup":
            result["addressLookupTableAddresses"] = [str(Pubkey.new_unique())]
        return status, result
    assert N.native_curve_sell(MINT, AMOUNT, 3, user=USER, rpc=rpc, jup_call=jup) is None
    if failure in ("rate", "server"):
        assert len([c for c in io.calls if c[0] == "GET"]) == 1


def test_token_2022_input_uses_its_own_atas_and_native_amount():
    s = dict(state(), token_program=N.TOKEN_2022_PROGRAM)
    got = build(Offline(accounts(s)))
    tx, ix = sell_instruction(got)
    assert str(tx.message.account_keys[ix.accounts[4]]) == str(N.ata(s["curve"], MINT, N.TOKEN_2022_PROGRAM))
    assert str(tx.message.account_keys[ix.accounts[5]]) == str(N.ata(USER, MINT, N.TOKEN_2022_PROGRAM))
    assert str(tx.message.account_keys[ix.accounts[9]]) == N.TOKEN_2022_PROGRAM


def test_native_packet_shaping_keeps_the_capped_tail_that_fits():
    io = Offline(); original = io.jup; quotes = []
    def jup(method, path, body=None):
        if method == "GET":
            quotes.append(path)
            io.fat = "maxAccounts=20" not in path
        return original(method, path, body)
    got = N.native_curve_sell(MINT, AMOUNT, 3, user=USER, rpc=io.rpc, jup_call=jup,
                              packet_limit=1232, tail_max_accounts=20)
    assert got["_native_swap"]["bytes"] <= 1232
    assert got["_native_swap"]["tail_shape"] == "&maxAccounts=20"
    assert len(quotes) == 2


def test_slippage_below_exit_ladder_refuses_before_snapshot():
    io = Offline()
    assert N.native_curve_sell(MINT, AMOUNT, 0.5, user=USER, rpc=io.rpc, jup_call=io.jup) is None
    assert io.calls == []


def test_legacy_account_order_matches_retained_normal_sol_sell():
    fixture = json.loads((Path(__file__).parent / "fixtures/memecoin/pump_sol_curve_sells.json").read_text())
    matched = 0
    for row in fixture["rows"]:
        raw = base64.b64decode(row["pre_state"]["account_b64"])
        if raw[83:115] != bytes(32) or raw[48] or raw[81] or raw[82] or raw[124]: continue
        for ix in row["sell"]["pump_instructions"]:
            if len(ix["accounts"]) != 16: continue
            keys = ix["accounts"]; mint, user = keys[2], keys[6]
            s = dict(state(), mint=mint, curve=keys[3], creator=str(Pubkey.from_bytes(raw[49:81])),
                     token_program=keys[9], fee_recipient=keys[1], buyback_fee_recipient=keys[15])
            io = Offline()
            got = N.native_curve_sell(mint, AMOUNT, 3, user=user, rpc=io.rpc, jup_call=io.jup,
                                      state=s, simulate=False)
            tx, built = sell_instruction(got)
            assert [str(tx.message.account_keys[i]) for i in built.accounts] == keys
            matched += 1
    assert matched > 0

"""Funded EVM type-2 (EIP-1559) signing (spec 2026-09-30-memecoin-evm-type2-signing-design.md).

A chain in MEMECOIN_EVM_TYPE2_CHAINS with a positive MEMECOIN_EVM_TIP_GWEI_<chain> signs type 2:
maxFeePerGas = the price the caller computed (unchanged), maxPriorityFeePerGas = min(tip, cap).
Anything else signs today's legacy bytes, byte for byte. Every test signs with the production
signer (`sign_tx` inside `build_real_evm_executor`) and a real eth_account key.
"""
import base64
import hashlib
import json
import logging
import sqlite3

import pytest
import rlp
from eth_account import Account
from eth_utils import to_checksum_address

from sniper.execution import evm_real_seams as S
from sniper.execution.submission_wiring import evm_tx_identity
from sniper.persistence import db_memecoin as m

CHAIN = "8453"
KEY = "0x" + "11" * 32
NODE_GP = 2 * 10 ** 9                    # eth_gasPrice; the executor signs 1.5x = 3 gwei
CAP = 3 * 10 ** 9
TIP_GWEI = "0.05"
TIP = 5 * 10 ** 7
SPENDER = "0x" + "44" * 20
SWAP = {"to": to_checksum_address("0x" + "bb" * 20), "data": "0x" + "ab" * 80, "value": 10 ** 15, "gas": 200_000}


class _Rpc:
    def __init__(self, *, native=10 ** 18, gas_price=NODE_GP, nonce=7):
        self.native, self.gas_price, self.nonce, self.sent = native, gas_price, nonce, []

    def __call__(self, _method, _url, headers=None, body=None, timeout=10):
        req = json.loads(body)
        meth = req["method"]
        if meth == "eth_getTransactionCount":
            return 200, {"result": hex(self.nonce)}, None
        if meth == "eth_gasPrice":
            return 200, {"result": hex(self.gas_price)}, None
        if meth == "eth_getBalance":
            return 200, {"result": hex(self.native)}, None
        if meth == "eth_call":
            return 200, {"result": "0x" + "00" * 32}, None
        if meth == "eth_sendRawTransaction":
            self.sent.append(req["params"][0])
            return 200, {"result": "0x" + "11" * 32}, None
        if meth == "eth_getTransactionReceipt":
            return 200, {"result": {"status": "0x1", "blockNumber": "0x1",
                                    "transactionHash": req["params"][0]}}, None
        raise AssertionError("unexpected method %s" % meth)


@pytest.fixture
def type2(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_TYPE2_CHAINS", CHAIN)
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_" + CHAIN, TIP_GWEI)
    return monkeypatch


@pytest.fixture(autouse=True)
def _clean_knobs(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_TYPE2_CHAINS", raising=False)
    monkeypatch.delenv("MEMECOIN_EVM_TIP_GWEI_" + CHAIN, raising=False)
    monkeypatch.delenv("MEMECOIN_EVM_APPROVAL_PIPELINE", raising=False)


def _executor(rpc, chain=CHAIN):
    return S.build_real_evm_executor(acct=Account.from_key(KEY), chain_id=chain,
                                     rpc_url="https://rpc", http=rpc,
                                     usdc_fn=lambda: 0.0, token_fn=lambda _mint: 0.0)


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    c.execute("INSERT INTO memecoin_positions (id, mint, network_id, is_open, execution_mode, "
              "entry_price, opened_at) VALUES (1, ?, ?, 1, 'real', 1.0, '2026-09-30T00:00:00+00:00')",
              ("0x" + "aa" * 20, CHAIN))
    c.commit()
    return c


def _four_paths(rpc):
    """Raw hex from each signer family: approval, swap `_sign`, the exit gate, LI.FI/top-up."""
    ex = _executor(rpc)
    assert ex["_approve"](SPENDER, 12345) is True
    approve = rpc.sent[-1]
    swap = ex["_sign"](dict(SWAP))[0]
    exit_ = ex["_make_exit_sign_fn"](_conn(), 1, "close_claim")(dict(SWAP))[0]
    lifi = ex["lifi_local"]["sign_fn"]({"to": SWAP["to"], "data": SWAP["data"], "value": SWAP["value"],
                                        "gas": SWAP["gas"], "gasPrice": CAP, "nonce": 9,
                                        "chainId": int(CHAIN)})[0]
    return {"approve": approve, "sign": swap, "exit_gate": exit_, "lifi_sign_fn": lifi}


def _fields(raw_hex):
    raw = bytes.fromhex(raw_hex[2:])
    assert raw[0] == 2, "not a type-2 envelope"
    chain_id, nonce, tip, cap, gas, to, value, data, access, *_sig = rlp.decode(raw[1:])
    as_int = lambda b: int.from_bytes(b, "big")
    return {"chain_id": as_int(chain_id), "nonce": as_int(nonce), "tip": as_int(tip), "cap": as_int(cap),
            "gas": as_int(gas), "to": "0x" + to.hex(), "value": as_int(value), "data": "0x" + data.hex(),
            "access": access}


# Today's legacy bytes for the four paths above (fixed key, nonce 7/9, node price 2 gwei), captured on
# main 848a61355 before this change. sha256 of the raw hex string.
GOLDEN_LEGACY = {
    "approve": "ed91ea971a5ac5e888bf2a33eac43facd7eb50efa65a51b4effcf19e33633cfb",
    "sign": "f63f5efe968dcfda21e26e416371b27b44a428faeb8832ff404bf25aae840ae2",
    "exit_gate": "f63f5efe968dcfda21e26e416371b27b44a428faeb8832ff404bf25aae840ae2",
    "lifi_sign_fn": "4bc8ad60ef1a5400eb6d38854f6a836c8b2c301942677d94906a4fefa197ff82",
}


def test_an_unlisted_chain_signs_todays_legacy_bytes_on_every_path():
    got = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in _four_paths(_Rpc()).items()}
    assert got == GOLDEN_LEGACY


def test_a_tip_alone_or_a_malformed_list_keeps_todays_legacy_bytes(monkeypatch):
    """beta/eth-mainnet sets the tip for legacy chain-1 signing before type 2 is on: a tip must not
    switch the envelope. A list with one malformed entry is empty, as MEMECOIN_EVM_PREAPPROVE_CHAINS."""
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_" + CHAIN, TIP_GWEI)
    for listed in (None, CHAIN + ",base"):
        if listed is None:
            monkeypatch.delenv("MEMECOIN_EVM_TYPE2_CHAINS", raising=False)
        else:
            monkeypatch.setenv("MEMECOIN_EVM_TYPE2_CHAINS", listed)
        got = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in _four_paths(_Rpc()).items()}
        assert got == GOLDEN_LEGACY, listed


def test_a_listed_chain_with_a_tip_signs_type2_at_todays_cap_on_every_path(type2):
    for path, raw in _four_paths(_Rpc()).items():
        f = _fields(raw)
        assert (f["chain_id"], f["cap"], f["tip"], f["access"]) == (int(CHAIN), CAP, TIP, []), path


def test_a_listed_chain_without_a_tip_signs_legacy_and_says_so_at_build(monkeypatch, caplog):
    monkeypatch.setenv("MEMECOIN_EVM_TYPE2_CHAINS", CHAIN)
    for tip in (None, "0", "-1", "nan", "junk"):
        if tip is None:
            monkeypatch.delenv("MEMECOIN_EVM_TIP_GWEI_" + CHAIN, raising=False)
        else:
            monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_" + CHAIN, tip)
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            got = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in _four_paths(_Rpc()).items()}
        assert got == GOLDEN_LEGACY, tip
        assert any("MEMECOIN_EVM_TIP_GWEI_" + CHAIN in r.getMessage() and r.levelno == logging.ERROR
                   for r in caplog.records), tip


def test_the_chain_lookup_matches_an_int_a_decimal_and_a_hex_chain_id(type2):
    """Gamma LOW 2: the signers pass int(chain) and the knob is a string list."""
    for chain_id in (int(CHAIN), CHAIN, hex(int(CHAIN))):
        assert S._type2_tip_wei({"chainId": chain_id}) == TIP, chain_id
    for chain_id in (None, "base", 1, "0x1"):
        assert S._type2_tip_wei({"chainId": chain_id}) is None, chain_id


def test_the_tip_is_capped_at_a_cap_below_it(type2):
    """The exit gate's reduced-fee branch can sign below the tip: a node refuses tip > cap."""
    acct = Account.from_key(KEY)
    tx = {"to": SWAP["to"], "data": "0x", "value": 0, "gas": 21000, "gasPrice": TIP // 2, "nonce": 1,
          "chainId": int(CHAIN)}
    f = _fields(S.sign_tx(tx, acct)[0])
    assert (f["cap"], f["tip"]) == (TIP // 2, TIP // 2)


def test_the_exit_gates_reduced_fee_signs_type2_under_its_admission_bound(type2):
    # native covers value + gas x reduced price, not gas x 3 gwei; no WETH to unwrap.
    need_reduced = SWAP["gas"] * int(NODE_GP * 1.2) + SWAP["value"]
    rpc = _Rpc(native=need_reduced)
    ex = _executor(rpc)
    raw = ex["_make_exit_sign_fn"](_conn(), 1, "close_claim")(dict(SWAP))[0]
    f = _fields(raw)
    assert f["gas"] * f["cap"] + f["value"] <= need_reduced
    assert f["tip"] == min(TIP, f["cap"])


def test_the_signed_identity_is_the_hash_of_the_typed_bytes(type2):
    raw, h = S.sign_tx({"to": SWAP["to"], "data": "0x", "value": 0, "gas": 21000, "gasPrice": CAP,
                        "nonce": 1, "chainId": int(CHAIN)}, Account.from_key(KEY))
    assert raw.startswith("0x02") and h == evm_tx_identity(raw)


# ---------------------------------------------------------------- the decoder

def _b64(raw_hex):
    return base64.b64encode(raw_hex.encode("ascii")).decode("ascii")


@pytest.mark.parametrize("data, value", [("0x", 0), ("0x" + "cd" * 300, 7 * 10 ** 16)])
def test_the_cost_decoder_reads_a_type2_payload(data, value):
    tx = {"type": 2, "chainId": 1, "nonce": 1335, "maxFeePerGas": CAP, "maxPriorityFeePerGas": TIP,
          "gas": 1_200_000, "to": SWAP["to"], "value": value, "data": data}
    raw = "0x" + Account.from_key(KEY).sign_transaction(tx).raw_transaction.hex().removeprefix("0x")
    for payload in (_b64(raw), base64.b64encode(bytes.fromhex(raw[2:])).decode("ascii")):
        cost = S.decode_signed_tx_cost(payload)
        assert cost == {"nonce": 1335, "gas_price": CAP, "gas_limit": 1_200_000, "value": value,
                        "max_priority_fee": TIP, "worst_case_wei": 1_200_000 * CAP + value}
    assert S.signed_gas_facts(raw) == {"gas_price_wei": CAP, "gas_limit": 1_200_000,
                                       "max_priority_fee_wei": TIP}


def test_other_typed_envelopes_still_decode_to_none():
    acct = Account.from_key(KEY)
    t1 = acct.sign_transaction({"type": 1, "chainId": 1, "nonce": 1, "gasPrice": CAP, "gas": 21000,
                                "to": SWAP["to"], "value": 0, "data": "0x", "accessList": []})
    raw1 = "0x" + t1.raw_transaction.hex().removeprefix("0x")
    raw4 = "0x04" + rlp.encode([1, 1, TIP, CAP, 21000, b"\x11" * 20, 0, b"", [], [], 0, 1, 1]).hex()
    short = "0x02" + rlp.encode([1, 1, TIP, CAP, 21000, b"\x11" * 20, 0, b"", [], 0, 1]).hex()   # 11 items
    for raw in (raw1, raw4, short, "0x02", "0x02c0"):
        assert S.decode_signed_tx_cost(_b64(raw)) is None, raw[:6]


def test_a_legacy_payload_decodes_as_before():
    tx = {"chainId": 1, "nonce": 5, "gasPrice": CAP, "gas": 21000, "to": SWAP["to"], "value": 3, "data": "0x"}
    raw = "0x" + Account.from_key(KEY).sign_transaction(tx).raw_transaction.hex().removeprefix("0x")
    assert S.decode_signed_tx_cost(_b64(raw)) == {"nonce": 5, "gas_price": CAP, "gas_limit": 21000,
                                                  "value": 3, "worst_case_wei": 21000 * CAP + 3}


# ---------------------------------------------------------------- consumers of the bytes

def test_an_unadmitted_type2_close_is_labelled_never_admitted_gas():
    from sniper.services.memecoin_real_runner import evm_close_provably_dead
    tx = {"type": 2, "chainId": 1, "nonce": 40, "maxFeePerGas": CAP, "maxPriorityFeePerGas": TIP,
          "gas": 1_200_000, "to": SWAP["to"], "value": 0, "data": "0x"}
    raw = "0x" + Account.from_key(KEY).sign_transaction(tx).raw_transaction.hex().removeprefix("0x")
    pos = {"id": 1, "mint": "0x" + "aa" * 20, "close_submitted_at": "2026-09-30T00:00:00+00:00",
           "exit_intent_json": json.dumps({"nonce": 40, "balance_snapshot": 100.0,
                                           "signed_payload_b64": _b64(raw)})}
    native_eth = (1_200_000 * CAP - 1) / 1e18                   # one wei short of the cap bound
    verdict = evm_close_provably_dead(
        pos, confirmed_nonce_fn=lambda: 40, balance_fn=lambda _m: 100.0, pending_nonce_fn=lambda: 40,
        native_balance_fn=lambda: native_eth, now_iso="2026-09-30T00:05:00+00:00")
    assert verdict and verdict.startswith("never_admitted_gas")


def test_an_ambiguous_type2_preapproval_gives_its_nonce_back(monkeypatch, tmp_path):
    """The nonce-floor rewind reads the nonce from the signed bytes (spec §3)."""
    import importlib.util                                  # a site-packages `tests` shadows ours
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "preapprove_harness", Path(__file__).with_name("test_memecoin_evm_preapprove.py"))
    P = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(P)
    monkeypatch.setattr(S, "quote_usd_price", lambda *_a, **_k: 1.0)
    for name, value in {"MEMECOIN_EVM_APPROVAL_PIPELINE": "1", "MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS": P.CHAIN,
                        "MEMECOIN_EVM_PREAPPROVE_CHAINS": P.CHAIN, "MEMECOIN_EVM_TYPE2_CHAINS": P.CHAIN,
                        "MEMECOIN_EVM_TIP_GWEI_" + P.CHAIN: TIP_GWEI}.items():
        monkeypatch.setenv(name, value)
    path, c = P._db(tmp_path)
    pid = P._open(c)
    http, seen = P._http(send="ambiguous")
    ex = P._ex(http)
    ex["preapprove_position_fn"](path, pid)
    assert seen["sends"][0].startswith("0x02")
    assert ex["_sign"](dict(P._SWAP))[2] == 5               # the node's count, not approval nonce + 1
    assert P._rows(c)[0]["send_status"] == "ambiguous"


# ---------------------------------------------------------------- the gas guard's price

def test_the_expected_price_is_base_plus_tip_on_type2_and_the_signed_price_on_legacy(type2):
    assert _executor(_Rpc())["_expected_gas_price"]() == NODE_GP + TIP
    assert _executor(_Rpc(), chain="4663")["_expected_gas_price"]() == CAP


def test_chain_1_is_type2_by_default_and_an_empty_list_is_the_rollback(monkeypatch, caplog):
    """beta/eth-mainnet defaults MEMECOIN_EVM_TYPE2_CHAINS to "1" (Lambda 2026-09-30). Unset + tip:
    chain 1 signs type 2. Unset + no tip: legacy, and the build says so. "" lists nothing: legacy
    (the rollback; a restart applies it, the list is read when the executor is built)."""
    def chain1_envelope():
        raw = _executor(_Rpc(), chain="1")["_sign"](dict(SWAP))[0]
        return "type2" if bytes.fromhex(raw[2:])[0] == 2 else "legacy"
    monkeypatch.delenv("MEMECOIN_EVM_TYPE2_CHAINS", raising=False)
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", TIP_GWEI)
    assert chain1_envelope() == "type2"
    monkeypatch.delenv("MEMECOIN_EVM_TIP_GWEI_1")
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert chain1_envelope() == "legacy"
    assert any("MEMECOIN_EVM_TIP_GWEI_1" in r.getMessage() and r.levelno == logging.ERROR
               for r in caplog.records)
    monkeypatch.setenv("MEMECOIN_EVM_TIP_GWEI_1", TIP_GWEI)
    monkeypatch.setenv("MEMECOIN_EVM_TYPE2_CHAINS", "")
    assert chain1_envelope() == "legacy"

"""CLEDGER 72737: Ethereum sells and token approvals must not retain the low baseline tip."""
import json
import sqlite3

import pytest

from sniper.execution import chain1_protect as c1, evm_real_seams as S
from sniper.persistence import db_memecoin as m
from sniper.settings import memecoin as cfg
from test_memecoin_chain1_entry_tip_floor import (
    GWEI, KEY, SWAP, _entry_and_exit_raw, _fields, _rpc, headroom,
)
from eth_account import Account


def test_exit_floor_defaults_to_one_gwei_only_on_ethereum(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", raising=False)
    assert cfg.evm_exit_tip_floor_wei_for("1") == GWEI
    for chain in ("56", "4663", "8453", "5042", None):
        assert cfg.evm_exit_tip_floor_wei_for(chain) == 0
    for raw in ("abc", "nan", "inf", "1e308", "-1", " ", "0"):
        monkeypatch.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", raw)
        assert cfg.evm_exit_tip_floor_wei_for("1") == 0


def test_exit_floor_does_not_change_entry_or_unwrap_bytes(headroom):
    headroom.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", "1")
    entry, exit_raw, plain, _ex = _entry_and_exit_raw(headroom)
    assert _fields(exit_raw)["tip"] == GWEI
    assert entry == plain
    assert _fields(_entry_and_exit_raw.unwrap)["tip"] == int(0.1 * GWEI)


def test_token_approval_uses_the_exit_floor_and_still_waits_for_its_receipt(headroom):
    from sniper.execution import evm_exit_executor as E
    headroom.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", "1")
    headroom.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "1")
    headroom.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663,56")
    signed, confirmed = [], []
    real_sign = S.sign_tx

    def sign(tx, acct, **kw):
        out = real_sign(tx, acct, **kw)
        signed.append((tx, out[0]))
        return out

    headroom.setattr(S, "sign_tx", sign)
    headroom.setattr(S, "eth_send_raw", lambda *a, **kw: "0x" + "11" * 32)
    headroom.setattr(S, "eth_confirm", lambda *a, **kw: confirmed.append(kw) or "confirmed")

    def execute(*a, approve_fn, **kw):
        assert approve_fn("0x" + "aa" * 20, "0x" + "bb" * 20, 123) is True
        return {"status": "aborted", "reason": "fixture"}

    headroom.setattr(E, "execute_exit_evm", execute)
    _entry, _exit, _plain, ex = _entry_and_exit_raw(headroom)
    ex["exit_fn"](None, {"id": 1, "mint": "0x" + "aa" * 20, "network_id": "1"},
                  "manual", "2026-10-09T19:51:49Z")
    approvals = [raw for tx, raw in signed if tx["data"].startswith("0x095ea7b3")]
    assert len(approvals) == len(confirmed) == 1
    assert _fields(approvals[0])["tip"] == GWEI
    assert confirmed[0]["require_canonical"] is True


def test_post_fill_preapproval_uses_the_exit_floor_and_shared_nonce(headroom, tmp_path):
    import test_memecoin_evm_preapprove as P
    headroom.setattr(P, "CHAIN", "1")
    headroom.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE", "1")
    headroom.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663,56")
    headroom.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "1")
    headroom.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", "1")
    headroom.setattr(S, "quote_usd_price", lambda *a, **kw: 1.0)
    assert not cfg.memecoin_evm_approval_pipeline("1")
    path, c = P._db(tmp_path)
    pid = P._open(c)
    rpc, seen = P._http(balance=123456789012345678901234567)
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="1",
        rpc_url="https://rpc.flashbots.net/fast", http=rpc)
    headroom.setattr(S, "eth_confirm", lambda *a, **kw: "confirmed")
    ex["preapprove_position_fn"](path, pid)
    assert seen["sends"], P._rows(c)
    [raw] = seen["sends"]
    assert _fields(raw)["tip"] == GWEI
    assert ex["_sign"](dict(P._SWAP))[2] == 6
    [row] = P._rows(c)
    assert row["send_status"] == "accepted"
    assert row["amount_atomic"] == "123456789012345678901234567"


@pytest.mark.parametrize("rpc_args,status", [
    ({"balance": None}, "skipped_read"),
    ({"allowance": None}, "skipped_read"),
    ({"allowance": 1000}, "skipped_allowance"),
    ({"balance": 0}, "skipped_balance_zero"),
    ({"send": "rejected"}, "rejected"),
    ({"send": "ambiguous"}, "ambiguous"),
])
def test_ethereum_preapproval_failure_preserves_the_next_nonce(headroom, tmp_path, rpc_args, status):
    import test_memecoin_evm_preapprove as P
    headroom.setattr(P, "CHAIN", "1")
    headroom.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "1")
    headroom.setenv("MEMECOIN_EVM_APPROVAL_PIPELINE_CHAINS", "4663,56")
    path, c = P._db(tmp_path)
    pid = P._open(c)
    rpc, seen = P._http(**rpc_args)
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="1",
        rpc_url="https://rpc.flashbots.net/fast", http=rpc)
    headroom.setattr(S, "eth_confirm", lambda *a, **kw: "unknown")
    ex["preapprove_position_fn"](path, pid)
    assert P._rows(c)[0]["send_status"] == status
    assert ex["_sign"](dict(P._SWAP))[2] == 5


@pytest.mark.parametrize("boundary", ["gas", "sign"])
def test_ethereum_unsigned_preapproval_does_not_leave_a_nonce_gap(headroom, tmp_path, boundary):
    import test_memecoin_evm_preapprove as P
    headroom.setattr(P, "CHAIN", "1")
    headroom.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "1")
    path, c = P._db(tmp_path)
    pid = P._open(c)
    rpc, seen = P._http()
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="1",
        rpc_url="https://rpc.flashbots.net/fast", http=rpc)
    with headroom.context() as failure:
        if boundary == "gas":
            failure.setattr(S, "eth_gas_price", lambda *a, **kw: None)
        else:
            failure.setattr(S, "sign_tx", lambda *a, **kw: None)
        ex["preapprove_position_fn"](path, pid)
    assert not seen["sends"]
    assert P._rows(c)[0]["send_status"] == "not_sent"
    assert ex["_sign"](dict(P._SWAP))[2] == 5


@pytest.mark.parametrize("boundary", ["gas", "sign", "malformed"])
def test_ethereum_unsigned_swap_does_not_leave_a_nonce_gap(headroom, boundary):
    import test_memecoin_evm_preapprove as P
    headroom.setenv("MEMECOIN_EVM_PREAPPROVE_CHAINS", "1")
    rpc, _ = P._http()
    ex = S.build_real_evm_executor(acct=Account.create(), chain_id="1",
        rpc_url="https://rpc.flashbots.net/fast", http=rpc)
    with headroom.context() as failure:
        if boundary == "gas":
            failure.setattr(S, "eth_gas_price", lambda *a, **kw: None)
        elif boundary == "sign":
            failure.setattr(S, "sign_tx", lambda *a, **kw: None)
        tx = {} if boundary == "malformed" else dict(P._SWAP)
        if boundary == "gas":
            tx.pop("gasPrice")
        assert ex["_sign"](tx) is None
    assert ex["_sign"](dict(P._SWAP))[2] == 5


@pytest.mark.parametrize("gas", [600_000, 1_500_000])
def test_exit_floor_preserves_the_gas_reserve_cap(headroom, gas):
    headroom.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", "1")
    _entry, _exit, _plain, ex = _entry_and_exit_raw(headroom)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    raw = ex["_make_exit_sign_fn"](c, 1, "close_claim")(dict(SWAP, gas=gas))[0]
    fields = _fields(raw)
    assert fields["tip"] == min(GWEI, fields["max_fee"])
    assert fields["gas"] * fields["max_fee"] <= int(0.001 * 1e18)


def test_low_native_keeps_the_affordable_tip_instead_of_stranding_the_exit(headroom):
    headroom.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", "1")
    from sniper.execution import evm_wallet
    _entry, _raw, _plain, ex = _entry_and_exit_raw(headroom)
    native = 600_000_000_000_000
    headroom.setattr(evm_wallet, "native_balance_raw_or_none", lambda *a, **kw: native)
    raw = ex["_make_exit_sign_fn"](sqlite3.connect(":memory:"), 1, "close_claim")(
        dict(SWAP, value=0))[0]
    fields = _fields(raw)
    assert fields["tip"] == int(0.1 * GWEI)
    assert fields["gas"] * fields["max_fee"] <= native


def test_zero_exit_floor_restores_the_previous_signed_bytes(headroom):
    headroom.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", "0")
    _entry, raw, plain, _ex = _entry_and_exit_raw(headroom)
    assert raw == plain


def test_same_nonce_recovery_sell_also_gets_the_exit_floor(headroom):
    from sniper.execution import evm_exit_executor as E
    headroom.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", "1")
    _entry, _exit, plain, ex = _entry_and_exit_raw(headroom)
    captured = []

    def replace(*a, sign_fn, **kw):
        captured.append(sign_fn(dict(SWAP), nonce=7, replaces=plain))
        return {"status": "skipped"}

    headroom.setattr(E, "replace_exit_evm", replace)
    ex["replace_close_fn"](sqlite3.connect(":memory:"), {
        "id": 1, "mint": "0x" + "aa" * 20, "network_id": "1"})
    assert captured[0][2] == 7
    assert _fields(captured[0][0])["tip"] == GWEI


def test_exit_floor_does_not_add_rpc_reads(headroom):
    seen = []
    base_rpc = _rpc()

    def http(*a, **kw):
        seen.append(json.loads(kw["body"])["method"])
        return base_rpc(*a, **kw)

    ex = S.build_real_evm_executor(acct=Account.from_key(KEY), chain_id="1", rpc_url="rpc",
                                   http=http)
    c = sqlite3.connect(":memory:")
    headroom.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", "0")
    ex["_make_exit_sign_fn"](c, 1, "close_claim")(dict(SWAP))
    baseline = list(seen)
    seen.clear()
    headroom.setenv("MEMECOIN_EVM_EXIT_TIP_FLOOR_GWEI_1", "1")
    ex["_make_exit_sign_fn"](c, 1, "close_claim")(dict(SWAP))
    # The gas memo is warm on both runs after the first sign.
    assert [x for x in baseline if x != "eth_gasPrice"] == seen

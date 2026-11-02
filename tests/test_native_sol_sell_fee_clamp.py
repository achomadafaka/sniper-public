"""Real native SOL sell bytes pass through the common exit clamp before signing, offline."""
import base64
import struct

import pytest
from solders.keypair import Keypair
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from sniper.execution import pump_native_exit as native
from sniper.execution import solana_exit_executor as exits
from sniper.execution import solana_real_seams as seams
from sniper.execution import solana_sender_ab as sender
from sniper.execution.solana_swap_executor import signed_priority_fee_lamports
from sniper.settings import memecoin as cfg
from test_pump_sol_curve_sell_builder import AMOUNT, MINIMUM, MINT, Offline, sell_instruction

CAP = 5_000_000
REQUESTED = 100_000_000
CU_LIMIT = 600_000
CEILING = 3_000_000


def _quote(kp):
    io = Offline()
    quote = native.native_curve_sell(
        MINT, AMOUNT, 3, user=str(kp.pubkey()), rpc=io.rpc, jup_call=io.jup,
        priority_fee_microlamports=REQUESTED, compute_unit_limit=CU_LIMIT, packet_limit=1232)
    assert quote is not None and quote["_native_swap"]["simulated"] is True
    assert quote["_native_swap"]["bytes"] <= 1232
    assert io.bodies[0]["quoteResponse"]["inAmount"] == str(MINIMUM)
    tx, sell = sell_instruction(quote)
    assert all(signature == Signature.default() for signature in tx.signatures)
    assert bytes(sell.data) == bytes.fromhex("33e685a4017f83ad") + struct.pack("<QQ", AMOUNT, MINIMUM)
    assert signed_priority_fee_lamports(quote["_native_swap"]["swapTransaction"]) == 60_000_000
    return quote


def _assert_only_price_changed(original, updated):
    before = VersionedTransaction.from_bytes(base64.b64decode(original)).message
    after = VersionedTransaction.from_bytes(base64.b64decode(updated)).message
    assert before.header == after.header and before.account_keys == after.account_keys
    assert before.recent_blockhash == after.recent_blockhash
    assert before.address_table_lookups == after.address_table_lookups
    assert len(before.instructions) == len(after.instructions) == 6
    assert list(before.instructions[:1]) + list(before.instructions[2:]) == list(after.instructions[:1]) + list(after.instructions[2:])
    assert bytes(after.instructions[1].data) == b"\x03" + struct.pack("<Q", CAP)


def _assert_fee(info):
    assert info["clamped"] is True and info["bounded"] is True
    assert info["fee_ceiling_lamports"] == CEILING
    assert info["cap_micro_lamports"] == CAP and info["requested_micro_lamports"] == REQUESTED
    assert info["cu_limit"] == CU_LIMIT and info["tip_lamports"] == 0


def test_real_native_sol_sell_is_clamped_and_bounded_without_changing_sell_or_tail():
    quote = _quote(Keypair())
    original = quote["_native_swap"]["swapTransaction"]
    updated, info = sender.clamp_exit_priority(quote["_native_swap"], cap=CAP)
    _assert_fee(info)
    assert updated["simulated"] is True and updated["lastValidBlockHeight"] == 124
    assert signed_priority_fee_lamports(updated["swapTransaction"]) == CEILING
    _assert_only_price_changed(original, updated["swapTransaction"])
    assert quote["_native_swap"]["swapTransaction"] == original


def test_real_exit_sign_dispatches_native_prebuilt_and_clamps_before_custody_signature(monkeypatch):
    kp = Keypair(); quote = _quote(kp)
    original = quote["_native_swap"]["swapTransaction"]
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS", CAP)
    def network_forbidden(*_a, **_k):
        pytest.fail("offline native clamp regression reached a provider")
    monkeypatch.setattr(seams, "_rpc_call", network_forbidden)
    monkeypatch.setattr(seams, "_jup_call", network_forbidden)
    monkeypatch.setattr(seams.m, "record_exit_execution", lambda *_a, **_k: None)
    captured = {}
    def execute_exit(_conn, _position, _reason, _now, **kwargs):
        captured["sign_fn"] = kwargs["sign_fn"]
        return {"status": "stuck"}
    monkeypatch.setattr(exits, "execute_exit", execute_exit)
    executor = seams.build_real_solana_executor(
        keypair=kp, rpc_url="offline://forbidden", http=network_forbidden,
        token_fn=lambda *_a: 0, usdc_fn=lambda: 0)
    executor["exit_fn"](None, {"id": 1, "mint": MINT, "network_id": "solana"},
                        "b_stop", "2026-10-23T00:00:00Z")
    events = []; real_clamp = sender.clamp_exit_priority; real_sign = seams.sign_swap_tx
    def clamp(swap, *, cap):
        events.append("clamp")
        assert swap["swapTransaction"] == original  # real jupiter_swap_tx returned native bytes
        return real_clamp(swap, cap=cap)
    def sign(swap, signer):
        events.append("sign")
        assert signer is kp
        assert signed_priority_fee_lamports(swap["swapTransaction"]) == CEILING
        _assert_only_price_changed(original, swap["swapTransaction"])
        return real_sign(swap, signer)
    monkeypatch.setattr(sender, "clamp_exit_priority", clamp)
    monkeypatch.setattr(seams, "sign_swap_tx", sign)
    def before_sign():
        events.append("before_sign")
        assert signed_priority_fee_lamports(original) == 60_000_000
        return True
    sign_fn = captured["sign_fn"]
    signed = sign_fn(quote, before_sign=before_sign)
    assert signed is not None and events == ["before_sign", "clamp", "sign"]
    tx = VersionedTransaction.from_bytes(base64.b64decode(signed[0]))
    assert tx.verify_with_results() == [True]
    assert str(tx.signatures[0]) == signed[1]
    assert signed[2]["last_valid_height"] == 124
    assert signed_priority_fee_lamports(signed[0]) == CEILING
    _assert_only_price_changed(original, signed[0])
    _assert_fee(sign_fn.sign_timing()["exit_fee"])
    assert quote["_native_swap"]["swapTransaction"] == original

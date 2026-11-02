"""Ordinary Solana entry and pump-native exit prices are config seams; the defaults sign today's bytes.

2026-09-27, Alpha, under the operator's "improve/tune the system". Three seams:
  (a) the Jupiter entry build takes MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS. Unset keeps
      prioritizationFeeLamports "auto"; set sends computeUnitPriceMicroLamports and keeps the
      dynamic compute-unit limit, the way the exit path does.
  (b) the Raydium-fallback entry build takes the same knob. Unset keeps 25,000.
  (c) the pump-native exit takes its price from the exit_tier_plan fee (MEMECOIN_EXIT_PRIORITY_FEE_*),
      so it moves with the other exits. A disabled exit fee (0) keeps the native default, 200,000;
      the 600,000 compute-unit limit is out of scope and stays.
"""
import base64
import json
import os
import struct
import subprocess
import sys
from types import SimpleNamespace

import pytest
from solders.keypair import Keypair

from sniper.execution import solana_real_seams as S
from sniper.execution import solana_swap_executor as ex
from sniper.settings import memecoin as cfg

SOL = "So11111111111111111111111111111111111111112"


def _entry_sign(monkeypatch):
    """The seam's real entry sign function, captured the way the call-timing tests capture it."""
    # Synthetic payloads isolate the fee/build seam, outside automatic admission.
    monkeypatch.setattr(ex.m, "pending_row", lambda *_: {
        "strategy": "alpha_b", "source_user_id": "u1", "signal_source": "manual"})
    monkeypatch.setattr(ex, "execute_entry", lambda *a, **k: k["sign_fn"])
    monkeypatch.setattr(S, "sign_swap_tx", lambda swap, key: swap)
    executor = S.build_real_solana_executor(
        keypair=SimpleNamespace(pubkey=lambda: "offline-owner"), rpc_url="offline://rpc")
    return executor["execute_fn"](None, 1, "MINT", 10, "2026-09-27T00:00:00Z")


def _jupiter_body(monkeypatch, knob):
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS", knob)
    bodies = []

    def build_once(body, http, *, exclude=()):
        bodies.append(json.loads(body))
        return 200, {"swapTransaction": "TX"}, None, "h"
    monkeypatch.setattr(S, "_jup_build_once", build_once)
    assert _entry_sign(monkeypatch)({"inAmount": "10", "outputMint": "MINT"})
    return bodies[0]


def _raydium_body(monkeypatch, knob):
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS", knob)
    monkeypatch.setattr(S, "_mint_is_token_2022", lambda *_a: False)
    bodies = []

    def http_status(_http, method, url, body=None, **_k):
        bodies.append(json.loads(body))
        return 200, {"success": True, "data": [{"transaction": "TX"}]}, None
    monkeypatch.setattr(S, "_http_status", http_status)
    quote = {"_venue": "raydium", "inputMint": SOL, "outputMint": "MINT", "_raydium_response": {"r": 1}}
    assert _entry_sign(monkeypatch)(quote)
    return bodies[0]


# --- (a) the Jupiter entry ---------------------------------------------------------------------

@pytest.mark.usefixtures("fake_payload_clamp")
def test_unset_the_jupiter_entry_posts_exactly_todays_payload(monkeypatch):
    assert _jupiter_body(monkeypatch, None) == {
        "quoteResponse": {"inAmount": "10", "outputMint": "MINT"},
        "userPublicKey": "offline-owner", "wrapAndUnwrapSol": True,
        "dynamicComputeUnitLimit": True, "prioritizationFeeLamports": "auto"}


@pytest.mark.usefixtures("fake_payload_clamp")
def test_set_the_jupiter_entry_carries_the_price_and_keeps_the_dynamic_limit(monkeypatch):
    body = _jupiter_body(monkeypatch, 900_000)
    assert body["computeUnitPriceMicroLamports"] == 900_000
    assert body["dynamicComputeUnitLimit"] is True
    assert "prioritizationFeeLamports" not in body


# --- (b) the Raydium-fallback entry ------------------------------------------------------------

@pytest.mark.usefixtures("fake_payload_clamp")
def test_unset_the_raydium_entry_keeps_25000(monkeypatch):
    assert _raydium_body(monkeypatch, None)["computeUnitPriceMicroLamports"] == "25000"


@pytest.mark.usefixtures("fake_payload_clamp")
def test_set_the_raydium_entry_carries_the_price(monkeypatch):
    assert _raydium_body(monkeypatch, 900_000)["computeUnitPriceMicroLamports"] == "900000"


# --- (c) the pump-native exit ------------------------------------------------------------------

def _native_exit_tx(monkeypatch, *, enabled, fee):
    """The seam's native exit build over the offline curve fakes; returns (seam tx, default tx)."""
    import sniper.execution.pump_native_exit as N
    from test_memecoin_pump_native_exit import FRIES, MCDX, _fake_jup, _fake_rpc
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_PRIORITY_FEE_ENABLED", enabled)
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_PRIORITY_FEE_MICROLAMPORTS", fee)
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_PRIORITY_FEE_CAP_MICROLAMPORTS", 2_000_000)
    keypair = Keypair()
    rpc, fake_jup = _fake_rpc(), _fake_jup()

    def jup(method, path, body=None):   # the fake's swap instruction names its own signer
        status, j = fake_jup(method, path, body)
        if path == "/swap-instructions":
            j["swapInstruction"]["accounts"][0]["pubkey"] = str(keypair.pubkey())
        return status, j
    monkeypatch.setattr(S, "_rpc_call", lambda _url, method, params, _http: (200, {"result": rpc(method, params)}))
    monkeypatch.setattr(S, "_jup_call", lambda method, path, body=None, http=None: jup(method, path, body))
    monkeypatch.setattr(S, "token_balance_raw", lambda *_a, **_k: 5707019249939)
    monkeypatch.setattr(S, "jupiter_exit_quote", lambda *_a, **_k: {
        "inAmount": "5707019249939", "outAmount": "27000000", "otherAmountThreshold": "26000000",
        "routePlan": [{"swapInfo": {"label": "Pump.fun", "inputMint": FRIES, "outputMint": MCDX}}]})
    executor = S.build_real_solana_executor(keypair=keypair, rpc_url="https://rpc",
                                            http=lambda *_a, **_k: (200, {}, None),
                                            usdc_fn=lambda: 0.0, token_fn=lambda _m: 0.0)
    quote = executor["exit_quote_fn"](FRIES, 5707019.249939, 10.0)
    assert quote["_venue"] == "pump_native"
    default = N.build_native_exit(FRIES, 5707019249939, 10.0, user=executor["pubkey"],
                                  rpc=rpc, jup_call=jup)
    return quote["_native_swap"]["swapTransaction"], default["_native_swap"]["swapTransaction"]


def _cu_price(tx_b64):
    from solders.transaction import VersionedTransaction
    vt = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
    data = bytes(vt.message.instructions[1].data)
    assert data[0] == 3, "instruction 1 is SetComputeUnitPrice"
    return struct.unpack("<Q", data[1:9])[0]


@pytest.mark.parametrize("enabled,fee", [(True, 200_000), (False, 0), (False, 900_000)])
def test_todays_exit_fee_settings_build_the_same_native_exit_bytes(monkeypatch, enabled, fee):
    seam, default = _native_exit_tx(monkeypatch, enabled=enabled, fee=fee)
    assert seam == default and _cu_price(seam) == 200_000


def test_the_native_exit_moves_with_the_exit_fee(monkeypatch):
    seam, _ = _native_exit_tx(monkeypatch, enabled=True, fee=900_000)
    assert _cu_price(seam) == 900_000


# --- bounds -----------------------------------------------------------------------------------

def test_unset_parses_to_none():
    env = {k: v for k, v in os.environ.items() if k != "MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS"}
    out = subprocess.run([sys.executable, "-c", "from sniper.settings import memecoin as c;"
                          "print(repr(c.MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS))"],
                         env=env, text=True, capture_output=True, check=True, timeout=60)
    assert out.stdout.strip() == "None"


@pytest.mark.parametrize("value", ["0", "-5", "fast", 50_000_001, True])
def test_an_invalid_entry_price_fails_the_armed_config_check(monkeypatch, value):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS", value)
    with pytest.raises(Exception, match="MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS"):
        cfg.validate_real_exit_policy()


@pytest.mark.parametrize("value", [None, 1, 900_000, 50_000_000])
def test_a_valid_entry_price_passes_the_armed_config_check(monkeypatch, value):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS", value)
    try:
        cfg.validate_real_exit_policy()
    except Exception as exc:          # other knobs in the test env may fail; this one must not
        assert "MEMECOIN_ENTRY_CU_PRICE_MICROLAMPORTS" not in str(exc)


from entry_clamp_fake import fake_payload_clamp  # noqa: E402,F401

"""The tape binds a coin vault; its authority must bind back to that exact PumpSwap vault."""
import base64
import copy

import pytest
from solders.pubkey import Pubkey

from sniper.execution import solana_real_seams as seams
from sniper.execution.pump_native_exit import TOKEN_2022_PROGRAM, TOKEN_PROGRAM
from sniper.execution.pumpswap_buy import parse_pool_account
from sniper.services import memecoin_native_exit_rung as rung
from sniper.settings import memecoin as cfg
from test_pumpswap_native_exit import BASE_VAULT, POOL, STATE


@pytest.mark.parametrize("program", [TOKEN_PROGRAM, TOKEN_2022_PROGRAM])
def test_coin_vault_resolves_its_exact_pumpswap_authority(monkeypatch, program):
    account = copy.deepcopy(STATE["pool24"]["value"][1])
    account["owner"] = program
    calls = []
    def rpc(_url, method, params, _http):
        calls.append((method, params[0]))
        value = account if params[0] == BASE_VAULT else STATE["pool24"]["value"][0]
        return 200, {"result": {"value": value}}
    monkeypatch.setattr(seams, "_rpc_call", rpc)
    monkeypatch.setattr(cfg, "solana_read_rpc", lambda: "https://read.invalid")
    expected = parse_pool_account(STATE["pool24"]["value"][0]["data"][0])["virtual_quote"]
    assert rung._fetch_virtual_quote(BASE_VAULT) == expected
    assert calls == [("getAccountInfo", BASE_VAULT), ("getAccountInfo", POOL)]


@pytest.mark.parametrize("failure,want", [
    ("wrong_backbinding", rung.NOT_PUMPSWAP),
    ("short_vault", None),
    ("missing_pool", None),
    ("rpc_error", None),
    ("http_error", None),
    ("short_owner", None),
    ("wrong_vault_mint", rung.NOT_PUMPSWAP),
])
def test_vault_identity_does_not_turn_incomplete_reads_into_known_rejection(monkeypatch, failure, want):
    vault = copy.deepcopy(STATE["pool24"]["value"][1])
    pool = copy.deepcopy(STATE["pool24"]["value"][0])
    if failure == "wrong_backbinding":
        raw = bytearray(base64.b64decode(pool["data"][0]))
        raw[139:171] = bytes(Pubkey.from_string("11111111111111111111111111111111"))
        pool["data"][0] = base64.b64encode(raw).decode()
    elif failure == "short_vault":
        vault["data"][0] = base64.b64encode(b"short").decode()
    elif failure == "short_owner":
        vault["owner"] = "partial"
    elif failure == "wrong_vault_mint":
        raw = bytearray(base64.b64decode(vault["data"][0]))
        raw[:32] = bytes(Pubkey.from_string("11111111111111111111111111111111"))
        vault["data"][0] = base64.b64encode(raw).decode()
    calls = []
    def rpc(_url, _method, params, _http):
        calls.append(params[0])
        if failure == "rpc_error":
            return 200, {"error": {"code": -32000}}
        value = vault if params[0] == BASE_VAULT else None if failure == "missing_pool" else pool
        return (503 if failure == "http_error" else 200), {"result": {"value": value}}
    monkeypatch.setattr(seams, "_rpc_call", rpc)
    monkeypatch.setattr(cfg, "solana_read_rpc", lambda: "https://read.invalid")
    assert rung._fetch_virtual_quote(BASE_VAULT) == want
    assert len(calls) <= 2

"""Blocker 6 — exit sizing must carry RAW ATOMIC end-to-end, never reconstruct atomic from a human
float. Memecoin balances (1e9+ tokens at 6-9 decimals) exceed 2^53, so `int(uiAmount * 10**dec)` loses
integer precision -> the sell amount != the true on-chain balance -> revert/false-stuck or dust-drift.
The raw readers return the exact on-chain integer."""
import pytest

from sniper.execution import evm_wallet as EW
from sniper.execution import solana_wallet as SW

# a balance whose atomic value is far past 2^53 (float64 integer precision ceiling ~9.007e15)
_BIG_ATOMIC = 123456789012345678          # ~1.23e17
assert _BIG_ATOMIC > 2 ** 53


def _sol_account(amount_str, pubkey="account"):
    return {"pubkey": pubkey, "account": {"owner": SW._TOKEN_PROGRAM, "data": {"parsed": {
        "type": "account", "info": {"owner": "pk", "mint": "mint", "tokenAmount": {
            "amount": amount_str, "decimals": 9, "uiAmount": float(amount_str) / 1e9}}}}}}


def _sol_http(amount_str):
    def http(method, url, headers=None, body=None, timeout=10):
        return 200, {"result": {"value": [_sol_account(amount_str)]}}, None
    return http


def test_solana_raw_balance_is_exact_past_2_53():
    raw = SW.token_balance_raw("pk", "mint", "https://rpc", http=_sol_http(str(_BIG_ATOMIC)))
    assert raw == _BIG_ATOMIC                                   # exact integer, no float loss
    # the human path would NOT round-trip to the exact atomic
    human = SW.token_balance("pk", "mint", "https://rpc", http=_sol_http(str(_BIG_ATOMIC)))
    assert int(human * 1e9) != _BIG_ATOMIC                      # demonstrates the precision loss avoided


def test_solana_raw_balance_sums_accounts():
    def http(method, url, headers=None, body=None, timeout=10):
        return 200, {"result": {"value": [
            _sol_account("100", "first"), _sol_account("250", "second"),
        ]}}, None
    assert SW.token_balance_raw("pk", "mint", "https://rpc", http=http) == 350


def test_solana_raw_balance_failure_is_unknown():
    http = lambda *a, **k: (200, {"error": "x"}, None)
    with pytest.raises(SW.TokenBalanceReadError, match="json_rpc_error"):
        SW.token_balance_raw("pk", "mint", "https://rpc", http=http)


def test_evm_raw_balance_is_exact_past_2_53():
    http = lambda *a, **k: (200, {"result": hex(_BIG_ATOMIC)}, None)
    assert EW.erc20_balance_raw("0xowner", "0xtok", "https://rpc", http=http) == _BIG_ATOMIC


def test_evm_raw_balance_fail_closed():
    http = lambda *a, **k: (200, {"result": "0x"}, None)
    assert EW.erc20_balance_raw("0xowner", "0xtok", "https://rpc", http=http) == 0


def test_evm_native_raw_balance_keeps_the_atomic_gas_target_exact():
    raw = _BIG_ATOMIC + 7
    http = lambda *a, **k: (200, {"result": hex(raw)}, None)
    assert EW.native_balance_raw_or_none("0xowner", "https://rpc", http=http, backoff_s=0) == raw

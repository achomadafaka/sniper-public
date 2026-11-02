"""A failed balance read must raise, not report a confident zero.

Solana sizing and reconciliation readers preserve unknown; EVM checked reads do the same. See
2026-07-29 pos#23496, alerted as a 100% ledger-vs-chain drift while the wallet held every token and
both Solana RPCs were returning 429.
"""
import pytest

from sniper.execution import evm_wallet, solana_wallet


PUB = "3EKhb6kyNvNhoAtF9CXxBfneTn4A31TxWvKn5Ppj9bLH"
MINT = "6oGuFDbEeaSzTcvrmmd2MqfNYwHKXFoN7regcR22pump"


def _http(response=None, exc=None):
    def call(_method, _url, headers=None, body=None, timeout=10):
        if exc is not None:
            raise exc
        return 200, response, None
    return call


def _accounts(amount, decimals=6):
    return {"result": {"value": [{"account": {"data": {"parsed": {"info": {"tokenAmount": {
        "amount": str(amount), "decimals": decimals}}}}}}]}}


def test_solana_plain_reader_also_preserves_unknown_on_failure():
    http = _http(exc=RuntimeError("HTTP Error 429: Too Many Requests"))
    with pytest.raises(solana_wallet.TokenBalanceReadError):
        solana_wallet.token_balance(PUB, MINT, "http://rpc", http=http)


def test_solana_checked_reader_raises_on_transport_failure():
    http = _http(exc=RuntimeError("HTTP Error 429: Too Many Requests"))
    with pytest.raises(Exception):
        solana_wallet.token_balance_checked(PUB, MINT, "http://rpc", http=http)


def test_solana_checked_reader_raises_on_jsonrpc_error_envelope():
    http = _http(response={"error": {"code": -32005, "message": "rate limit"}})
    with pytest.raises(Exception):
        solana_wallet.token_balance_checked(PUB, MINT, "http://rpc", http=http)


def test_solana_checked_reader_returns_the_real_balance():
    http = _http(response=_accounts(39178307637))
    assert solana_wallet.token_balance_checked(
        PUB, MINT, "http://rpc", http=http) == pytest.approx(39178.307637)


def test_solana_checked_reader_returns_zero_for_a_successful_empty_read():
    """No token account is a real, trustworthy zero — it must NOT raise, or true drift goes unseen."""
    http = _http(response={"result": {"value": []}})
    assert solana_wallet.token_balance_checked(PUB, MINT, "http://rpc", http=http) == 0.0


def test_evm_checked_reader_raises_on_failure_but_reads_a_real_balance():
    owner, token = "0x" + "ab" * 20, "0x" + "cd" * 20
    with pytest.raises(Exception):
        evm_wallet.erc20_balance_checked(
            owner, token, "http://rpc", http=_http(exc=RuntimeError("boom")), decimals=6)
    http = _http(response={"result": hex(39178307637)})
    assert evm_wallet.erc20_balance_checked(
        owner, token, "http://rpc", http=http, decimals=6) == pytest.approx(39178.307637)
    # the plain reader keeps its fail-closed zero for the sell-sizing callers
    assert evm_wallet.erc20_balance(
        owner, token, "http://rpc", http=_http(exc=RuntimeError("boom")), decimals=6) == 0.0

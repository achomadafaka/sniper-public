"""A failed read is a failed read: never a crash, never a null (2026-10-07, Alpha P1(a)).

8453, 2026-10-07 16:27:21, 17:07:41, 17:20:29 and 17:24:33Z: four delivered transfers died in
`push lane: transfers handler failed ... KeyError: 'result'`. push_detection's default read did
`_counted_rpc(...)["result"]`, so a JSON-RPC error body from the read host raised out of the
handler: no outcome line, no retry, the tx not even named in the log. Each could have been an armed
buy. Here an error body (or a transport failure) on the tx or receipt read is recorded as
read_error with the provider's words, re-read like a null within the existing budget, and left
undecided. Every other method keeps raising (now RpcErrorBody, not KeyError): the relay decoder
turns a raised chainId/getCode read into contract_code_unavailable (retryable), where a None would
read as chain_mismatch, a decided no_buy.
"""
import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg

WALLET = "0x" + "ab" * 20
MINT = "0x" + "cd" * 20
TX = "0x" + "22" * 32
BUY = {"mint": MINT, "token_delta": 1.0, "source_size_usd": 100.0, "source_price": 100.0}
RECEIPT = {"logs": [], "status": "0x1", "blockNumber": "0x10"}
ERROR = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "header not found"}}


@pytest.fixture(autouse=True)
def _chain(monkeypatch):
    w._configure("8453")
    monkeypatch.setattr(w, "erc20_decimals", lambda mint, rpc_url: 18)
    monkeypatch.setattr(w, "direct_buy_from_receipt",
                        lambda receipt, tx, owner, **kw: dict(BUY) if receipt else None)
    monkeypatch.setattr(w, "RELAY_ENVELOPE_CHAINS", frozenset())
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S", 2.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_MAX_RETRIES", 4, raising=False)
    yield
    w._configure("4663")


class _Rpc:
    """The read host behind the DEFAULT read_fn: the receipt answers an error body (or raises)
    for the first `bad` reads, then a real receipt."""

    def __init__(self, bad, *, raise_exc=None, method="eth_getTransactionReceipt"):
        self.bad, self.raise_exc, self.method, self.calls = bad, raise_exc, method, []

    def __call__(self, url, method, params):
        self.calls.append(method)
        if method == self.method and self.calls.count(method) <= self.bad:
            if self.raise_exc is not None:
                raise self.raise_exc
            return dict(ERROR)
        if method == "eth_getTransactionReceipt":
            return {"jsonrpc": "2.0", "id": 1, "result": dict(RECEIPT)}
        if method == "eth_getTransactionByHash":
            return {"jsonrpc": "2.0", "id": 1, "result": {"from": WALLET, "blockHash": "0x1"}}
        return dict(ERROR)


def _detect(monkeypatch, rpc):
    monkeypatch.setattr(w, "_rpc", rpc)
    out = {}
    row = w.push_detection("http://unused", {"transactionHash": TX, "address": MINT},
                           {WALLET: ("u1", "h1")}, decimals_cache={MINT: 18}, quote_usd=2650.0,
                           now=w.now_iso(), outcome=out, sleep_fn=lambda s: None)
    return row, out


def test_an_error_body_on_the_receipt_read_is_a_read_error_not_a_crash(monkeypatch):
    """THE regression: KeyError('result') raised out of push_detection on 658ea184b."""
    rpc = _Rpc(bad=99)
    row, out = _detect(monkeypatch, rpc)
    assert row is None and out["reason"] == "read_error"
    assert "header not found" in out["read_errors"]["receipt"]
    assert rpc.calls.count("eth_getTransactionReceipt") == 5        # re-read like a null: 1 + 4
    assert not w._detection_complete(out)                           # undecided, as a null read


def test_an_error_body_then_a_receipt_is_detected(monkeypatch):
    row, out = _detect(monkeypatch, _Rpc(bad=1))
    assert row is not None and out["reason"] == "detected" and out["retries"] == 1
    assert not out.get("read_errors")            # the last read was clean: no stale error left


def test_a_transport_failure_on_the_tx_read_is_a_read_error(monkeypatch):
    rpc = _Rpc(bad=99, raise_exc=RuntimeError("HTTP 429"), method="eth_getTransactionByHash")
    row, out = _detect(monkeypatch, rpc)
    assert row is None and out["reason"] == "read_error"
    assert "HTTP 429" in out["read_errors"]["tx"]


def test_a_clean_null_after_an_error_is_a_null(monkeypatch):
    """The reason follows the LAST read: an error then a clean null is receipt_null."""
    calls = []

    def rpc(url, method, params):
        calls.append(method)
        if method == "eth_getTransactionReceipt":
            return dict(ERROR) if calls.count(method) == 1 else {"jsonrpc": "2.0", "result": None}
        return {"jsonrpc": "2.0", "result": {"from": WALLET, "blockHash": "0x1"}}
    row, out = _detect(monkeypatch, rpc)
    assert row is None and out["reason"] == "receipt_null"


def test_every_other_method_still_raises_a_named_error(monkeypatch):
    """The relay decoder's eth_chainId / eth_getCode reads must keep RAISING on an error body:
    its broad except makes that contract_code_unavailable (retryable). A None would compare as
    chain_mismatch, a decided no_buy."""
    monkeypatch.setattr(w, "_rpc", lambda url, method, params: dict(ERROR))
    with pytest.raises(w.RpcErrorBody, match="header not found"):
        w._rpc_result("http://unused", "eth_chainId", [])


def test_the_poll_head_read_names_the_providers_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")

    class _Conn:
        def close(self):
            pass
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_8453", "http://unused")
    monkeypatch.setattr(w.time, "sleep", lambda s: None)
    monkeypatch.setattr(w, "_rpc", lambda url, method, params: dict(ERROR))
    w.run_poll(duration_s=0.05, period_s=0.01)
    out = capsys.readouterr().out
    assert "eth_blockNumber failed" in out and "header not found" in out and "KeyError" not in out


def _run_push_with(monkeypatch, tmp_path, detection):
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")

    class _Conn:
        def close(self):
            pass
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "push_detection", detection)
    raised = []

    def fake_stream(ws_url, **kwargs):
        try:                              # the real stream catches and logs a handler's raise
            kwargs["on_transfer"]({"transactionHash": TX, "address": MINT})
        except Exception as exc:
            raised.append(exc)
        return kwargs.get("state")
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    return raised


def test_the_read_error_line_carries_the_providers_words(monkeypatch, capsys, tmp_path):
    def read_error(rpc_url, log, watched, *, outcome, **kw):
        outcome.update(reason="read_error", retries=4, read_errors={"receipt": "RpcErrorBody: x"})
        return None
    _run_push_with(monkeypatch, tmp_path, read_error)
    line = [l for l in capsys.readouterr().out.splitlines() if "[onchain-evm] transfer" in l][0]
    assert "outcome=read_error" in line and "read_error=receipt:RpcErrorBody: x" in line
    assert TX[:12] in line


def test_any_other_handler_raise_names_the_tx_then_propagates(monkeypatch, capsys, tmp_path):
    def broken(rpc_url, log, watched, *, outcome, **kw):
        raise ValueError("decode exploded")
    raised = _run_push_with(monkeypatch, tmp_path, broken)
    assert len(raised) == 1 and isinstance(raised[0], ValueError)      # the push lane still logs it
    line = [l for l in capsys.readouterr().out.splitlines() if "outcome=handler_error" in l][0]
    assert TX[:12] in line and "ValueError: decode exploded" in line

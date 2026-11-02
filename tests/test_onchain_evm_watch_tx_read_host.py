"""The EVM watcher reads a just-delivered transaction from the WS provider's own HTTP endpoint first
(2026-09-28, Alpha GO).

Measured 2026-09-28: after the null-read retry went live, every 4663 transfer needed at least one
retry because the read host (the custody endpoint) returned a NULL transaction at arrival and 0.4 s
later, while the WS provider's HTTP endpoint (robinhood-rpc.publicnode.com) served a transaction from
its own newest block on the FIRST read 10 of 10 times, in 39-73 ms. Detection took 2.0 s after arrival
and J7 decided at +0.29 s.

MEMECOIN_ONCHAIN_EVM_TX_READ_URL_<chain> (default empty = today, byte for byte) names that endpoint.
The transaction and its receipt are read there first; a 403, a 429, a timeout, a JSON-RPC error or a
null falls back to the read host with the existing null retry, so the new host can never drop a
transfer. A 403 or 429 cools the host down (MEMECOIN_ONCHAIN_EVM_TX_READ_COOLDOWN_S) so a
rate-limiting provider is not hammered. Every outcome line and detection says which host answered and
how long it took.
"""
import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg

WALLET = "0x" + "ab" * 20
MINT = "0x" + "cd" * 20
TX = "0x" + "11" * 32
BUY = {"mint": MINT, "token_delta": 1.0, "source_size_usd": 100.0, "source_price": 100.0}
RECEIPT = {"logs": [], "status": "0x1", "blockNumber": "0x10"}
TXBODY = {"from": WALLET}


@pytest.fixture(autouse=True)
def _chain(monkeypatch):
    w._configure("4663")
    monkeypatch.setattr(w, "erc20_decimals", lambda mint, rpc_url: 18)
    monkeypatch.setattr(w, "direct_buy_from_receipt",
                        lambda receipt, tx, owner, **kw: dict(BUY) if receipt and tx else None)
    monkeypatch.setattr(w, "RELAY_ENVELOPE_CHAINS", frozenset())
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S", 2.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_MAX_RETRIES", 4, raising=False)
    yield
    w._configure("4663")


class _Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class _Post:
    """A scripted HTTP endpoint. Answers are queued PER METHOD (the receipt is read on a worker
    thread beside the tx, so call order between the two is not fixed); `any_` serves both."""

    def __init__(self, *any_, tx=(), receipt=()):
        self.queues = {"eth_getTransactionByHash": list(tx), "eth_getTransactionReceipt": list(receipt)}
        self.any_, self.calls = list(any_), []

    def __call__(self, url, body, timeout):
        self.calls.append(body["method"])
        queue = self.queues.get(body["method"]) or self.any_
        answer = queue.pop(0) if queue else (200, {"result": None})
        if isinstance(answer, Exception):
            raise answer
        return answer


def _base(receipt_nulls=0, tx_nulls=0):
    calls = []

    def rpc(url, method, params):
        calls.append(method)
        n = calls.count(method)
        if method == "eth_getTransactionReceipt":
            return {"result": None if n <= receipt_nulls else dict(RECEIPT)}
        return {"result": None if n <= tx_nulls else dict(TXBODY)}
    return rpc, calls


def _detect(monkeypatch, host, base, outcome=None):
    monkeypatch.setattr(w, "_counted_rpc", base)
    outcome = {} if outcome is None else outcome
    row = w.push_detection("http://read-host", {"transactionHash": TX, "address": MINT},
                           {WALLET: ("u1", "h1")}, decimals_cache={MINT: 18}, quote_usd=2650.0,
                           now=w.now_iso(), outcome=outcome, sleep_fn=lambda s: None,
                           tx_read_host=host)
    return row, outcome


def test_an_empty_knob_reads_exactly_as_before(monkeypatch):
    monkeypatch.delenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663", raising=False)
    assert w.tx_read_host_from_env() is None
    base, calls = _base()
    row, out = _detect(monkeypatch, None, base)
    assert row is not None and sorted(calls) == ["eth_getTransactionByHash", "eth_getTransactionReceipt"]
    assert out["hosts"] == {"tx": "read_host", "receipt": "read_host"}


def test_the_knob_builds_a_host(monkeypatch):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_TX_READ_URL_4663", "https://robinhood-rpc.publicnode.com")
    host = w.tx_read_host_from_env()
    assert host is not None and host.url == "https://robinhood-rpc.publicnode.com"


def test_the_tx_read_host_answers_first(monkeypatch):
    post = _Post(receipt=[(200, {"result": dict(RECEIPT)})], tx=[(200, {"result": dict(TXBODY)})])
    host = w.TxReadHost("https://pn", cooldown_s=60, timeout_s=1.5, post_fn=post, clock=_Clock())
    base, calls = _base()
    row, out = _detect(monkeypatch, host, base)
    assert row is not None and calls == []                    # the read host was never asked
    assert out["hosts"] == {"tx": "tx_read", "receipt": "tx_read"} and out["retries"] == 0


def test_a_403_falls_back_to_the_read_host_and_detects(monkeypatch):
    post = _Post((403, None), (403, None))
    clock = _Clock()
    host = w.TxReadHost("https://pn", cooldown_s=60, timeout_s=1.5, post_fn=post, clock=clock)
    base, calls = _base()
    row, out = _detect(monkeypatch, host, base)
    assert row is not None and out["hosts"] == {"tx": "read_host", "receipt": "read_host"}
    # The receipt worker and the tx read race to the host: one sees the 403, the other "cooling".
    assert "http_403" in out["tx_read_miss"].values()


def test_a_403_cools_the_host_down(monkeypatch):
    post = _Post((403, None))
    clock = _Clock()
    host = w.TxReadHost("https://pn", cooldown_s=60, timeout_s=1.5, post_fn=post, clock=clock)
    assert host.read("eth_getTransactionByHash", [TX]) == (None, "http_403")
    clock.t += 30
    assert host.read("eth_getTransactionByHash", [TX]) == (None, "cooling")
    assert len(post.calls) == 1                               # not hammered while cooling
    clock.t += 31
    post.queues["eth_getTransactionByHash"].append((200, {"result": dict(TXBODY)}))
    assert host.read("eth_getTransactionByHash", [TX]) == (dict(TXBODY), None)


def test_a_429_cools_too_and_a_timeout_does_not(monkeypatch):
    clock = _Clock()
    host = w.TxReadHost("https://pn", cooldown_s=60, timeout_s=1.5,
                        post_fn=_Post((429, None)), clock=clock)
    assert host.read("eth_getTransactionByHash", [TX]) == (None, "http_429")
    assert host.read("eth_getTransactionByHash", [TX]) == (None, "cooling")
    host = w.TxReadHost("https://pn", cooldown_s=60, timeout_s=1.5,
                        post_fn=_Post(TimeoutError("slow"), (200, {"result": dict(TXBODY)})),
                        clock=clock)
    assert host.read("eth_getTransactionByHash", [TX]) == (None, "transport")
    assert host.read("eth_getTransactionByHash", [TX]) == (dict(TXBODY), None)


def test_a_json_rpc_error_is_a_miss_not_an_answer(monkeypatch):
    host = w.TxReadHost("https://pn", cooldown_s=60, timeout_s=1.5,
                        post_fn=_Post((200, {"error": {"code": -32000, "message": "nope"}})),
                        clock=_Clock())
    assert host.read("eth_getTransactionByHash", [TX]) == (None, "rpc_error")


def test_null_on_both_hosts_takes_the_retry_path(monkeypatch):
    # The tx-read host answers null for the tx twice, then has it; the read host is null too.
    post = _Post(receipt=[(200, {"result": dict(RECEIPT)})],
                 tx=[(200, {"result": None}), (200, {"result": None}), (200, {"result": dict(TXBODY)})])
    host = w.TxReadHost("https://pn", cooldown_s=60, timeout_s=1.5, post_fn=post, clock=_Clock())
    base, calls = _base(tx_nulls=99)
    row, out = _detect(monkeypatch, host, base)
    assert row is not None and out["retries"] == 2 and out["hosts"]["tx"] == "tx_read"
    assert calls.count("eth_getTransactionByHash") == 2        # the fallback ran on each null


def test_the_outcome_line_names_the_host_and_its_latency(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")

    class _Conn:
        def close(self):
            pass
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})

    def no_buy(rpc_url, log, watched, *, outcome, **kw):
        outcome.update(reason="no_buy", retries=0, hosts={"tx": "tx_read", "receipt": "read_host"},
                       host_ms={"tx": 52.0, "receipt": 310.0})
        return None
    monkeypatch.setattr(w, "push_detection", no_buy)

    def fake_stream(ws_url, **kwargs):
        kwargs["on_transfer"]({"transactionHash": TX, "address": MINT})
        return kwargs.get("state")
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    line = [l for l in capsys.readouterr().out.splitlines() if "[onchain-evm] transfer" in l][0]
    assert "tx_host=tx_read/52ms" in line and "receipt_host=read_host/310ms" in line

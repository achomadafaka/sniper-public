"""The EVM wallet watcher must not drop a delivered buy in silence (2026-09-28, Alpha GO).

Measured 2026-09-28: since the 06:43Z go-live the watchers detected 1 of 13 J7 4663 buys by watched
addresses and 0 of 2 on 56, although the watcher's own decode reads 12 of the 14 misses as buys. The
Transfer to the watched address is in every one of those receipts. On 56 the read node (defibit)
answered a null receipt for 2 of 15 transactions from the WS provider's newest block, and
push_detection took the null as final: no buy, no retry, no line.

Fixed here: a null tx or receipt is re-read within a bounded time AND attempt budget
(MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S, _MAX_RETRIES; 0 = the old single read), every retry and
every non-detection says so in a UTC-stamped line, the poll loop prints a provider's JSON-RPC error
instead of KeyError('result'), and the push stream's staleness check reads the clock AFTER a handler
returns, so a slow handler cannot make a stalled socket look healthy.
"""
import json

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg

WALLET = "0x" + "ab" * 20
MINT = "0x" + "cd" * 20
TX = "0x" + "11" * 32
BUY = {"mint": MINT, "token_delta": 1.0, "source_size_usd": 100.0, "source_price": 100.0}
RECEIPT = {"logs": [], "status": "0x1", "blockNumber": "0x10"}


@pytest.fixture(autouse=True)
def _chain(monkeypatch):
    w._configure("4663")
    monkeypatch.setattr(w, "erc20_decimals", lambda mint, rpc_url: 18)
    # A buy exactly when the receipt is present: the decode itself is tested elsewhere.
    monkeypatch.setattr(w, "direct_buy_from_receipt",
                        lambda receipt, tx, owner, **kw: dict(BUY) if receipt else None)
    monkeypatch.setattr(w, "RELAY_ENVELOPE_CHAINS", frozenset())
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S", 2.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_MAX_RETRIES", 4, raising=False)
    yield
    w._configure("4663")


class _Reads:
    """Receipt is null for the first `nulls` reads, then real. Counts every read."""

    def __init__(self, nulls, tx_nulls=0):
        self.nulls, self.tx_nulls, self.calls = nulls, tx_nulls, []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "eth_getTransactionReceipt":
            n = sum(1 for c in self.calls if c == method)
            return None if n <= self.nulls else dict(RECEIPT)
        n = sum(1 for c in self.calls if c == method)
        return None if n <= self.tx_nulls else {"from": WALLET}


def _detect(reads, **kw):
    out = {}
    row = w.push_detection("http://unused", {"transactionHash": TX, "address": MINT},
                           {WALLET: ("u1", "h1")}, decimals_cache={MINT: 18}, quote_usd=2650.0,
                           now=w.now_iso(), read_fn=reads, outcome=out,
                           sleep_fn=lambda s: None, **kw)
    return row, out


def test_a_null_receipt_then_a_real_one_is_detected():
    """THE regression: fails with the retry at 0 (see the next test)."""
    reads = _Reads(nulls=1)
    row, out = _detect(reads)
    assert row is not None and row["wallet"] == WALLET
    assert out["reason"] == "detected" and out["retries"] == 1


def test_with_the_retry_off_the_null_receipt_is_dropped_as_before(monkeypatch, capsys):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S", 0.0)
    reads = _Reads(nulls=1)
    row, out = _detect(reads)
    assert row is None and out["reason"] == "receipt_null" and out["retries"] == 0
    assert reads.calls.count("eth_getTransactionReceipt") == 1


def test_the_retry_is_bounded_by_attempts_and_says_each_one(capsys):
    reads = _Reads(nulls=99)
    row, out = _detect(reads)
    assert row is None and (out["reason"], out["retries"]) == ("receipt_null", 4)
    assert reads.calls.count("eth_getTransactionReceipt") == 5          # the read plus 4 retries
    lines = [l for l in capsys.readouterr().out.splitlines() if "null read retry" in l]
    assert len(lines) == 4 and all(l[:4] == "2026" or l[:2] == "20" for l in lines)


def test_the_retry_is_bounded_by_time():
    clock = iter([0.0, 0.5, 1.2, 2.1, 9.0, 9.0, 9.0])
    reads = _Reads(nulls=99)
    row, out = _detect(reads, clock=lambda: next(clock))
    # Deadline 2.0 s from 0.0: retries at 0.5 and 1.2 run, 2.1 is past it.
    assert row is None and out["retries"] == 2


def test_a_null_tx_is_retried_too():
    reads = _Reads(nulls=0, tx_nulls=2)
    row, out = _detect(reads)
    assert row is not None and out["retries"] == 2
    assert reads.calls.count("eth_getTransactionReceipt") == 1          # the receipt is not re-read


def test_a_delivered_transfer_that_is_not_detected_says_why(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")

    class _Conn:                           # no write on a miss: a write would fail on this stub
        def close(self):
            pass
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})

    def no_buy(rpc_url, log, watched, *, outcome, **kw):
        outcome.update(reason="receipt_null", retries=4)
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
    assert "outcome=receipt_null" in line and "retries=4" in line and TX[:12] in line
    assert line.split()[0].endswith("+00:00")                           # UTC-stamped


def test_poll_prints_the_providers_error_not_a_key_error(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")

    class _Conn:
        def close(self):
            pass
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_4663", "http://unused")
    monkeypatch.setattr(w.time, "sleep", lambda s: None)

    def rpc(url, method, params):
        if method == "eth_blockNumber":
            return {"jsonrpc": "2.0", "id": 1, "result": "0x20"}
        return {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "limit exceeded"}}
    monkeypatch.setattr(w, "_rpc", rpc)
    w.run_poll(duration_s=0.05, period_s=0.01)
    out = capsys.readouterr().out
    assert "limit exceeded" in out and "KeyError" not in out


def test_a_slow_handler_cannot_hide_a_stalled_socket():
    """The staleness check used the clock read BEFORE the handler ran, so time spent in a handler
    (a null-read retry) was invisible to it."""
    t = [0.0]

    def now():
        return t[0]

    head = json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                       "params": {"subscription": "h", "result": {"number": "0x1"}}})
    xfer = json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                       "params": {"subscription": "x", "result": {"transactionHash": TX}}})

    class _WS:
        def __init__(self):
            self.frames = [json.dumps({"id": 1, "result": "h"}), json.dumps({"id": 2, "result": "x"}),
                           head, xfer]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send(self, _):
            pass

        def __iter__(self):
            for f in self.frames:
                yield f
            stop[0] = True

    stop = [False]
    state = P.PushState()

    def slow_transfer(log):
        t[0] += 16.0                      # a handler that took longer than the 15 s staleness bound

    connects = []

    def connect(url, open_timeout=10):
        connects.append(url)
        return _WS()

    # Stop on the second connect: a reconnect happened, or the frames ran out without one.
    P.run_push_stream("wss://x", on_transfer=slow_transfer, on_initialize=lambda log: None,
                      addresses_fn=lambda: [WALLET], pool_manager=None, init_topic="0x00",
                      state=state, connect_fn=connect, sleep_fn=lambda s: None, now_fn=now,
                      stale_after_s=15.0, should_stop=lambda: stop[0] or len(connects) >= 2)
    assert state.reconnects == 1


def test_a_relay_decode_miss_names_its_reason(monkeypatch):
    """A delivered relay transfer decided no_buy says WHICH decode step refused (2026-09-28): the
    11:56:31Z notanicecat69 miss printed outcome=no_buy with no reason, so its cause had to be
    reconstructed from a replay."""
    import sniper.services.memecoin_evm_onchain_watch as ow
    monkeypatch.setattr(w, "RELAY_ENVELOPE_CHAINS", frozenset({"4663"}))
    monkeypatch.setattr(w, "direct_buy_from_receipt", lambda *a, **k: None)

    def refuse(*a, evidence=None, **k):
        assert k.get("live") is True, "the watcher decodes LIVE events: an errored read may re-ask head"
        evidence["reason"] = "contract_code_unavailable"
        return None
    monkeypatch.setattr(ow, "read_relayer_purchase", refuse)
    row, out = _detect(_Reads(0))
    assert row is None and out["reason"] == "no_buy"
    assert out["decode"] == "contract_code_unavailable"


def test_the_no_buy_line_carries_the_decode_reason(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")

    class _Conn:
        def close(self):
            pass
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})

    def no_buy(rpc_url, log, watched, *, outcome, **kw):
        outcome.update(reason="no_buy", retries=0, decode="unsupported_relayer_swap")
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
    assert "outcome=no_buy" in line and "decode=unsupported_relayer_swap" in line


class _MempoolReads:
    """BSC read host (2026-09-28): the receipt is null on the first read while the tx comes back
    from the mempool (`blockHash` null); both are mined on the next read."""

    def __init__(self, pending_reads=1):
        self.pending_reads, self.calls = pending_reads, []

    def __call__(self, method, params):
        self.calls.append(method)
        n = sum(1 for c in self.calls if c == method)
        if method == "eth_getTransactionReceipt":
            return None if n <= 1 else dict(RECEIPT, blockHash="0xB1")
        mined = n > self.pending_reads
        return {"from": WALLET, "blockHash": "0xb1" if mined else None}


def test_a_mempool_tx_is_reread_until_it_carries_the_receipts_block(monkeypatch, capsys):
    """REGRESSION: the retry used to re-read only the null receipt, so the pending tx went into the
    Relay proof, whose tx-block == receipt-block check refused 279 of 305 decodes on 56."""
    monkeypatch.setattr(w, "direct_buy_from_receipt",
                        lambda receipt, tx, owner, **kw: dict(BUY)
                        if receipt and (tx or {}).get("blockHash")
                        and tx["blockHash"].lower() == receipt["blockHash"].lower() else None)
    reads = _MempoolReads(pending_reads=1)
    row, out = _detect(reads)
    assert row is not None and out["reason"] == "detected"
    assert reads.calls.count("eth_getTransactionByHash") == 2
    assert "tx_read=ok receipt_read=ok" in capsys.readouterr().out


def test_a_tx_still_pending_after_the_budget_is_not_detected(monkeypatch, capsys):
    monkeypatch.setattr(w, "direct_buy_from_receipt",
                        lambda receipt, tx, owner, **kw: dict(BUY)
                        if receipt and (tx or {}).get("blockHash") else None)
    reads = _MempoolReads(pending_reads=99)
    row, out = _detect(reads)
    assert row is None and out["retries"] == 4
    assert "tx_read=pending" in capsys.readouterr().out


class _StuckMempoolReads:
    """The 56 read host after c38b768e: getTransactionByHash stays pending for the whole budget
    while the receipt is mined; only a block-and-index read returns the mined copy."""

    def __init__(self, index_answer="mined"):
        self.index_answer, self.calls = index_answer, []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "eth_getTransactionReceipt":
            return dict(RECEIPT, blockHash="0xB1", transactionIndex="0x3")
        if method == "eth_getTransactionByBlockHashAndIndex":
            assert params == ["0xB1", "0x3"]
            if self.index_answer == "mined":
                return {"from": WALLET, "hash": TX, "blockHash": "0xb1"}
            return {"from": WALLET, "hash": "0x" + "22" * 32, "blockHash": "0xb1"}
        return {"from": WALLET, "hash": TX, "blockHash": None}


def _block_checked(monkeypatch):
    monkeypatch.setattr(w, "direct_buy_from_receipt",
                        lambda receipt, tx, owner, **kw: dict(BUY)
                        if receipt and (tx or {}).get("blockHash")
                        and tx["blockHash"].lower() == receipt["blockHash"].lower() else None)


def test_a_stuck_mempool_tx_is_read_by_block_and_index(monkeypatch):
    """REGRESSION: 18 of 21 56 relay buys read pending on every retry and were refused."""
    _block_checked(monkeypatch)
    reads = _StuckMempoolReads()
    row, out = _detect(reads)
    assert row is not None and out["reason"] == "detected"
    assert out["mined_copy"] == "ok" and out["retries"] == 0
    assert reads.calls.count("eth_getTransactionByBlockHashAndIndex") == 1


def test_a_block_and_index_answer_for_another_tx_is_not_used(monkeypatch):
    _block_checked(monkeypatch)
    row, out = _detect(_StuckMempoolReads(index_answer="other"))
    assert row is None and out["mined_copy"] == "mismatch"

"""The EVM push watcher decides a transaction once, not once per Transfer log (2026-09-28, Alpha GO).

Measured 2026-09-28 11:33Z: one 4663 tx (0x771b36e855) reached the watcher as 11 Transfer logs to
watched addresses within 0.85 s, and on_transfer read the tx and receipt again for every log: 22
publicnode calls in under a second, the burst shape that earns its bare HTTP 403. push_detection
already decides a tx for EVERY watched owner in it, so a later log of the same tx can only repeat the
answer. MEMECOIN_ONCHAIN_EVM_TX_DEDUPE_S (default 60, 0 = as before) skips a log whose tx was decided
inside the window. A null read is not a decision: the next log of that tx reads again.
"""
import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.services import memecoin_onchain_push as P
from sniper.settings import memecoin as cfg

WALLET = "0x" + "ab" * 20
MINT = "0x" + "cd" * 20
TX = "0x" + "77" * 32
BUY = {"mint": MINT, "token_delta": 1.0, "source_size_usd": 100.0, "source_price": 100.0}


class _Conn:
    def __init__(self, inserts):
        self.inserts = inserts

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("INSERT"):
            self.inserts.append(params)
        return self

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def watcher(monkeypatch, tmp_path):
    inserts = []
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn(inserts))
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "send_wake", lambda address: True)
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    monkeypatch.setattr(w, "tx_read_host_from_env", lambda: None)
    monkeypatch.setattr(w, "erc20_decimals", lambda mint, rpc_url: 18)
    monkeypatch.setattr(w, "RELAY_ENVELOPE_CHAINS", frozenset())
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_DEDUPE_S", 60.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S", 0.0, raising=False)
    w._configure("4663")
    yield inserts
    w._configure("4663")


def _drive(monkeypatch, n_logs, *, tx_body={"from": WALLET}, buy=True):
    reads = []

    def counted(url, method, params):
        reads.append(method)
        if method == "eth_getTransactionByHash":
            return {"result": tx_body}
        return {"result": {"logs": [], "status": "0x1", "blockNumber": "0x10"}}
    monkeypatch.setattr(w, "_counted_rpc", counted)
    monkeypatch.setattr(w, "direct_buy_from_receipt",
                        lambda receipt, tx, owner, **kw: dict(BUY) if buy and receipt and tx else None)

    def fake_stream(ws_url, **kwargs):
        for _ in range(n_logs):
            kwargs["on_transfer"]({"transactionHash": TX, "address": MINT})
        return kwargs.get("state")
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    w.run_push("wss://test", price=price)
    return reads


@pytest.mark.parametrize("buy", [True, False], ids=["detected", "no_buy"])
def test_eleven_logs_of_one_tx_cost_one_read_pair(watcher, monkeypatch, capsys, buy):
    reads = _drive(monkeypatch, 11, buy=buy)
    assert len(reads) == 2                                   # was 22 on 2026-09-28
    assert len(watcher) == (1 if buy else 0)                 # one row, never a duplicate write
    out = capsys.readouterr().out
    assert out.count("outcome=duplicate_tx") == 10          # every skipped log is still accounted for


def test_zero_reads_every_log_as_before(watcher, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_TX_DEDUPE_S", 0.0, raising=False)
    assert len(_drive(monkeypatch, 11)) == 22


def test_a_null_read_is_not_a_decision(watcher, monkeypatch, capsys):
    reads = _drive(monkeypatch, 2, tx_body=None)
    assert len(reads) == 4                                   # the second log reads again
    assert "outcome=duplicate_tx" not in capsys.readouterr().out


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_the_window_expires():
    clock = _Clock()
    decided = w._Decided(clock=clock)
    decided.add("0xa")
    clock.t = 60.0
    assert decided.seen("0xa", 60.0)
    clock.t = 60.1
    assert not decided.seen("0xa", 60.0)


def test_memory_is_bounded_oldest_first():
    decided = w._Decided(clock=_Clock(), cap=3)
    for tx in ("0x1", "0x2", "0x3", "0x4", "0x5"):
        decided.add(tx)
    assert not decided.seen("0x1", 60.0) and not decided.seen("0x2", 60.0)
    assert all(decided.seen(tx, 60.0) for tx in ("0x3", "0x4", "0x5"))

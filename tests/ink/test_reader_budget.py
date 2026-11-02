"""The Ink reader's canonical read budget (Alpha (C), 2026-09-29).

backfill_block read a receipt for EVERY transaction in a block before keeping only watched owners'.
Ink carries ~5.5 txs per 1.00 s block (measured, 20 blocks, 2026-09-29), so a live frontier cost
~6.5 calls/s on one public endpoint, which was also the chain's free-submission endpoint. Now:
  - receipts only for txs whose sender is watched (the owner rule is unchanged: tx.from); the block
    itself is still read and recorded, so canonical coverage is unchanged;
  - one provider group per reader with call/429/error counts, and a 429 backs the reader off;
  - the reader refuses a read host another role uses (custody, pool, submission).
"""
import sqlite3
import urllib.error

import pytest

from scripts.research import onchain_ink_watch as ink
from sniper.persistence import db_evm_stream as journal

WALLET = "0x" + "11" * 20
BLOCK_HASH = "0x" + "cc" * 32


def _block(senders):
    txs = [{"hash": "0x%064x" % (i + 1), "from": s, "blockHash": BLOCK_HASH, "blockNumber": "0x64"}
           for i, s in enumerate(senders)]
    return {"number": "0x64", "hash": BLOCK_HASH, "timestamp": "0x6ad00f00", "transactions": txs}


def _rpc(block):
    asked = []

    def rpc(method, params):
        asked.append(method)
        if method == "eth_getBlockByNumber":
            return block
        return {"transactionHash": params[0], "blockHash": BLOCK_HASH, "blockNumber": "0x64",
                "status": "0x1", "logs": []}
    rpc.asked = asked
    return rpc


def test_only_watched_senders_cost_a_receipt_read(tmp_path):
    rpc = _rpc(_block(["0x" + "22" * 20] * 4 + [WALLET]))
    with sqlite3.connect(tmp_path / "j.db") as stream:
        journal.ensure_stream_tables(stream)
        assert ink.backfill_block(stream, rpc, 100, watched={WALLET}) is True
        assert rpc.asked.count("eth_getTransactionReceipt") == 1
        assert len(journal.next_unprojected(stream)) == 1
        assert journal.canonical_block_hash(stream, 100) == BLOCK_HASH


def test_an_empty_watched_set_reads_the_block_and_no_receipt(tmp_path):
    rpc = _rpc(_block(["0x" + "22" * 20] * 6))
    with sqlite3.connect(tmp_path / "j.db") as stream:
        journal.ensure_stream_tables(stream)
        assert ink.backfill_block(stream, rpc, 100, watched=set()) is True
        assert rpc.asked == ["eth_getBlockByNumber"]
        assert journal.canonical_block_hash(stream, 100) == BLOCK_HASH


def test_the_reader_counts_calls_and_backs_off_on_429(monkeypatch):
    stats, slept = {}, []

    class Resp:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return self.body
    answers = iter([urllib.error.HTTPError("https://x", 429, "Too Many", {}, None),
                    Resp(b'{"jsonrpc":"2.0","id":1,"result":"0x10"}')])

    def urlopen(req, timeout=None):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(ink.urllib.request, "urlopen", urlopen)
    read = ink._rpc_reader("https://ink.reads.example", stats=stats, sleep=slept.append,
                           clock=lambda: 100.0)
    with pytest.raises(OSError):
        read("eth_blockNumber", [])
    assert read("eth_blockNumber", []) == "0x10"
    assert stats["rpc"] == {"calls": 2, "http_429": 1, "errors": 1}
    assert slept == [1.0]                          # the next call waited the 1 s hold out


def test_the_reader_refuses_a_host_another_role_uses(monkeypatch):
    monkeypatch.setenv("MEMECOIN_EVM_RPC_URL_4663", "https://custody.example")
    assert ink.read_host_refusal("https://rpc-gel.inkonchain.com")      # a 57073 submission endpoint
    assert ink.read_host_refusal("https://ink.reads.example") is None


def test_a_429_on_one_reader_holds_every_reader_of_the_group(monkeypatch):
    """Gamma LOW-2: reconcile and projection share one host. A 429 seen by one must slow the other,
    and their counts must not race: one lock, one streak, one hold per stats dict."""
    import json as _json
    stats, slept, clock = {}, [], [100.0]

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, *a):
            return _json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0x1"}).encode()
    answers = iter([urllib.error.HTTPError("https://x", 429, "Too Many", {}, None), Resp()])

    def urlopen(req, timeout=None):
        a = next(answers)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(ink.urllib.request, "urlopen", urlopen)

    def sleep(s):
        slept.append(s)
        clock[0] += s
    reconcile = ink._rpc_reader("https://ink.reads.example", stats=stats, sleep=sleep, clock=lambda: clock[0])
    project = ink._rpc_reader("https://ink.reads.example", stats=stats, sleep=sleep, clock=lambda: clock[0])
    with pytest.raises(OSError):
        reconcile("eth_blockNumber", [])
    assert project("eth_blockNumber", []) == "0x1"
    assert slept == [1.0], "the second reader waited out the group's hold"
    assert stats["rpc"] == {"calls": 2, "http_429": 1, "errors": 1}

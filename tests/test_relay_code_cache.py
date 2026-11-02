"""The on-chain watcher verifies the pinned Relay code once per TTL, and reads tx and receipt at once
(2026-09-28, Alpha GO).

The first live RH detection (row 2865, 0xdetweiler, 06:43:23Z) spent 263 ms between the socket's
Transfer log and the row: five sequential reads (tx, receipt, chainId, two getCode). The socket had
beaten J7 by about 146 ms, and the row still landed about 117 ms after J7's attempt. The pinned
contracts do not change, so the watcher re-verifies them on a TTL (MEMECOIN_ONCHAIN_RELAY_CODE_TTL_S,
default 600 s), and a read after expiry that hashes wrong still refuses. The address
verifier passes no TTL and reads every time, as before.
"""
import gzip
import json
import threading
from pathlib import Path

import pytest

from sniper.services import memecoin_evm_onchain_watch as M

FIX = json.load(gzip.open(Path(__file__).parent / "fixtures" / "relay_purchases_2026-09-28.json.gz", "rt"))
BUY = next(b for b in FIX["buys"] if b["chain"] == "56" and b["shape"] == "no_kyber")


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch):
    monkeypatch.setattr(M, "_RELAY_CODE_CACHE", {}, raising=False)


def _reader(code):
    calls = []

    def read(method, params):
        calls.append(method)
        if method == "eth_chainId":
            return hex(56)
        if method == "eth_getCode":
            return code.get(params[0], "0xdeadbeef")
        raise AssertionError(method)
    read.calls = calls
    return read


def _read(read_fn, now, *, ttl=600.0, evidence=None):
    return M.read_relayer_purchase(BUY["receipt"], BUY["tx"], BUY["wallet"], chain="56",
                                   read_fn=read_fn, any_route=True, evidence=evidence,
                                   code_ttl_s=ttl, clock=lambda: now)


def test_a_verified_code_is_not_read_again_inside_the_ttl():
    read = _reader(FIX["code_56"])
    first = _read(read, 1000.0)
    assert first["code_basis"] == "receipt_block"
    assert read.calls.count("eth_getCode") == 2
    read.calls.clear()
    again = _read(read, 1599.0)
    assert again is not None and again["code_basis"] == "cached"
    assert read.calls == [], "a cached verification must make no RPC read"


def test_unpinned_chain_cannot_use_warmed_code(monkeypatch):
    monkeypatch.setattr(M, "_CHAIN_ID_PINNED", set())
    M.warm_relay_code("56", _reader(FIX["code_56"]), ttl_s=600, clock=lambda: 1000.0)
    read = _reader(FIX["code_56"])
    def wrong_chain(method, params):
        return "0x1234" if method == "eth_chainId" else read(method, params)
    evidence = {}
    assert _read(wrong_chain, 1001.0, evidence=evidence) is None
    assert evidence["reason"] == "chain_mismatch"


def test_the_ttl_expires_and_the_code_is_read_again():
    read = _reader(FIX["code_56"])
    _read(read, 1000.0)
    read.calls.clear()
    assert _read(read, 1600.5)["code_basis"] == "receipt_block"
    assert read.calls.count("eth_getCode") == 2


def test_a_code_hash_changed_by_the_next_refresh_refuses():
    _read(_reader(FIX["code_56"]), 1000.0)
    changed = dict(FIX["code_56"])
    first = next(iter(changed))
    changed[first] = changed[first][:-2] + "00"
    evidence = {}
    assert _read(_reader(changed), 1700.0, evidence=evidence) is None
    assert evidence["reason"] == "contract_code_mismatch"


def test_without_a_ttl_every_call_reads_as_before():
    """The address verifier's path: no TTL, no cache."""
    read = _reader(FIX["code_56"])
    _read(read, 1000.0, ttl=None)
    _read(read, 1001.0, ttl=None)
    assert read.calls.count("eth_getCode") == 4 and read.calls.count("eth_chainId") == 2


def test_the_watcher_fetches_tx_and_receipt_concurrently(monkeypatch):
    import scripts.research.onchain_evm_wallet_watch as w
    receipt_started = threading.Event()

    def read(method, params):
        if method == "eth_getTransactionReceipt":
            receipt_started.set()
            return BUY["receipt"]
        if method == "eth_getTransactionByHash":
            assert receipt_started.wait(2.0), "tx was read before the receipt read even started"
            return BUY["tx"]
        return _reader(FIX["code_56"])(method, params)
    w._configure("56")
    try:
        log = next(l for l in BUY["receipt"]["logs"] if len(l["topics"]) == 3
                   and l["topics"][2][-40:] == BUY["wallet"][2:] and l["address"].lower() == BUY["mint"])
        row = w.push_detection("http://unused", log, {BUY["wallet"]: ("uid", BUY["handle"])},
                               decimals_cache={BUY["mint"]: 18,
                                               "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18},
                               quote_usd=600.0, now="2026-09-28T06:50:00+00:00", read_fn=read,
                               payment_price_fn=lambda token: 1.0)
    finally:
        w._configure("4663")
    assert row is not None and row["buy"]["mint"] == BUY["mint"]

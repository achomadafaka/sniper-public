"""The null-read retry step, per chain (spec 2026-09-29-memecoin-evm-null-read-step-design.md, Alpha GO).

Every 56 buy decode read a null receipt at arrival and slept the fixed 0.4 s step (2.0 s budget / (4
retries + 1)) before the retry succeeded: arrival -> decoded p50 0.857 s on 56 against 0.138 s on 4663.
Measured on the 56 read host: arrival is p50 0.13 s after the block, the host shows the block at p50
0.277 / p90 0.362 s. D1: MEMECOIN_ONCHAIN_EVM_NULL_READ_STEP_S (`56:0.1`) sleeps that step instead and
lets the attempts run to the same 2.0 s budget. D2: a retry that needs both the tx and the receipt
reads them concurrently, on every chain, for zero added reads.
"""
import threading

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.settings import memecoin as cfg

WALLET = "0x" + "ab" * 20
MINT = "0x" + "cd" * 20
TX = "0x" + "11" * 32
BUY = {"mint": MINT, "token_delta": 1.0, "source_size_usd": 100.0, "source_price": 100.0}
RECEIPT = {"logs": [], "status": "0x1", "blockNumber": "0x10"}


@pytest.fixture(autouse=True)
def _chain(monkeypatch):
    monkeypatch.delenv("MEMECOIN_ONCHAIN_EVM_NULL_READ_STEP_S", raising=False)
    monkeypatch.setattr(w, "erc20_decimals", lambda mint, rpc_url: 18)
    monkeypatch.setattr(w, "direct_buy_from_receipt",
                        lambda receipt, tx, owner, **kw: dict(BUY) if receipt else None)
    monkeypatch.setattr(w, "RELAY_ENVELOPE_CHAINS", frozenset())
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_RETRY_S", 2.0, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_ONCHAIN_EVM_NULL_READ_MAX_RETRIES", 4, raising=False)
    w._configure("56")
    yield
    w._configure("4663")


class _Clock:
    """Reads take no time; every sleep moves the clock by its length."""

    def __init__(self):
        self.t, self.sleeps = 0.0, []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(round(s, 6))
        self.t += s


class _Reads:
    """The receipt is null until the clock reaches `at` (None: never); the tx is always there."""

    def __init__(self, clock, at):
        self.clock, self.at, self.calls = clock, at, []

    def __call__(self, method, params):
        self.calls.append(method)
        if method == "eth_getTransactionReceipt":
            ready = self.at is not None and self.clock() >= self.at - 1e-9
            return dict(RECEIPT) if ready else None
        return {"from": WALLET}


def _detect(reads, clock):
    out = {}
    row = w.push_detection("http://unused", {"transactionHash": TX, "address": MINT},
                           {WALLET: ("u1", "h1")}, decimals_cache={MINT: 18}, quote_usd=600.0,
                           now=w.now_iso(), read_fn=reads, outcome=out,
                           sleep_fn=clock.sleep, clock=clock)
    return row, out


def test_56_with_the_knob_sleeps_the_chain_step(monkeypatch):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_NULL_READ_STEP_S", "56:0.1")
    clock = _Clock()
    row, out = _detect(_Reads(clock, at=0.25), clock)
    assert row is not None and out["reason"] == "detected"
    assert clock.sleeps == [0.1, 0.1, 0.1] and out["retries"] == 3


def test_without_the_knob_56_keeps_todays_step():
    clock = _Clock()
    row, out = _detect(_Reads(clock, at=0.25), clock)
    assert row is not None and clock.sleeps == [0.4] and out["retries"] == 1


def test_the_knob_keeps_the_whole_budget_never_stopping_at_four(monkeypatch):
    """A receipt that never comes is re-read until the unchanged 2.0 s budget, not for 0.4 s."""
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_NULL_READ_STEP_S", "56:0.1")
    clock = _Clock()
    row, out = _detect(_Reads(clock, at=None), clock)
    assert row is None and out["reason"] == "receipt_null"
    assert out["retries"] == 20 and sum(clock.sleeps) == pytest.approx(2.0)


@pytest.mark.parametrize("knob,chain", [("56:0.1", "4663"), ("abc", "56"), ("56:0", "56"),
                                        ("56:-1", "56"), ("56:x", "56"), ("bsc", "56")])
def test_another_chain_or_a_bad_entry_keeps_todays_step(monkeypatch, knob, chain):
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_NULL_READ_STEP_S", knob)
    w._configure(chain)
    clock = _Clock()
    _detect(_Reads(clock, at=0.25), clock)
    assert clock.sleeps == [0.4]


def test_the_step_knob_parses_chain_names_and_ignores_bad_entries():
    env = {"MEMECOIN_ONCHAIN_EVM_NULL_READ_STEP_S": " bsc:0.1, 8453:0.2, atlantis:0.1, 4663:nan, 56:0.3"}
    f = cfg.memecoin_onchain_evm_null_read_step_s
    assert f("8453", env=env) == 0.2 and f("56", env=env) == 0.3, "the last entry for a chain wins"
    assert f("4663", env=env) is None and f("1", env=env) is None
    assert f("56", env={}) is None


def test_a_retry_rereads_the_tx_and_the_receipt_together(monkeypatch):
    """D2: both null at arrival, so the retry needs both. Each re-read blocks until the other has
    started; read one after the other, the first waits alone and the barrier breaks."""
    barrier = threading.Barrier(2, timeout=2.0)
    calls = []

    def reads(method, params):
        calls.append(method)
        if calls.count(method) == 1:
            return None                              # the first pair: both null
        barrier.wait()                               # the retry pair must be in flight together
        return dict(RECEIPT) if method == "eth_getTransactionReceipt" else {"from": WALLET}
    clock = _Clock()
    row, out = _detect(reads, clock)
    assert row is not None and out["retries"] == 1
    assert sorted(calls) == sorted(["eth_getTransactionByHash", "eth_getTransactionReceipt"] * 2)


def test_a_retry_that_needs_only_the_receipt_reads_only_the_receipt():
    """Zero added reads: the mined tx already in hand is not read again."""
    clock = _Clock()
    reads = _Reads(clock, at=0.25)
    _detect(reads, clock)
    assert reads.calls.count("eth_getTransactionByHash") == 1


def test_each_retry_reads_the_pair_once_and_nothing_more(monkeypatch):
    """Gamma LOW (2026-09-29): pin the reads per iteration. With the tx and the receipt null for
    the whole budget, every retry costs exactly one tx read and one receipt read."""
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_NULL_READ_STEP_S", "56:0.1")
    calls = []

    def reads(method, params):
        calls.append(method)
        return None
    clock = _Clock()
    row, out = _detect(reads, clock)
    assert row is None and out["retries"] == 20
    assert calls.count("eth_getTransactionByHash") == calls.count("eth_getTransactionReceipt") == 21
    assert len(calls) == 42

"""The EVM wallet watcher's detection path: a warm native price, an arrival stamp, stage stamps, and
the mint's decimals read beside the transaction (2026-09-28, Alpha GO).

Measured 2026-09-28: every transfer first fetched ETH/USD from Binance on the detection path
(median 252 ms from this host) and only then stamped detected_at, so row 2865's "263 ms" was that
fetch. A background refresher now keeps the price warm (every 5 s); a detection uses it when it is at
most 10 s old and fetches synchronously (and says so) when it is older or absent. detected_at is the
ARRIVAL stamp; source_valuation_json carries the price's age and source, decoded_at and commit_at;
wake_sent_at goes to the watcher's log line (a second ledger write would land exactly when the lane
writes its attempt).
"""
import json
import threading
import time

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.services import memecoin_onchain_push as P

WALLET = "0x" + "ab" * 20


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# --- the warm price -----------------------------------------------------------------------------

def test_a_cached_price_is_used_within_the_ttl():
    calls, clock = [], _Clock()
    price = w.NativePrice(lambda: calls.append(1) or 2650.0, clock=clock, log_fn=lambda line: None)
    price.refresh()
    clock.t += 9.5
    assert price.get() == (2650.0, pytest.approx(9.5), "cache")
    assert len(calls) == 1


def test_a_stale_price_is_fetched_on_the_detection_path_and_said():
    lines, calls, clock = [], [], _Clock()
    price = w.NativePrice(lambda: calls.append(1) or 2600.0, clock=clock, log_fn=lines.append)
    price.refresh()
    clock.t += 10.5
    value, age, source = price.get()
    assert (value, source) == (2600.0, "sync") and age == 0.0 and len(calls) == 2
    assert lines and "stale" in lines[0] and "10.5" in lines[0]


def test_an_absent_price_is_fetched():
    lines = []
    price = w.NativePrice(lambda: 2650.0, clock=_Clock(), log_fn=lines.append)
    assert price.get() == (2650.0, 0.0, "sync")
    assert lines and "absent" in lines[0]


def test_a_failed_fetch_is_unknown_never_zero():
    price = w.NativePrice(lambda: None, clock=_Clock(), log_fn=lambda line: None)
    assert price.get() == (None, None, "sync")


def test_the_refresher_keeps_it_warm():
    values = iter([2600.0, 2610.0, 2620.0] + [2630.0] * 100)
    price = w.NativePrice(lambda: next(values), log_fn=lambda line: None, refresh_s=0.02)
    price.start()
    time.sleep(0.15)
    price.stop()
    value, age, source = price.get()
    assert source == "cache" and value >= 2620.0 and age < 0.1


# --- the detection path ---------------------------------------------------------------------------

class _Conn:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=()):
        if sql.lstrip().upper().startswith("INSERT"):
            self.log.append(("insert", params))
        return self

    def commit(self):
        self.log.append(("commit", w.now_iso()))

    def close(self):
        pass


@pytest.fixture
def watcher(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn(events))
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "send_wake", lambda address: events.append(("wake", w.now_iso())) or True)
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")
    w._configure("4663")
    yield events
    w._configure("4663")


BUY = {"mint": "0x" + "cd" * 20, "token_delta": 1.0, "source_size_usd": 100.0, "source_price": 100.0}


def _drive(monkeypatch, price):
    def fake_stream(ws_url, **kwargs):
        kwargs["on_transfer"]({"transactionHash": "0x" + "11" * 32, "address": BUY["mint"]})
        return kwargs.get("state")
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    w.run_push("wss://test", price=price)


def test_detected_at_is_the_arrival_and_the_row_carries_its_stages(watcher, monkeypatch, capsys):
    arrived = []

    def slow_decode(rpc_url, log, watched, *, now, quote_usd, **_kw):
        arrived.append(now)
        time.sleep(0.05)
        return dict(tx_hash="0x" + "11" * 32, wallet=WALLET, source_user_id="u1", handle="h1",
                    buy=BUY, log=log, now=now)
    monkeypatch.setattr(w, "push_detection", slow_decode)
    price = w.NativePrice(lambda: 2650.0, log_fn=lambda line: None)
    price.refresh()
    before = w.now_iso()
    _drive(monkeypatch, price)
    kinds = [e[0] for e in watcher]
    assert kinds == ["insert", "commit", "wake"]
    params = watcher[0][1]
    detected_at, valuation = params[10], json.loads(params[13])
    assert detected_at == arrived[0] and before <= detected_at
    assert valuation["quote_usd"] == 2650.0 and valuation["quote_price_source"] == "cache"
    assert valuation["quote_price_age_s"] < 1
    assert detected_at < valuation["decoded_at"] <= valuation["commit_at"] <= watcher[1][1]
    line = [l for l in capsys.readouterr().out.splitlines() if "[onchain-evm] detection" in l][0]
    stamps = dict(part.split("=", 1) for part in line.split()[3:])
    assert stamps["arrival"] == detected_at and stamps["commit_at"] == valuation["commit_at"]
    assert stamps["wake_sent_at"] >= watcher[2][1] >= stamps["commit_at"]


def test_the_mints_decimals_are_read_beside_the_transaction(monkeypatch):
    """Both reads must be in flight at once: a barrier of two times out if they run in turn."""
    barrier = threading.Barrier(2, timeout=2.0)
    w._configure("4663")

    def decimals(mint, rpc_url):
        barrier.wait()
        return 18

    def read(method, params):
        if method == "eth_getTransactionByHash":
            barrier.wait()
            return {"from": "0x" + "ee" * 20}
        return {"logs": [], "status": "0x1"}
    monkeypatch.setattr(w, "erc20_decimals", decimals)
    cache = {}
    w.push_detection("http://unused", {"transactionHash": "0x" + "22" * 32, "address": BUY["mint"]},
                     {}, decimals_cache=cache, quote_usd=2650.0, now=w.now_iso(), read_fn=read)
    assert cache == {BUY["mint"]: 18}


def test_a_known_mint_is_not_read_again(monkeypatch):
    monkeypatch.setattr(w, "erc20_decimals", lambda *a: pytest.fail("cached mint re-read"))
    w._configure("4663")
    w.push_detection("http://unused", {"transactionHash": "0x" + "22" * 32, "address": BUY["mint"]},
                     {}, decimals_cache={BUY["mint"]: 18}, quote_usd=2650.0, now=w.now_iso(),
                     read_fn=lambda m, p: {"logs": [], "status": "0x1"} if "Receipt" in m else {})


def test_the_arrival_is_stamped_before_a_slow_price_read(watcher, monkeypatch):
    """The 263 ms of row 2865: the old path stamped detected_at after the price fetch."""
    fetch_started = []

    def slow_fetch():
        fetch_started.append(w.now_iso())
        time.sleep(0.05)
        return 2650.0
    monkeypatch.setattr(w, "push_detection", lambda rpc_url, log, watched, *, now, **_kw: dict(
        tx_hash="0x" + "11" * 32, wallet=WALLET, source_user_id="u1", handle="h1", buy=BUY, log=log,
        now=now))
    _drive(monkeypatch, w.NativePrice(slow_fetch, log_fn=lambda line: None))
    detected_at, valuation = watcher[0][1][10], json.loads(watcher[0][1][13])
    assert detected_at <= fetch_started[0] and valuation["quote_price_source"] == "sync"

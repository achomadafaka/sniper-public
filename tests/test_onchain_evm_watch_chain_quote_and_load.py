"""The EVM wallet watcher on a chain without Uniswap V4, in its own quote asset, with its RPC load
in the heartbeat (2026-09-28).

- Chain 56 became eligible at 06:11:02Z and crash-looped once a minute on `V4[CHAIN]` (KeyError:
  '56'): BSC has no V4 PoolManager. It must start with no Initialize subscription instead.
- Every chain priced its buys with ETH/USD, so a BSC source size was ETH-priced.
- MEMECOIN_ONCHAIN_EVM_RPC_URL_4663 is the host the funded path broadcasts through, and a dark socket
  moves this watcher onto range getLogs against it. The heartbeat now counts what it sends there.
"""
import json
from collections import deque
from concurrent.futures import Future

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from sniper.execution import evm_wallet
from sniper.services import memecoin_onchain_push as P

WALLET = "0x" + "ab" * 20


class _Conn:
    def execute(self, *_a):
        return self

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture
def watcher(monkeypatch, tmp_path):
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_RPC_SENT", deque(), raising=False)
    monkeypatch.setattr(w, "_RPC_TOTAL", [0], raising=False)
    monkeypatch.setattr(evm_wallet, "asset_usd_price",
                        lambda symbol, http=None: {"ETH": 4000.0, "BNB": 600.0}[symbol])
    beat = tmp_path / "hb.json"

    def configure(chain):
        w._configure(chain)
        monkeypatch.setattr(w, "HEARTBEAT", beat)
    yield configure, beat
    w._configure("4663")


def _push(monkeypatch, drive):
    seen = {}

    def fake_stream(ws_url, **kwargs):
        seen.update(kwargs)
        result = drive(kwargs)
        if isinstance(result, Future):
            result.result(timeout=2)  # Finish asynchronous BSC decode before stream shutdown.
        return kwargs.get("state")
    monkeypatch.setattr(P, "run_push_stream", fake_stream)
    w.run_push("wss://test")
    return seen


def test_a_chain_with_no_v4_deployment_starts_without_the_initialize_subscription(watcher, monkeypatch):
    configure, _beat = watcher
    configure("56")
    seen = _push(monkeypatch, lambda kw: None)
    assert seen["pool_manager"] is None
    assert P.initialize_filter(seen["pool_manager"], seen["init_topic"]) is None


@pytest.mark.parametrize("chain,usd", [("56", 600.0), ("4663", 4000.0), ("8453", 4000.0)])
def test_a_detection_is_priced_in_the_chains_own_quote_asset(watcher, monkeypatch, chain, usd):
    configure, _beat = watcher
    configure(chain)
    priced = []

    def fake_detection(rpc_url, log, watched, *, quote_usd, **_kw):
        priced.append(quote_usd)
    monkeypatch.setattr(w, "push_detection", fake_detection)
    _push(monkeypatch, lambda kw: kw["on_transfer"]({"transactionHash": "0x" + "11" * 32}))
    assert priced == [usd]


def test_the_push_heartbeat_counts_requests_sent_to_the_http_host(watcher, monkeypatch):
    configure, beat = watcher
    configure("4663")
    sent = []

    def fake_rpc(url, method, params):
        sent.append(method)
        return {"result": {"from": "0x" + "cd" * 20, "logs": []}}
    monkeypatch.setattr(w, "_rpc", fake_rpc)

    def drive(kw):
        kw["on_transfer"]({"transactionHash": "0x" + "11" * 32})
        kw["on_head"]()
    _push(monkeypatch, drive)
    hb = json.loads(beat.read_text())
    # The start-up relay code warm-up (2026-09-28) reads the same host once, before any transfer.
    path = [m for m in sent if m not in ("eth_chainId", "eth_getCode")]
    assert sorted(path) == ["eth_getTransactionByHash", "eth_getTransactionReceipt"]   # read concurrently
    assert hb["rpc_requests_60s"] == len(sent) and hb["rpc_requests_total"] == len(sent)


def test_the_poll_fallback_heartbeat_counts_its_requests(watcher, monkeypatch):
    configure, beat = watcher
    configure("4663")
    sent = []

    def fake_rpc(url, method, params):
        sent.append(method)
        return {"result": "0x64" if method == "eth_blockNumber" else []}
    monkeypatch.setattr(w, "_rpc", fake_rpc)
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_4663", "http://stub")
    w.run_poll(duration_s=0.05, period_s=0.01)
    hb = json.loads(beat.read_text())
    assert "eth_getLogs" in sent
    assert hb["rpc_requests_total"] == len(sent)


# --- the lane wake (2026-09-28, Alpha) ---------------------------------------------------------
# The first live RH row (2865) was swept by the runner's on-chain lane 1.987 s after it was written:
# the lane's 1 s poll, because this watcher never sent the wake the Solana watcher sends. One
# datagram per commit, always AFTER the commit, so the sweep it triggers can read the row.

def _wake_order(watcher, monkeypatch, detection):
    configure, _beat = watcher
    configure("4663")
    order = []

    class Conn(_Conn):
        def commit(self):
            order.append("commit")
    monkeypatch.setattr(w, "get_connection", lambda path: Conn())
    monkeypatch.setattr(w, "_record", lambda conn, **row: order.append("record"))
    monkeypatch.setattr(w, "send_wake", lambda address: order.append("wake") or True)
    monkeypatch.setattr(w, "push_detection", lambda *a, **k: detection)
    return order


def test_a_pushed_detection_wakes_the_lane_once_after_its_commit(watcher, monkeypatch):
    order = _wake_order(watcher, monkeypatch, {"tx_hash": "0x1"})
    _push(monkeypatch, lambda kw: kw["on_transfer"]({"transactionHash": "0x" + "11" * 32}))
    assert order == ["record", "commit", "wake"]


def test_a_transfer_that_is_not_a_buy_sends_no_wake(watcher, monkeypatch):
    order = _wake_order(watcher, monkeypatch, None)
    _push(monkeypatch, lambda kw: kw["on_transfer"]({"transactionHash": "0x" + "11" * 32}))
    assert "wake" not in order


def test_a_polled_detection_wakes_the_lane_once_after_its_commit(watcher, monkeypatch):
    order = _wake_order(watcher, monkeypatch, {"tx_hash": "0x1"})
    served = []

    def fake_rpc(url, method, params):
        if method == "eth_blockNumber":
            return {"result": "0x64"}
        logs = [] if served else [{"transactionHash": "0x" + "22" * 32}]
        served.append(1)
        return {"result": logs}
    monkeypatch.setattr(w, "_rpc", fake_rpc)
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_4663", "http://stub")
    w.run_poll(duration_s=0.05, period_s=0.01)
    assert order[:3] == ["record", "commit", "wake"] and order.count("wake") == 1

"""The 56 dark poll's coverage is readable (2026-10-17, Alpha/Gamma).

publicnode refused the 56 subscription 38 times on 10-17; each refusal is a dark window the poll
fallback covers from the backfill host, up to ~45 s behind the head (max_behind). It wrote no line
per poll, so a buy inside a refusal window could not be verified from logs. Pins: a Transfer to an
armed address inside the polled range becomes a detection; each dark-poll window prints the range it
read and what it found; the heartbeat carries windows, blocks, logs and detections."""
import json
from types import SimpleNamespace

import pytest

import scripts.research.onchain_evm_wallet_watch as w
from thread_local_time import patch_local

WALLET = "0x" + "ab" * 20
MINT = "0x" + "cd" * 20
TX = "0x" + "11" * 32
HEAD = 1000


@pytest.fixture
def poll(monkeypatch, tmp_path):
    w._configure("56")
    monkeypatch.setattr(w, "HEARTBEAT", tmp_path / "hb.json")

    class _Conn:
        def close(self):
            pass
    monkeypatch.setattr(w, "get_connection", lambda path: _Conn())
    monkeypatch.setattr(w.m, "ensure_memecoin_tables", lambda conn: None)
    monkeypatch.setattr(w, "_require_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_verified_addresses", lambda conn: {WALLET: ("u1", "h1")})
    monkeypatch.setattr(w, "_relay_origin_worker", lambda *a, **k: None)
    monkeypatch.setattr(w, "tx_read_host_from_env", lambda **k: None)
    monkeypatch.setenv("MEMECOIN_ONCHAIN_EVM_RPC_URL_56", "http://read.unused")
    patch_local(monkeypatch, "sleep", lambda s: None)    # this thread only (xdist neighbours keep theirs)
    monkeypatch.setattr(w, "_rpc_result", lambda url, method, params, **k: hex(HEAD))
    asked, committed = [], []

    def dark_logs(url, lo, hi, topics):
        asked.append((lo, hi))
        assert topics[2] == [w._topic(WALLET)], "the filter names the armed address as recipient"
        return [{"transactionHash": TX, "address": MINT, "blockNumber": hex(995),
                 "topics": [w.TRANSFER_TOPIC, "0x" + "00" * 32, w._topic(WALLET)]}] if lo <= 995 <= hi else []
    monkeypatch.setattr(w, "_dark_logs", dark_logs)
    monkeypatch.setattr(w, "_backfill_post", lambda url, method, params: {"timestamp": "0x10"})
    monkeypatch.setattr(w, "push_detection", lambda rpc_url, log, watched, **kw: {
        "tx_hash": log["transactionHash"], "log": log, "now": kw["now"], "valuation": {}})
    monkeypatch.setattr(w, "_commit_detections", lambda conn, rows: committed.extend(rows))
    w._DARK_POLL_STATS.update(windows=0, blocks=0, logs=0, detections=0)
    yield asked, committed
    w._configure("4663")


def test_a_transfer_to_an_armed_address_inside_the_polled_range_becomes_a_detection(poll, capsys):
    asked, committed = poll
    price = SimpleNamespace(get=lambda: (600.0, 1.0, "test"))
    read = w.run_poll(duration_s=0.2, period_s=0.01, price=price, logs_url="https://backfill.unused",
                      start_block=990, max_behind=60, read_transport=SimpleNamespace(tx_host=lambda: None))
    assert asked[0][0] == 991 and read == HEAD
    assert [row["tx_hash"] for row in committed] == [TX]
    assert committed[0]["valuation"]["backfill"] is True
    out = capsys.readouterr().out
    assert "[onchain-evm] dark poll read 991..1000 (10 blocks): 1 log, 1 detection" in out, out
    assert w._DARK_POLL_STATS == {"windows": 1, "blocks": 10, "logs": 1, "detections": 1}


def test_an_empty_dark_window_still_says_what_it_covered(poll, capsys):
    asked, committed = poll
    price = SimpleNamespace(get=lambda: (600.0, 1.0, "test"))
    w.run_poll(duration_s=0.2, period_s=0.01, price=price, logs_url="https://backfill.unused",
               start_block=996, max_behind=60, read_transport=SimpleNamespace(tx_host=lambda: None))
    assert committed == []
    assert "[onchain-evm] dark poll read 997..1000 (4 blocks): 0 logs, 0 detections" in capsys.readouterr().out

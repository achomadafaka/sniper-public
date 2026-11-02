"""Base 8453 step 1 part E: the runner starts the lane, and fill timing reads its own file (plan
2026-10-14). Offline: the snapshot poll and the hook warm-up are stubbed; nothing reaches a host."""
import sqlite3
import time
from pathlib import Path

import pytest

from sniper.services import memecoin_base_fast_lane as base
from sniper.services import memecoin_fill_timing as timing
from sniper.services import memecoin_rh_fast_lane as rh

HOST = "https://base-lane.example"


def _ctx():
    return {"address": "0x" + "ef" * 20, "v4_direct_quote_fn": lambda *a, **k: None,
            "rpc_url": "https://custody", "submission_transport": object()}


@pytest.fixture
def lane_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE_RPC_URL", HOST)
    monkeypatch.setattr(rh, "warm_hook", lambda shadow, log: (None, None))
    polls = []
    from sniper.services import memecoin_bsc_fast_lane as bsc
    monkeypatch.setattr(bsc.Snapshot, "poll_once", lambda self: polls.append(self.weth))
    yield polls
    assert base.stop_base_lane(timeout_s=5.0) == []


@pytest.mark.parametrize("mode", ["off", "step0"])
def test_the_runner_starts_nothing_off_the_runner_modes(lane_env, monkeypatch, mode):
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", mode)
    assert base.start_base_lane({"8453": _ctx()}, log=lambda line: None) is None


def test_runner_shadow_starts_the_listener_snapshot_and_safety_threads_and_stops_cleanly(lane_env, monkeypatch):
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", "runner_shadow")
    lines = []
    thread = base.start_base_lane({"8453": _ctx()}, log=lines.append)
    assert thread is not None and thread.name == "base-fast-lane"
    deadline = time.monotonic() + 3
    while not lane_env and time.monotonic() < deadline:
        time.sleep(0.01)
    assert lane_env and lane_env[0] == base.WETH                  # the snapshot polls Base's WETH
    assert any("runner_shadow started" in line for line in lines), lines
    names = {t.name for _stop, threads in base._LANE_RUNS for t in threads}
    assert names == {"base-fast-lane", "base-fast-snapshot", "base-fast-safety"}


def test_base_start_uses_the_existing_warmed_dex_transport(lane_env, monkeypatch):
    monkeypatch.setenv("MEMECOIN_BASE_FAST_LANE", "runner_shadow")
    calls = []
    def warmed(method, url, headers=None, body=None):
        calls.append((method, url))
        return 200, {"pairs": []}, None
    from sniper.execution import solana_quote
    def cold(*args, **kwargs):
        raise AssertionError("Base used the cold Dex transport")
    monkeypatch.setattr(solana_quote, "_ds_default_http", cold)
    monkeypatch.setattr(rh, "lane_dex_base", lambda base: warmed)
    assert base.start_base_lane({"8453": _ctx()}, log=lambda line: None) is not None
    assert base._ACTIVE["lane"]["dex"].stats("0x" + "ab" * 20, network_id="8453") is None
    assert len(calls) == 1 and calls[0][0] == "GET"


def test_the_runner_loop_starts_the_base_lane_beside_the_other_two():
    source = (Path(__file__).resolve().parents[1] / "scripts/memecoin_runner_loop.py").read_text()
    assert "start_base_lane(contexts, log=_p)" in source
    assert source.index("start_bsc_lane(contexts, log=_p)") < source.index("start_base_lane(contexts, log=_p)")


def test_fill_timing_reads_the_base_lane_s_own_rows():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE rh_fast_lane_events (id INTEGER PRIMARY KEY, tx_hash TEXT, reason TEXT)")
    conn.execute("CREATE TABLE rh_fast_lane_live (id INTEGER PRIMARY KEY, source_tx TEXT, our_tx TEXT, "
                 "reason TEXT)")
    conn.execute("INSERT INTO rh_fast_lane_events (tx_hash, reason) VALUES ('0xsrc', 'hook:filled:')")
    conn.execute("INSERT INTO rh_fast_lane_live (source_tx, our_tx, reason) VALUES ('0xsrc', '0xOURS', 'filled')")
    lanes = {"base_lane": conn}
    assert timing.fast_of(lanes, "8453", "0xsrc") == {"lane": "base fast lane", "reason": "hook:filled:"}
    assert timing.lane_live_of(lanes, "8453", "0xsrc", "0xours")["reason"] == "filled"
    assert ("base_lane", "memecoin_base_fast_lane.db") in timing._LANE_FILES

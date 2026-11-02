"""Research replay stops before the next read when funded exposure appears."""
import json
import sqlite3

import pytest

from scripts.research import evm_rpc_rtt_replay as replay


@pytest.mark.parametrize("spacing", ["0", "-1", "0.99", "nan", "inf"])
def test_invalid_spacing_refuses_before_environment_or_rpc(monkeypatch, spacing):
    monkeypatch.setattr("sys.argv", ["replay", "--spacing", spacing])
    monkeypatch.setattr("sniper.util.envfiles.load_env_files",
                        lambda *a: pytest.fail("environment read before argument validation"))
    with pytest.raises(SystemExit) as exc:
        replay.main()
    assert exc.value.code == 2


def test_new_funded_position_stops_before_the_next_host_request(monkeypatch, tmp_path):
    writer = sqlite3.connect(tmp_path / "sniper_tracker.db")
    writer.execute("CREATE TABLE memecoin_positions (execution_mode TEXT, is_open INTEGER, network_id TEXT)")
    writer.execute("CREATE TABLE memecoin_entry_attempts (trace_json TEXT, network_id TEXT, decision_started_at TEXT)")
    trace = {"substage_events": [{"name": "evm.quote_initial", "rungs": [
        {"rung": "v3", "calls": [{"method": "eth_call", "params": [{"to": "0x1"}, "latest"]}]}]}]}
    writer.execute("INSERT INTO memecoin_entry_attempts VALUES (?, '4663', '2026-09-26')",
                   (json.dumps(trace),))
    writer.commit()
    monkeypatch.setattr(replay, "REPO", str(tmp_path))
    monkeypatch.setattr("sniper.util.envfiles.load_env_files", lambda *a: None)
    monkeypatch.setenv("MEMECOIN_RESEARCH_QUICKNODE_RPC_URL_4663", "https://rpc.invalid")
    monkeypatch.setattr("sys.argv", ["replay", "--repeats", "1"])
    monkeypatch.setattr(replay.time, "sleep", lambda *a: None)
    calls = []
    def rpc(url, method, params, ua):
        calls.append(method)
        if method == "eth_call":
            writer.execute("INSERT INTO memecoin_positions VALUES ('real', 1, '4663')")
            writer.commit()
        return 1, None, None, "0x1237"
    monkeypatch.setattr(replay, "rpc", rpc)
    try:
        assert replay.main() == 0
        assert calls == ["eth_chainId", "eth_call"]
    finally:
        writer.close()

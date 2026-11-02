"""Offline response-boundary and whole-run refusal checks for Relay coverage research."""
import io
import json
import socket
import sqlite3
import urllib.error
import urllib.parse

import pytest

from scripts.research import relay_deposit_coverage as coverage


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("coverage tests must not connect to a provider")
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def sample(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.db"
    logs = tmp_path / "logs"
    logs.mkdir()
    txs = ["0x" + suffix * 32 for suffix in ("ab", "cd", "ef")]
    with sqlite3.connect(ledger) as conn:
        conn.execute("CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, origin_event_id TEXT, "
                     "network_id TEXT, source_user_id TEXT, source_handle TEXT, terminal_reason TEXT, "
                     "decision_started_at TEXT, trace_json TEXT)")
        for i, tx in enumerate(txs):
            conn.execute("INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?,?,?)",
                         (str(i), "onchain:4663:" + tx + ":mint", "4663", str(i), "user" + str(i),
                          "wallet_initiation_unproved", "2026-10-09T01:00:00", "{}"))
    (logs / "onchain_evm_armed.log").write_text("".join(
        "2026-10-09T01:00:00 [onchain-evm] relay origin %s reason=origin_rate_limit verify_ms=8000\n"
        % tx[:12] for tx in txs) +
        "2026-10-09T01:00:00 [onchain-evm] relay origin 0x1234567890 reason=verified verify_ms=1\n")
    sleeps = []
    monkeypatch.setattr(coverage.time, "sleep", sleeps.append)
    return ledger, logs, txs, sleeps


def install_provider(monkeypatch, answers):
    calls = []

    def fetch(request, **kwargs):
        tx = urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)["hash"][0]
        calls.append(tx)
        queue = answers.get(tx, [{"requests": []}])
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, int):
            raise urllib.error.HTTPError(request.full_url, answer, "provider refusal", {}, None)
        if isinstance(answer, Exception):
            raise answer
        return io.BytesIO(json.dumps(answer).encode())

    monkeypatch.setattr(coverage.urllib.request, "urlopen", fetch)
    return calls


@pytest.mark.parametrize("completed", [0, 1])
def test_second_429_stops_all_transactions_and_preserves_partial_results(sample, monkeypatch, completed):
    ledger, logs, txs, sleeps = sample
    calls = install_provider(monkeypatch, {txs[completed]: [429, 429]})
    result = coverage.run(str(ledger), "2026-10-09", logs)

    assert calls == txs[:completed] + [txs[completed], txs[completed]]
    assert [r["class"] for r in result["rows"]] == ["not_found"] * completed + ["relay_error"]
    assert result["rows"][-1]["attempt_id"] == str(completed)
    assert result["rows"][-1]["error"].startswith("HTTPError:")
    assert result["status"] == "stopped" and result["stop_reason"] == "relay_rate_limit"
    assert result["eligible_rows"] == 3 and result["processed_rows"] == completed + 1
    assert result["unprocessed_rows"] == 2 - completed
    assert sum(r["n"] for r in result["table"]) == completed + 1
    assert result["outcomes"] == {"origin_rate_limit": 3, "verified": 1}
    assert sleeps == [6.0] * completed + [120]


def test_first_429_can_recover_and_finish_the_run(sample, monkeypatch):
    ledger, logs, txs, sleeps = sample
    calls = install_provider(monkeypatch, {txs[0]: [429, {"requests": []}]})
    result = coverage.run(str(ledger), "2026-10-09", logs)

    assert calls == [txs[0], *txs]
    assert [r["class"] for r in result["rows"]] == ["not_found"] * 3
    assert result["status"] == "complete" and result["stop_reason"] is None
    assert result["eligible_rows"] == result["processed_rows"] == 3
    assert result["unprocessed_rows"] == 0
    assert sleeps == [120, 6.0, 6.0, 6.0]


@pytest.mark.parametrize("refusal", [503, TimeoutError("timeout"), {}])
def test_other_row_errors_remain_unavailable_and_do_not_stop_the_run(sample, monkeypatch, refusal):
    ledger, logs, txs, _sleeps = sample
    calls = install_provider(monkeypatch, {txs[0]: [refusal]})
    result = coverage.run(str(ledger), "2026-10-09", logs)

    assert calls == txs
    assert [r["class"] for r in result["rows"]] == ["relay_error", "not_found", "not_found"]
    assert result["status"] == "complete" and result["stop_reason"] is None
    assert result["unprocessed_rows"] == 0


@pytest.mark.parametrize("payload", [
    {}, {"requests": None}, {"requests": False}, {"requests": {}},
    {"requests": ""}, {"requests": 0}, {"requests": [None]},
    {"requests": [{}, False]}, {"requests": [[], {}]}, None, [],
])
def test_malformed_requests_are_unavailable_instead_of_clean_absence(payload):
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises((ValueError, TypeError)):
            coverage.classify(conn, {"source_user_id": "u", "init": {}}, payload)


@pytest.mark.parametrize("payload, expected", [
    ({"requests": []}, "not_found"),
    ({"requests": [{"status": "pending"}]}, "unsettled"),
    ({"requests": [{}, {}]}, "ambiguous"),
    ({"requests": [{"status": "success", "protocol": {"deposit": {"origin": {
        "chainId": 1, "depositor": "0xABC", "transactionId": "0xorigin"}}}}]}, "evm_beneficiary"),
])
def test_valid_request_shapes_keep_their_classification(payload, expected):
    with sqlite3.connect(":memory:") as conn:
        cls, _facts = coverage.classify(conn, {"source_user_id": "u", "init": {"beneficiary": "0xabc"}}, payload)
    assert cls == expected

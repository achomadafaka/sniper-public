"""Failing acceptance tests for A.6 — per-page progress emitters.

Spec: docs/superpowers/specs/2026-05-12-fetch-rebuild-design.md §A.6,
      acceptance criteria 20-22.

These tests target the NOT-YET-IMPLEMENTED emitter wiring:
  - progress_emitter: Callable[[str, dict], None] | None on fetch_user_positions
  - progress_emitter: Callable[[str, dict], None] | None on fetch_user_activity
  - progress_emitter: Callable[[str, dict], None] | None on fetch_wallet_data
  - JsonlEmitter wired from run_wallet_protocol.py via --run-log-dir

Current state on main (ede8e1d):
  - fetch_user_positions has the OLD emitter shape: Callable[[int, int], None]
    (takes n_rows, offset — not (event_name, dict)).
  - fetch_user_activity has NO progress_emitter param at all.
  - fetch_wallet_data has NO progress_emitter param at all.
  - run_wallet_protocol.py does NOT wire a progress emitter to fetch_wallet_data.

All tests in this file must fail until the implementation is complete.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helper: list-based mock emitter — collects (event_name, payload) tuples
# ---------------------------------------------------------------------------

class _ListEmitter:
    """Drop-in for progress_emitter: Callable[[str, dict], None]."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __call__(self, event_name: str, payload: dict) -> None:
        self.events.append((event_name, payload))

    def event_names(self) -> list[str]:
        return [e for e, _ in self.events]

    def payloads_for(self, event_name: str) -> list[dict]:
        return [p for e, p in self.events if e == event_name]


# ---------------------------------------------------------------------------
# Minimal fixtures for mocking HTTP responses
# ---------------------------------------------------------------------------

def _make_page(n: int, offset: int = 0) -> list[dict]:
    """Return a list of n fake position rows with distinct timestamps."""
    return [{"conditionId": f"cid_{offset + i}", "timestamp": 1700000000 + offset + i}
            for i in range(n)]


# ---------------------------------------------------------------------------
# A.6.1 — fetch_user_positions emitter signature is (str, dict)
# ---------------------------------------------------------------------------

def test_fetch_user_positions_progress_emitter_accepts_str_dict_callable():
    """progress_emitter on fetch_user_positions must accept Callable[[str, dict], None].

    Fails if the param annotation is the old Callable[[int, int], None] shape
    or if the parameter doesn't exist at all.
    """
    from sniper.markets.polymarket_api import fetch_user_positions
    sig = inspect.signature(fetch_user_positions)
    assert "progress_emitter" in sig.parameters, \
        "fetch_user_positions missing progress_emitter parameter"

    hints = fetch_user_positions.__annotations__
    # The implementer must change the annotation to Callable[[str, dict], None] | None.
    # We can't compare Callable types directly at runtime, so we verify that the
    # emitter is called with (str, dict) by exercising it with a mock.
    emitter = _ListEmitter()
    # Patch HTTP to return one full page then empty page
    with patch("sniper.markets.polymarket_api._throttle_requests"), \
         patch("sniper.markets.polymarket_api._get_httpx_client") as mock_client_fn:
        mock_resp_full = MagicMock()
        mock_resp_full.status_code = 200
        mock_resp_full.json.return_value = _make_page(50)
        mock_resp_empty = MagicMock()
        mock_resp_empty.status_code = 200
        mock_resp_empty.json.return_value = []
        mock_client = MagicMock()
        mock_client.get.side_effect = [mock_resp_full, mock_resp_empty]
        mock_client_fn.return_value = mock_client

        fetch_user_positions(None, "0xabc", progress_emitter=emitter)

    assert len(emitter.events) > 0, \
        "progress_emitter was never called — no events emitted"
    # Each call must be (str, dict) — check first event
    event_name, payload = emitter.events[0]
    assert isinstance(event_name, str), \
        f"progress_emitter first arg must be str, got {type(event_name)}"
    assert isinstance(payload, dict), \
        f"progress_emitter second arg must be dict, got {type(payload)}"


# ---------------------------------------------------------------------------
# A.6.2 — fetch_user_positions emits fetch_progress per successful page
# ---------------------------------------------------------------------------

def test_fetch_user_positions_emits_fetch_progress_per_page():
    """Each successful page must emit a fetch_progress event.

    Spec: 'fetch_progress emitted by _request_json (or per-page hook) after
    each successful page'.
    Two full pages → 2 fetch_progress events.
    """
    from sniper.markets.polymarket_api import fetch_user_positions
    emitter = _ListEmitter()

    with patch("sniper.markets.polymarket_api._throttle_requests"), \
         patch("sniper.markets.polymarket_api._get_httpx_client") as mock_client_fn:
        mock_client = MagicMock()
        # Page 1: 50 rows, Page 2: 50 rows, Page 3: empty → stop
        r1 = MagicMock(status_code=200)
        r1.json.return_value = _make_page(50, 0)
        r2 = MagicMock(status_code=200)
        r2.json.return_value = _make_page(50, 50)
        r3 = MagicMock(status_code=200)
        r3.json.return_value = []
        mock_client.get.side_effect = [r1, r2, r3]
        mock_client_fn.return_value = mock_client

        fetch_user_positions(None, "0xabc", progress_emitter=emitter)

    progress_events = emitter.payloads_for("fetch_progress")
    assert len(progress_events) == 2, \
        f"Expected 2 fetch_progress events (one per page), got {len(progress_events)}"


# ---------------------------------------------------------------------------
# A.6.3 — fetch_progress payload shape
# ---------------------------------------------------------------------------

def test_fetch_user_positions_fetch_progress_payload_shape():
    """fetch_progress payload must carry {endpoint, n_rows, offset, page, elapsed_s}.

    Spec payload: {endpoint: str, n_rows: int, offset: int, page: int, elapsed_s: float}
    No extra undocumented keys required; all 5 required keys must be present.
    """
    from sniper.markets.polymarket_api import fetch_user_positions
    emitter = _ListEmitter()

    with patch("sniper.markets.polymarket_api._throttle_requests"), \
         patch("sniper.markets.polymarket_api._get_httpx_client") as mock_client_fn:
        mock_client = MagicMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = _make_page(50)
        r2 = MagicMock(status_code=200)
        r2.json.return_value = []
        mock_client.get.side_effect = [r1, r2]
        mock_client_fn.return_value = mock_client

        fetch_user_positions(None, "0xabc", endpoint="closed-positions",
                             progress_emitter=emitter)

    progress_events = emitter.payloads_for("fetch_progress")
    assert progress_events, "No fetch_progress events emitted"
    payload = progress_events[0]
    required_keys = {"endpoint", "n_rows", "offset", "page", "elapsed_s"}
    missing = required_keys - set(payload.keys())
    assert not missing, f"fetch_progress payload missing keys: {missing}"
    assert payload["endpoint"] == "closed-positions", \
        f"endpoint in payload should be 'closed-positions', got {payload['endpoint']!r}"
    assert isinstance(payload["n_rows"], int)
    assert isinstance(payload["offset"], int)
    assert isinstance(payload["page"], int)
    assert isinstance(payload["elapsed_s"], float)


# ---------------------------------------------------------------------------
# A.6.4 — fetch_user_positions emits fetch_terminated once at end
# ---------------------------------------------------------------------------

def test_fetch_user_positions_emits_fetch_terminated_once():
    """fetch_terminated must be emitted exactly once, at natural termination.

    Reason must be in {'empty_page', 'partial_page', 'api_refused'}.
    """
    from sniper.markets.polymarket_api import fetch_user_positions
    emitter = _ListEmitter()

    with patch("sniper.markets.polymarket_api._throttle_requests"), \
         patch("sniper.markets.polymarket_api._get_httpx_client") as mock_client_fn:
        mock_client = MagicMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = _make_page(50)
        r2 = MagicMock(status_code=200)
        r2.json.return_value = []  # empty page → terminates
        mock_client.get.side_effect = [r1, r2]
        mock_client_fn.return_value = mock_client

        fetch_user_positions(None, "0xabc", progress_emitter=emitter)

    terminated = emitter.payloads_for("fetch_terminated")
    assert len(terminated) == 1, \
        f"Expected exactly 1 fetch_terminated event, got {len(terminated)}"


def test_fetch_user_positions_fetch_terminated_reason_vocabulary():
    """fetch_terminated reason must be one of the FetchTermination vocabulary strings."""
    from sniper.markets.polymarket_api import fetch_user_positions
    emitter = _ListEmitter()

    with patch("sniper.markets.polymarket_api._throttle_requests"), \
         patch("sniper.markets.polymarket_api._get_httpx_client") as mock_client_fn:
        mock_client = MagicMock()
        # Partial page → reason should be 'partial_page'
        r1 = MagicMock(status_code=200)
        r1.json.return_value = _make_page(30)  # 30 < page_limit=50 → partial
        mock_client.get.side_effect = [r1]
        mock_client_fn.return_value = mock_client

        fetch_user_positions(None, "0xabc", progress_emitter=emitter)

    terminated = emitter.payloads_for("fetch_terminated")
    assert terminated, "No fetch_terminated event emitted"
    reason = terminated[0].get("reason")
    assert reason in {"empty_page", "partial_page", "api_refused"}, \
        f"fetch_terminated reason {reason!r} not in vocabulary"
    assert reason == "partial_page", \
        f"Expected 'partial_page' for a short final page, got {reason!r}"


def test_fetch_user_positions_fetch_terminated_payload_shape():
    """fetch_terminated payload must carry {endpoint, reason, n_rows, offset, status}."""
    from sniper.markets.polymarket_api import fetch_user_positions
    emitter = _ListEmitter()

    with patch("sniper.markets.polymarket_api._throttle_requests"), \
         patch("sniper.markets.polymarket_api._get_httpx_client") as mock_client_fn:
        mock_client = MagicMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = _make_page(10)  # partial page
        mock_client.get.side_effect = [r1]
        mock_client_fn.return_value = mock_client

        fetch_user_positions(None, "0xabc", endpoint="positions", progress_emitter=emitter)

    terminated = emitter.payloads_for("fetch_terminated")
    assert terminated
    payload = terminated[0]
    required = {"endpoint", "reason", "n_rows", "offset", "status"}
    missing = required - set(payload.keys())
    assert not missing, f"fetch_terminated payload missing keys: {missing}"


# ---------------------------------------------------------------------------
# A.6.5 — fetch_user_positions with emitter=None is a no-op
# ---------------------------------------------------------------------------

def test_fetch_user_positions_no_emitter_is_noop():
    """progress_emitter=None (default) must not raise and must not emit anything."""
    from sniper.markets.polymarket_api import fetch_user_positions

    with patch("sniper.markets.polymarket_api._throttle_requests"), \
         patch("sniper.markets.polymarket_api._get_httpx_client") as mock_client_fn:
        mock_client = MagicMock()
        r1 = MagicMock(status_code=200)
        r1.json.return_value = _make_page(10)
        mock_client.get.side_effect = [r1]
        mock_client_fn.return_value = mock_client

        rows, term = fetch_user_positions(None, "0xabc")
    # No exception raised — rows returned normally
    assert len(rows) == 10


# ---------------------------------------------------------------------------
# A.6.6 — fetch_user_activity has progress_emitter parameter
# ---------------------------------------------------------------------------

def test_fetch_user_activity_accepts_progress_emitter_param():
    """fetch_user_activity must accept progress_emitter: Callable[[str, dict], None].

    Currently fetch_user_activity has NO progress_emitter param — this test
    will fail with TypeError when passing the kwarg.
    """
    from sniper.markets.polymarket_api import fetch_user_activity
    sig = inspect.signature(fetch_user_activity)
    assert "progress_emitter" in sig.parameters, \
        "fetch_user_activity missing progress_emitter parameter (spec A.6)"


# ---------------------------------------------------------------------------
# A.6.7 — fetch_user_activity emits fetch_window_complete per window
# ---------------------------------------------------------------------------

def test_fetch_user_activity_emits_fetch_window_complete_per_window():
    """When walk_full_history=True, each end_ts window emits fetch_window_complete.

    Spec: 'fetch_window_complete emitted by fetch_user_activity after each
    end_ts window when walk_full_history=True'.
    """
    from sniper.markets.polymarket_api import fetch_user_activity
    emitter = _ListEmitter()

    # Simulate 2 windows: each exhausts its offset cap, then the third window is empty
    ts_base = 1700000000
    # Window 1: 50 rows filling offset cap → triggers walk to window 2
    # Window 2: 20 rows (partial) → natural stop
    window1_rows = [{"conditionId": f"cid_{i}", "timestamp": ts_base - i,
                     "transactionHash": f"tx_{i}"} for i in range(50)]
    window2_rows = [{"conditionId": f"cid_w2_{i}", "timestamp": ts_base - 100 - i,
                     "transactionHash": f"tx_w2_{i}"} for i in range(20)]

    call_count = [0]

    def fake_request_json(session, url, params=None, cache_ttl_seconds=None):
        n = call_count[0]
        call_count[0] += 1
        offset = (params or {}).get("offset", 0)
        if n == 0:
            # First page of window 1: full
            return window1_rows if offset == 0 else []
        elif n == 1:
            # Hit the Polymarket 3000-row cap → break inner loop (HTTP 400 sim)
            raise __import__("httpx").HTTPStatusError(
                "400", request=MagicMock(), response=MagicMock(status_code=400)
            )
        elif n == 2:
            # Second window (after end_ts shift): partial → stop walk
            return window2_rows if offset == 0 else []
        else:
            return []

    with patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        fetch_user_activity(None, "0xabc", walk_full_history=True,
                            progress_emitter=emitter)

    window_complete = emitter.payloads_for("fetch_window_complete")
    assert len(window_complete) >= 1, \
        f"Expected at least 1 fetch_window_complete event, got {len(window_complete)}"


# ---------------------------------------------------------------------------
# A.6.8 — fetch_window_complete payload shape
# ---------------------------------------------------------------------------

def test_fetch_user_activity_fetch_window_complete_payload_shape():
    """fetch_window_complete payload must carry {endpoint, window_end_ts, n_rows_window, total_rows}."""
    from sniper.markets.polymarket_api import fetch_user_activity
    emitter = _ListEmitter()

    ts_base = 1700000000
    window1_rows = [{"conditionId": f"cid_{i}", "timestamp": ts_base - i,
                     "transactionHash": f"tx_{i}"} for i in range(20)]

    call_count = [0]

    def fake_request_json(session, url, params=None, cache_ttl_seconds=None):
        n = call_count[0]
        call_count[0] += 1
        if n == 0:
            return window1_rows
        return []

    with patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        fetch_user_activity(None, "0xabc", walk_full_history=True,
                            progress_emitter=emitter)

    wc_payloads = emitter.payloads_for("fetch_window_complete")
    assert wc_payloads, "No fetch_window_complete events emitted"
    payload = wc_payloads[0]
    required = {"endpoint", "window_end_ts", "n_rows_window", "total_rows"}
    missing = required - set(payload.keys())
    assert not missing, f"fetch_window_complete payload missing keys: {missing}"


# ---------------------------------------------------------------------------
# A.6.9 — fetch_user_activity emits fetch_terminated once
# ---------------------------------------------------------------------------

def test_fetch_user_activity_emits_fetch_terminated_once():
    """fetch_user_activity must emit fetch_terminated exactly once at outer termination."""
    from sniper.markets.polymarket_api import fetch_user_activity
    emitter = _ListEmitter()

    def fake_request_json(session, url, params=None, cache_ttl_seconds=None):
        return []  # immediate empty → stop

    with patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        fetch_user_activity(None, "0xabc", walk_full_history=False,
                            progress_emitter=emitter)

    terminated = emitter.payloads_for("fetch_terminated")
    assert len(terminated) == 1, \
        f"Expected exactly 1 fetch_terminated event from fetch_user_activity, got {len(terminated)}"


def test_fetch_user_activity_retry_exhaustion_returns_partial_rows():
    """A degraded later page should preserve already-fetched activity rows."""
    from sniper.markets.polymarket_api import fetch_user_activity
    emitter = _ListEmitter()

    first_page = [
        {"conditionId": "cid_1", "timestamp": 1700000002, "transactionHash": "tx_1"},
        {"conditionId": "cid_2", "timestamp": 1700000001, "transactionHash": "tx_2"},
    ]
    calls = [0]

    def fake_request_json(session, url, params=None, cache_ttl_seconds=None):
        calls[0] += 1
        if calls[0] == 1:
            return first_page
        raise __import__("httpx").ConnectError("retry budget exhausted")

    with patch("sniper.markets.polymarket_api._request_json", side_effect=fake_request_json):
        rows = fetch_user_activity(
            None,
            "0xabc",
            page_limit=2,
            walk_full_history=False,
            progress_emitter=emitter,
        )

    assert rows == first_page
    terminated = emitter.payloads_for("fetch_terminated")
    assert len(terminated) == 1
    assert terminated[0]["reason"] == "request_failed"
    assert terminated[0]["n_rows"] == 2
    assert terminated[0]["offset"] == 2


# ---------------------------------------------------------------------------
# A.6.10 — fetch_wallet_data accepts and threads progress_emitter
# ---------------------------------------------------------------------------

def test_fetch_wallet_data_accepts_progress_emitter_param():
    """fetch_wallet_data must accept a progress_emitter kwarg and thread it through.

    Currently no such param exists — this test will fail immediately.
    """
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data
    sig = inspect.signature(fetch_wallet_data)
    assert "progress_emitter" in sig.parameters, \
        "fetch_wallet_data missing progress_emitter parameter (spec A.6)"


def test_fetch_wallet_data_threads_emitter_to_fetch_functions():
    """fetch_wallet_data must pass progress_emitter to both fetch_user_positions
    and fetch_user_activity so emitter sees events from all endpoints.
    """
    from sniper.analysis.wallet_protocol.fetch import fetch_wallet_data
    emitter = _ListEmitter()

    with patch("sniper.analysis.wallet_protocol.fetch.polymarket_api") as mock_api:
        from sniper.markets.polymarket_api import FetchTermination
        mock_api.fetch_closed_positions.return_value = []
        mock_api.fetch_user_positions.return_value = ([], FetchTermination("empty_page", offset=0))
        mock_api.fetch_user_activity.return_value = []
        mock_api.fetch_user_value.return_value = 0.0

        fetch_wallet_data("0xabc", progress_emitter=emitter)

    # Both calls should have been forwarded the emitter
    calls = mock_api.fetch_user_positions.call_args_list
    assert any("progress_emitter" in (call.kwargs or {}) for call in calls) or \
           any(emitter in (call.args or ()) for call in calls), \
        "fetch_wallet_data did not forward progress_emitter to fetch_user_positions"

    act_calls = mock_api.fetch_user_activity.call_args_list
    assert any("progress_emitter" in (act.kwargs or {}) for act in act_calls) or \
           any(emitter in (act.args or ()) for act in act_calls), \
        "fetch_wallet_data did not forward progress_emitter to fetch_user_activity"


# ---------------------------------------------------------------------------
# A.6.11 — run_wallet_protocol constructs JsonlEmitter when --run-log-dir set
# ---------------------------------------------------------------------------

def test_run_wallet_protocol_constructs_emitter_when_run_log_dir_set():
    """run_wallet_protocol.py must construct a JsonlEmitter when --run-log-dir
    is passed and wire it through to fetch_wallet_data.

    Spec: 'scripts/run_wallet_protocol.py constructs a JsonlEmitter when
    --run-log-dir is set and wires it through to fetch_wallet_data so every
    page lands as one JSONL line.'

    Currently fetch_wallet_data has no progress_emitter param and
    run_wallet_protocol doesn't wire one — both will fail.
    """
    import importlib
    import importlib.util

    script_path = Path(__file__).parent.parent.parent / "scripts" / "run_wallet_protocol.py"
    assert script_path.exists(), f"Script not found: {script_path}"

    spec = importlib.util.spec_from_file_location("run_wallet_protocol", script_path)
    module = importlib.util.module_from_spec(spec)
    # Don't execute (side effects), just inspect the source for wiring
    source = script_path.read_text()

    # The script must reference progress_emitter in the fetch_wallet_data call
    assert "progress_emitter" in source, \
        "run_wallet_protocol.py does not pass progress_emitter to fetch_wallet_data"

    # And it must construct a JsonlEmitter (it already imports it — check wiring call)
    # The emitter must be passed to fetch_wallet_data, not just emitter.emit()
    # We check that the fetch_wallet_data call site includes progress_emitter=
    assert "fetch_wallet_data" in source, "fetch_wallet_data call not found in script"
    # Find the fetch_wallet_data call block; it must include progress_emitter
    fetch_call_idx = source.index("fetch_wallet_data")
    # Look at the surrounding ~300 chars for progress_emitter
    window = source[fetch_call_idx:fetch_call_idx + 400]
    assert "progress_emitter" in window, \
        "fetch_wallet_data call in run_wallet_protocol.py does not include progress_emitter"


# ---------------------------------------------------------------------------
# A.6.12 — JsonlEmitter writes one JSONL line per emitted event
# ---------------------------------------------------------------------------

def test_jsonl_emitter_writes_one_line_per_event(tmp_path):
    """JsonlEmitter.emit() must write one valid JSON line per call.

    This tests the existing JsonlEmitter to confirm the emitter contract
    that the fetch functions will use. JsonlEmitter already exists — this
    test should PASS (it's a baseline check). If it fails, the emitter
    itself is broken.

    NOTE: This test is intentionally included here as a green baseline so
    reviewers can distinguish 'emitter works, wiring missing' from
    'emitter itself broken'. CI will count it in the red suite until the
    other tests pass too — the suite overall is red.
    """
    from sniper.analysis.wallet_protocol.run_events import JsonlEmitter
    log_file = tmp_path / "test.jsonl"
    emitter = JsonlEmitter(log_file, run_id="r1", wallet="0xabc")

    emitter.emit("fetch_progress", {"endpoint": "closed-positions", "n_rows": 50,
                                    "offset": 0, "page": 1, "elapsed_s": 0.5})
    emitter.emit("fetch_terminated", {"endpoint": "closed-positions", "reason": "empty_page",
                                       "n_rows": 50, "offset": 50, "status": None})

    lines = log_file.read_text().strip().split("\n")
    assert len(lines) == 2, f"Expected 2 JSONL lines, got {len(lines)}"

    row0 = json.loads(lines[0])
    assert row0["event"] == "fetch_progress"
    assert row0["payload"]["endpoint"] == "closed-positions"
    assert row0["payload"]["n_rows"] == 50

    row1 = json.loads(lines[1])
    assert row1["event"] == "fetch_terminated"
    assert row1["payload"]["reason"] == "empty_page"

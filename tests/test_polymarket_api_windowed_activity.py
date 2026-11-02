"""Tests for walked-history pagination in fetch_user_activity."""

from unittest.mock import patch

import pytest

import sniper.markets.polymarket_api as polymarket_api


@pytest.fixture(autouse=True)
def _disable_polymarket_pagination_sleep(monkeypatch):
    """Inter-page sleep in `polymarket_api.fetch_user_activity` (and the
    other paginators in that module) exists for live rate-limit safety.
    These tests already monkey-patch `_request_json` to a synchronous
    stub — the sleeps are pure harness waste (~10s/test at 60-page
    walks). File-local autouse so the patch doesn't leak to tests that
    legitimately exercise rate-limit timing."""
    monkeypatch.setattr(polymarket_api.time, "sleep", lambda *_a, **_kw: None)


class _FakeSession:
    """Minimal stand-in for requests.Session so the fetcher's signature works."""
    pass


def test_single_window_legacy_behavior_unchanged(monkeypatch):
    """Default walk_full_history=False → at most 3000 rows, then stop."""
    page_calls: list[dict] = []

    def _fake_request(session, url, params=None, **kw):
        page_calls.append(dict(params or {}))
        # Every call returns a full page so the paginator keeps walking
        if len(page_calls) > 60:  # safety brake — would have stopped earlier
            return []
        # Use offset to fabricate rows; never exhaust on the first window
        offset = (params or {}).get("offset", 0)
        return [
            {"transactionHash": f"0x{i + offset:08x}", "timestamp": 9_000_000 - (i + offset), "side": "BUY"}
            for i in range(50)
        ]

    monkeypatch.setattr(polymarket_api, "_request_json", _fake_request)
    rows = polymarket_api.fetch_user_activity(
        _FakeSession(), "0xabc", page_limit=50, max_pages=60,
    )
    # 60 pages × 50 rows = 3000
    assert len(rows) == 3000
    # Should NOT have shifted `end=` parameter (single-window mode)
    assert all("end" not in c for c in page_calls)


def test_walk_full_history_shifts_end_parameter(monkeypatch):
    """When the first window completes (3000 rows), `end=` shifts to oldest_ts."""
    page_calls: list[dict] = []

    def _fake_request(session, url, params=None, **kw):
        page_calls.append(dict(params or {}))
        offset = (params or {}).get("offset", 0)
        end_arg = (params or {}).get("end")

        # First window (no `end` param): return 60 full pages × 50 rows = 3000
        if end_arg is None:
            if offset >= 3000:
                return []
            base_ts = 9_000_000
            return [
                {
                    "transactionHash": f"0x{i + offset:08x}",
                    "timestamp": base_ts - (i + offset),
                    "side": "BUY",
                }
                for i in range(50)
            ]

        # Second window: should be called with end < oldest_ts of first window
        # The oldest_ts of window 1 was base_ts - 2999 = 8_997_001
        # So end_arg should be 8_997_000
        assert end_arg <= 8_997_000
        if offset >= 100:  # smaller second window
            return []
        # Timestamp is intrinsic to the row (tx ⟺ ts), NOT a function of the
        # query window: a transaction lives in exactly one block with one
        # timestamp, so the SAME transactionHash must always carry the SAME
        # timestamp. (Pinning to end_arg modeled one tx at several timestamps —
        # physically impossible — which only "terminated" by accident under the
        # old tx-only dedup. Once dedup keys on the full per-fill identity, the
        # third window's re-served rows fall above the shifted `end` and are
        # dropped by the ts>end guard, so the walk terminates at 3100.)
        return [
            {
                "transactionHash": f"0xff{i + offset:06x}",
                "timestamp": 8_997_000 - (i + offset),
                "side": "BUY",
            }
            for i in range(50)
        ]

    monkeypatch.setattr(polymarket_api, "_request_json", _fake_request)
    rows = polymarket_api.fetch_user_activity(
        _FakeSession(), "0xabc", page_limit=50, max_pages=60,
        walk_full_history=True,
    )
    # 3000 from window 1 + 100 from window 2
    assert len(rows) == 3100
    # At least one call shifted `end` param
    assert any("end" in c for c in page_calls)


def test_walk_dedups_overlapping_tx_hashes(monkeypatch):
    """Same transactionHash returned in two windows is counted once."""
    call_count = {"n": 0}

    def _fake_request(session, url, params=None, **kw):
        call_count["n"] += 1
        end_arg = (params or {}).get("end")
        if call_count["n"] == 1 and end_arg is None:
            # First window: small (5 rows < page_limit) → walker tries to advance
            return [
                {"transactionHash": f"0x{i:08x}", "timestamp": 1000 - i, "side": "BUY"}
                for i in range(5)
            ]
        return []  # second window empty → walker exits

    monkeypatch.setattr(polymarket_api, "_request_json", _fake_request)
    rows = polymarket_api.fetch_user_activity(
        _FakeSession(), "0xabc", page_limit=50, max_pages=60,
        walk_full_history=True,
    )
    # Should not double-count
    hashes = [r["transactionHash"] for r in rows]
    assert len(hashes) == len(set(hashes))


# test_walk_respects_max_total_rows removed: Phase A drops the max_total_rows
# cap entirely. Walking continues until natural exhaustion per the rebuild spec
# (docs/superpowers/specs/2026-05-12-fetch-rebuild-design.md A.2).


def test_walk_respects_start_ts_lower_bound(monkeypatch):
    """When start_ts is provided, walker bails once a row older than it is seen."""
    def _fake_request(session, url, params=None, **kw):
        offset = (params or {}).get("offset", 0)
        end_arg = (params or {}).get("end") or 9_000_000
        return [
            {"transactionHash": f"0x{end_arg}_{offset + i}",
             "timestamp": end_arg - (offset + i) * 100,
             "side": "BUY"}
            for i in range(50)
        ]

    monkeypatch.setattr(polymarket_api, "_request_json", _fake_request)
    rows = polymarket_api.fetch_user_activity(
        _FakeSession(), "0xabc", page_limit=50, max_pages=60,
        walk_full_history=True, start_ts=8_999_000,
    )
    # Walker should bail in the first window when timestamps go below start_ts
    assert all(int(r["timestamp"]) >= 8_999_000 - 1 for r in rows)


def test_walk_stops_when_no_backward_progress(monkeypatch):
    """If a window's oldest_ts >= previous end_ts, walker exits to avoid infinite loop.

    Phase A removed max_total_rows; backward-progress detection is now the
    primary loop-termination signal.
    """
    call_count = {"n": 0}

    def _fake_request(session, url, params=None, **kw):
        call_count["n"] += 1
        offset = (params or {}).get("offset", 0)
        # Always return rows with timestamp == 9_000_000 (no progress)
        if offset >= 3000:
            return []
        return [
            {"transactionHash": f"0x{call_count['n']}_{offset + i}",
             "timestamp": 9_000_000,
             "side": "BUY"}
            for i in range(50)
        ]

    monkeypatch.setattr(polymarket_api, "_request_json", _fake_request)
    rows = polymarket_api.fetch_user_activity(
        _FakeSession(), "0xabc", page_limit=50, max_pages=60,
        walk_full_history=True,
    )
    # Should stop after first window (no backward progress in subsequent)
    assert len(rows) <= 3000


def test_default_page_limit_is_1000_for_fewer_round_trips():
    """capture-hardening-v2: fetch_user_activity's default page_limit is
    1000, not 50. The Polymarket /activity endpoint accepts limit=1000
    (verified live 2026-05-21); the offset 3000 cap is separate, so a
    window completes in ~3 requests instead of ~60. Regression pin —
    reverting the default silently restores the ~15x round-trip cost."""
    import inspect

    sig = inspect.signature(polymarket_api.fetch_user_activity)
    assert sig.parameters["page_limit"].default == 1000


def test_bigger_page_limit_collects_identical_rows(monkeypatch):
    """Output invariance: a walk with page_limit=1000 collects the same
    deduped row set as page_limit=50 — only the request count differs."""
    def _make_request(page_limit_seen):
        def _fake_request(session, url, params=None, **kw):
            limit = (params or {}).get("limit", 50)
            page_limit_seen.append(limit)
            offset = (params or {}).get("offset", 0)
            end = (params or {}).get("end")
            base = end if end is not None else 10_000
            # 2500 total rows in the (single) window, descending timestamps.
            if offset >= 2500:
                return []
            return [
                {"transactionHash": f"0xtx_{base}_{offset + i}",
                 "timestamp": base - (offset + i),
                 "side": "BUY"}
                for i in range(min(limit, 2500 - offset))
            ]
        return _fake_request

    seen_small: list[int] = []
    monkeypatch.setattr(polymarket_api, "_request_json", _make_request(seen_small))
    rows_small = polymarket_api.fetch_user_activity(
        _FakeSession(), "0xabc", page_limit=50, max_pages=120,
    )

    seen_big: list[int] = []
    monkeypatch.setattr(polymarket_api, "_request_json", _make_request(seen_big))
    rows_big = polymarket_api.fetch_user_activity(
        _FakeSession(), "0xabc", page_limit=1000, max_pages=120,
    )

    assert [r["transactionHash"] for r in rows_small] == [
        r["transactionHash"] for r in rows_big
    ]
    # Bigger pages → strictly fewer HTTP calls.
    assert len(seen_big) < len(seen_small)

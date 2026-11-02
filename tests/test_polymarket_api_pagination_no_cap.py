"""
Failing acceptance tests for Phase A split #1 — A.1 pagination cap removal.

Spec: docs/superpowers/specs/2026-05-12-fetch-rebuild-design.md
Acceptance criteria covered: #1, #2, #3

All tests are expected to FAIL on main until the implementation lands.
Failure mode: ImportError on FetchTermination (doesn't exist yet) or
  AssertionError on max_pages removal / sortDirection / return type.

Imports are done inside each test method so that collection succeeds and
pytest reports each test as FAILED (with ImportError) rather than one
ERROR that swallows all tests.
"""

import inspect
import pytest
import httpx

import sniper.markets.polymarket_api as polymarket_api


@pytest.fixture(autouse=True)
def _disable_polymarket_pagination_sleep(monkeypatch):
    """Same rationale as the sister fixture in
    `test_polymarket_api_windowed_activity.py`: tests mock the HTTP
    transport, so `polymarket_api`'s inter-page `time.sleep(0.1)` is
    pure harness latency. File-local to avoid leaking into tests that
    intentionally exercise rate-limit timing."""
    monkeypatch.setattr(polymarket_api.time, "sleep", lambda *_a, **_kw: None)


# ---------------------------------------------------------------------------
# Helpers: build a mock httpx transport that serves paginated responses
# ---------------------------------------------------------------------------

def _make_transport(pages: list, status_on_extra: int = 200):
    """
    Returns an httpx.MockTransport that serves pages[i] for request i.
    After pages are exhausted, returns an empty list (natural end).
    If status_on_extra != 200, returns that status code instead.
    """
    call_count = [0]
    page_data = list(pages)

    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(page_data):
            return httpx.Response(200, json=page_data[idx])
        if status_on_extra != 200:
            return httpx.Response(status_on_extra, text="refused")
        return httpx.Response(200, json=[])

    return httpx.MockTransport(handler)


def _make_client(pages: list, status_on_extra: int = 200) -> httpx.Client:
    transport = _make_transport(pages, status_on_extra)
    return httpx.Client(transport=transport)


def _page(n: int, base: int = 0) -> list:
    """Build a fake page of n position dicts starting at base."""
    return [{"id": base + i, "wallet": "0xtest"} for i in range(n)]


# ---------------------------------------------------------------------------
# A.1 — FetchTermination dataclass shape
# ---------------------------------------------------------------------------

class TestFetchTerminationShape:
    """Spec: FetchTermination dataclass with reason / status / offset fields."""

    def test_fetch_termination_has_reason_field(self):
        from sniper.markets.polymarket_api import FetchTermination
        ft = FetchTermination(reason="empty_page", status=None, offset=0)
        assert ft.reason == "empty_page"

    def test_fetch_termination_has_status_field(self):
        from sniper.markets.polymarket_api import FetchTermination
        ft = FetchTermination(reason="api_refused", status=403, offset=100)
        assert ft.status == 403

    def test_fetch_termination_has_offset_field(self):
        from sniper.markets.polymarket_api import FetchTermination
        ft = FetchTermination(reason="partial_page", status=None, offset=250)
        assert ft.offset == 250

    def test_fetch_termination_status_none_for_natural_end(self):
        from sniper.markets.polymarket_api import FetchTermination
        ft = FetchTermination(reason="empty_page", status=None, offset=50)
        assert ft.status is None

    def test_fetch_termination_reason_literals(self):
        from sniper.markets.polymarket_api import FetchTermination
        valid_reasons = {"empty_page", "partial_page", "api_refused"}
        for r in valid_reasons:
            ft = FetchTermination(reason=r, status=None, offset=0)
            assert ft.reason in valid_reasons


# ---------------------------------------------------------------------------
# A.1 — No max_pages on fetch_user_positions
# ---------------------------------------------------------------------------

class TestFetchUserPositionsNoMaxPages:
    """Spec AC #1: max_pages parameter must NOT exist on the function signature."""

    def test_fetch_user_positions_has_no_max_pages_parameter(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        sig = inspect.signature(fetch_user_positions)
        assert "max_pages" not in sig.parameters, (
            "fetch_user_positions still has max_pages parameter — cap not removed"
        )

    def test_fetch_user_positions_has_no_page_count_limit_parameter(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        sig = inspect.signature(fetch_user_positions)
        for p in sig.parameters:
            assert "max_page" not in p.lower(), (
                f"Unexpected page-cap parameter found: {p!r}"
            )


# ---------------------------------------------------------------------------
# A.1 — Return type is tuple[list[dict], FetchTermination]
# ---------------------------------------------------------------------------

class TestFetchUserPositionsReturnShape:
    """Spec AC #1: returns (rows, FetchTermination)."""

    def test_returns_tuple_of_two_on_empty_first_page(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([])
        result = fetch_user_positions(session=client, wallet="0xabc", endpoint="closed-positions")
        assert isinstance(result, tuple), "must return a tuple"
        assert len(result) == 2, "tuple must have exactly 2 elements"

    def test_second_element_is_fetch_termination(self):
        from sniper.markets.polymarket_api import FetchTermination, fetch_user_positions
        client = _make_client([])
        _, ft = fetch_user_positions(session=client, wallet="0xabc", endpoint="closed-positions")
        assert isinstance(ft, FetchTermination)

    def test_first_element_is_list(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([])
        rows, _ = fetch_user_positions(session=client, wallet="0xabc", endpoint="closed-positions")
        assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# A.1 — Termination reason: empty_page
# ---------------------------------------------------------------------------

class TestPaginationTerminatesOnEmptyPage:
    """Spec AC #1: paginates until empty page → reason='empty_page'."""

    def test_empty_first_page_terminates_empty_page(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([[]])
        rows, ft = fetch_user_positions(session=client, wallet="0xabc", endpoint="closed-positions")
        assert ft.reason == "empty_page"
        assert rows == []

    def test_two_full_pages_then_empty_terminates_empty_page(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([_page(50), _page(50), []])
        rows, ft = fetch_user_positions(session=client, wallet="0xabc",
                                        endpoint="closed-positions", page_limit=50)
        assert ft.reason == "empty_page"
        assert len(rows) == 100

    def test_empty_page_termination_sets_correct_offset(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([_page(50), []])
        _, ft = fetch_user_positions(session=client, wallet="0xabc",
                                     endpoint="closed-positions", page_limit=50)
        assert ft.offset == 50


# ---------------------------------------------------------------------------
# A.1 — Termination reason: partial_page
# ---------------------------------------------------------------------------

class TestPaginationTerminatesOnPartialPage:
    """Spec AC #1: paginates until partial final page → reason='partial_page'."""

    def test_partial_final_page_terminates_partial_page(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([_page(50), _page(50), _page(48)])
        rows, ft = fetch_user_positions(session=client, wallet="0xabc",
                                        endpoint="closed-positions", page_limit=50)
        assert ft.reason == "partial_page"
        assert len(rows) == 148

    def test_single_partial_page_terminates_partial_page(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([_page(23)])
        rows, ft = fetch_user_positions(session=client, wallet="0xabc",
                                        endpoint="closed-positions", page_limit=50)
        assert ft.reason == "partial_page"
        assert len(rows) == 23

    def test_partial_page_offset_reflects_last_successful_page_start(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([_page(50), _page(48)])
        _, ft = fetch_user_positions(session=client, wallet="0xabc",
                                     endpoint="closed-positions", page_limit=50)
        assert ft.offset == 50


# ---------------------------------------------------------------------------
# A.1 — Termination reason: api_refused (4xx)
# ---------------------------------------------------------------------------

class TestPaginationTerminatesOn4xx:
    """Spec AC #1: 4xx after retries → reason='api_refused'."""

    def test_4xx_on_first_page_terminates_api_refused(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([], status_on_extra=404)
        rows, ft = fetch_user_positions(session=client, wallet="0xabc",
                                        endpoint="closed-positions")
        assert ft.reason == "api_refused"
        assert ft.status == 404

    def test_4xx_after_good_pages_terminates_api_refused(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([_page(50), _page(50)], status_on_extra=429)
        rows, ft = fetch_user_positions(session=client, wallet="0xabc",
                                        endpoint="closed-positions", page_limit=50)
        assert ft.reason == "api_refused"
        assert ft.status == 429
        assert len(rows) == 100

    def test_api_refused_carries_http_status_code(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([], status_on_extra=403)
        _, ft = fetch_user_positions(session=client, wallet="0xabc",
                                     endpoint="closed-positions")
        assert ft.status == 403


# ---------------------------------------------------------------------------
# A.1 — sortDirection=DESC for closed-positions (spec AC #2)
# ---------------------------------------------------------------------------

class TestClosedPositionsSortDirectionDesc:
    """Spec AC #2: closed-positions uses sortDirection=DESC."""

    def test_closed_positions_request_includes_sort_direction_desc(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(str(request.url))
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        fetch_user_positions(session=client, wallet="0xabc", endpoint="closed-positions")

        assert captured, "no request was made"
        assert "sortDirection=DESC" in captured[0], (
            f"Expected sortDirection=DESC in URL; got: {captured[0]}"
        )

    def test_closed_positions_request_does_not_use_asc(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        captured = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(str(request.url))
            return httpx.Response(200, json=[])

        client = httpx.Client(transport=httpx.MockTransport(handler))
        fetch_user_positions(session=client, wallet="0xabc", endpoint="closed-positions")

        assert "sortDirection=ASC" not in captured[0], (
            "closed-positions must use DESC not ASC"
        )


# ---------------------------------------------------------------------------
# A.1 — open positions endpoint also paginates to exhaustion (spec AC #3)
# ---------------------------------------------------------------------------

class TestOpenPositionsPaginatesToExhaustion:
    """Spec AC #3: positions (open) paginates until natural end, no max_pages cap."""

    def test_open_positions_terminates_on_empty_page(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([_page(50), []])
        rows, ft = fetch_user_positions(session=client, wallet="0xabc",
                                        endpoint="positions", page_limit=50)
        assert ft.reason == "empty_page"
        assert len(rows) == 50

    def test_open_positions_terminates_on_partial_page(self):
        from sniper.markets.polymarket_api import fetch_user_positions
        client = _make_client([_page(50), _page(30)])
        rows, ft = fetch_user_positions(session=client, wallet="0xabc",
                                        endpoint="positions", page_limit=50)
        assert ft.reason == "partial_page"
        assert len(rows) == 80

    def test_open_positions_returns_fetch_termination(self):
        from sniper.markets.polymarket_api import FetchTermination, fetch_user_positions
        client = _make_client([])
        _, ft = fetch_user_positions(session=client, wallet="0xabc", endpoint="positions")
        assert isinstance(ft, FetchTermination)

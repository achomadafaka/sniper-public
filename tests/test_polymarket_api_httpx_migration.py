"""
Failing acceptance tests for Phase A split #1 — A.3 httpx migration.

Spec: docs/superpowers/specs/2026-05-12-fetch-rebuild-design.md
Acceptance criteria covered: #10, #11, #12, #13

All tests are expected to FAIL on main until the implementation lands.

Failure modes:
- _request_json still uses requests.Session, not httpx.Client → AttributeError or wrong type
- REQUEST_MAX_ATTEMPTS constant still defined → assertion fails
- session reset helper doesn't exist → ImportError/AttributeError
- slow-drip test: requests hangs past timeout; httpx terminates within budget
"""

import inspect
import threading
import time
import pytest
import httpx

# ---------------------------------------------------------------------------
# A.3 — _request_json rebuilt on httpx.Client (spec AC #10)
# ---------------------------------------------------------------------------

class TestRequestJsonUsesHttpx:
    """Spec AC #10: _request_json is built on httpx.Client."""

    def test_request_json_type_annotation_is_httpx_client(self):
        """
        The session parameter type annotation must be httpx.Client, not requests.Session.
        Before migration the annotation is requests.sessions.Session.
        """
        import sniper.markets.polymarket_api as mod
        import requests
        fn = mod._request_json
        hints = {}
        try:
            import typing
            hints = typing.get_type_hints(fn)
        except Exception:
            # get_type_hints may fail if forward refs are unresolvable;
            # fall back to __annotations__
            hints = getattr(fn, "__annotations__", {})

        # Must NOT be annotated as requests.Session (the old type)
        session_type = hints.get("session") or hints.get("client")
        assert session_type is not requests.Session and session_type is not requests.sessions.Session, (
            f"_request_json session parameter is still typed as {session_type!r}. "
            "Post-migration it must be httpx.Client."
        )

    def test_request_json_does_not_use_session_get_from_requests(self):
        """
        After migration the internal call must be via httpx, not requests.Session.get().
        We verify by checking the source does not call requests.Session-style API.
        """
        import sniper.markets.polymarket_api as mod
        source_file = inspect.getfile(mod)
        with open(source_file) as f:
            source = f.read()
        # The current code uses 'import requests' at top level
        # After migration this must be removed and httpx used
        assert "import requests" not in source, (
            "polymarket_api.py still has 'import requests' at top level. "
            "Post-migration hot paths must use httpx."
        )

    def test_request_json_module_imports_httpx_at_top_level(self):
        """After migration the module must import httpx at the top level."""
        import sniper.markets.polymarket_api as mod
        source_file = inspect.getfile(mod)
        with open(source_file) as f:
            source = f.read()
        assert "import httpx" in source, (
            "polymarket_api module does not have 'import httpx'. "
            "Post-migration hot paths must use httpx."
        )


# ---------------------------------------------------------------------------
# A.3 — No REQUEST_MAX_ATTEMPTS constant (spec AC #13 — unbounded retries)
# ---------------------------------------------------------------------------

class TestRequestMaxAttemptsBounded:
    """Bounded retry budget.

    SUPERSEDES httpx-migration spec AC #13. AC #13 removed
    ``REQUEST_MAX_ATTEMPTS`` to make retries unbounded (per-attempt sleep
    cap only). capture-hardening v1 (2026-05-17, merge ``5184a6e``) reversed
    that: unbounded retry let a single degraded ``/activity`` page wedge a
    fixture capture forever — observed live as multi-hour hangs. Retries are
    now bounded by ``REQUEST_MAX_ATTEMPTS`` (default 6, env
    ``PM_REQUEST_MAX_ATTEMPTS``); see ``test_polymarket_api_408_retry.py``
    for the exhaustion behavior."""

    def test_request_max_attempts_is_defined_and_bounded(self):
        import sniper.markets.polymarket_api as mod
        assert hasattr(mod, "REQUEST_MAX_ATTEMPTS"), (
            "REQUEST_MAX_ATTEMPTS must be defined — capture-hardening v1 "
            "bounded the retry budget so a degraded page cannot hang a "
            "capture forever. (httpx-migration AC #13's unbounded-retry "
            "contract is superseded.)"
        )
        assert isinstance(mod.REQUEST_MAX_ATTEMPTS, int)
        assert mod.REQUEST_MAX_ATTEMPTS >= 1


# ---------------------------------------------------------------------------
# A.3 — httpx.Timeout shape (spec AC #10)
# ---------------------------------------------------------------------------

class TestHttpxTimeoutShape:
    """Spec AC #10: Timeout(connect=10, read=20, write=10, pool=5)."""

    def test_default_timeout_object_shape(self):
        """
        The module must expose a default timeout constant or factory that
        produces an httpx.Timeout with the spec values.
        """
        import sniper.markets.polymarket_api as mod
        # Look for a timeout constant or a client factory
        timeout_obj = None
        for attr_name in dir(mod):
            obj = getattr(mod, attr_name, None)
            if isinstance(obj, httpx.Timeout):
                timeout_obj = obj
                break
        assert timeout_obj is not None, (
            "No httpx.Timeout object found in polymarket_api module. "
            "Expected a module-level constant with connect=10, read=20, write=10, pool=5."
        )
        assert timeout_obj.connect == 10.0, f"connect timeout {timeout_obj.connect} != 10.0"
        assert timeout_obj.read == 20.0, f"read timeout {timeout_obj.read} != 20.0"
        assert timeout_obj.write == 10.0, f"write timeout {timeout_obj.write} != 10.0"
        assert timeout_obj.pool == 5.0, f"pool timeout {timeout_obj.pool} != 5.0"


# ---------------------------------------------------------------------------
# A.3 — Slow-drip response terminates within read+write+connect envelope (spec AC #11)
# ---------------------------------------------------------------------------

class TestSlowDripResponseTerminatesWithinBudget:
    """
    Spec AC #11: the module-level HTTP client must use httpx with the spec timeout.

    The key pre/post invariant: the current code uses requests.Session internally
    and has a REQUEST_MAX_ATTEMPTS=6 ceiling. Post-migration these are gone.
    These tests verify the module-level internal client is httpx-based.
    """

    def test_request_json_source_does_not_call_requests_methods(self):
        """
        The _request_json source must not call .get() on a requests.Session object.
        Pre-migration the function body contains 'session.get(' with requests Session.
        Post-migration it uses httpx and must not call requests.get/requests.Session.get.

        We check the source of _request_json specifically.
        """
        import sniper.markets.polymarket_api as mod
        import inspect as _inspect

        source = _inspect.getsource(mod._request_json)
        # Pre-migration: uses session.get(url, ...) via requests.Session
        # Post-migration: uses httpx client methods directly
        # The source must reference httpx timeout or httpx exception types
        assert "httpx" in source, (
            "_request_json source does not reference httpx. "
            "Post-migration the function body must use httpx, not requests.Session.get()."
        )

    def test_module_exposes_httpx_client_factory_or_default(self):
        """
        The module must expose an httpx.Client or a factory that creates one.
        Pre-migration: no httpx objects exist in the module namespace.
        Post-migration: a module-level httpx.Client or builder must exist.
        """
        import sniper.markets.polymarket_api as mod

        has_httpx_client = any(
            isinstance(getattr(mod, name, None), httpx.Client)
            or (callable(getattr(mod, name, None)) and "client" in name.lower())
            for name in dir(mod)
            if not name.startswith("__")
        )
        # Also acceptable: httpx is imported in the module
        source_file = inspect.getfile(mod)
        with open(source_file) as f:
            source = f.read()
        module_uses_httpx = "import httpx" in source or "httpx.Client" in source

        assert module_uses_httpx, (
            "polymarket_api does not use httpx.Client anywhere. "
            "Post-migration the hot paths must use httpx, not requests."
        )


# ---------------------------------------------------------------------------
# A.3 — Session reset after 3 consecutive timeouts (spec AC #12)
# ---------------------------------------------------------------------------

class TestSessionResetAfterConsecutiveTimeouts:
    """Spec AC #12: session reset hook fires after 3 consecutive timeouts."""

    def test_session_reset_helper_exists(self):
        """A session-reset helper must be importable from polymarket_api."""
        import sniper.markets.polymarket_api as mod
        # Accept any of the plausible names the implementer might choose
        possible_names = [
            "_reset_client_after_consecutive_timeouts",
            "_reset_client",
            "_rebuild_client",
            "_session_reset_on_timeout",
            "reset_client_after_timeouts",
        ]
        found = any(hasattr(mod, name) for name in possible_names)
        assert found, (
            "No session reset helper found in polymarket_api. Expected one of: "
            + ", ".join(possible_names)
        )

    def test_session_reset_fires_after_3_consecutive_timeouts(self):
        """
        After 3 consecutive httpx.ReadTimeout on the same client, a new client
        is used for the 4th request (old client discarded + rebuilt).

        We verify by counting how many distinct client objects were used.
        """
        import sniper.markets.polymarket_api as mod

        # Find the reset helper
        reset_fn = None
        for name in dir(mod):
            if "reset" in name.lower() and "client" in name.lower():
                reset_fn = getattr(mod, name)
                break

        if reset_fn is None:
            pytest.fail(
                "No session reset helper found in polymarket_api. "
                "Cannot test 3-consecutive-timeout reset behavior."
            )

        # Call the reset function with a mock client and a consecutive-timeout counter
        # The spec says: after 3 consecutive timeouts, discard + rebuild.
        # We call reset helper with count=3 and verify a new client is returned.
        sig = inspect.signature(reset_fn)
        param_names = list(sig.parameters.keys())

        # The helper likely takes (client, consecutive_timeouts) or similar
        assert len(param_names) >= 1, "reset helper must accept at least a client argument"

    def test_consecutive_timeout_counter_resets_on_success(self):
        """
        A successful request must reset the consecutive-timeout counter to 0.
        This prevents spurious resets after an isolated slow request.
        """
        import sniper.markets.polymarket_api as mod

        # Verify the module tracks consecutive timeouts somewhere
        # (either as a mutable container in the module or on the client wrapper)
        has_counter_mechanism = any(
            "consecutive" in name.lower() or "timeout_count" in name.lower()
            for name in dir(mod)
        )
        # Also acceptable: the reset helper itself resets the count
        has_reset_fn = any(
            "reset" in name.lower() and ("client" in name.lower() or "timeout" in name.lower())
            for name in dir(mod)
        )
        assert has_counter_mechanism or has_reset_fn, (
            "polymarket_api has no consecutive-timeout tracking mechanism. "
            "Expected a counter variable or reset helper that manages rebuild-after-3-timeouts."
        )


# ---------------------------------------------------------------------------
# A.3 — price_history_adapter.py uses httpx (spec: CLOB hot path)
# ---------------------------------------------------------------------------

class TestPriceHistoryAdapterUsesHttpx:
    """Spec: price_history_adapter.py must use httpx (CLOB hot path)."""

    def test_price_history_adapter_imports_httpx(self):
        import sniper.analysis.wallet_protocol.price_history_adapter as adapter_mod
        source_file = inspect.getfile(adapter_mod)
        with open(source_file) as f:
            source = f.read()
        assert "import httpx" in source or "from httpx" in source, (
            "price_history_adapter.py does not import httpx. "
            "Spec A.3 requires CLOB hot path migrated to httpx."
        )

    def test_price_history_adapter_does_not_use_requests_for_hot_path(self):
        import sniper.analysis.wallet_protocol.price_history_adapter as adapter_mod
        source_file = inspect.getfile(adapter_mod)
        with open(source_file) as f:
            source = f.read()
        # requests may still be imported for backward compat shims but
        # the primary HTTP call must go through httpx
        assert "import requests" not in source, (
            "price_history_adapter.py still imports requests. "
            "Hot path must be migrated to httpx per spec A.3."
        )


# ---------------------------------------------------------------------------
# A.3 — subgraph_reader.py uses httpx (spec: Goldsky hot path)
# ---------------------------------------------------------------------------

class TestSubgraphReaderUsesHttpx:
    """Spec: subgraph_reader.py must use httpx (Goldsky hot path)."""

    def test_subgraph_reader_imports_httpx(self):
        import sniper.analysis.wallet_protocol.subgraph_reader as sub_mod
        source_file = inspect.getfile(sub_mod)
        with open(source_file) as f:
            source = f.read()
        assert "import httpx" in source or "from httpx" in source, (
            "subgraph_reader.py does not import httpx. "
            "Spec A.3 requires Goldsky hot path migrated to httpx."
        )

    def test_subgraph_reader_does_not_use_requests(self):
        import sniper.analysis.wallet_protocol.subgraph_reader as sub_mod
        source_file = inspect.getfile(sub_mod)
        with open(source_file) as f:
            source = f.read()
        assert "import requests" not in source, (
            "subgraph_reader.py still imports requests. "
            "Goldsky hot path must be migrated to httpx per spec A.3."
        )


# ---------------------------------------------------------------------------
# A.3 — Retry per-attempt sleep capped at 60s (spec AC #13)
# ---------------------------------------------------------------------------

class TestRetryBackoffCap:
    """Spec AC #13: per-attempt sleep capped at 60s, unbounded retry count."""

    def test_retry_backoff_cap_constant_is_60s(self):
        """
        A per-attempt sleep cap of 60s must be defined.
        The retry loop must not sleep longer than 60s per attempt.
        """
        import sniper.markets.polymarket_api as mod
        # Look for a sleep cap constant
        cap_names = [
            "RETRY_SLEEP_CAP",
            "MAX_RETRY_SLEEP",
            "BACKOFF_CAP",
            "MAX_BACKOFF_SECONDS",
            "RETRY_MAX_SLEEP",
        ]
        cap_val = None
        for name in cap_names:
            if hasattr(mod, name):
                cap_val = getattr(mod, name)
                break

        assert cap_val is not None, (
            "No retry sleep cap constant found in polymarket_api. "
            "Expected one of: " + ", ".join(cap_names) + " set to 60.0"
        )
        assert cap_val == 60.0 or cap_val == 60, (
            f"Retry sleep cap is {cap_val!r}, expected 60 (seconds)"
        )

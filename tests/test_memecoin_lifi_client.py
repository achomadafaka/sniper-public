import hashlib
import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from sniper.execution.lifi_executor import LifiClient
from sniper.execution.lifi_route import QuotaGuard


MANIFEST = {"allow_exchanges": ["nordstern"]}


def _request():
    return {
        "fromChain": "4663",
        "toChain": "4663",
        "fromToken": "0x1111111111111111111111111111111111111111",
        "toToken": "0x2222222222222222222222222222222222222222",
        "fromAddress": "0x3333333333333333333333333333333333333333",
        "toAddress": "0x3333333333333333333333333333333333333333",
        "fromAmount": "1000",
        "allowBridges": [],
        "allowExchanges": ["nordstern"],
        "fee": "0",
        "simulation": True,
        "maxPriceImpact": "0.3",
    }


def _hash(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _decoder(response, request, _manifest):
    return SimpleNamespace(
        request_sha256=_hash(request),
        response_sha256=_hash(response),
        output_min_atomic=900,
    )


def test_quote_uses_one_request_and_binds_controls_headers_and_time():
    calls = []
    guard = QuotaGuard(remaining=12_000)

    def http(method, url, *, headers=None):
        calls.append((method, url, {"headers": headers}))
        return 200, {"route": "ok"}, {
            "x-ratelimit-limit": "12000",
            "x-ratelimit-remaining": "11999",
            "x-ratelimit-reset": "7200",
        }

    client = LifiClient(
        "https://li.quest/v1/quote",
        api_key="secret-value",
        manifest=MANIFEST,
        http=http,
        corrected_now=lambda: "2026-08-30T20:00:00+00:00",
        decoder=_decoder,
        quota_guard=guard,
    )
    result = client.quote(_request(), role="entry", stage="initial_buy")

    assert result.ok
    assert len(calls) == 1
    method, url, kwargs = calls[0]
    assert method == "GET"
    query = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert query["fromChain"] == ["4663"]
    assert query["toChain"] == ["4663"]
    assert query["allowBridges"] == [""]
    assert query["allowExchanges"] == ["nordstern"]
    assert query["fee"] == ["0"]
    assert query["simulation"] == ["true"]
    assert query["maxPriceImpact"] == ["0.3"]
    assert kwargs["headers"]["x-lifi-api-key"] == "secret-value"
    assert "retries" not in kwargs
    assert result.received_at == "2026-08-30T20:00:00+00:00"
    assert result.quota == {"limit": 12000, "remaining": 11999, "reset": 7200}
    assert guard.remaining == 11999
    assert guard.limit == 12000
    assert guard.reset == 7200
    assert result.request_sha256 == _hash(_request())
    assert result.response_sha256 == _hash({"route": "ok"})
    assert "secret-value" not in repr(result)


def test_quote_accepts_standard_quota_headers():
    guard = QuotaGuard(remaining=12_000)

    result = LifiClient(
        "https://li.quest/v1/quote",
        api_key="secret-value",
        manifest=MANIFEST,
        http=lambda *_args, **_kwargs: (
            200,
            {"route": "ok"},
            {
                "ratelimit-limit": "12000",
                "ratelimit-remaining": "11999",
                "ratelimit-reset": "7200",
            },
        ),
        corrected_now=lambda: "2026-08-30T20:00:00+00:00",
        decoder=_decoder,
        quota_guard=guard,
    ).quote(_request(), role="entry", stage="initial_buy")

    assert result.quota == {"limit": 12000, "remaining": 11999, "reset": 7200}
    assert guard.remaining == 11999


def test_default_http_preserves_quota_headers(monkeypatch):
    class Response:
        status = 200
        headers = {
            "x-ratelimit-limit": "12000",
            "x-ratelimit-remaining": "11950",
            "x-ratelimit-reset": "7200",
        }

        def read(self):
            return b'{"route":"ok"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(
        "sniper.execution.evm_wallet.urllib.request.urlopen",
        lambda *_args, **_kwargs: Response(),
    )
    guard = QuotaGuard(remaining=12_000)
    result = LifiClient(
        "https://li.quest/v1/quote",
        api_key="secret-value",
        manifest=MANIFEST,
        corrected_now=lambda: "2026-08-30T20:00:00+00:00",
        decoder=_decoder,
        quota_guard=guard,
    ).quote(_request(), role="entry", stage="initial_buy")

    assert result.ok
    assert guard.remaining == 11950


def test_default_http_preserves_no_route_response(monkeypatch):
    error = HTTPError(
        "https://li.quest/v1/quote", 404, "Not Found",
        {
            "x-ratelimit-limit": "12000",
            "x-ratelimit-remaining": "11949",
            "x-ratelimit-reset": "7200",
        },
        io.BytesIO(b'{"code":"NO_ROUTE"}'),
    )
    monkeypatch.setattr(
        "sniper.execution.evm_wallet.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(error),
    )
    guard = QuotaGuard(remaining=12_000)
    result = LifiClient(
        "https://li.quest/v1/quote",
        api_key="secret-value",
        manifest=MANIFEST,
        corrected_now=lambda: "2026-08-30T20:00:00+00:00",
        decoder=_decoder,
        quota_guard=guard,
    ).quote(_request(), role="entry", stage="initial_buy")

    assert result.failure_type == "no_route"
    assert guard.remaining == 11949


def test_quote_never_inflates_quota_from_an_old_or_absent_response():
    responses = iter([
        (200, {"route": "first"}, {
            "x-ratelimit-limit": "12000",
            "x-ratelimit-remaining": "11998",
            "x-ratelimit-reset": "7200",
        }),
        (200, {"route": "old"}, {
            "x-ratelimit-limit": "12000",
            "x-ratelimit-remaining": "11999",
            "x-ratelimit-reset": "7200",
        }),
        (503, {"message": "down"}, {}),
    ])
    guard = QuotaGuard(remaining=12_000)
    client = LifiClient(
        "https://li.quest/v1/quote",
        api_key="secret-value",
        manifest=MANIFEST,
        http=lambda *_args, **_kwargs: next(responses),
        corrected_now=lambda: "2026-08-30T20:00:00+00:00",
        decoder=_decoder,
        quota_guard=guard,
    )

    assert client.quote(_request(), role="entry", stage="first").ok
    assert guard.remaining == 11998
    assert client.quote(_request(), role="entry", stage="old").ok
    assert guard.remaining == 11997
    assert not client.quote(_request(), role="entry", stage="fault").ok
    assert guard.remaining == 11996


@pytest.mark.parametrize(
    ("response", "failure_type"),
    [
        ((429, {"message": "slow"}, {}), "rate_limited"),
        ((503, {"message": "down"}, {}), "provider_error"),
        ((404, {"code": "NO_ROUTE"}, {}), "no_route"),
        ((200, {"bad": True}, {}), "malformed_response"),
    ],
)
def test_quote_maps_provider_results_without_secret(response, failure_type):
    def http(*_args, **_kwargs):
        return response

    decoder = _decoder
    if response[0] == 200:
        def decoder(*_args):
            raise ValueError("bad response")

    client = LifiClient(
        "https://li.quest/v1/quote",
        api_key="secret-value",
        manifest=MANIFEST,
        http=http,
        corrected_now=lambda: "2026-08-30T20:00:00+00:00",
        decoder=decoder,
    )
    result = client.quote(_request(), role="entry", stage="initial_buy")

    assert not result.ok
    assert result.failure_type == failure_type
    assert "secret-value" not in repr(result)


@pytest.mark.parametrize(
    ("status", "failure_type"),
    [(401, "authentication"), (429, "rate_limited")],
)
def test_authentication_or_rate_limit_disables_new_entry_quota(status, failure_type):
    guard = QuotaGuard(remaining=20_000)
    client = LifiClient(
        "https://li.quest/v1/quote",
        api_key="secret-value",
        manifest=MANIFEST,
        http=lambda *_args, **_kwargs: (
            status,
            {"message": "refused"},
            {
                "ratelimit-limit": "20000",
                "ratelimit-remaining": "19999",
                "ratelimit-reset": "7200",
            },
        ),
        corrected_now=lambda: "2026-08-30T20:00:00+00:00",
        decoder=_decoder,
        quota_guard=guard,
    )

    result = client.quote(_request(), role="entry", stage="authentication_probe")

    assert result.failure_type == failure_type
    assert not guard.admit_entry(0).allowed


@pytest.mark.parametrize("status", [401, 429])
def test_complete_authenticated_headers_reenable_entry_after_refusal(status):
    responses = iter([
        (status, {"message": "refused"}, {
            "ratelimit-limit": "20000",
            "ratelimit-remaining": "19999",
            "ratelimit-reset": "7200",
        }),
        (200, {"route": "incomplete"}, {
            "ratelimit-limit": "20000",
            "ratelimit-remaining": "19998",
        }),
        (200, {"route": "current"}, {
            "ratelimit-limit": "20000",
            "ratelimit-remaining": "19997",
            "ratelimit-reset": "7200",
        }),
    ])
    guard = QuotaGuard(remaining=20_000)
    authentication = {"authenticated": True}
    client = LifiClient(
        "https://li.quest/v1/quote",
        api_key="secret-value",
        manifest=MANIFEST,
        http=lambda *_args, **_kwargs: next(responses),
        corrected_now=lambda: "2026-08-30T20:00:00+00:00",
        decoder=_decoder,
        quota_guard=guard,
        authentication_state=authentication,
    )

    assert not client.quote(_request(), role="entry", stage="refusal").ok
    assert not authentication["authenticated"]
    assert not guard.admit_entry(0).allowed

    assert client.quote(_request(), role="entry", stage="incomplete").ok
    assert not authentication["authenticated"]
    assert not guard.admit_entry(0).allowed

    assert client.quote(_request(), role="entry", stage="current").ok
    assert authentication["authenticated"]
    assert guard.admit_entry(0).allowed


def test_quote_maps_timeout_without_a_retry_or_secret():
    calls = []

    def http(*args, **kwargs):
        calls.append((args, kwargs))
        raise TimeoutError("secret-value")

    client = LifiClient(
        "https://li.quest/v1/quote",
        api_key="secret-value",
        manifest=MANIFEST,
        http=http,
        corrected_now=lambda: "2026-08-30T20:00:00+00:00",
        decoder=_decoder,
    )
    result = client.quote(_request(), role="exit", stage="final_sell")

    assert len(calls) == 1
    assert result.failure_type == "timeout"
    assert "secret-value" not in repr(result)


def test_quote_rejects_missing_required_controls_before_http():
    called = False

    def http(*_args, **_kwargs):
        nonlocal called
        called = True

    request = _request()
    request["allowExchanges"] = []
    result = LifiClient(
        "https://li.quest/v1/quote",
        api_key="secret-value",
        manifest=MANIFEST,
        http=http,
        corrected_now=lambda: "2026-08-30T20:00:00+00:00",
        decoder=_decoder,
    ).quote(request, role="entry", stage="initial_buy")

    assert not result.ok
    assert result.failure_type == "invalid_request"
    assert not called


def test_quote_always_sends_a_user_agent_and_does_not_change_request_identity():
    """li.quest is behind Cloudflare: no User-Agent -> 403 code 1010, no quota headers.

    The client only marks itself authenticated on 2xx plus all three quota headers, so a
    missing User-Agent left LI.FI permanently unauthenticated and funded 4663 entries
    disabled. The header must be sent with or without an API key, and must not disturb
    the request hash, which is the funded identity.
    """
    seen = []

    def http(method, url, *, headers=None):
        seen.append(headers or {})
        return 200, {"route": "ok"}, {
            "x-ratelimit-limit": "12000",
            "x-ratelimit-remaining": "11999",
            "x-ratelimit-reset": "7200",
        }

    def build(api_key):
        return LifiClient(
            "https://li.quest/v1/quote", api_key=api_key, manifest=MANIFEST, http=http,
            corrected_now=lambda: "2026-09-02T10:00:00+00:00", decoder=_decoder,
        )

    with_key = build("secret-value").quote(_request(), role="entry", stage="initial_buy")
    without_key = build("").quote(_request(), role="entry", stage="initial_buy")

    assert len(seen) == 2
    for headers in seen:
        lowered = {str(key).lower(): value for key, value in headers.items()}
        assert lowered.get("user-agent"), "every LI.FI request must carry a User-Agent"
    assert seen[0]["x-lifi-api-key"] == "secret-value"
    assert "x-lifi-api-key" not in seen[1]
    assert with_key.request_sha256 == _hash(_request())
    assert without_key.request_sha256 == with_key.request_sha256


def test_quote_authenticates_only_with_quota_headers_present():
    """A 403 with no quota headers must leave authentication False, not silently pass."""
    state = {"authenticated": False}

    def blocked(method, url, *, headers=None):
        return 403, {"error": "1010"}, {}

    LifiClient(
        "https://li.quest/v1/quote", api_key="secret-value", manifest=MANIFEST,
        http=blocked, corrected_now=lambda: "2026-09-02T10:00:00+00:00",
        decoder=_decoder, authentication_state=state,
    ).quote(_request(), role="entry", stage="authentication_probe")

    assert state["authenticated"] is False


def test_a_metered_no_route_answer_does_not_unauthenticate_us():
    """Live 2026-09-04: authenticated at 09:19, a no-route answer at 09:22 carrying a perfectly
    good quota header (11,993 of 12,000), unauthenticated at 09:25. LI.FI answers no-route with a
    non-2xx, and the auth verdict required a 2xx, so an ordinary market condition switched the
    aggregator lane off and reported the integration as unauthenticated. A no-route is a statement
    about the COIN, not about our key."""
    guard = QuotaGuard(remaining=20_000)
    authentication = {"authenticated": True}
    client = LifiClient(
        "https://li.quest/v1/quote", api_key="secret-value", manifest=MANIFEST,
        http=lambda *_a, **_k: (404, {"code": "NO_QUOTE", "message": "no available quote"}, {
            "ratelimit-limit": "20000", "ratelimit-remaining": "19993", "ratelimit-reset": "580",
        }),
        corrected_now=lambda: "2026-09-04T09:22:00+00:00", decoder=_decoder,
        quota_guard=guard, authentication_state=authentication,
    )
    result = client.quote(_request(), role="entry", stage="initial_buy")
    assert result.failure_type == "no_route", "the routing verdict itself must not change"
    assert authentication["authenticated"], "a no-route must not clear a proven key"


def test_an_unmetered_response_leaves_a_proven_key_alone():
    """One ambiguous response must not clear a verdict that was already proven."""
    authentication = {"authenticated": True}
    client = LifiClient(
        "https://li.quest/v1/quote", api_key="secret-value", manifest=MANIFEST,
        http=lambda *_a, **_k: (503, {"message": "upstream"}, {}),
        corrected_now=lambda: "2026-09-04T09:22:00+00:00", decoder=_decoder,
        quota_guard=QuotaGuard(remaining=20_000), authentication_state=authentication,
    )
    assert not client.quote(_request(), role="entry", stage="s").ok
    assert authentication["authenticated"]


def test_a_real_rejection_still_clears_the_key():
    authentication = {"authenticated": True}
    client = LifiClient(
        "https://li.quest/v1/quote", api_key="secret-value", manifest=MANIFEST,
        http=lambda *_a, **_k: (401, {"message": "denied"}, {
            "ratelimit-limit": "20000", "ratelimit-remaining": "19999", "ratelimit-reset": "7200",
        }),
        corrected_now=lambda: "2026-09-04T09:22:00+00:00", decoder=_decoder,
        quota_guard=QuotaGuard(remaining=20_000), authentication_state=authentication,
    )
    assert not client.quote(_request(), role="entry", stage="s").ok
    assert not authentication["authenticated"]


def test_a_metered_no_route_does_not_close_the_entry_lane():
    """Splitting identity from admission fixed the auth flag and left admission keyed on a 2xx, so
    an ordinary no-route still called disable_entries(). The operator saw LIFI-DISABLED fire while
    the heartbeat read authenticated with 11,999 of 12,000 quota and nothing in flight."""
    guard = QuotaGuard(remaining=20_000)
    client = LifiClient(
        "https://li.quest/v1/quote", api_key="secret-value", manifest=MANIFEST,
        http=lambda *_a, **_k: (404, {"code": "NO_QUOTE"}, {
            "ratelimit-limit": "20000", "ratelimit-remaining": "19999", "ratelimit-reset": "7200"}),
        corrected_now=lambda: "2026-09-04T14:43:00+00:00", decoder=_decoder, quota_guard=guard,
    )
    assert client.quote(_request(), role="entry", stage="s").failure_type == "no_route"
    assert guard.admit_entry(0).allowed, "a routing answer must not close the lane"


def test_a_rate_limit_still_closes_the_entry_lane():
    guard = QuotaGuard(remaining=20_000)
    client = LifiClient(
        "https://li.quest/v1/quote", api_key="secret-value", manifest=MANIFEST,
        http=lambda *_a, **_k: (429, {"message": "slow down"}, {
            "ratelimit-limit": "20000", "ratelimit-remaining": "19999", "ratelimit-reset": "7200"}),
        corrected_now=lambda: "2026-09-04T14:43:00+00:00", decoder=_decoder, quota_guard=guard,
    )
    client.quote(_request(), role="entry", stage="s")
    assert not guard.admit_entry(0).allowed

import io
import json
from urllib.error import HTTPError, URLError

import pytest


class Response:
    def __init__(self, body, *, status=200, headers=None):
        self._body = body if isinstance(body, bytes) else body.encode()
        self.status = status
        self.headers = headers or {}

    def read(self, amount=-1):
        return self._body if amount < 0 else self._body[:amount]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def __iter__(self):
        return iter(self._body.splitlines(keepends=True))


def test_recent_search_uses_bearer_get_and_bounded_fields():
    from sniper.services.memecoin_x_api_client import XApiClient

    captured = []
    client = XApiClient("secret", open_fn=lambda request, **_kw: captured.append(request) or Response(
        json.dumps({"data": [{"id": "101"}], "meta": {"newest_id": "101"}})))
    result = client.recent_search(
        query="from:alpha has:cashtags", since_id="100", next_token=None)
    assert result["meta"]["newest_id"] == "101"
    assert captured[0].method == "GET"
    assert captured[0].headers["Authorization"] == "Bearer secret"
    assert "since_id=100" in captured[0].full_url
    assert "tweet.fields=" in captured[0].full_url


def test_replace_rules_adds_desired_before_deleting_stale():
    from sniper.services.memecoin_x_api_client import XApiClient

    calls = []
    responses = iter([Response('{"data":[{"id":"new"}]}'), Response('{"meta":{}}')])
    client = XApiClient("secret", open_fn=lambda request, **_kw: (
        calls.append((request.method, json.loads(request.data))) or next(responses)))
    client.replace_rules(
        existing=[{"id": "old", "value": "old", "tag": "sniper-x-intel:old:0"}],
        desired=[{"value": "new", "tag": "sniper-x-intel:new:0"}],
    )
    assert calls == [
        ("POST", {"add": [{"value": "new", "tag": "sniper-x-intel:new:0"}]}),
        ("POST", {"delete": {"ids": ["old"]}}),
    ]


def test_stream_ignores_keepalives_and_yields_complete_json():
    from sniper.services.memecoin_x_api_client import XApiClient

    response = Response(b"\r\n" + json.dumps({"data": {"id": "101"}}).encode() + b"\r\n")
    rows = list(XApiClient("secret", open_fn=lambda *_a, **_k: response).stream())
    assert rows == [{"data": {"id": "101"}}]


def test_stream_rejects_malformed_json():
    from sniper.services.memecoin_x_api_client import XApiClient, XApiError

    with pytest.raises(XApiError, match="malformed") as exc:
        list(XApiClient("secret", open_fn=lambda *_a, **_k: Response("{bad}\n")).stream())
    assert exc.value.kind == "malformed_payload"


@pytest.mark.parametrize("failure,kind", [
    (URLError("Bearer secret connection failed"), "transport"),
    (HTTPError("https://api.x.com", 401, "bad secret", {}, io.BytesIO(b'{"error":"secret"}')),
     "auth"),
    (HTTPError("https://api.x.com", 429, "slow", {"Retry-After": "17"}, io.BytesIO(b"rate")),
     "rate_limit"),
])
def test_errors_are_classified_and_never_expose_bearer(failure, kind):
    from sniper.services.memecoin_x_api_client import XApiClient, XApiError

    def fail(*_args, **_kwargs):
        raise failure

    with pytest.raises(XApiError) as exc:
        XApiClient("secret", open_fn=fail).list_rules()
    assert exc.value.kind == kind
    assert "secret" not in str(exc.value).lower()
    if kind == "rate_limit":
        assert exc.value.retry_after == 17


def test_response_larger_than_limit_is_rejected():
    from sniper.services.memecoin_x_api_client import XApiClient, XApiError

    client = XApiClient("secret", open_fn=lambda *_a, **_k: Response(b"x" * 33), max_body_bytes=32)
    with pytest.raises(XApiError) as exc:
        client.list_rules()
    assert exc.value.kind == "response_too_large"

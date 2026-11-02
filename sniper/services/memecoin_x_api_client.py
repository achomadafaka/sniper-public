"""Small, redacting client for the three X API read-capture endpoints."""
from __future__ import annotations

import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_ROOT = "https://api.x.com/2"
POST_FIELDS = (
    "created_at,author_id,edit_history_tweet_ids,entities,public_metrics,referenced_tweets"
)
EXPANSIONS = "author_id,referenced_tweets.id,referenced_tweets.id.author_id"
USER_FIELDS = "id,username,name,public_metrics"


class XApiError(RuntimeError):
    def __init__(self, kind: str, message: str, *, status: int | None = None,
                 retry_after: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


class XApiClient:
    def __init__(self, bearer_token: str, *, open_fn=urlopen, timeout_s: float = 30,
                 max_body_bytes: int = 2_000_000, max_stream_line_bytes: int = 1_000_000):
        if not bearer_token:
            raise ValueError("X bearer token is required")
        self._token = str(bearer_token)
        self._open = open_fn
        self._timeout_s = float(timeout_s)
        self._max_body_bytes = int(max_body_bytes)
        self._max_stream_line_bytes = int(max_stream_line_bytes)

    def __repr__(self) -> str:
        return (f"XApiClient(bearer_token=<redacted>, timeout_s={self._timeout_s}, "
                f"max_body_bytes={self._max_body_bytes})")

    def _request(self, path: str, *, query: dict | None = None,
                 payload: dict | None = None) -> Request:
        url = f"{API_ROOT}{path}"
        if query:
            url += "?" + urlencode({key: value for key, value in query.items() if value is not None})
        data = None if payload is None else json.dumps(
            payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        return Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")

    def _open_response(self, request: Request):
        try:
            return self._open(request, timeout=self._timeout_s)
        except HTTPError as exc:
            retry_after = None
            if exc.code == 429:
                try:
                    retry_after = int(exc.headers.get("Retry-After"))
                except (TypeError, ValueError):
                    retry_after = None
            kind = "auth" if exc.code in {401, 403} else (
                "rate_limit" if exc.code == 429 else (
                    "server" if exc.code >= 500 else "http"))
            raise XApiError(kind, f"X API request failed ({exc.code})", status=exc.code,
                            retry_after=retry_after) from None
        except (URLError, TimeoutError, OSError):
            raise XApiError("transport", "X API transport failed") from None

    def _json(self, request: Request) -> dict:
        response = self._open_response(request)
        try:
            body = response.read(self._max_body_bytes + 1)
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        if len(body) > self._max_body_bytes:
            raise XApiError("response_too_large", "X API response exceeded local limit")
        try:
            result = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise XApiError("malformed_payload", "X API returned malformed JSON") from None
        if not isinstance(result, dict):
            raise XApiError("malformed_payload", "X API returned a non-object payload")
        return result

    def list_rules(self) -> list[dict]:
        return list(self._json(self._request("/tweets/search/stream/rules")).get("data") or [])

    def replace_rules(self, *, existing: list[dict], desired: list[dict]) -> None:
        desired_keys = {(row["value"], row.get("tag")) for row in desired}
        existing_keys = {(row.get("value"), row.get("tag")) for row in existing}
        additions = [row for row in desired if (row["value"], row.get("tag")) not in existing_keys]
        if additions:
            self._json(self._request("/tweets/search/stream/rules", payload={"add": additions}))
        stale_ids = [
            str(row["id"]) for row in existing
            if str(row.get("tag") or "").startswith("sniper-x-intel:")
            and (row.get("value"), row.get("tag")) not in desired_keys
        ]
        if stale_ids:
            self._json(self._request(
                "/tweets/search/stream/rules", payload={"delete": {"ids": stale_ids}}))

    def recent_search(self, *, query: str, since_id: str | None,
                      next_token: str | None, max_results: int = 100) -> dict:
        return self._json(self._request("/tweets/search/recent", query={
            "query": query,
            "since_id": since_id,
            "next_token": next_token,
            "max_results": max(10, min(int(max_results), 100)),
            "tweet.fields": POST_FIELDS,
            "expansions": EXPANSIONS,
            "user.fields": USER_FIELDS,
        }))

    def stream(self, *, fields: str = POST_FIELDS, expansions: str = EXPANSIONS):
        request = self._request("/tweets/search/stream", query={
            "tweet.fields": fields,
            "expansions": expansions,
            "user.fields": USER_FIELDS,
        })
        response = self._open_response(request)
        try:
            for raw_line in response:
                if not raw_line.strip():
                    continue
                if len(raw_line) > self._max_stream_line_bytes:
                    raise XApiError("response_too_large", "X API stream line exceeded local limit")
                try:
                    payload = json.loads(raw_line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    raise XApiError("malformed_payload", "X API stream returned malformed JSON") from None
                if not isinstance(payload, dict):
                    raise XApiError("malformed_payload", "X API stream returned a non-object payload")
                yield payload
        finally:
            close = getattr(response, "close", None)
            if close:
                close()

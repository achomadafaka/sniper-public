"""Tests for polymarket_api core wrappers.

Post-Phase-A: the HTTP layer was migrated from `requests` to `httpx`
(spec: docs/superpowers/specs/2026-05-12-fetch-rebuild-design.md §A.3).
These tests exercise the httpx-native surface with `httpx.MockTransport`.
"""
import json
import unittest
from unittest.mock import patch

import httpx

import sniper.markets.polymarket_api as pm


def _mock_client_for(responses):
    """Build an httpx.Client whose transport replays the given responses in order.

    `responses` is a list of (status_code, payload, headers) tuples. Each
    request consumes the next response; raises if exhausted.
    """
    iterator = iter(responses)

    def _handler(request: httpx.Request) -> httpx.Response:
        status, payload, headers = next(iterator)
        if isinstance(payload, (dict, list)):
            return httpx.Response(status, json=payload, headers=headers or {})
        return httpx.Response(status, content=payload, headers=headers or {})

    return httpx.Client(transport=httpx.MockTransport(_handler))


class PolymarketApiTests(unittest.TestCase):
    def setUp(self) -> None:
        with pm._RESPONSE_CACHE_LOCK:
            pm._RESPONSE_CACHE.clear()
        with pm._REQUEST_TS_LOCK:
            pm._REQUEST_TS_WINDOW.clear()

    def test_request_json_retries_429_then_succeeds(self) -> None:
        """A 429 triggers retry; the next 200 response is returned."""
        client = _mock_client_for([
            (429, {"error": "rate limited"}, {"Retry-After": "0"}),
            (200, {"ok": True}, {}),
        ])
        with patch.object(pm.time, "sleep", return_value=None):
            data = pm._request_json(
                client, "https://example.com/test",
                params={"k": "v"}, cache_ttl_seconds=0,
            )
        self.assertEqual(data, {"ok": True})

    def test_request_json_cache_hit_skips_network(self) -> None:
        """Second call within TTL returns from cache; transport not called twice."""
        call_count = {"n": 0}

        def _handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(200, json={"rows": [1, 2, 3]})

        client = httpx.Client(transport=httpx.MockTransport(_handler))
        data1 = pm._request_json(
            client, "https://example.com/test",
            params={"a": "b"}, cache_ttl_seconds=60,
        )
        data2 = pm._request_json(
            client, "https://example.com/test",
            params={"a": "b"}, cache_ttl_seconds=60,
        )
        self.assertEqual(data1, {"rows": [1, 2, 3]})
        self.assertEqual(data2, {"rows": [1, 2, 3]})
        self.assertEqual(call_count["n"], 1)

    def test_cache_prune_removes_all_expired_entries(self) -> None:
        now = 1000.0
        with patch.object(pm.time, "time", return_value=now):
            pm._cache_put("expired-a", {"ok": 1}, ttl_seconds=1)
            pm._cache_put("expired-b", {"ok": 2}, ttl_seconds=1)
            pm._cache_put("fresh", {"ok": 3}, ttl_seconds=100)

        with pm._RESPONSE_CACHE_LOCK:
            pm._RESPONSE_CACHE["expired-a"] = (now - 1, {"ok": 1})
            pm._RESPONSE_CACHE["expired-b"] = (now - 1, {"ok": 2})
            pm._RESPONSE_CACHE["fresh"] = (now + 100, {"ok": 3})

        with patch.object(pm.time, "time", return_value=now):
            pruned = pm.cache_prune()

        self.assertEqual(pruned, 2)
        with pm._RESPONSE_CACHE_LOCK:
            self.assertIn("fresh", pm._RESPONSE_CACHE)
            self.assertNotIn("expired-a", pm._RESPONSE_CACHE)
            self.assertNotIn("expired-b", pm._RESPONSE_CACHE)

    def test_request_json_does_not_retry_non_retriable_4xx(self) -> None:
        """A 404 raises HTTPStatusError immediately, no retry."""
        call_count = {"n": 0}

        def _handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(404, json={"error": "not found"})

        client = httpx.Client(transport=httpx.MockTransport(_handler))
        with patch.object(pm.time, "sleep", return_value=None):
            with self.assertRaises(httpx.HTTPStatusError):
                pm._request_json(
                    client, "https://example.com/missing",
                    params={"a": "b"},
                )
        self.assertEqual(call_count["n"], 1)

    def test_fetch_wallet_profile_enrichment_uses_user_data_and_next_data(self) -> None:
        """Profile fetch consumes both userData JSON and __NEXT_DATA__ HTML."""
        user_data = {
            "createdAt": "2024-11-20T19:33:42.605481Z",
            "name": "meropi",
            "pseudonym": "Several-Memo",
            "profileImage": "https://example.com/p.png",
        }
        next_data = {
            "props": {
                "pageProps": {
                    "dehydratedState": {
                        "queries": [
                            {
                                "queryKey": ["user-stats", "0xabc"],
                                "state": {"data": {"trades": 2722, "joinDate": "2024-11-20T14:33:42.605000Z"}},
                            },
                            {
                                "queryKey": ["/api/profile/marketsTraded", "0xabc", "0xabc"],
                                "state": {"data": {"traded": 2719}},
                            },
                        ]
                    }
                }
            }
        }
        html = (
            "<html><body>"
            f"<script id=\"__NEXT_DATA__\" type=\"application/json\">{json.dumps(next_data)}</script>"
            "</body></html>"
        )

        def _handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "userData" in url or "user-data" in url:
                return httpx.Response(200, json=user_data)
            return httpx.Response(200, content=html.encode("utf-8"),
                                  headers={"Content-Type": "text/html"})

        client = httpx.Client(transport=httpx.MockTransport(_handler))
        payload = pm.fetch_wallet_profile_enrichment(client, "0xabc")

        self.assertEqual(payload["wallet"], "0xabc")
        self.assertEqual(payload["profile_prediction_count"], 2722)
        # Earliest of createdAt and user-stats.joinDate should win.
        self.assertEqual(payload["profile_joined_ts"], 1732113222)
        self.assertEqual(payload["username"], "meropi")
        self.assertEqual(payload["pseudonym"], "Several-Memo")
        self.assertIn("polymarket_api_profile_userData", payload["profile_enrichment_source"])
        self.assertIn("polymarket_profile_next_data", payload["profile_enrichment_source"])


if __name__ == "__main__":
    unittest.main()

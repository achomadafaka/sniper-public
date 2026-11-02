from sniper.feeds import fomo_feed as f


def test_browser_headers_present():
    for h in ("origin", "referer", "user-agent", "x-supported-chains"):
        assert h in f.BROWSER_HEADERS


def test_get_refreshes_on_403_then_succeeds():
    calls = {"n": 0, "refreshed": False}

    def fake_http(method, url, headers, body=None):
        if "auth.privy.io" in url:
            calls["refreshed"] = True
            return 200, {"token": "NEWJWT", "refresh_token": "R2"}, None
        calls["n"] += 1
        if calls["n"] == 1:
            from urllib.error import HTTPError
            raise HTTPError(url, 403, "forbidden", {}, None)
        assert headers["authorization"] == "Bearer NEWJWT"
        return 200, {"responseObject": {"feed": []}}, "Sun, 05 Jul 2026 10:00:00 GMT"

    auth = f.FomoAuth(access="OLD", refresh="R1", ca_id="ca", store_path="")
    status, j, date = f.fomo_get(auth, f.build_feed_path(), http=fake_http)
    assert status == 200 and calls["refreshed"] is True
    assert auth.access == "NEWJWT" and auth.refresh == "R2"  # rotation persisted in-memory


def test_request_posts_authenticated_json_body():
    seen = {}

    def fake_http(method, url, headers, body=None):
        seen.update(method=method, url=url, headers=headers, body=body)
        return 200, {"statusCode": 200}, "Thu, 16 Jul 2026 12:00:00 GMT"

    auth = f.FomoAuth(access="JWT", refresh="R1", ca_id="ca", store_path="")
    status, payload, date = f.fomo_request(
        auth,
        "POST",
        "/follows",
        body={"user_id": "operator", "following_id": "wallet"},
        http=fake_http,
    )

    assert (status, payload, date) == (
        200,
        {"statusCode": 200},
        "Thu, 16 Jul 2026 12:00:00 GMT",
    )
    assert seen["method"] == "POST"
    assert seen["url"] == f.FOMO_API + "/follows"
    assert seen["headers"]["authorization"] == "Bearer JWT"
    assert seen["headers"]["content-type"] == "application/json"
    assert seen["body"] == {"user_id": "operator", "following_id": "wallet"}


def test_request_refreshes_once_on_post_403_and_preserves_body():
    api_calls = []

    def fake_http(method, url, headers, body=None):
        if "auth.privy.io" in url:
            return 200, {"token": "NEWJWT", "refresh_token": "R2"}, None
        api_calls.append((method, headers["authorization"], body))
        if len(api_calls) == 1:
            from urllib.error import HTTPError
            raise HTTPError(url, 403, "forbidden", {}, None)
        return 200, {"statusCode": 200}, None

    auth = f.FomoAuth(access="OLD", refresh="R1", ca_id="ca", store_path="")
    status, payload, _ = f.fomo_request(
        auth,
        "POST",
        "/follows",
        body={"user_id": "operator", "following_id": "wallet"},
        http=fake_http,
    )

    assert status == 200
    assert payload == {"statusCode": 200}
    assert api_calls == [
        ("POST", "Bearer OLD", {"user_id": "operator", "following_id": "wallet"}),
        ("POST", "Bearer NEWJWT", {"user_id": "operator", "following_id": "wallet"}),
    ]
    assert auth.access == "NEWJWT" and auth.refresh == "R2"


def test_refresh_sends_browser_headers_for_cloudflare():
    seen = {}

    def fake_http(method, url, headers, body=None):
        seen.update(headers)
        return 200, {"token": "X", "refresh_token": "Y"}, None

    f.FomoAuth(access="A", refresh="R", ca_id="c", store_path="").refresh_token(http=fake_http)
    # Cloudflare (error 1010) 403s without these
    for h in ("origin", "referer", "user-agent"):
        assert h in seen
    assert seen["authorization"] == "Bearer A"  # required by Privy


def test_refresh_that_mints_no_app_token_is_a_failure():
    """2026-08-03 outage. Privy returned 200 but WITHOUT `token` (the fomo-app-scoped JWT).

    `self.access = j.get("token") or self.access` silently kept the old token, `_persist()`
    rewrote it, and no exception was raised — so the runner's loop advanced `last_refresh` and
    considered the refresh done. The stale token then expired ~10 minutes later, and because
    Privy only mints `token` while the presented access token is STILL VALID, refresh could
    never recover: permanent deadlock needing a browser re-login.

    A refresh that mints nothing must RAISE so the caller retries instead of banking a no-op.
    """
    def fake_http(method, url, headers, body=None):
        assert "auth.privy.io" in url
        return 200, {"refresh_token": "R2"}, None          # rotated, but no `token`

    auth = f.FomoAuth(access="OLD", refresh="R1", ca_id="ca", store_path="")
    try:
        auth.refresh_token(http=fake_http)
    except Exception as exc:
        assert "token" in str(exc).lower()
    else:
        raise AssertionError("a refresh that minted no app token must raise, not silently no-op")
    # the ROTATED refresh token must still be kept — Privy rotates single-use, so dropping it
    # would break the chain permanently even though this attempt failed.
    assert auth.refresh == "R2"
    assert auth.access == "OLD"


def test_access_expires_in_reads_the_jwt():
    """The loop needs the token's real remaining life, not an assumption about process age."""
    import base64, json as _json

    def jwt(exp):
        payload = base64.urlsafe_b64encode(
            _json.dumps({"exp": exp}).encode()).decode().rstrip("=")
        return f"h.{payload}.sig"

    auth = f.FomoAuth(access=jwt(2_000_000_000), refresh="R", ca_id="ca", store_path="")
    ttl = auth.access_expires_in(now_ts=1_999_999_400)
    assert ttl is not None and abs(ttl - 600) < 2

    expired = f.FomoAuth(access=jwt(1_000), refresh="R", ca_id="ca", store_path="")
    assert expired.access_expires_in(now_ts=2_000) < 0

    assert f.FomoAuth(access="not-a-jwt", refresh="R", ca_id="ca",
                      store_path="").access_expires_in(now_ts=1) is None


def test_refresh_due_fires_on_token_ttl_not_just_process_age():
    """2026-08-03 outage, second half. `last_refresh` is seeded at PROCESS START, so a runner
    that inherits an already-old token waits its full 45min interval regardless of how much life
    the token actually has. On 08-03 the runner started at 12:26:58 holding a token minted
    12:21:52 that died at 13:21:52 — the interval alone left almost no margin, and one failed
    refresh was enough to lose the token permanently.

    Refresh must also fire whenever the token is inside the expiry margin.
    """
    from sniper.services.memecoin_runner import refresh_due

    # plenty of interval left AND plenty of token life -> no refresh
    assert refresh_due(now=1000, last_refresh=900, interval_s=2700, ttl_s=3000, margin_s=600) is False
    # interval elapsed -> refresh (existing behaviour preserved)
    assert refresh_due(now=4000, last_refresh=900, interval_s=2700, ttl_s=3000, margin_s=600) is True
    # interval NOT elapsed but the token is nearly dead -> refresh anyway (the fix)
    assert refresh_due(now=1000, last_refresh=900, interval_s=2700, ttl_s=300, margin_s=600) is True
    # already expired -> still try; the caller surfaces the failure
    assert refresh_due(now=1000, last_refresh=900, interval_s=2700, ttl_s=-5, margin_s=600) is True
    # unreadable ttl -> fall back to the interval alone, never crash the loop
    assert refresh_due(now=1000, last_refresh=900, interval_s=2700, ttl_s=None, margin_s=600) is False


def test_refresh_reports_whether_it_actually_extended():
    """Privy re-issues the SAME app token while the current one is still valid (observed
    2026-08-03: `token` present, `iat` unchanged, refresh_token rotated). It only mints a NEW
    token near expiry, and none at all once expired.

    So "a token came back" is NOT proof of progress. The caller must know whether expiry actually
    moved, or it banks a no-op and lets the token die — the 08-03 deadlock. Each attempt also
    rotates the single-use refresh token, so the caller must back off rather than spin.
    """
    import base64, json as _json

    def jwt(iat, exp):
        payload = base64.urlsafe_b64encode(
            _json.dumps({"iat": iat, "exp": exp}).encode()).decode().rstrip("=")
        return f"h.{payload}.sig"

    old, new = jwt(1000, 4600), jwt(4000, 7600)

    def same_token(method, url, headers, body=None):
        return 200, {"token": old, "refresh_token": "R2"}, None

    def newer_token(method, url, headers, body=None):
        return 200, {"token": new, "refresh_token": "R3"}, None

    auth = f.FomoAuth(access=old, refresh="R1", ca_id="ca", store_path="")
    assert auth.refresh_token(http=same_token) is not None
    assert auth.extended is False        # re-issued the same token: no progress
    assert auth.refresh == "R2"          # rotation still banked

    assert auth.refresh_token(http=newer_token) is not None
    assert auth.extended is True         # expiry actually moved

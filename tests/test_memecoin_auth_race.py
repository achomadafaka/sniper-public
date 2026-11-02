"""Privy single-use refresh-token rotation race: two FomoAuth instances share one
store; when one rotates the token, the other must reload the fresh token before
POSTing instead of spending its stale (now-invalid) copy -> silent auth death."""
from sniper.feeds import fomo_feed as f


def test_refresh_reloads_rotated_token_before_posting(tmp_path):
    store = str(tmp_path / "tok.json")
    b = f.FomoAuth(access="A0", refresh="R1", ca_id="c", store_path=store)  # B holds R1
    a = f.FomoAuth(access="A0", refresh="R1", ca_id="c", store_path=store)  # A holds R1

    # A refreshes: R1 -> R2, persisted to the shared store.
    a.refresh_token(http=lambda *A, **K: (200, {"token": "J2", "refresh_token": "R2"}, None))
    assert b.refresh == "R1"                       # B's in-memory token is now stale

    seen = {}

    def http_b(method, url, headers, body=None):
        seen["rt"] = body["refresh_token"]
        return 200, {"token": "J3", "refresh_token": "R3"}, None

    b.refresh_token(http=http_b)
    assert seen["rt"] == "R2"                       # reloaded the rotated token, not stale R1
    assert b.refresh == "R3"


def test_refresh_with_no_store_still_works():
    # store_path="" path (used across the suite) must not lock/reload and must refresh.
    seen = {}

    def http(method, url, headers, body=None):
        seen["rt"] = body["refresh_token"]
        return 200, {"token": "JWT", "refresh_token": "R2"}, None

    auth = f.FomoAuth(access="A", refresh="R1", ca_id="c", store_path="")
    tok = auth.refresh_token(http=http)
    assert tok == "JWT" and seen["rt"] == "R1"

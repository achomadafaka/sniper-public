"""Item 9 — auth_dead disambiguation. A 401/403 (spent token) is unrecoverable auth death: a
restart won't fix it, the operator must replace the token, and it pages immediately. A 5xx / timeout
is a transient feed error: keep the loop alive (do NOT mark auth dead) so open positions still get
their bracket exits. This drives the heartbeat auth_ok flag the watchdog reads."""
from sniper.services.memecoin_runner import classify_fomo_error


def test_401_403_are_auth_dead():
    assert classify_fomo_error(status=401) == "auth_dead"
    assert classify_fomo_error(status=403) == "auth_dead"


def test_5xx_is_transient_feed_error():
    assert classify_fomo_error(status=502) == "feed_error"
    assert classify_fomo_error(status=503) == "feed_error"


def test_timeout_or_connection_is_feed_error():
    assert classify_fomo_error(exc=TimeoutError("slow")) == "feed_error"
    assert classify_fomo_error(exc=ConnectionError("reset")) == "feed_error"


def test_200_is_ok():
    assert classify_fomo_error(status=200) == "ok"


def test_httperror_401_403_exception_is_auth_dead():
    # fomo_get RAISES urllib HTTPError on a spent token (it never RETURNS status 401), so the
    # classifier must inspect the exception's .code — else auth death is silently seen as transient
    # and alert_auth_dead can never fire (the exact regression this guards).
    import urllib.error
    for code in (401, 403):
        err = urllib.error.HTTPError("https://fomo", code, "unauthorized", {}, None)
        assert classify_fomo_error(exc=err) == "auth_dead"


def test_httperror_5xx_exception_is_feed_error():
    import urllib.error
    err = urllib.error.HTTPError("https://fomo", 503, "unavailable", {}, None)
    assert classify_fomo_error(exc=err) == "feed_error"


# --- the page must fire on the RAISE path (the residual the review caught) ---

def test_handle_auth_page_fires_once_on_transition():
    from sniper.services.memecoin_runner import handle_auth_page
    fired = []
    page = lambda down_s=None: fired.append(down_s)          # noqa: E731
    # healthy -> auth_dead: page fires, state latches truthy
    st = handle_auth_page("auth_dead", False, page_fn=page, now=1000.0, repage_s=900)
    assert fired == [0.0] and st
    # still dead, inside the re-page window: silent
    st = handle_auth_page("auth_dead", st, page_fn=page, now=1300.0, repage_s=900)
    assert fired == [0.0] and st
    # recovered: state resets, no page
    assert handle_auth_page("ok", st, page_fn=page, now=1400.0, repage_s=900) is False
    assert fired == [0.0]


def test_handle_auth_page_repages_with_elapsed_outage():
    """The 2026-09-02 hole: the page fired once at the crossing and the feed stayed dead 121 min
    with no further signal. Every page after the first must carry how long it has been down."""
    from sniper.services.memecoin_runner import handle_auth_page
    fired = []
    page = lambda down_s=None: fired.append(down_s)          # noqa: E731
    st = handle_auth_page("auth_dead", False, page_fn=page, now=0.0, repage_s=900)
    for t in (899.0, 900.0, 1200.0, 1800.0):                 # 899 is inside the window; 900 is due
        st = handle_auth_page("auth_dead", st, page_fn=page, now=t, repage_s=900)
    assert fired == [0.0, 900.0, 1800.0]                     # elapsed since the FIRST page, not the last
    # repage_s=0 restores the one-shot behaviour
    fired.clear()
    st = handle_auth_page("auth_dead", False, page_fn=page, now=0.0, repage_s=0)
    st = handle_auth_page("auth_dead", st, page_fn=page, now=99999.0, repage_s=0)
    assert fired == [0.0]


def test_auth_page_fires_through_run_once_raise_path():
    """A spent Privy token RAISES HTTPError(401) through run_once (fomo_get re-raises after a failed
    refresh). The loop catches it, classify_fomo_error(exc) -> auth_dead, and handle_auth_page pages —
    the whole chain the residual broke. This drives run_once for real, not a stub."""
    import urllib.error

    import pytest

    from sniper.feeds.fomo_feed import FomoAuth
    from sniper.persistence.db_core import get_connection
    from sniper.services.memecoin_runner import classify_fomo_error, handle_auth_page, run_once

    def raising_http(method, url, headers=None, body=None, timeout=None):
        raise urllib.error.HTTPError(url, 401, "unauthorized", {}, None)

    conn = get_connection(":memory:")
    from sniper.persistence import db_memecoin as m
    m.ensure_memecoin_tables(conn)
    auth = FomoAuth(access="spent", refresh="spent", store_path="")   # don't load the real token store

    with pytest.raises(urllib.error.HTTPError):
        run_once(conn, auth, 100.0, http=raising_http)     # 401 propagates out of run_once

    # loop-equivalent handling of the raise:
    try:
        run_once(conn, auth, 100.0, http=raising_http)
        kind = "ok"
    except Exception as e:
        kind = classify_fomo_error(exc=e)
    assert kind == "auth_dead"
    fired = []
    handle_auth_page(kind, False, page_fn=lambda down_s=None: fired.append(1))
    assert fired == [1]                                    # the page fires on the exact auth-death path


def test_privy_app_token_unavailable_is_auth_dead_not_transient():
    """A refresh that mints no app token is UNRECOVERABLE — Privy will not mint one once the
    presented token has expired, so only an operator (browser re-login) can fix it.

    Classified as a transient `feed_error` it would keep auth_ok=True and spin forever without
    paging: exactly what happened on 2026-08-03, where the runner hammered the feed for ~20
    minutes (feed 401 -> privy refresh -> retry -> Cloudflare 429) while looking healthy.
    """
    from sniper.feeds.fomo_feed import PrivyAppTokenUnavailable
    from sniper.services.memecoin_runner import classify_fomo_error

    assert classify_fomo_error(exc=PrivyAppTokenUnavailable("no token")) == "auth_dead"
    # a genuinely transient failure must stay transient so exit sweeps keep running
    assert classify_fomo_error(exc=TimeoutError("slow")) == "feed_error"


def test_feed_error_does_not_clear_the_auth_dead_state():
    """2026-09-02 paged TWICE (11:21:54, 11:32:34) under a latch documented as one-shot, because
    `kind != "auth_dead"` cleared on a transient `feed_error` and the next 401 re-paged. Only a
    healthy cycle means auth recovered. Left unfixed it also restarts the elapsed clock, so a
    121-minute outage reports a small `down_s` forever."""
    from sniper.services.memecoin_runner import handle_auth_page
    fired = []
    page = lambda down_s=None: fired.append(down_s)          # noqa: E731
    st = handle_auth_page("auth_dead", False, page_fn=page, now=0.0, repage_s=900)
    st = handle_auth_page("feed_error", st, page_fn=page, now=100.0, repage_s=900)   # flap
    assert fired == [0.0], "a transient feed error must not re-page"
    assert st, "a transient feed error must not clear the auth-dead state"
    # the elapsed clock still runs from the ONSET, not from the flap
    st = handle_auth_page("auth_dead", st, page_fn=page, now=900.0, repage_s=900)
    assert fired == [0.0, 900.0]
    # a feed_error while healthy stays healthy and pages nothing
    assert handle_auth_page("feed_error", False, page_fn=page, now=1.0, repage_s=900) is False
    assert fired == [0.0, 900.0]

"""A dark J7 account pages, and an expiring J7 token pages before it can go dark.

2026-09-24: account 1's JWT expired at 14:43:25Z, its socket died at the 14:49:02Z restart, the
log collected 1,184 'Invalid token' connect_errors, and 54 armed wallets were dark for ~5 h.
Nothing paged. No test here uses a real token; every JWT is built from a fixture payload.
"""
import base64
import json
import threading
import time

import pytest

from sniper.feeds import j7_feed as J
from sniper.services import memecoin_alerts as A


def _jwt(payload):
    enc = lambda obj: base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return "%s.%s.%s" % (enc({"alg": "HS256"}), enc(payload), "c2lnbmF0dXJl")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    # Each test is its own state root: the paged-milestone file must never leak between tests.
    monkeypatch.setenv("SNIPER_STATE_ROOT", str(tmp_path))
    _restart()
    yield
    _restart()
    with A._j7_lock:
        A._j7_account_state.clear()


def _restart():
    """A new runner process: the in-memory alert state is gone; the state root is not."""
    with A._j7_lock:
        A._j7_expiry_state.clear()
        getattr(A, "_j7_expiry_saved", {}).clear()
        A._j7_expiry_loaded = False


def _sink(ok=True):
    """A Telegram stand-in answering like send_alert's real path: (ok, latency)."""
    sent = []

    def send(message):
        sent.append(message)
        return (ok, 0.0)
    return sent, send


# -- the token never leaves -------------------------------------------------------------------

def test_the_exp_claim_is_decoded_locally_and_only_the_number_comes_back():
    assert J.token_exp(_jwt({"exp": 1790863050})) == 1790863050.0     # 2026-10-01T13:57:30Z


@pytest.mark.parametrize("bad", ["", "not-a-jwt", "a.b", "a.!!!.c", _jwt({"sub": "x"}),
                                 _jwt({"exp": "soon"}), _jwt({"exp": -5}), None])
def test_an_unreadable_exp_is_none_and_never_raises(bad):
    assert J.token_exp(bad) is None


def test_a_stored_error_string_cannot_carry_the_token():
    token = _jwt({"exp": 1})
    kept = J._redacted("auth failed for %s: Invalid token" % token, token)
    assert token not in kept and "<token>" in kept and len(kept) <= 200


def test_the_feed_hands_out_expiries_not_tokens():
    token = _jwt({"exp": 1790863050})
    feed = J.J7WalletsFeed(token)
    out = feed.token_expiries()
    assert out == [(1, 1790863050.0)]
    assert token not in repr(out)


# -- J7-ACCOUNT-DOWN ---------------------------------------------------------------------------

def _status(*accounts):
    return {"status": "degraded", "accounts": list(accounts)}


def _acct(status, err=None, fomo=54, pump=0):
    return {"status": status, "last_error": err, "tracked_count": fomo, "pump_tracked_count": pump}


def test_a_dark_account_pages_once_after_five_minutes_and_once_on_recovery():
    sent, send = _sink()
    down = _status(_acct("connect_failed", "Invalid token"), _acct("connected"))
    for t in (0.0, 120.0, 299.0):
        A.note_j7_accounts(down, now_mono=t, send_fn=send)
    assert sent == [], "inside the window a reconnecting account must not page"
    for t in (301.0, 400.0, 3000.0):
        A.note_j7_accounts(down, now_mono=t, send_fn=send)
    assert len(sent) == 1
    assert "J7-ACCOUNT-DOWN" in sent[0] and "account 1" in sent[0]
    assert "Invalid token" in sent[0] and "fomo=54" in sent[0]
    A.note_j7_accounts(_status(_acct("connected"), _acct("connected")), now_mono=3100.0,
                       send_fn=send)
    A.note_j7_accounts(_status(_acct("connected"), _acct("connected")), now_mono=3200.0,
                       send_fn=send)
    assert len(sent) == 2 and "J7-ACCOUNT-RECOVERED" in sent[1] and "account 1" in sent[1]


def test_a_flap_that_reconnects_inside_the_window_never_pages():
    sent, send = _sink()
    A.note_j7_accounts(_status(_acct("reconnecting")), now_mono=0.0, send_fn=send)
    A.note_j7_accounts(_status(_acct("connected")), now_mono=200.0, send_fn=send)
    A.note_j7_accounts(_status(_acct("reconnecting")), now_mono=250.0, send_fn=send)
    A.note_j7_accounts(_status(_acct("reconnecting")), now_mono=500.0, send_fn=send)
    assert sent == [], "the down clock restarts at the second outage"


def test_a_single_account_feed_status_is_account_one():
    sent, send = _sink()
    single = {"status": "auth_error", "last_error": "expired", "tracked_count": 3,
              "pump_tracked_count": None}
    A.note_j7_accounts(single, now_mono=0.0, send_fn=send)
    A.note_j7_accounts(single, now_mono=301.0, send_fn=send)
    assert len(sent) == 1 and "account 1" in sent[0]


def test_two_threads_crossing_the_threshold_together_page_once(monkeypatch):
    """⚠️ The down check reads `paged` and then writes it. The state entry is instrumented to yield
    between the two, which is what a busy host does for free; without the lock both threads read
    False and both page."""
    class _SlowEntry(dict):
        def __getitem__(self, key):
            # READ, THEN yield, then return the value read -- the window a real interleaving opens
            # between the check and the write. Sleeping BEFORE the read (the first version) lets
            # each thread see the previous thread's write and hides a missing lock.
            value = super().__getitem__(key)
            if key == "paged":
                time.sleep(0.02)
            return value

    monkeypatch.setattr(A, "_j7_account_state", {1: _SlowEntry({"down_since": 0.0, "paged": False})})
    sent, lock = [], threading.Lock()

    def guarded(message):
        with lock:
            sent.append(message)

    down = _status(_acct("connect_failed", "Invalid token"))
    # A BARRIER, because thread start-up alone can serialise six short calls and let a missing
    # lock pass: the first version of this test did exactly that.
    gate = threading.Barrier(6)

    def race():
        gate.wait()
        A.note_j7_accounts(down, now_mono=400.0, send_fn=guarded)

    threads = [threading.Thread(target=race) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sent) == 1, sent


# -- J7-TOKEN-EXPIRING -------------------------------------------------------------------------

EXP = 1790863050.0          # 2026-10-01T13:57:30Z, J7_TOKEN_2's real deadline


def test_the_token_pages_at_24_hours_at_2_hours_and_when_it_passes():
    sent, send = _sink()
    for left in (48 * 3600, 25 * 3600):
        A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - left, send_fn=send)
    assert sent == []
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - 23 * 3600, send_fn=send)
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - 20 * 3600, send_fn=send)
    assert len(sent) == 1 and "account 2" in sent[0] and "expires in 23.0 h" in sent[0]
    assert "2026-10-01T13:57:30Z" in sent[0]
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - 1.5 * 3600, send_fn=send)
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - 1.0 * 3600, send_fn=send)
    assert len(sent) == 2 and "expires in 1.5 h" in sent[1]
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP + 60, send_fn=send)
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP + 600, send_fn=send)
    assert len(sent) == 3 and "EXPIRED" in sent[2]


def test_a_runner_started_inside_the_window_pages_the_tightest_milestone_once():
    sent, send = _sink()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - 1.0 * 3600, send_fn=send)
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - 0.5 * 3600, send_fn=send)
    assert len(sent) == 1 and "expires in 1.0 h" in sent[0]


def test_an_unreadable_expiry_pages_once_as_unknown():
    sent, send = _sink()
    for now in (0.0, 1e9, 2e9):
        A.note_j7_token_expiry([(1, None)], now_epoch=now, send_fn=send)
    assert len(sent) == 1 and "expiry unknown" in sent[0] and "account 1" in sent[0]


def test_automatic_accounts_do_not_page_for_each_healthy_twelve_hour_token():
    sent, send = _sink()
    A.note_j7_token_expiry([(1, EXP), (2, EXP)], now_epoch=EXP - 11 * 3600,
                          auto_refresh_accounts={2}, send_fn=send)
    assert len(sent) == 1 and "account 1" in sent[0]
    for remaining in (2 * 3600, 3600, 1801):
        A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - remaining,
                              auto_refresh_accounts={2}, send_fn=send)
    assert len(sent) == 1


def test_automatic_accounts_page_if_renewal_has_not_extended_the_near_expiry_token():
    sent, send = _sink()
    for remaining in (1800, 900):
        A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - remaining,
                              auto_refresh_accounts={2}, send_fn=send)
    assert len(sent) == 1 and "account 2" in sent[0]
    assert "automatic renewal" in sent[0] and "restart" not in sent[0]
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP + 1,
                          auto_refresh_accounts={2}, send_fn=send)
    assert len(sent) == 2 and "EXPIRED" in sent[1]


def test_a_renewed_automatic_token_stops_the_old_expiry_warning():
    sent, send = _sink()
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - 900,
                          auto_refresh_accounts={2}, send_fn=send)
    A.note_j7_token_expiry([(2, EXP + 12 * 3600)], now_epoch=EXP + 1,
                          auto_refresh_accounts={2}, send_fn=send)
    assert len(sent) == 1


def test_automatic_accounts_still_report_unknown_expiry():
    sent, send = _sink()
    A.note_j7_token_expiry([(2, None)], now_epoch=EXP,
                          auto_refresh_accounts={2}, send_fn=send)
    assert len(sent) == 1 and "expiry unknown" in sent[0]


# -- across restarts (2026-10-16): ~20 deploys/restarts on 10-15/16 each re-sent the 24 h page, 52
# J7-TOKEN-EXPIRING pages in 30 h, and the real 2 h and EXPIRED pages were lost among them. 24 h and
# 2 h and EXPIRED are persisted per (account, exp), only after delivery, and never re-page.

def test_a_token_two_hours_from_exp_pages_once_across_restarts():
    sent, send = _sink()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - 1.9 * 3600, send_fn=send)
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - 1.8 * 3600, send_fn=send)
    assert len(sent) == 1 and "expires in 1.9 h" in sent[0]
    for left in (1.7, 1.0, 0.2):
        _restart()
        A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - left * 3600, send_fn=send)
    assert len(sent) == 1, sent


def test_the_24_hour_page_is_not_resent_by_a_restart_and_the_2_hour_one_still_comes():
    sent, send = _sink()
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - 23 * 3600, send_fn=send)
    _restart()
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - 20 * 3600, send_fn=send)
    assert len(sent) == 1
    _restart()
    A.note_j7_token_expiry([(2, EXP)], now_epoch=EXP - 1.5 * 3600, send_fn=send)
    assert len(sent) == 2 and "expires in 1.5 h" in sent[1]


def test_an_expired_token_pages_once_across_restarts():
    sent, send = _sink()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP + 60, send_fn=send)          # one page, the tightest
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP + 600, send_fn=send)
    assert len(sent) == 1 and "EXPIRED" in sent[0]
    _restart()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP + 900, send_fn=send)
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP + 960, send_fn=send)
    assert len(sent) == 1, "the same delivered expiry must not page again after a restart"


@pytest.mark.parametrize('remaining,wording', [(1.5 * 3600, 'expires in 1.5 h'), (-60, 'EXPIRED')])
def test_a_new_token_starts_fresh(remaining, wording):
    sent, send = _sink()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - remaining, send_fn=send)
    _restart()
    fresh = EXP + 15 * 86400
    A.note_j7_token_expiry([(1, fresh)], now_epoch=fresh - remaining, send_fn=send)
    assert len(sent) == 2 and wording in sent[1]


def test_an_unreadable_state_file_pages_as_before(tmp_path):
    from sniper.util.state_paths import state_path
    sent, send = _sink()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - 1.5 * 3600, send_fn=send)
    path = state_path(*A._J7_EXPIRY_PAGED_FILE)
    assert json.loads(path.read_text()), "the sent page was written"
    path.write_text("{not json")
    _restart()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - 1.0 * 3600, send_fn=send)
    assert len(sent) == 2, "an unreadable file is today's behaviour: the milestone pages again"


def test_nothing_is_written_until_a_page_is_sent():
    from sniper.util.state_paths import state_path
    sent, send = _sink()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - 48 * 3600, send_fn=send)
    assert sent == [] and not state_path(*A._J7_EXPIRY_PAGED_FILE).exists()


def test_a_page_that_failed_to_send_is_not_recorded_and_pages_again_after_a_restart():
    """Gamma LOW-1 on 8e187e562: the file recorded a 2 h milestone whose enqueue failed."""
    from sniper.util.state_paths import state_path
    failed, send_failed = _sink(ok=False)
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - 1.5 * 3600, send_fn=send_failed)
    assert len(failed) == 1 and not state_path(*A._J7_EXPIRY_PAGED_FILE).exists()
    _restart()
    sent, send = _sink()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - 1.4 * 3600, send_fn=send)
    assert len(sent) == 1 and "expires in 1.4 h" in sent[0]
    _restart()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP - 1.3 * 3600, send_fn=send)
    assert len(sent) == 1, "now sent, it is recorded"


@pytest.mark.parametrize('offset,persisted', [(-1.5 * 3600, [7200.0, 86400.0]),
                                             (60, [0.0, 7200.0, 86400.0])])
def test_a_milestone_is_recorded_on_delivery_not_on_queue_acceptance(monkeypatch, offset, persisted):
    """Codex F11 (review through 648ada5d7): the real send path answers True when the dispatcher
    QUEUE accepts the page, and the milestone was persisted then. A delivery that failed
    afterwards (or a crash before it) left the warning suppressed across restarts. Now the
    milestone is persisted from the dispatcher's own result, on its thread, only when delivered;
    the funded caller still only enqueues."""
    from sniper.services import memecoin_telegram as tg
    from sniper.util.state_paths import state_path
    answers, delivered = [False], []

    def deliver(text):
        delivered.append(text)
        return answers[0], 1.0
    dispatcher = tg._TelegramDispatcher(deliver, maxsize=8)
    monkeypatch.setattr(tg, "_get_dispatcher", lambda: dispatcher)
    marks = state_path(*A._J7_EXPIRY_PAGED_FILE)

    accepted = A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP + offset)
    assert accepted == [(True, 0.0)], "the caller sees queue acceptance, never waits"
    dispatcher._queue.join()
    assert len(delivered) == 1 and not marks.exists(), "accepted, not delivered: nothing recorded"

    _restart()                                            # the next runner pages it again
    answers[0] = True
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP + offset + 360)
    dispatcher._queue.join()
    assert len(delivered) == 2 and marks.exists()
    assert json.loads(marks.read_text()) == {A._expiry_key(1, EXP): persisted}

    _restart()
    A.note_j7_token_expiry([(1, EXP)], now_epoch=EXP + offset + 720)
    dispatcher._queue.join()
    assert len(delivered) == 2, "delivered once, recorded once"

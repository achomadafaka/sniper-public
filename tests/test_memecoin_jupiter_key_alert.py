"""A rejected Jupiter key must page on the FIRST rejection, once, and on recovery.

2026-09-24: the operator's new key was rejected at 16:01Z. Every funded exit quote fell back to the
keyless host (0.5 RPS / 30 per minute against the key's 1 RPS / 60), 119 exact exit quotes were lost
to rate limiting over 2 h 28 min, and the only trace was 131 identical log lines. Nothing paged.
"""
import threading

import pytest

from sniper.services import memecoin_alerts as alerts


@pytest.fixture(autouse=True)
def _clean_state():
    with alerts._jup_auth_lock:
        alerts._jup_auth_state.clear()
    yield
    with alerts._jup_auth_lock:
        alerts._jup_auth_state.clear()


def _sink():
    sent = []
    return sent, sent.append


def test_the_first_rejection_pages_and_the_repeats_do_not():
    sent, send = _sink()
    for _ in range(4):
        alerts.note_jupiter_auth("api", ok=False, role="funded", fallback_host="lite-api",
                                 fallback_limit="keyless 0.5 RPS / 30 per minute", send_fn=send)
    assert len(sent) == 1, sent
    assert "JUPITER-KEY-REJECTED" in sent[0]
    assert "streak=1" in sent[0]
    # The fallback and ITS limit are in the page: the operator's decision turns on how much
    # cheaper the fallback is, not on the bare fact of a 401.
    assert "lite-api" in sent[0] and "30 per minute" in sent[0]


def test_a_persistent_episode_escalates_at_five_and_twenty():
    sent, send = _sink()
    for _ in range(20):
        alerts.note_jupiter_auth("api", ok=False, role="funded", send_fn=send)
    assert [s.split("streak=")[1].split(";")[0] for s in sent] == ["1", "5", "20"]


def test_recovery_pages_once_and_only_after_a_paged_episode():
    sent, send = _sink()
    alerts.note_jupiter_auth("api", ok=False, role="funded", send_fn=send)
    alerts.note_jupiter_auth("api", ok=True, role="funded", send_fn=send)
    alerts.note_jupiter_auth("api", ok=True, role="funded", send_fn=send)
    assert len(sent) == 2, sent
    assert "JUPITER-KEY-RECOVERED" in sent[1]
    assert "after 1 rejections" in sent[1]

    # A recovery with no prior rejection at all says nothing.
    sent2, send2 = _sink()
    alerts.note_jupiter_auth("api", ok=True, role="paper", send_fn=send2)
    assert sent2 == []


def test_roles_and_hosts_do_not_share_a_streak():
    sent, send = _sink()
    alerts.note_jupiter_auth("api", ok=False, role="funded", send_fn=send)
    alerts.note_jupiter_auth("api", ok=False, role="paper", send_fn=send)
    assert len(sent) == 2
    assert "role=funded" in sent[0] and "role=paper" in sent[1]


@pytest.mark.parametrize("denied,kind", [(401, "KEY"), (403, "ACCESS")])
@pytest.mark.parametrize("failed_status", [None, 400, 429, 500])
def test_keyed_access_episode_requires_success_and_reports_actual_denial(
        monkeypatch, denied, kind, failed_status):
    from sniper.execution import solana_real_seams as seams

    sent = []
    monkeypatch.setattr(alerts, "_default_send", lambda text, **kw: sent.append(text))
    monkeypatch.setattr(seams, "_sink_note", lambda *a, **kw: None)
    monkeypatch.setattr(seams, "_jup_host_cooldown", {})
    env = {seams.active_key_var(): "test-key"}
    calls = []
    now = 100.0
    keyed_status = denied

    def http(method, url, headers=None):
        calls.append((url, headers))
        return (keyed_status if headers else 200), {"outAmount": "123"}, b""

    def call():
        return seams._jup_call_inner("GET", "/quote", body=None, http=http,
                                     clock=lambda: now, env=env)

    assert call()[0] == 200  # Existing keyless fallback still works.
    assert len(calls) == 2
    assert calls[0][1] == {"x-api-key": "test-key"} and not calls[1][1]
    assert len(sent) == 1  # Keyless success cannot recover the keyed host.
    assert f"JUPITER-{kind}-" in sent[0]
    assert f"HTTP {denied}" in sent[0]
    assert "needs replacing" not in sent[0] and "quotes continue" not in sent[0]

    now += 61
    keyed_status = failed_status
    call()
    assert len(sent) == 1  # Non-auth errors do not establish recovery either.

    now += 61
    keyed_status = 200
    call()
    call()
    assert len(sent) == 2
    assert f"JUPITER-{kind}-RECOVERED" in sent[1]


def test_the_streak_survives_concurrent_callers(monkeypatch):
    """⚠️ Jupiter quotes run on the main loop, the fastlane worker and the ladder's own pool, so
    the streak dict is shared mutable state.

    A PLAIN THREAD RACE DOES NOT REPRODUCE THIS and the first version of this test passed with the
    lock removed: `state["streak"] += 1` is short enough that the GIL hides the interleaving. The
    state ENTRY is therefore instrumented to yield between its read and its write, which is what a
    real scheduler does for free on a busier box. Without the lock two threads both read 0, both
    write 1, and milestone 1 pages twice.
    """
    import time

    class _SlowEntry(dict):
        def __getitem__(self, key):
            if key == "streak":
                time.sleep(0.002)
            return super().__getitem__(key)

    entry = _SlowEntry({"streak": 0, "paged": False})
    monkeypatch.setattr(alerts, "_jup_auth_state", {("funded", "api"): entry})
    sent, send = _sink()
    lock = threading.Lock()

    def guarded(message):
        with lock:
            sent.append(message)

    def hit():
        alerts.note_jupiter_auth("api", ok=False, role="funded", send_fn=guarded)

    threads = [threading.Thread(target=hit) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert entry["streak"] == 6, entry["streak"]
    milestones = sorted(int(s.split("streak=")[1].split(";")[0]) for s in sent)
    assert milestones == [1, 5], milestones

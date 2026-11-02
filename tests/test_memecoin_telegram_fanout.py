"""Multi-recipient Telegram fanout (2026-07-15).

The bot was send-only to ONE hardcoded chat, so a second person could press /start and receive
nothing — there is no /start handler and no subscriber list. `MEMECOIN_TG_CHAT_ID` now accepts a
comma-separated list (or a group id).

The PRIMARY chat (first in the list) owns the return contract: `ok` and `elapsed_ms` are its send's,
because those feed the front-run alert LATENCY instrument. Extra recipients are best-effort — a
slow or blocked follower must never distort the latency measurement or fail the alert.
"""
import sniper.services.memecoin_telegram as T


def _capture(monkeypatch, *, fail_for=()):
    sent = []

    def http(url, payload, timeout):
        sent.append(payload["chat_id"])
        if payload["chat_id"] in fail_for:
            raise RuntimeError("blocked by user")
        return 200
    monkeypatch.setattr(T, "_default_http", http)
    return sent


def test_sends_to_every_configured_chat(monkeypatch):
    monkeypatch.setenv("MEMECOIN_TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("MEMECOIN_TG_CHAT_ID", "111,222,-333")
    sent = _capture(monkeypatch)
    ok, ms = T.send_telegram("signal")
    assert sent == ["111", "222", "-333"]
    assert ok is True


def test_single_chat_is_unchanged(monkeypatch):
    """The existing single-id config must behave exactly as before — this is the live alert path."""
    monkeypatch.setenv("MEMECOIN_TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("MEMECOIN_TG_CHAT_ID", "6777359606")
    sent = _capture(monkeypatch)
    ok, _ms = T.send_telegram("signal")
    assert sent == ["6777359606"] and ok is True


def test_a_blocked_follower_does_not_fail_the_alert(monkeypatch):
    """If a shared-with user blocks the bot, the OPERATOR must still get their signal, and the send
    must still report ok — otherwise one stranger's block silently kills our own alerting."""
    monkeypatch.setenv("MEMECOIN_TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("MEMECOIN_TG_CHAT_ID", "111,222")
    sent = _capture(monkeypatch, fail_for=("222",))
    ok, _ms = T.send_telegram("signal")
    assert sent == ["111", "222"]
    assert ok is True, "primary succeeded -> the alert is ok"


def test_primary_failure_is_reported(monkeypatch):
    """The primary IS the operator. If that send fails, ok must be False even if followers worked."""
    monkeypatch.setenv("MEMECOIN_TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("MEMECOIN_TG_CHAT_ID", "111,222")
    _capture(monkeypatch, fail_for=("111",))
    ok, _ms = T.send_telegram("signal")
    assert ok is False


def test_whitespace_and_empties_are_tolerated(monkeypatch):
    """Operators will paste ' 111, 222 , ' — don't send to an empty chat id."""
    monkeypatch.setenv("MEMECOIN_TG_BOT_TOKEN", "tok")
    monkeypatch.setenv("MEMECOIN_TG_CHAT_ID", " 111 , 222 , ")
    sent = _capture(monkeypatch)
    T.send_telegram("signal")
    assert sent == ["111", "222"]

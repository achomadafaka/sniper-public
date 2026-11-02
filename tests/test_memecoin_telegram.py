"""send_telegram — fail-closed external sender for the front-run latency instrument."""
import io
import threading
import urllib.error

import pytest

import sniper.services.memecoin_telegram as telegram
from sniper.services.memecoin_telegram import _TelegramDispatcher, send_telegram


def _clear_env(mp):
    for k in ("MEMECOIN_TG_BOT_TOKEN", "MEMECOIN_TG_CHAT_ID",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        mp.delenv(k, raising=False)


def test_send_success_builds_url_and_payload(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOTTOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT42")
    seen = {}

    def http(url, payload, timeout):
        seen["url"] = url; seen["payload"] = payload; seen["timeout"] = timeout
        return 200

    ok, ms = send_telegram("hello", http=http)
    assert ok is True and ms >= 0
    assert seen["url"] == "https://api.telegram.org/botBOTTOK/sendMessage"
    assert seen["payload"]["chat_id"] == "CHAT42" and seen["payload"]["text"] == "hello"


def test_missing_token_fails_closed_without_calling_http(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT42")
    called = {"n": 0}

    def http(url, payload, timeout):
        called["n"] += 1
        return 200

    ok, ms = send_telegram("hi", http=http)
    assert ok is False and ms == 0.0 and called["n"] == 0


def test_missing_chat_fails_closed(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOTTOK")
    ok, ms = send_telegram("hi", http=lambda *a, **k: 200)
    assert ok is False and ms == 0.0


def test_http_exception_swallowed(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOTTOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT42")

    def http(url, payload, timeout):
        raise RuntimeError("network down")

    ok, ms = send_telegram("hi", http=http)
    assert ok is False and ms >= 0     # never raises; still reports elapsed


def test_shared_send_path_paces_repeated_messages_to_one_chat(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOTTOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT42")
    clock = {"now": 10.0}
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    limiter = telegram._TelegramRateLimiter(
        monotonic=lambda: clock["now"], sleep_fn=sleep,
    )
    sent_at = []

    def http(*_args):
        sent_at.append(clock["now"])
        return 200

    send_telegram("one", http=http, limiter=limiter, min_gap_s=3.0)
    send_telegram("two", http=http, limiter=limiter, min_gap_s=3.0)

    assert sent_at == [10.0, 13.0]
    assert sleeps == [3.0]


def test_429_sleeps_retry_after_and_retries_once_without_traceback(monkeypatch, caplog):
    _clear_env(monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "BOTTOK")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "CHAT42")
    calls = []
    sleeps = []
    before = telegram.telegram_flood_wait_count()

    def http(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise telegram.TelegramFloodWait(2.5)
        return 200

    ok, _ = send_telegram(
        "hello", http=http, limiter=telegram._TelegramRateLimiter(), min_gap_s=0,
        sleep_fn=sleeps.append,
    )

    assert ok is True and len(calls) == 2 and sleeps == [2.5]
    assert telegram.telegram_flood_wait_count() == before + 1
    records = [record for record in caplog.records if "flood wait" in record.message]
    assert len(records) == 1 and records[0].exc_info is None


def test_default_http_parses_telegram_retry_after(monkeypatch):
    error = urllib.error.HTTPError(
        "https://api.telegram.org", 429, "Too Many Requests", {},
        io.BytesIO(b'{"ok":false,"parameters":{"retry_after":7}}'),
    )
    monkeypatch.setattr(telegram.urllib.request, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error))

    with pytest.raises(telegram.TelegramFloodWait) as raised:
        telegram._default_http("https://api.telegram.org", {"chat_id": "1"}, 1)

    assert raised.value.retry_after == 7.0


def test_memecoin_token_preferred_over_shared(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("MEMECOIN_TG_BOT_TOKEN", "MEMETOK")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "SHARED")
    monkeypatch.setenv("MEMECOIN_TG_CHAT_ID", "MEMECHAT")
    seen = {}

    def http(url, payload, timeout):
        seen["url"] = url
        return 200

    send_telegram("x", http=http)
    assert "botMEMETOK/" in seen["url"]


def test_dispatcher_enqueue_returns_before_blocking_send_completes():
    started = threading.Event()
    release = threading.Event()

    def blocking_send(_text):
        started.set()
        release.wait(timeout=2.0)
        return True, 1.0

    dispatcher = _TelegramDispatcher(blocking_send, maxsize=2)

    assert dispatcher.enqueue("first") is True
    assert started.wait(timeout=1.0)
    assert release.is_set() is False
    release.set()


def test_dispatcher_queue_saturation_fails_open_without_blocking():
    started = threading.Event()
    release = threading.Event()

    def blocking_send(_text):
        started.set()
        release.wait(timeout=2.0)
        return True, 1.0

    dispatcher = _TelegramDispatcher(blocking_send, maxsize=1)
    assert dispatcher.enqueue("active") is True
    assert started.wait(timeout=1.0)
    assert dispatcher.enqueue("queued") is True
    assert dispatcher.enqueue("dropped") is False
    release.set()

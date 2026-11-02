"""A failed Jupiter /swap build says why, and a transport or server failure gets one other-host try.

CROWD 51cacc9f (2026-09-25 18:30Z) refused twice as "swap build returned nothing" while a host DNS
stall took every endpoint down; the cause was never logged because the build swallowed it.
"""
import logging

import pytest

from sniper.execution import solana_real_seams as S

QUOTE = {"inputMint": "A", "outputMint": "B", "inAmount": "1", "outAmount": "2",
         "routePlan": [{"swapInfo": {}}]}
FAKE_KEY = "test-key-not-a-real-one"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    S._jup_host_cooldown.clear()
    S.JUPITER_LOAD["by_host"] = {}
    monkeypatch.setattr(S, "_KEY_VAR", S.JUP_API_KEY_VAR)
    monkeypatch.delenv(S.JUP_API_KEY_VAR, raising=False)
    monkeypatch.delenv(S.CAPTURE_JUP_API_KEY_VAR, raising=False)
    yield
    S._jup_host_cooldown.clear()


def _http(script, calls):
    """`script[base]` is a list of answers for that host: a (status, json) tuple or an exception."""
    def http(method, url, headers=None, body=None):
        calls.append(url)
        for base, answers in script.items():
            if url.startswith(base):
                answer = answers.pop(0)
                if isinstance(answer, Exception):
                    raise answer
                return answer[0], answer[1], b""
        raise AssertionError("unexpected url " + url)
    return http


def test_a_transport_error_retries_once_on_the_other_host(caplog):
    calls = []
    http = _http({S.JUP_BASE: [OSError("Temporary failure in name resolution")],
                  S.JUP_FALLBACK_BASE: [(200, {"swapTransaction": "b64"})]}, calls)
    with caplog.at_level(logging.WARNING):
        assert S.jupiter_swap_tx(QUOTE, "PUB", http=http) == "b64"
    assert calls == [S.JUP_BASE + "/swap", S.JUP_FALLBACK_BASE + "/swap"]
    assert "OSError on lite-api.jup.ag; retry on api.jup.ag -> ok" in caplog.text


def test_a_5xx_retries_and_a_second_failure_still_refuses(caplog):
    calls = []
    http = _http({S.JUP_BASE: [(502, {})], S.JUP_FALLBACK_BASE: [(503, {})]}, calls)
    with caplog.at_level(logging.WARNING):
        assert S.jupiter_swap_tx(QUOTE, "PUB", http=http) is None
    assert len(calls) == 2
    assert "http_502 on lite-api.jup.ag; retry on api.jup.ag -> http_503" in caplog.text


def test_a_4xx_is_an_answer_and_is_not_retried(caplog):
    calls = []
    http = _http({S.JUP_BASE: [(400, {"error": "bad"})], S.JUP_FALLBACK_BASE: []}, calls)
    with caplog.at_level(logging.WARNING):
        assert S.jupiter_swap_tx(QUOTE, "PUB", http=http) is None
    assert len(calls) == 1
    assert "http_400 on lite-api.jup.ag (not retried" in caplog.text


def test_the_log_never_carries_the_key_or_a_url(caplog, monkeypatch):
    monkeypatch.setenv(S.JUP_API_KEY_VAR, FAKE_KEY)
    calls = []
    http = _http({S.JUP_FALLBACK_BASE: [TimeoutError("read timed out")],
                  S.JUP_BASE: [(200, {"swapTransaction": "b64"})]}, calls)
    with caplog.at_level(logging.WARNING):
        assert S.jupiter_swap_tx(QUOTE, "PUB", http=http) == "b64"
    assert FAKE_KEY not in caplog.text and "https://" not in caplog.text
    assert "TimeoutError on api.jup.ag; retry on lite-api.jup.ag -> ok" in caplog.text

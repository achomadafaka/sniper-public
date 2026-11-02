from __future__ import annotations

import gzip


def _gz(data):
    """gzip with mtime=0: compress() embeds the clock, so parametrize ids built from its bytes
    differed between xdist workers and every -n run of the suite aborted (2026-10-15)."""
    return gzip.compress(data, mtime=0)
import io
import json
import urllib.error
from email.message import Message

import pytest

import bot.supabase_helper as sb


def response(body: bytes, encoding: str | None = None):
    reply = io.BytesIO(body)
    reply.headers = Message()
    if encoding is not None:
        reply.headers["Content-Encoding"] = encoding
    return reply


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.setattr(sb, "SUPABASE_URL", "https://example.invalid")
    monkeypatch.setattr(sb, "SUPABASE_KEY", "test-key")
    sleeps = []
    monkeypatch.setattr(sb.time, "sleep", sleeps.append)
    return sleeps


def test_requests_gzip_without_changing_write_semantics(monkeypatch, transport):
    calls = []
    row = {"id": "fact-1", "statement": "A fact"}

    def urlopen(request, *, timeout):
        calls.append((request, timeout))
        return response(json.dumps([row]).encode())

    monkeypatch.setattr(sb.urllib.request, "urlopen", urlopen)

    assert sb._request(
        "POST", "brain_facts", row,
        {"Prefer": "return=representation"}, timeout=3,
    ) == [row]
    request, timeout = calls[0]
    assert request.get_header("Accept-encoding") == "gzip"
    assert request.get_header("Prefer") == "return=representation"
    assert request.get_header("Authorization") == "Bearer test-key"
    assert request.get_method() == "POST"
    assert json.loads(request.data) == row
    assert timeout == 3
    assert len(calls) == 1
    assert transport == []


def test_decompresses_negotiated_gzip_json(monkeypatch, transport):
    rows = [{"id": "fact-1", "detail": {"text": "Café", "optional": None}}]
    monkeypatch.setattr(sb.urllib.request, "urlopen", lambda *a, **k: response(
        _gz(json.dumps(rows).encode()), " GZip ",
    ))

    assert sb._request("GET", "brain_facts") == rows
    assert transport == []


@pytest.mark.parametrize("raw,encoding,expected", [
    (b'{"count": 0}', None, {"count": 0}),
    (b"", None, None),
    (b"", "gzip", None),
    (_gz(b""), "gzip", None),
])
def test_plain_and_empty_responses(monkeypatch, transport, raw, encoding, expected):
    monkeypatch.setattr(sb.urllib.request, "urlopen", lambda *a, **k: response(raw, encoding))

    assert sb._request("DELETE", "brain_facts?id=eq.fact-1") == expected
    assert transport == []


def test_explicit_identity_encoding_is_preserved(monkeypatch, transport):
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        return response(b"[]")

    monkeypatch.setattr(sb.urllib.request, "urlopen", urlopen)

    assert sb._request("GET", "brain_facts", extra_headers={"Accept-Encoding": "identity"}) == []
    assert calls[0].get_header("Accept-encoding") == "identity"


@pytest.mark.parametrize("raw,encoding,error", [
    (b"not gzip", "gzip", gzip.BadGzipFile),
    (_gz(b"[]")[:-8], "gzip", EOFError),
    (_gz(b"[]"), None, UnicodeDecodeError),
])
def test_bad_compression_is_not_empty_data(monkeypatch, transport, raw, encoding, error):
    calls = []

    def urlopen(request, *, timeout):
        calls.append(request)
        return response(raw, encoding)

    monkeypatch.setattr(sb.urllib.request, "urlopen", urlopen)

    with pytest.raises(error):
        sb._request("GET", "brain_facts")
    assert len(calls) == sb._MAX_RETRIES
    assert transport == [1.5, 3.0]


@pytest.mark.parametrize("encoding", [None, "gzip"])
def test_billing_402_raises_once_with_diagnostics(monkeypatch, transport, encoding):
    calls = []
    raw = b'{"message":"Fair use quota exceeded"}'
    if encoding:
        raw = _gz(raw)
    headers = Message()
    if encoding:
        headers["Content-Encoding"] = encoding

    def urlopen(request, *, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 402, "Payment Required", headers, io.BytesIO(raw))

    monkeypatch.setattr(sb.urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError, match="HTTP 402.*Fair use quota exceeded"):
        sb._request("GET", "brain_facts")
    assert len(calls) == 1
    assert transport == []


@pytest.mark.parametrize("code,expected_calls", [(402, 1), (404, 1), (429, 3), (503, 3)])
def test_corrupt_gzip_error_retains_http_status(monkeypatch, transport, code, expected_calls):
    calls = []
    headers = Message()
    headers["Content-Encoding"] = "gzip"

    def urlopen(request, *, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, code, "Error", headers, io.BytesIO(b"not gzip"))

    monkeypatch.setattr(sb.urllib.request, "urlopen", urlopen)

    with pytest.raises(RuntimeError, match=f"HTTP {code}.*invalid gzip"):
        sb._request("GET", "brain_facts")
    assert len(calls) == expected_calls
    assert transport == ([] if expected_calls == 1 else [1.5, 3.0])


@pytest.mark.parametrize("code", [429, 503])
def test_transient_http_failure_keeps_retry_and_write_body(monkeypatch, transport, code):
    calls = []
    row = {"id": "fact-1", "statement": "A fact"}

    def urlopen(request, *, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, code, "Retry", Message(), io.BytesIO(b"busy"))
        return response(json.dumps([row]).encode())

    monkeypatch.setattr(sb.urllib.request, "urlopen", urlopen)

    assert sb._request("POST", "rpc/brain_write_fact", row) == [row]
    assert len(calls) == 2
    assert calls[0].data == calls[1].data
    assert transport == [1.5]

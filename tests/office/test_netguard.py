"""Offline transport witnesses: no external network or funded resources."""
import socket

import pytest

from sniper.office import netguard as N
from sniper.office.tools import browse as B


PUBLIC = "93.184.216.34"


def dns(*addresses):
    return lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                                    for ip in addresses]


class Wire:
    def __init__(self, response):
        self.response = response
        self.sent = b""
        self.closed = False

    def settimeout(self, value):
        assert value > 0

    def sendall(self, value):
        self.sent += value

    def recv(self, size):
        chunk, self.response = self.response[:size], self.response[size:]
        return chunk

    def close(self):
        self.closed = True


def response(body=b"hello", headers=b"Content-Type: text/plain\r\n", status=b"200 OK"):
    return b"HTTP/1.0 " + status + b"\r\n" + headers + b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body


def install(monkeypatch, *responses, answers=(PUBLIC,)):
    wires = [Wire(r) for r in responses]
    calls = []
    monkeypatch.setattr(N.socket, "getaddrinfo", dns(*answers))

    def connect(target, deadline):
        calls.append(target)
        return wires[len(calls) - 1]

    monkeypatch.setattr(N, "connect", connect)
    return wires, calls


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1", "http://[::1]", "http://169.254.169.254/", "http://user:pass@public.test", "http://public.test:22", "ws://public.test", "http://public.test/\r\nX: yes"])
def test_nonpublic_or_credentialled_url_denied(monkeypatch, url):
    monkeypatch.setattr(N.socket, "getaddrinfo", dns(PUBLIC))
    assert N.allowed(url) is False


def test_mixed_dns_answers_fail_closed(monkeypatch):
    monkeypatch.setattr(N.socket, "getaddrinfo", dns(PUBLIC, "10.0.0.1"))
    assert N.allowed("https://public.test") is False


def test_redirect_to_localhost_denied(monkeypatch):
    _, calls = install(monkeypatch, response(headers=b"Location: http://127.0.0.1/\r\n", status=b"302 Found"))
    result = B.fetch("http://public.test", clock_fn=lambda: 123.)
    assert result.outcome == "refused"
    assert len(calls) == 1


def test_dns_rebinding_to_private_denied(monkeypatch):
    _, calls = install(monkeypatch, response(headers=b"Location: /next\r\n", status=b"302 Found"))
    answers = iter([dns(PUBLIC), dns("10.0.0.1")])
    monkeypatch.setattr(N.socket, "getaddrinfo", lambda *a, **kw: next(answers)(*a, **kw))
    result = B.fetch("http://public.test", clock_fn=lambda: 123.)
    assert result.outcome == "refused"
    assert len(calls) == 1


def test_profile_not_reused_and_no_subrequests(monkeypatch):
    html = b'<html><script>fetch("http://127.0.0.1")</script><img src="http://127.0.0.1"><p>read me</p></html>'
    wires, calls = install(monkeypatch, response(html, b"Content-Type: text/html\r\nSet-Cookie: secret=x\r\n"), response())
    first = B.fetch("http://public.test", clock_fn=lambda: 123.)
    second = B.fetch("http://public.test", clock_fn=lambda: 124.)
    assert first.result["text"] == "read me"
    assert first.result["type"] == "quoted_data"
    assert second.result["received_at"] == 124.
    assert len(calls) == 2
    assert all(b"Cookie:" not in w.sent and b"Authorization:" not in w.sent and w.closed for w in wires)


def test_download_never_executed_or_saved(monkeypatch, tmp_path):
    wires, _ = install(monkeypatch, response(b"#!/bin/sh\ntouch PWNED", b"Content-Type: text/plain\r\nContent-Disposition: attachment; filename=evil.sh\r\n"))
    monkeypatch.chdir(tmp_path)
    result = B.fetch("http://public.test", clock_fn=lambda: 123.)
    assert result.outcome == "refused"
    assert result.result["reason"] == "download_denied"
    assert not list(tmp_path.iterdir())
    assert wires[0].closed


def test_large_body_and_transport_failure_are_not_empty_success(monkeypatch):
    install(monkeypatch, response(b"x" * (B.MAX_BYTES + 1)))
    assert B.fetch("http://public.test", clock_fn=lambda: 123.).outcome == "refused"
    monkeypatch.setattr(N, "connect", lambda *a: (_ for _ in ()).throw(TimeoutError()))
    assert B.fetch("http://public.test", clock_fn=lambda: 123.).outcome == "timeout"


def test_actual_socket_is_pinned_to_validated_ip(monkeypatch):
    monkeypatch.setattr(N.socket, "getaddrinfo", dns(PUBLIC))
    target = N.resolve("http://public.test/")
    wire = Wire(b"")
    connected = []
    wire.connect = connected.append
    monkeypatch.setattr(N.socket, "socket", lambda *a: wire)
    assert N.connect(target, N.time.monotonic() + 2) is wire
    assert connected == [(PUBLIC, 80)]
    assert target.host == "public.test"


def test_https_keeps_original_hostname_for_certificate_validation(monkeypatch):
    monkeypatch.setattr(N.socket, "getaddrinfo", dns(PUBLIC))
    target = N.resolve("https://public.test/")
    wire = Wire(b"")
    connected, names = [], []
    wire.connect = connected.append
    monkeypatch.setattr(N.socket, "socket", lambda *a: wire)

    class TLS:
        def wrap_socket(self, sock, *, server_hostname):
            names.append(server_hostname)
            return sock

    monkeypatch.setattr(N.ssl, "create_default_context", lambda: TLS())
    assert N.connect(target, N.time.monotonic() + 2) is wire
    assert connected == [(PUBLIC, 443)]
    assert names == ["public.test"]


@pytest.mark.parametrize("raw,expected", [
    (response(headers=b"Content-Type: application/octet-stream\r\n"), "refused"),
    (response(headers=b"Content-Type: text/plain\r\nContent-Encoding: gzip\r\n"), "refused"),
    (b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 8\r\n\r\na", "refused"),
    (response(status=b"429 Too Many Requests"), "rate_limited"),
    (b"garbage\r\n", "refused"),
])
def test_bad_or_partial_responses_never_become_known_empty(monkeypatch, raw, expected):
    install(monkeypatch, raw)
    assert B.fetch("http://public.test", clock_fn=lambda: 123.).outcome == expected


def test_nonpublic_ipv6_site_local_and_zero_port_denied(monkeypatch):
    monkeypatch.setattr(N.socket, "getaddrinfo", dns(PUBLIC))
    assert N.allowed("http://[fec0::1]/") is False
    assert N.allowed("http://public.test:0/") is False


def test_dns_failure_not_success(monkeypatch):
    monkeypatch.setattr(N.socket, "getaddrinfo", lambda *a, **kw: (_ for _ in ()).throw(socket.gaierror()))
    assert N.allowed("http://public.test") is False
    assert B.fetch("http://public.test", clock_fn=lambda: 123.).outcome == "unknown"


def test_public_redirect_rechecks_dns_and_labels_unrendered_coverage(monkeypatch):
    wires, calls = install(monkeypatch, response(headers=b"Location: https://next.test/page\r\n", status=b"302 Found"), response())
    result = B.fetch("http://public.test", clock_fn=lambda: 123.)
    assert result.outcome == "success"
    assert result.result["source"] == "https://next.test/page"
    assert result.coverage == "single_public_document_text_only_unrendered"
    assert [target.host for target in calls] == ["public.test", "next.test"]
    assert all(w.closed for w in wires)


def test_file_url_never_reaches_transport(monkeypatch):
    _, calls = install(monkeypatch)
    assert B.fetch("file:///etc/passwd", clock_fn=lambda: 123.).outcome == "refused"
    assert calls == []


def test_tool_runs_only_through_broker_receipt(monkeypatch, tmp_path):
    from sniper.office import broker, capabilities, store
    install(monkeypatch, response())
    cut = {"basis": "corrected_utc", "receipt_cut": 120., "knowledge_cut": 120., "slot_cut": None,
           "clock_sample": {"corrected_utc": 120., "offset_s": 0.}}
    context = capabilities.Context("scout1", "scout", "office", 1, None, cut)
    conn = store.open_store(tmp_path / "office.db")
    host = broker.Broker(conn, None, {"browse": B.tool(clock_fn=lambda: 123.)}, generation_check=lambda ctx: True)
    receipt = B.browse("http://public.test", actor_id="scout1", broker=host, context=context)
    assert isinstance(receipt, broker.Receipt)
    assert receipt.result == {"type": "quoted_data", "text": "hello", "source": "http://public.test", "received_at": 123.}
    assert receipt.outcome == "success"
    assert conn.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    refused = B.browse("http://public.test", actor_id="head", broker=host, context=context)
    assert refused.outcome == "refused"
    conn.close()

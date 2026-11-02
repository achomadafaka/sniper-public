"""Broker-only, text-only research browser. No renderer, cookies, disk, or subprocess.

Each call has an empty in-memory profile. Embedded requests and WebSockets cannot
run. This intentionally refuses compressed/chunked/binary responses; use a separately
reviewed isolated browser when rendered pages are required.
"""
from email.parser import BytesHeaderParser
from html.parser import HTMLParser
import math
import socket
import time
from urllib.parse import urljoin

from sniper.office import broker as B, netguard as N

MAX_BYTES = 512 * 1024
MAX_HEADERS = 16 * 1024
MAX_REDIRECTS = 5
TIMEOUT_S = 10.


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "template"}:
            self.hidden += 1
        elif not self.hidden and tag in {"p", "br", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "template"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _recv(sock, deadline, size):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    sock.settimeout(remaining)
    return sock.recv(size)


def _request(target, deadline):
    sock = N.connect(target, deadline)
    try:
        host = f"[{target.host}]" if ":" in target.host else target.host
        request = (f"GET {target.path} HTTP/1.0\r\nHost: {host}\r\n"
                   "User-Agent: Sniper-Office-Research/1\r\nAccept: text/html,text/plain,application/xhtml+xml\r\n"
                   "Accept-Encoding: identity\r\nConnection: close\r\n\r\n")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        sock.settimeout(remaining)
        sock.sendall(request.encode("ascii"))
        raw = b""
        while b"\r\n\r\n" not in raw:
            chunk = _recv(sock, deadline, min(4096, MAX_HEADERS + 4 - len(raw)))
            if not chunk:
                raise N.Denied("response_invalid")
            raw += chunk
            if b"\r\n\r\n" not in raw and len(raw) >= MAX_HEADERS:
                raise N.Denied("headers_too_large")
        header, body = raw.split(b"\r\n\r\n", 1)
        if len(header) > MAX_HEADERS:
            raise N.Denied("headers_too_large")
        status, fields = header.split(b"\r\n", 1)
        pieces = status.split(b" ", 2)
        if pieces[0] not in {b"HTTP/1.0", b"HTTP/1.1"} or len(pieces) < 2 or not pieces[1].isdigit():
            raise N.Denied("response_invalid")
        status = int(pieces[1])
        headers = BytesHeaderParser().parsebytes(fields + b"\r\n\r\n")
        if headers.defects:
            raise N.Denied("response_invalid")
        if status in {301, 302, 303, 307, 308}:
            locations = headers.get_all("Location", [])
            if len(locations) != 1:
                raise N.Denied("redirect_invalid")
            return status, locations[0], None
        if status == 429:
            return status, None, None
        if status != 200:
            raise N.Denied("http_status_refused")
        if headers.get_content_disposition() == "attachment":
            raise N.Denied("download_denied")
        mime = headers.get_content_type()
        if mime not in {"text/plain", "text/html", "application/xhtml+xml"}:
            raise N.Denied("content_type_denied")
        if headers.get("Transfer-Encoding") or headers.get("Content-Encoding", "identity").lower() != "identity":
            raise N.Denied("encoding_denied")
        lengths = headers.get_all("Content-Length", [])
        if len(lengths) > 1 or (lengths and not lengths[0].isdigit()):
            raise N.Denied("length_invalid")
        length = int(lengths[0]) if lengths else None
        if length is not None and length > MAX_BYTES:
            raise N.Denied("body_too_large")
        while length is None or len(body) < length:
            chunk = _recv(sock, deadline, min(65536, MAX_BYTES + 1 - len(body)))
            if not chunk:
                break
            body += chunk
            if len(body) > MAX_BYTES:
                raise N.Denied("body_too_large")
        if len(body) > MAX_BYTES or (length is not None and len(body) != length):
            raise N.Denied("body_incomplete")
        try:
            text = body.decode(headers.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            raise N.Denied("charset_denied") from None
        if mime != "text/plain":
            parser = _Text()
            parser.feed(text)
            text = " ".join("".join(parser.parts).split())
        return status, None, text
    finally:
        sock.close()


def fetch(url, *, clock_fn):
    """Host handler; only Broker.execute authenticates actors and persists a Receipt.

    Socket reads share a total deadline. OS DNS resolution has its OS timeout;
    an expired deadline is checked before any socket is opened after DNS returns.
    """
    deadline = time.monotonic() + TIMEOUT_S
    try:
        for hop in range(MAX_REDIRECTS + 1):
            target = N.resolve(url)
            status, location, text = _request(target, deadline)
            if location is not None:
                if hop == MAX_REDIRECTS:
                    raise N.Denied("redirect_limit")
                url = urljoin(url, location)
                continue
            if status == 429:
                return B.ToolResult({"reason": "http_rate_limited"}, {}, None, [], "unknown", outcome="rate_limited")
            received_at = clock_fn()
            if type(received_at) not in (int, float) or not math.isfinite(received_at) or received_at < 0:
                raise N.Denied("clock_unknown")
            return B.ToolResult(B.quote_external(text, url, received_at), {"text": "untrusted_text"}, None,
                                [url], "single_public_document_text_only_unrendered", outcome="success" if text else "empty")
    except N.Denied as exc:
        return B.ToolResult({"reason": str(exc)}, {}, None, [], "unknown", outcome="refused")
    except (TimeoutError, socket.timeout):
        return B.ToolResult({"reason": "transport_timeout"}, {}, None, [], "unknown", outcome="timeout")
    except (OSError, ValueError, UnicodeError):
        return B.ToolResult({"reason": "transport_unknown"}, {}, None, [], "unknown", outcome="unknown")


def tool(*, clock_fn):
    return B.Tool(lambda args, context: fetch(args["url"], clock_fn=clock_fn), {"url": str}, "office-browse-text-v1")


def browse(url, *, actor_id, broker, context):
    return broker.execute(B.ToolRequest(actor_id, "browse", {"url": url}, context.decision_id, context.cut), context=context)

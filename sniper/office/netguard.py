"""Public-only, pinned-IP transport. No process-global resolver or proxy changes."""
from dataclasses import dataclass
import ipaddress
import socket
import ssl
import time
from urllib.parse import urlsplit


class Denied(ValueError):
    pass


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    tls: bool
    path: str
    family: int
    address: tuple


def _public(address):
    ip = ipaddress.ip_address(address)
    # IPv6 translation/tunnel forms can route an apparently public address internally.
    return (ip.is_global and not ip.is_multicast and not ip.is_unspecified
            and not getattr(ip, "is_site_local", False)
            and not (isinstance(ip, ipaddress.IPv6Address)
                     and (ip.ipv4_mapped is not None or ip.sixtofour is not None
                          or ip.teredo is not None or ip in ipaddress.ip_network("64:ff9b::/96"))))


def resolve(url):
    if (not isinstance(url, str) or not url or len(url) > 8192
            or any(ord(c) <= 32 or ord(c) >= 127 for c in url) or "\\" in url):
        raise Denied("url_denied")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        tls = parsed.scheme == "https"
        port = parsed.port if parsed.port is not None else (443 if tls else 80)
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username is not None
                or parsed.password is not None or port != (443 if tls else 80) or "%" in host):
            raise Denied("url_denied")
        # Literal addresses are checked before DNS too (including deceptive test resolvers).
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not _public(str(literal)):
            raise Denied("address_denied")
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
        if not answers or len(answers) > 32:
            raise Denied("dns_denied")
        for family, socktype, proto, _, address in answers:
            if family not in {socket.AF_INET, socket.AF_INET6} or not _public(address[0]):
                raise Denied("address_denied")
        family, _, _, _, address = answers[0]
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return Target(host, port, tls, path, family, address)
    except (ValueError, UnicodeError) as exc:
        if isinstance(exc, Denied):
            raise
        raise Denied("url_denied") from None


def allowed(url):
    """Preflight only; callers must resolve again at the actual connection boundary."""
    try:
        resolve(url)
        return True
    except (Denied, OSError):
        return False


def connect(target, deadline):
    """Dial the validated numeric sockaddr directly; TLS verifies the original hostname."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    sock = socket.socket(target.family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
    try:
        sock.settimeout(remaining)
        sock.connect(target.address)
        if target.tls:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            sock.settimeout(remaining)
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=target.host)
        return sock
    except BaseException:
        sock.close()
        raise

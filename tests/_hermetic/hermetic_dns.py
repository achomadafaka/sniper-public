"""Tests never resolve a real hostname (2026-10-01).

Imported by tests/conftest.py and, through tests/_hermetic/sitecustomize.py, by every child Python
process a test starts (conftest puts this directory first on PYTHONPATH). This host's resolver takes
~20 s to answer NXDOMAIN; a test that resolved a fake RPC host passed pytest-timeout's 60 s and the
worker was killed, refusing the deploy of 84a3bdd2a. A non-local name is refused at once with the
socket.gaierror such a lookup ends in anyway. SNIPER_TESTS_ALLOW_DNS=1 opts out.
"""
import ipaddress
import os
import socket

_REAL_GETADDRINFO = socket.getaddrinfo
_LOCAL_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


def is_local(host):
    if host is None:
        return True
    name = host.decode() if isinstance(host, (bytes, bytearray)) else str(host)
    name = name.strip("[]").rstrip(".").lower()
    if not name or name in _LOCAL_HOSTNAMES or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def hermetic_getaddrinfo(host, *args, **kwargs):
    if is_local(host):
        return _REAL_GETADDRINFO(host, *args, **kwargs)
    raise socket.gaierror(socket.EAI_NONAME, "tests are hermetic: DNS for %r refused "
                          "(SNIPER_TESTS_ALLOW_DNS=1 allows a live run)" % (host,))


hermetic_getaddrinfo.sniper_hermetic = True


def install():
    if os.environ.get("SNIPER_TESTS_ALLOW_DNS") == "1":
        return
    socket.getaddrinfo = hermetic_getaddrinfo

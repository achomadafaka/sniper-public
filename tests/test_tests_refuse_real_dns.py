"""Tests never resolve a real hostname (conftest guard, 2026-10-01).

The deploy of 84a3bdd2a was refused at 99% because this host's resolver answers NXDOMAIN in ~20 s:
a test resolving a fake RPC hostname passed pytest-timeout's 60 s and the worker was killed. The
guard refuses a non-local name at once with the gaierror the lookup would end in anyway.
"""
import socket
import time

import pytest


def test_a_non_local_name_is_refused_at_once():
    started = time.monotonic()
    with pytest.raises(socket.gaierror, match="tests are hermetic"):
        socket.getaddrinfo("rpc.example", 443)
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo(b"mainnet.helius-rpc.com", 443)
    assert time.monotonic() - started < 0.5


def test_local_names_and_ip_literals_still_resolve():
    for host in ("localhost", "127.0.0.1", "::1", "LOCALHOST.", None):
        assert socket.getaddrinfo(host, 80)


def test_the_guard_is_the_installed_resolver():
    assert getattr(socket.getaddrinfo, "sniper_hermetic", False) is True


def test_urllib_fails_fast_on_a_fake_rpc_host():
    import urllib.error
    import urllib.request
    started = time.monotonic()
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen("https://fake.rpc.test/", timeout=30)
    assert time.monotonic() - started < 0.5


def test_a_child_python_process_inherits_the_refusal():
    """Gamma LOW: a child re-imports socket unpatched; its lookup waited 10 s before the guard reached it."""
    import os
    import subprocess
    import sys
    child = ("import socket\n"
             "try:\n"
             "    socket.getaddrinfo('nonexistent-host.invalid', 80)\n"
             "except socket.gaierror as exc:\n"
             "    print(exc)\n")
    started = time.monotonic()
    out = subprocess.run([sys.executable, "-c", child], env=dict(os.environ), capture_output=True,
                         text=True, timeout=30)
    assert "tests are hermetic" in out.stdout, out.stdout + out.stderr
    assert time.monotonic() - started < 5.0

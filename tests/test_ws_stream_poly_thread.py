"""The upstream Polymarket price socket starts with the web app, not at import (2026-09-29).

`sniper.routes.ws_stream` started its `poly-ws-prices` thread at import. conftest imports the web
server, so every pytest xdist worker ran a live upstream socket loop for the whole run; it was alive
in the worker whose fake clock ran out and refused the deploy of 0aaab9a5. The loop sleeps on the
process-wide time.sleep and, connected, reads the process-wide time.monotonic on every message, so a
test that patches either fed it or was fed on by it. The thread now starts in `register_ws_stream`
(the web server calls it at import, as before), once, and conftest switches the autostart off.
"""
import subprocess
import sys
import threading
from pathlib import Path

import sniper.routes.ws_stream as ws

REPO = Path(__file__).resolve().parents[1]


def test_importing_the_module_starts_no_thread():
    """A fresh interpreter: the import alone must not start the upstream socket."""
    code = ("import threading, sniper.routes.ws_stream as w; "
            "print(w._poly_ws_thread is None, "
            "any(t.name == 'poly-ws-prices' for t in threading.enumerate()))")
    out = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True,
                         text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    assert out.stdout.split() == ["True", "False"], out.stdout


def test_the_starter_starts_one_thread_once(monkeypatch):
    ran = threading.Event()
    monkeypatch.setattr(ws, "_poly_ws_price_loop", ran.set)
    monkeypatch.setattr(ws, "_poly_ws_thread", None)
    monkeypatch.setattr(ws, "_has_sync_websockets", lambda: True)
    first = ws._start_poly_ws_thread(force=True)
    assert first is not None and first.name == "poly-ws-prices"
    assert ws._start_poly_ws_thread(force=True) is first, "idempotent: one upstream socket"
    first.join(2.0)
    assert ran.is_set()


def test_without_websockets_no_thread_starts(monkeypatch):
    monkeypatch.setattr(ws, "_poly_ws_thread", None)
    monkeypatch.setattr(ws, "_has_sync_websockets", lambda: False)
    assert ws._start_poly_ws_thread(force=True) is None


def test_pytest_workers_run_no_upstream_socket():
    """conftest switches the autostart off before it imports the web server."""
    assert ws._POLY_WS_AUTOSTART is False
    assert ws._start_poly_ws_thread() is None
    assert not any(t.name == "poly-ws-prices" for t in threading.enumerate())


def test_the_web_app_registration_starts_the_upstream_socket(monkeypatch):
    """Production is unchanged: sniper/web/server.py calls register_ws_stream at its import."""
    calls = []
    monkeypatch.setattr(ws, "_start_poly_ws_thread", lambda *a, **k: calls.append(1))

    class _Sock:
        def route(self, _path):
            return lambda fn: fn
    ws.register_ws_stream(object(), _Sock())
    assert calls == [1]
    src = (REPO / "sniper/web/server.py").read_text()
    assert "register_ws_stream(app, sock)" in src

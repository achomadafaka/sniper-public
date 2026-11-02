"""The two rules that must never silently break: a full list refuses, and the server decides."""
import importlib.util
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "j7_watchlist.py")
_SPEC = importlib.util.spec_from_file_location("j7_watchlist", _PATH)
j7 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(j7)


class _Args:
    def __init__(self, **kw):
        self.kind = kw.get("kind", "fomo")
        self.target = kw.get("target", "someone")
        self.account = kw.get("account", 1)
        self.apply = kw.get("apply", False)


def _rows(n, handle="other"):
    return [{"handle": "%s%d" % (handle, i), "fomo_user_id": "uuid-%d" % i} for i in range(n)]


def test_a_full_list_refuses_instead_of_evicting(monkeypatch):
    monkeypatch.setattr(j7, "fetch", lambda kind, account: (_rows(50), 50))
    monkeypatch.setattr(j7, "_request", lambda *a, **k: pytest.fail("must not write"))
    assert j7._write(_Args(apply=True), "add") == 1


def test_adding_a_wallet_already_present_is_a_no_op(monkeypatch):
    monkeypatch.setattr(j7, "fetch", lambda kind, account: (_rows(3) + [
        {"handle": "airtightfish", "fomo_user_id": "ddd41488"}], 50))
    monkeypatch.setattr(j7, "_request", lambda *a, **k: pytest.fail("must not write"))
    assert j7._write(_Args(target="airtightfish", apply=True), "add") == 0


def test_a_dry_run_never_writes(monkeypatch):
    monkeypatch.setattr(j7, "fetch", lambda kind, account: (_rows(3), 50))
    monkeypatch.setattr(j7, "_request", lambda *a, **k: pytest.fail("dry run must not write"))
    assert j7._write(_Args(apply=False), "add") == 0


def test_the_server_reply_decides_the_outcome(monkeypatch, tmp_path):
    monkeypatch.setattr(j7, "fetch", lambda kind, account: (_rows(3), 50))
    monkeypatch.setattr(j7, "_token", lambda account: "t")
    monkeypatch.setattr(j7, "CHANGE_LOG", str(tmp_path / "changes.jsonl"))
    # The server says the target is NOT there after an add: that is a failure, not a success.
    monkeypatch.setattr(j7, "_request", lambda *a, **k: {"fomo_users": _rows(3), "limit": 50})
    assert j7._write(_Args(target="newname", apply=True), "add") == 1
    # And when it IS there, the add is confirmed.
    monkeypatch.setattr(j7, "_request", lambda *a, **k: {
        "fomo_users": _rows(3) + [{"handle": "newname", "fomo_user_id": "u"}], "limit": 50})
    assert j7._write(_Args(target="newname", apply=True), "add") == 0

"""Heartbeat identity belongs to the resolved loaded release, not a later pointer."""
import json
from pathlib import Path

import pytest

from test_memecoin_desk_pumpswap_worker import W, worker


def heartbeat(w):
    w.heartbeat(True)
    return json.loads((w.base / "universe.heartbeat").read_text())


def test_release_identity_is_static_when_the_code_root_pointer_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "ROOT", Path("/pinned/releases/" + "a" * 40))
    w = worker(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(W, "ROOT", Path("/pinned/releases/" + "b" * 40))
        beat = heartbeat(w)
        assert beat["release_sha"] == "a" * 40
        assert beat["ps_keep_window_s"] == 660
        assert beat["ps_keep_widen_end"] is None
    finally:
        for db in w.files._open.values():
            db.close()


@pytest.mark.parametrize("name", ["Sniper", "deadbeef", "F" * 40, "g" * 40])
def test_unpinned_or_invalid_root_identity_is_unknown(tmp_path, monkeypatch, name):
    monkeypatch.setattr(W, "ROOT", Path("/unverified/" + name))
    w = worker(tmp_path, monkeypatch)
    try:
        assert heartbeat(w)["release_sha"] is None
    finally:
        for db in w.files._open.values():
            db.close()

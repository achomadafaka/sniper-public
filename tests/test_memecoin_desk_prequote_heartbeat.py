"""The desk prequote book's taken/missed counters reach the runner heartbeat (Alpha 2026-10-23, for Gamma)."""
from types import SimpleNamespace

from sniper.services import memecoin_desk_prequote as pq
from sniper.services import memecoin_runner as run


def test_prequote_counters_reach_the_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setitem(pq._BOOK, "book", SimpleNamespace(
        stats={"quotes": 9, "quote_errors": 1, "taken": 3, "missed": 2, "pass_errors": 0, "private": 5}))
    path = str(tmp_path / "hb.json")
    run.write_heartbeat({"events": 1, "opened": 0}, auth_ok=True, path=path, desk_prequote=pq.stats())
    assert run.read_heartbeat(path)["desk_prequote"] == {
        "quotes": 9, "quote_errors": 1, "taken": 3, "missed": 2, "pass_errors": 0}


def test_no_book_writes_no_prequote_block(tmp_path, monkeypatch):
    monkeypatch.setitem(pq._BOOK, "book", None)
    path = str(tmp_path / "hb.json")
    run.write_heartbeat({"events": 1, "opened": 0}, auth_ok=True, path=path, desk_prequote=pq.stats())
    assert "desk_prequote" not in run.read_heartbeat(path)

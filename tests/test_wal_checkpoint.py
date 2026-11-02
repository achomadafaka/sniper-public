import sqlite3

from scripts import wal_checkpoint as wal


def test_large_fully_checkpointed_wal_does_not_warn(monkeypatch, tmp_path, capsys):
    db_path = tmp_path / "tracker.db"
    sqlite3.connect(db_path).close()
    monkeypatch.setattr(wal, "_wal_mb", lambda _path: 161.0)
    monkeypatch.setattr(wal, "_checkpoint", lambda _conn, _mode: (0, 40_901, 40_901))

    assert wal.checkpoint_db(db_path) == 0
    assert "WARN" not in capsys.readouterr().out

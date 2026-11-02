import sqlite3

from scripts import wal_checkpoint


def _seed_wal(tmp_path):
    path = tmp_path / "state.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE facts(value TEXT)")
    conn.execute("INSERT INTO facts(value) VALUES ('seed')")
    conn.commit()
    conn.close()
    return path


def test_reclaim_requires_explicit_cli_flag():
    assert wal_checkpoint._parse_args([]).reclaim is False
    assert wal_checkpoint._parse_args(["--reclaim"]).reclaim is True


def test_default_checkpoint_never_uses_blocking_modes(tmp_path, monkeypatch):
    path = _seed_wal(tmp_path)
    modes = []

    def checkpoint(_conn, mode, timeout=30):
        modes.append((mode, timeout))
        return (0, 1, 1)

    monkeypatch.setattr(wal_checkpoint, "_checkpoint", checkpoint)
    monkeypatch.setattr(wal_checkpoint, "_wal_mb", lambda _path: 200.0)

    assert wal_checkpoint.checkpoint_db(path) == 0
    assert modes == [("PASSIVE", 30)]


def test_reclaim_mode_may_escalate(tmp_path, monkeypatch):
    path = _seed_wal(tmp_path)
    modes = []

    def checkpoint(_conn, mode, timeout=30):
        modes.append((mode, timeout))
        return (0, 1, 1)

    monkeypatch.setattr(wal_checkpoint, "_checkpoint", checkpoint)
    monkeypatch.setattr(wal_checkpoint, "_wal_mb", lambda _path: 200.0)

    assert wal_checkpoint.checkpoint_db(path, reclaim=True) == 0
    assert modes == [("PASSIVE", 30), ("RESTART", 15), ("TRUNCATE", 2)]

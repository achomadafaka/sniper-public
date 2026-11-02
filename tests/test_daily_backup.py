import sqlite3
import gzip
import shutil
from pathlib import Path

from scripts.daily_backup import create_backup


ROOT = Path(__file__).resolve().parents[1]


def _source(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany("INSERT INTO facts(value) VALUES(?)", [("a",), ("b",), ("c",)])
    conn.commit()
    conn.close()


def test_online_backup_is_verified_atomic_and_retains_two(tmp_path):
    source = tmp_path / "sniper_tracker.db"
    backups = tmp_path / "backups"
    _source(source)

    for stamp in ("20260728T010000Z", "20260728T020000Z", "20260728T030000Z"):
        result = create_backup(
            source, backups, keep=2, compress=False, timestamp=stamp, max_wal_bytes=64 << 20,
        )
        assert result["integrity"] == "ok"

    paths = sorted(backups.glob("sniper_tracker-*.db"))
    assert [path.name for path in paths] == [
        "sniper_tracker-20260728T020000Z.db",
        "sniper_tracker-20260728T030000Z.db",
    ]
    conn = sqlite3.connect(paths[-1])
    assert conn.execute("SELECT value FROM facts ORDER BY id").fetchall() == [
        ("a",), ("b",), ("c",),
    ]
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    assert not list(backups.glob("*.tmp*"))


def test_daily_backup_wrapper_is_repo_relative_and_never_stops_services():
    source = (ROOT / "scripts" / "daily_backup.sh").read_text()
    assert "/home/sniper" not in source
    assert "systemctl" not in source
    assert "wal_checkpoint" not in source
    assert "daily_backup.py" in source


def test_compressed_backup_round_trips_to_a_valid_database(tmp_path):
    source = tmp_path / "sniper_tracker.db"
    backups = tmp_path / "backups"
    restored = tmp_path / "restored.db"
    _source(source)

    result = create_backup(
        source, backups, keep=2, compress=True,
        timestamp="20260728T040000Z", max_wal_bytes=64 << 20,
    )

    archive = Path(result["path"])
    with gzip.open(archive, "rb") as compressed, restored.open("wb") as output:
        shutil.copyfileobj(compressed, output)
    conn = sqlite3.connect(restored)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 3
    conn.close()

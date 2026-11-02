import sqlite3

from scripts.repair_memecoin_holder_history import repair_history


def _db(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE memecoin_holder_evidence_history(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        evidence_state TEXT NOT NULL
    )""")
    conn.executemany(
        "INSERT INTO memecoin_holder_evidence_history(evidence_state) VALUES(?)",
        [("unknown",), ("positive",), ("unknown",), ("zero",), ("unknown",)],
    )
    conn.commit()
    conn.close()


def test_holder_history_cleanup_is_dry_run_by_default(tmp_path):
    path = tmp_path / "tracker.db"
    _db(path)

    result = repair_history(path, apply=False, batch_size=2, vacuum=False)

    assert result == {"unknown_before": 3, "deleted": 0, "unknown_after": 3}


def test_holder_history_cleanup_deletes_only_unknown_in_bounded_ranges(tmp_path):
    path = tmp_path / "tracker.db"
    _db(path)

    result = repair_history(path, apply=True, batch_size=2, vacuum=True)

    conn = sqlite3.connect(path)
    states = [row[0] for row in conn.execute(
        "SELECT evidence_state FROM memecoin_holder_evidence_history ORDER BY id",
    )]
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    conn.close()
    assert result == {"unknown_before": 3, "deleted": 3, "unknown_after": 0}
    assert states == ["positive", "zero"]
    assert integrity == "ok"

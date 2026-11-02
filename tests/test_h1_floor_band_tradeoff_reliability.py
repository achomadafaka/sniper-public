import sqlite3

from scripts.research import h1_floor_band_tradeoff as study


def test_cohort_counts_missing_classification_and_uses_source_age():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_entry_attempts (attempt_id TEXT, network_id TEXT, "
                 "ticker TEXT, classification_json TEXT, source_created_at TEXT, "
                 "decision_started_at TEXT)")
    conn.execute("CREATE TABLE memecoin_positions (id INTEGER, attempt_id TEXT, "
                 "execution_mode TEXT, opened_at TEXT, realized_pnl REAL, is_open INTEGER, "
                 "their_size_usd REAL)")
    for i, classification in enumerate((None,
            '{"facts":{"volume_h1":5000,"effective_mcap":100000}}')):
        conn.execute("INSERT INTO memecoin_entry_attempts VALUES (?,?,?,?,?,?)",
                     (str(i), "4663", "X", classification, "2026-09-24T12:00:00+00:00",
                      "2026-09-24T12:00:01+00:00"))
        conn.execute("INSERT INTO memecoin_positions VALUES (?,?,?,?,?,?,?)",
                     (i, str(i), "real", "2026-09-24T12:00:02+00:00", None, 1, 100))
    rows, coverage = study.admitted_rows(conn, "2026-09-24")
    assert coverage == {"no_gate_facts": 1}
    assert len(rows) == 1
    assert rows[0]["organic"] == 5000

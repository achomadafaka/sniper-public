"""Tests for data/backtest/schema.py — DuckDB schema + connection helper."""
import duckdb

from data.backtest.schema import SCHEMA_VERSION, apply_schema, open_backtest_db


def _tables(con):
    return {row[0] for row in con.execute("SHOW TABLES").fetchall()}


def test_apply_schema_creates_all_tables(tmp_path):
    db = tmp_path / "t.db"
    con = duckdb.connect(str(db))
    apply_schema(con)
    tables = _tables(con)
    expected = {
        "gamma_events",
        "prices_history",
        "trades",
        "sackmann_matches",
        "event_alignment",
        "schema_version",
    }
    missing = expected - tables
    assert not missing, f"missing tables: {missing}"


def test_apply_schema_is_idempotent(tmp_path):
    db = tmp_path / "t.db"
    con = duckdb.connect(str(db))
    apply_schema(con)
    apply_schema(con)  # second call must not raise
    version = con.execute("SELECT version FROM schema_version ORDER BY applied_at DESC LIMIT 1").fetchone()[0]
    assert version == SCHEMA_VERSION


def test_gamma_events_has_expected_columns(tmp_path):
    db = tmp_path / "t.db"
    con = duckdb.connect(str(db))
    apply_schema(con)
    cols = {r[0] for r in con.execute("DESCRIBE gamma_events").fetchall()}
    expected = {"condition_id", "title", "end_date", "token_id_yes", "token_id_no", "tour_prefix", "event_slug", "start_date"}
    assert expected <= cols, f"missing columns: {expected - cols}"


def test_trades_uses_row_hash_primary_key(tmp_path):
    db = tmp_path / "t.db"
    con = duckdb.connect(str(db))
    apply_schema(con)
    # row_hash is surrogate PK so multiple fills with same transaction_hash don't collide
    con.execute("""
        INSERT INTO trades (row_hash, condition_id, transaction_hash, asset, timestamp_sec, outcome, price, size, side)
        VALUES ('h1', 'cid', '0xtx', 'assetA', 100, 'YES', 0.5, 1.0, 'BUY'),
               ('h2', 'cid', '0xtx', 'assetA', 100, 'YES', 0.5, 2.0, 'BUY')
    """)
    n = con.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert n == 2  # both rows inserted; PK is row_hash, not transaction_hash


def test_prices_history_pk_allows_same_token_ts_different_fidelity(tmp_path):
    db = tmp_path / "t.db"
    con = duckdb.connect(str(db))
    apply_schema(con)
    con.execute("""
        INSERT INTO prices_history (token_id, timestamp_sec, price, fidelity)
        VALUES ('t1', 1700000000, 0.5, 1),
               ('t1', 1700000000, 0.5, 60)
    """)
    n = con.execute("SELECT COUNT(*) FROM prices_history").fetchone()[0]
    assert n == 2


def test_open_backtest_db_creates_file(tmp_path):
    path = tmp_path / "sub" / "backtest.db"
    con = open_backtest_db(str(path))
    assert path.exists()
    # verify schema applied
    assert "gamma_events" in _tables(con)
    con.close()


def test_gamma_events_timestamp_accepts_gamma_iso_with_z(tmp_path):
    db = tmp_path / "t.db"
    con = duckdb.connect(str(db))
    apply_schema(con)
    # Gamma ISO format with Z suffix; DuckDB should parse it
    con.execute("""
        INSERT INTO gamma_events (condition_id, event_id, title, event_slug, tour_prefix, token_id_yes, token_id_no, start_date, end_date)
        VALUES ('cid', 'ev1', 'Madrid: A vs B', 'atp-a-b', 'atp', 'tA', 'tB', '2026-04-20T16:12:14.831398Z', '2026-04-29T09:00:00Z')
    """)
    row = con.execute("SELECT end_date FROM gamma_events").fetchone()
    assert row[0] is not None  # TIMESTAMP parsed

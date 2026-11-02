import sqlite3

from sniper.research.memecoin_opportunity.store import SCHEMA_VERSION, connect


def columns(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_schema_version_is_17(tmp_path):
    conn = connect(tmp_path / "radar.db")
    assert SCHEMA_VERSION == 17
    assert conn.execute("SELECT version FROM op_schema_version").fetchone()[0] == 17


def test_the_v3_tables_exist(tmp_path):
    conn = connect(tmp_path / "radar.db")
    assert columns(conn, "op_paper_position_actions") >= {
        "action_id",
        "request_id",
        "lifecycle_version",
        "sequence",
        "action",
        "fraction_ppm",
        "quantity_atomic",
        "expected_out_atomic",
        "scored_out_atomic",
        "reason_codes_json",
        "policy_hash",
        "observed_at",
    }
    assert columns(conn, "op_paper_position_states") >= {
        "state_id",
        "request_id",
        "lifecycle_version",
        "sequence",
        "state_json",
        "policy_hash",
        "observed_at",
    }
    assert columns(conn, "op_paper_exit_followups") >= {
        "followup_id",
        "request_id",
        "lifecycle_version",
        "horizon_s",
        "due_at",
        "status",
        "expected_out_atomic",
        "return_bps",
        "observed_at",
    }


def test_outcomes_gain_four_nullable_columns(tmp_path):
    conn = connect(tmp_path / "radar.db")
    assert columns(conn, "op_paper_outcomes") >= {
        "origin_kind",
        "policy_hash",
        "decision_facts_json",
        "exit_reason_codes_json",
    }


def test_outcomes_separate_the_cash_return_from_the_baseline_movement(tmp_path):
    """`radar-realistic-v3` needs both numbers. One column cannot carry both."""
    conn = connect(tmp_path / "radar.db")
    assert columns(conn, "op_paper_outcomes") >= {
        "baseline_return_bps",
        "baseline_move_bps",
    }


def test_the_new_result_codes_are_accepted(tmp_path):
    conn = connect(tmp_path / "radar.db")
    for code in ("adaptive_exit", "trail_exit", "creator_exit"):
        conn.execute(
            "INSERT INTO op_paper_outcomes ("
            "outcome_id,request_id,lifecycle_version,status,result_code,"
            "entry_quote_atomic,return_bps,outcome_at,row_fingerprint,inserted_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                code,
                f"r-{code}",
                "radar-adaptive-v3",
                "valid",
                code,
                "1000",
                10,
                "2026-08-11T00:00:00Z",
                "f",
                "2026-08-11T00:00:00Z",
            ),
        )


def test_a_version_13_database_migrates_and_keeps_its_rows(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE op_schema_version (version INTEGER NOT NULL);
        INSERT INTO op_schema_version VALUES (13);
        CREATE TABLE op_paper_outcomes (
            outcome_id TEXT PRIMARY KEY,
            request_id TEXT NOT NULL,
            lifecycle_version TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('valid','censored')),
            result_code TEXT NOT NULL CHECK (
                result_code IN ('target_first','stop_first','time_exit','route_censored')
            ),
            terminal_observation_id TEXT,
            entry_quote_atomic TEXT NOT NULL,
            exit_floor_atomic TEXT,
            return_bps INTEGER,
            outcome_at TEXT NOT NULL,
            row_fingerprint TEXT NOT NULL,
            inserted_at TEXT NOT NULL,
            UNIQUE(request_id, lifecycle_version),
            CHECK ((status='valid') = (return_bps IS NOT NULL))
        );
        INSERT INTO op_paper_outcomes (
            outcome_id,request_id,lifecycle_version,status,result_code,
            entry_quote_atomic,return_bps,outcome_at,row_fingerprint,inserted_at
        ) VALUES (
            'o1','r1','radar-realistic-v2','valid','stop_first','1000',-800,
            '2026-08-07T00:00:00Z','f','2026-08-07T00:00:00Z'
        );
        """
    )
    old.close()

    reopened = connect(path)
    assert reopened.execute("SELECT version FROM op_schema_version").fetchone()[0] == 17
    assert columns(reopened, "op_paper_outcomes") >= {
        "baseline_return_bps",
        "baseline_move_bps",
    }
    row = reopened.execute(
        "SELECT return_bps,origin_kind FROM op_paper_outcomes WHERE outcome_id='o1'"
    ).fetchone()
    assert row[0] == -800 and row[1] is None
    reopened.execute(
        "INSERT INTO op_paper_outcomes ("
        "outcome_id,request_id,lifecycle_version,status,result_code,"
        "entry_quote_atomic,return_bps,outcome_at,row_fingerprint,inserted_at"
        ") VALUES ('o2','r2','radar-adaptive-v3','valid','trail_exit','1000',10,"
        "'2026-08-11T00:00:00Z','f2','2026-08-11T00:00:00Z')"
    )


def test_a_version_14_database_gains_the_baseline_columns_without_losing_rows(tmp_path):
    path = tmp_path / "v14.db"
    conn = connect(path)
    conn.execute("UPDATE op_schema_version SET version=14")
    conn.execute(
        "INSERT INTO op_paper_outcomes ("
        "outcome_id,request_id,lifecycle_version,status,result_code,"
        "entry_quote_atomic,return_bps,outcome_at,row_fingerprint,inserted_at"
        ") VALUES ('o14','r14','radar-realistic-v2','valid','stop_first','1000',-2519,"
        "'2026-08-11T00:00:00Z','f14','2026-08-11T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    reopened = connect(path)
    assert reopened.execute("SELECT version FROM op_schema_version").fetchone()[0] == 17
    row = reopened.execute(
        "SELECT return_bps,baseline_return_bps,baseline_move_bps FROM op_paper_outcomes "
        "WHERE outcome_id='o14'"
    ).fetchone()
    assert row[0] == -2519
    assert row[1] is None and row[2] is None


def test_a_version_15_database_gains_the_breadth_label_table_without_losing_rows(tmp_path):
    """Schema 16 adds `radar-buyer-breadth-v1` labels. Existing facts must survive."""
    path = tmp_path / "v15.db"
    old = connect(path)
    old.execute("UPDATE op_schema_version SET version=15")
    old.execute("DROP TABLE IF EXISTS op_breadth_labels")
    old.execute(
        "INSERT INTO op_creator_outcomes("
        "network_id,venue_id,creator_id,scored_outcomes,sum_return_bps,last_outcome_at) "
        "VALUES('solana','pumpfun_solana','C1',4,800,'2026-08-18T00:00:00.000000Z')"
    )
    old.close()

    reopened = connect(path)
    assert reopened.execute("SELECT version FROM op_schema_version").fetchone()[0] == 17
    # the pre-existing fact is untouched
    assert reopened.execute(
        "SELECT scored_outcomes FROM op_creator_outcomes WHERE creator_id='C1'"
    ).fetchone()[0] == 4
    # and the new table exists with its immutability triggers
    assert reopened.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='op_breadth_labels'"
    ).fetchone()[0] == 1
    triggers = {
        row[0] for row in reopened.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='op_breadth_labels'"
        )
    }
    assert triggers == {"op_breadth_labels_no_update", "op_breadth_labels_no_delete"}
    reopened.close()


def test_a_version_16_database_gains_the_scan_cursor_without_losing_rows(tmp_path):
    """Schema 17 adds the shared EVM scan cursor. Existing facts must survive."""
    path = tmp_path / "v16.db"
    old = connect(path)
    old.execute("UPDATE op_schema_version SET version=16")
    old.execute("DROP TABLE IF EXISTS op_evm_scan_cursors")
    old.execute(
        "INSERT INTO op_creator_outcomes("
        "network_id,venue_id,creator_id,scored_outcomes,sum_return_bps,last_outcome_at) "
        "VALUES('solana','pumpfun_solana','C1',7,1400,'2026-08-18T00:00:00.000000Z')"
    )
    old.close()

    reopened = connect(path)
    assert reopened.execute("SELECT version FROM op_schema_version").fetchone()[0] == 17
    assert reopened.execute(
        "SELECT scored_outcomes FROM op_creator_outcomes WHERE creator_id='C1'"
    ).fetchone()[0] == 7
    assert reopened.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='op_evm_scan_cursors'"
    ).fetchone()[0] == 1
    triggers = {
        row[0] for row in reopened.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='op_evm_scan_cursors'"
        )
    }
    assert triggers == {"op_evm_scan_cursors_no_rewind", "op_evm_scan_cursors_no_delete"}
    reopened.close()

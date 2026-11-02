import sqlite3

from sniper.hyperlive import edge_desk as ED
from sniper.hyperlive import intel_store as IS


NOW_MS = 1_000_000
ARM_ID = "wallet-copy-v2"


def _database(tmp_path):
    path = tmp_path / "hl_intel.sqlite"
    conn = IS.connect(str(path))
    IS.ensure_schema(conn)
    return path, conn


def _open(
    conn,
    *,
    target,
    coin,
    side="short",
    entry=10.0,
    active=False,
    source_positions=None,
    source_ts=NOW_MS - 10_000,
    arm_id=None,
    mark_coin=None,
    mark=None,
):
    IS.set_copy_target(conn, target, active=active, ts_ms=NOW_MS - 20_000)
    copy_id = IS.open_copy(
        conn, target, coin, side, entry, ts_ms=NOW_MS - 30_000
    )
    if source_positions is not None:
        IS.save_copy_trader_state(
            conn, target, source_positions, ts_ms=source_ts
        )
    if arm_id:
        conn.execute(
            "INSERT INTO hl_altshort_observations("
            "copy_id, arm_id, target, coin, side, detect_ts_ms, entry_mark, "
            "regime_json, benchmark_entry_json, status) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'open')",
            (
                copy_id,
                arm_id,
                target,
                coin,
                side,
                NOW_MS - 30_000,
                entry,
                "{}",
                "{}",
            ),
        )
    if mark_coin is not None and mark is not None:
        conn.execute(
            "INSERT INTO hl_altshort_regime_marks(ts_ms, coin, mark) VALUES (?,?,?)",
            (NOW_MS - 20_000, mark_coin, mark),
        )
    conn.commit()
    return copy_id


def _classify(conn):
    return ED.classify_open_positions(
        conn,
        now_ms=NOW_MS,
        current_arm_id=ARM_ID,
        copy_heartbeat={"ts_ms": NOW_MS - 5_000},
    )


def test_classifies_all_open_position_truth_states(tmp_path):
    _, conn = _database(tmp_path)
    _open(
        conn,
        target="0xcurrent",
        coin="ALT",
        active=True,
        source_positions={"alt": {"side": "short"}},
        arm_id=ARM_ID,
        mark_coin="ALT",
        mark=9.0,
    )
    _open(
        conn,
        target="0xdraining",
        coin="OLD",
        source_positions={"old": {"side": "short"}},
        mark_coin="OLD",
        mark=9.0,
    )
    _open(
        conn,
        target="0xready",
        coin="kSHIB",
        source_positions={},
        mark_coin="KSHIB",
        mark=9.0,
    )
    _open(
        conn,
        target="0xblocked",
        coin="NO_MARK",
        source_positions={},
    )
    _open(
        conn,
        target="0xstale",
        coin="STALE",
        source_positions={"STALE": {"side": "short"}},
        source_ts=NOW_MS - 300_001,
        mark_coin="STALE",
        mark=9.0,
    )

    payload = _classify(conn)

    assert payload["counts"] == {
        "current_lane": 1,
        "draining_legacy": 1,
        "reconcile_ready": 1,
        "reconcile_blocked": 1,
        "source_unverified": 1,
    }
    assert sum(payload["counts"].values()) == payload["total"] == 5
    assert {
        state: rows[0]["target"] for state, rows in payload["groups"].items()
    } == {
        "current_lane": "0xcurrent",
        "draining_legacy": "0xdraining",
        "reconcile_ready": "0xready",
        "reconcile_blocked": "0xblocked",
        "source_unverified": "0xstale",
    }
    conn.close()


def test_matches_k_token_mark_without_rewriting_stored_symbol(tmp_path):
    _, conn = _database(tmp_path)
    _open(
        conn,
        target="0xready",
        coin="kSHIB",
        source_positions={},
        mark_coin="KSHIB",
        mark=9.0,
    )

    row = _classify(conn)["groups"]["reconcile_ready"][0]

    assert row["coin"] == "kSHIB"
    assert row["current_mark"] == 9.0
    assert row["unrealized_bps"] == 1000.0
    conn.close()


def test_inactive_current_arm_position_is_draining_with_provenance(tmp_path):
    _, conn = _database(tmp_path)
    _open(
        conn,
        target="0xinactive",
        coin="ALT",
        source_positions={"ALT": {"side": "short"}},
        arm_id=ARM_ID,
        mark_coin="ALT",
        mark=9.0,
    )

    row = _classify(conn)["groups"]["draining_legacy"][0]

    assert row["current_arm"] is True
    assert row["target_active"] is False
    assert row["reason"] == "inactive_target_source_held"
    conn.close()


def test_source_key_collision_fails_closed(tmp_path):
    _, conn = _database(tmp_path)
    _open(
        conn,
        target="0xcollision",
        coin="kPEPE",
        source_positions={
            "kPEPE": {"side": "short"},
            "KPEPE": {"side": "short"},
        },
        mark_coin="KPEPE",
        mark=9.0,
    )

    row = _classify(conn)["groups"]["source_unverified"][0]

    assert row["reason"] == "ambiguous_source_coin_key"
    conn.close()


def test_malformed_source_json_fails_closed(tmp_path):
    _, conn = _database(tmp_path)
    _open(
        conn,
        target="0xmalformed",
        coin="ALT",
        source_positions={"ALT": {"side": "short"}},
        mark_coin="ALT",
        mark=9.0,
    )
    conn.execute(
        "UPDATE hl_copy_trader_state SET positions_json='not-json' "
        "WHERE wallet='0xmalformed'"
    )
    conn.commit()

    row = _classify(conn)["groups"]["source_unverified"][0]

    assert row["reason"] == "invalid_source_positions"
    conn.close()


def test_future_source_sync_beyond_tolerance_is_unverified(tmp_path):
    _, conn = _database(tmp_path)
    _open(
        conn,
        target="0xfuture",
        coin="ALT",
        source_positions={"ALT": {"side": "short"}},
        source_ts=NOW_MS + 60_001,
        mark_coin="ALT",
        mark=9.0,
    )

    row = _classify(conn)["groups"]["source_unverified"][0]

    assert row["reason"] == "stale_or_future_source_sync"
    conn.close()


def test_stale_copy_heartbeat_makes_marks_unavailable(tmp_path):
    _, conn = _database(tmp_path)
    _open(
        conn,
        target="0xblocked",
        coin="ALT",
        source_positions={},
        mark_coin="ALT",
        mark=9.0,
    )

    payload = ED.classify_open_positions(
        conn,
        now_ms=NOW_MS,
        current_arm_id=ARM_ID,
        copy_heartbeat={"ts_ms": NOW_MS - 300_001},
    )
    row = payload["groups"]["reconcile_blocked"][0]

    assert row["current_mark"] is None
    assert row["mark_available"] is False
    assert row["reason"] == "source_exited_mark_unavailable"
    conn.close()


def test_classifier_is_read_only_on_read_only_connection(tmp_path):
    path, writable = _database(tmp_path)
    _open(
        writable,
        target="0xcurrent",
        coin="ALT",
        active=True,
        source_positions={"ALT": {"side": "short"}},
        arm_id=ARM_ID,
        mark_coin="ALT",
        mark=9.0,
    )
    writable.close()
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    before = conn.total_changes

    payload = _classify(conn)

    assert payload["total"] == 1
    assert conn.total_changes == before == 0
    conn.close()


def test_comparison_key_is_case_insensitive_only():
    assert ED.comparison_key("kPEPE") == ED.comparison_key("KPEPE")
    assert ED.comparison_key("KAITO") == "kaito"

import sqlite3

import pytest

from sniper.persistence import db_memecoin_x as X


NOW = "2026-07-26T12:00:00+00:00"
MINT = "0x1111111111111111111111111111111111111111"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    X.ensure_memecoin_x_tables(conn)
    return conn


def test_legacy_exact_signal_migration_preserves_id_and_outbox_link(tmp_path):
    conn = sqlite3.connect(tmp_path / "legacy.db")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE memecoin_x_signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logical_key TEXT UNIQUE NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN
                ('capture_tag','fast_consensus','tier2_scout','official_context')),
            network_id TEXT NOT NULL,
            mint TEXT NOT NULL,
            triggered_at TEXT NOT NULL,
            source_handle TEXT,
            evidence_observation_ids_json TEXT NOT NULL,
            actor_handles_json TEXT NOT NULL,
            relationship_counts_json TEXT NOT NULL,
            display_only INTEGER NOT NULL DEFAULT 1 CHECK (display_only=1),
            created_at TEXT NOT NULL
        );
        CREATE TABLE memecoin_x_notification_outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logical_key TEXT UNIQUE NOT NULL,
            signal_event_id INTEGER NOT NULL,
            channel TEXT NOT NULL CHECK (channel IN ('dashboard','telegram')),
            state TEXT NOT NULL CHECK (state IN ('pending','sent','failed','suppressed')),
            attempts INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            last_attempt_at TEXT,
            sent_at TEXT,
            bounded_error TEXT
        );
    """)
    conn.execute(
        "INSERT INTO memecoin_x_signal_events "
        "(id,logical_key,event_type,network_id,mint,triggered_at,source_handle,"
        "evidence_observation_ids_json,actor_handles_json,relationship_counts_json,created_at) "
        "VALUES (7,'legacy:7','capture_tag','4663',?,?, 'alpha','[]','[]','{}',?)",
        (MINT, NOW, NOW),
    )
    conn.execute(
        "INSERT INTO memecoin_x_notification_outbox "
        "(id,logical_key,signal_event_id,channel,state,created_at) "
        "VALUES (9,'telegram:legacy:7',7,'telegram','pending',?)",
        (NOW,),
    )
    conn.commit()

    X.ensure_memecoin_x_tables(conn)
    X.ensure_memecoin_x_tables(conn)

    row = conn.execute(
        "SELECT id,identity_state,network_id,mint FROM memecoin_x_signal_events WHERE id=7"
    ).fetchone()
    assert tuple(row) == (7, "exact_coin", "4663", MINT)
    assert conn.execute(
        "SELECT signal_event_id FROM memecoin_x_notification_outbox WHERE id=9"
    ).fetchone()[0] == 7


def test_ticker_only_signal_persists_without_fake_coin_identity():
    conn = _conn()
    event_id = X.persist_signal_event(conn, {
        "logical_key": "x:100:1001:MEME",
        "event_type": "cashtag_mention",
        "identity_state": "ticker_only",
        "ticker": "MEME",
        "network_id": None,
        "mint": None,
        "triggered_at": NOW,
        "source_handle": "alpha",
        "source_user_id": "1001",
        "post_id": "100",
        "edit_root_id": "100",
        "post_url": "https://x.com/alpha/status/100",
        "relationship": "direct",
        "cashtag_source": "actor_text",
        "excerpt": "Watching $MEME",
        "x_created_at": "2026-07-26T11:59:58+00:00",
        "consensus_count": 1,
        "details": {"cashtags": ["MEME"]},
    }, created_at=NOW)
    row = conn.execute(
        "SELECT ticker,identity_state,network_id,mint,post_url FROM memecoin_x_signal_events "
        "WHERE id=?", (event_id,),
    ).fetchone()
    assert tuple(row) == (
        "MEME", "ticker_only", None, None, "https://x.com/alpha/status/100")


def test_signal_identity_constraints_reject_ticker_with_fake_mint():
    conn = _conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memecoin_x_signal_events "
            "(logical_key,event_type,identity_state,ticker,network_id,mint,triggered_at,"
            "evidence_observation_ids_json,actor_handles_json,relationship_counts_json,"
            "details_json,created_at) VALUES "
            "('bad','cashtag_mention','ticker_only','MEME','4663',?,?,'[]','[]','{}','{}',?)",
            (MINT, NOW, NOW),
        )


def test_active_api_roster_uses_stable_user_id_and_excludes_inactive():
    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_x_accounts(handle,display_handle,x_user_id,role,active) "
        "VALUES ('alpha','Alpha','100','tier1_trader',1),"
        "('sleeping','Sleeping','200','tier2_source',0),"
        "('suggested','Suggested','300','suggested',1)"
    )
    conn.commit()
    assert X.active_api_roster(conn) == [{
        "x_user_id": "100", "handle": "alpha", "display_handle": "Alpha",
        "role": "tier1_trader",
    }]
    conn.execute("UPDATE memecoin_x_accounts SET handle='alpha_new' WHERE x_user_id='100'")
    conn.commit()
    assert X.active_api_roster(conn)[0]["x_user_id"] == "100"
    assert X.active_api_roster(conn)[0]["handle"] == "alpha_new"


def test_api_capture_state_compare_and_swap_rejects_stale_writer():
    conn = _conn()
    state = X.api_capture_state(conn)
    assert state["state"] == "stopped" and state["revision"] == 0
    updated = X.update_api_capture_state(
        conn, expected_revision=0, state="healthy", updated_at=NOW,
        frontier_post_id="100",
    )
    assert updated["revision"] == 1 and updated["frontier_post_id"] == "100"
    with pytest.raises(ValueError, match="stale X API state revision"):
        X.update_api_capture_state(
            conn, expected_revision=0, state="recovering", updated_at=NOW)


def test_api_resource_counts_once_per_corrected_utc_day():
    conn = _conn()
    assert X.count_api_resource(
        conn, utc_day="2026-07-26", resource_id="100", source="stream",
        counted_at=NOW) is True
    assert X.count_api_resource(
        conn, utc_day="2026-07-26", resource_id="100", source="recovery",
        counted_at=NOW) is False
    assert X.count_api_resource(
        conn, utc_day="2026-07-27", resource_id="100", source="recovery",
        counted_at="2026-07-27T00:00:01+00:00") is True
    assert X.api_resource_count(conn, utc_day="2026-07-26") == 1
    assert X.api_resource_count(conn, utc_day="2026-07-27") == 1

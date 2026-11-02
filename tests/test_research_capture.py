"""Tests for research capture: position_snapshots table and query functions."""

import sqlite3
import time

import db


def _make_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    db.init_db(conn)
    return conn


WALLET = "0xabc123def456"


def _sample_positions(n=3):
    """Return a list of mock Polymarket position dicts."""
    positions = []
    for i in range(n):
        positions.append({
            "conditionId": f"0xcond{i:04d}",
            "outcome": "Yes",
            "title": f"Market {i}",
            "avgPrice": 0.40 + i * 0.05,
            "curPrice": 0.50 + i * 0.05,
            "size": 100.0 + i * 10,
            "initialValue": 50.0,
            "cashPnl": 5.0,
        })
    return positions


def test_insert_position_snapshots_basic():
    conn = _make_db()
    positions = _sample_positions(3)
    ts = int(time.time())

    count = db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=ts)
    assert count == 3

    rows = conn.execute("SELECT * FROM position_snapshots WHERE wallet = ?", (WALLET,)).fetchall()
    assert len(rows) == 3
    assert rows[0]["condition_id"] == "0xcond0000"
    assert rows[0]["snapshot_ts"] == ts


def test_insert_position_snapshots_empty():
    conn = _make_db()
    count = db.insert_position_snapshots(conn, wallet=WALLET, positions=[], snapshot_ts=1000)
    assert count == 0


def test_insert_position_snapshots_skips_missing_condition_id():
    conn = _make_db()
    positions = [{"outcome": "Yes", "avgPrice": 0.5, "curPrice": 0.6, "size": 10}]
    count = db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=1000)
    assert count == 0


def test_insert_position_snapshots_normalizes_wallet():
    conn = _make_db()
    positions = _sample_positions(1)
    wallet_upper = "0xABC123DEF456"
    db.insert_position_snapshots(conn, wallet=wallet_upper, positions=positions, snapshot_ts=1000)

    rows = conn.execute("SELECT wallet FROM position_snapshots").fetchall()
    assert len(rows) == 1
    assert rows[0]["wallet"] == wallet_upper.lower()


def test_insert_position_snapshots_append_only():
    """Multiple snapshots at different timestamps append, not overwrite."""
    conn = _make_db()
    positions = _sample_positions(2)

    db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=1000)
    db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=2000)

    rows = conn.execute("SELECT * FROM position_snapshots WHERE wallet = ?", (WALLET,)).fetchall()
    assert len(rows) == 4  # 2 positions x 2 snapshots


def test_capture_position_snapshots_skips_unchanged_snapshot():
    conn = _make_db()
    positions = _sample_positions(2)

    first = db.capture_position_snapshots(
        conn, wallet=WALLET, positions=positions, snapshot_ts=1000
    )
    second = db.capture_position_snapshots(
        conn, wallet=WALLET, positions=positions, snapshot_ts=2000
    )

    assert first["changed"] is True
    assert first["inserted"] == 2
    assert second["changed"] is False
    assert second["inserted"] == 0
    rows = conn.execute("SELECT * FROM position_snapshots WHERE wallet = ?", (WALLET,)).fetchall()
    assert len(rows) == 2


def test_capture_position_snapshots_skips_on_price_only_change():
    """Price-only changes do not trigger a new snapshot (structural signature)."""
    conn = _make_db()
    positions = _sample_positions(1)
    changed_positions = _sample_positions(1)
    changed_positions[0]["curPrice"] = 0.77  # only mark-to-market changed

    first = db.capture_position_snapshots(
        conn, wallet=WALLET, positions=positions, snapshot_ts=1000
    )
    second = db.capture_position_snapshots(
        conn, wallet=WALLET, positions=changed_positions, snapshot_ts=2000
    )

    assert first["changed"] is True
    # Price-only change should NOT trigger a new snapshot
    assert second["changed"] is False
    assert second["inserted"] == 0
    rows = conn.execute(
        "SELECT DISTINCT snapshot_ts FROM position_snapshots WHERE wallet = ?",
        (WALLET,),
    ).fetchall()
    assert len(rows) == 1


def test_capture_position_snapshots_inserts_on_structural_change():
    """Size change (structural) triggers a new snapshot."""
    conn = _make_db()
    positions = _sample_positions(1)
    changed_positions = _sample_positions(1)
    changed_positions[0]["size"] = 999.0  # structural change

    first = db.capture_position_snapshots(
        conn, wallet=WALLET, positions=positions, snapshot_ts=1000
    )
    second = db.capture_position_snapshots(
        conn, wallet=WALLET, positions=changed_positions, snapshot_ts=2000
    )

    assert first["changed"] is True
    assert second["changed"] is True
    rows = conn.execute(
        "SELECT DISTINCT snapshot_ts FROM position_snapshots WHERE wallet = ? ORDER BY snapshot_ts",
        (WALLET,),
    ).fetchall()
    assert [r["snapshot_ts"] for r in rows] == [1000, 2000]


def test_capture_position_snapshots_force_write_bypasses_signature():
    """force_write=True writes even when structural signature is unchanged."""
    conn = _make_db()
    positions = _sample_positions(2)

    first = db.capture_position_snapshots(
        conn, wallet=WALLET, positions=positions, snapshot_ts=1000
    )
    second = db.capture_position_snapshots(
        conn, wallet=WALLET, positions=positions, snapshot_ts=2000,
        force_write=True,
    )

    assert first["changed"] is True
    assert first["inserted"] == 2
    assert second["changed"] is True
    assert second["inserted"] == 2
    rows = conn.execute(
        "SELECT DISTINCT snapshot_ts FROM position_snapshots WHERE wallet = ? ORDER BY snapshot_ts",
        (WALLET,),
    ).fetchall()
    assert [r["snapshot_ts"] for r in rows] == [1000, 2000]


def test_capture_position_snapshots_force_write_one_row_per_position():
    """Each forced cycle writes one row per open position."""
    conn = _make_db()
    positions = _sample_positions(3)

    db.capture_position_snapshots(
        conn, wallet=WALLET, positions=positions, snapshot_ts=1000,
        force_write=True,
    )
    rows = conn.execute(
        "SELECT * FROM position_snapshots WHERE wallet = ? AND snapshot_ts = 1000",
        (WALLET,),
    ).fetchall()
    assert len(rows) == 3


def test_latest_position_snapshot_ts_empty():
    conn = _make_db()
    assert db.latest_position_snapshot_ts(conn, WALLET) == 0


def test_latest_position_snapshot_ts_returns_max():
    conn = _make_db()
    positions = _sample_positions(1)
    db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=1000)
    db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=2000)

    assert db.latest_position_snapshot_ts(conn, WALLET) == 2000


def test_position_snapshot_history_all():
    conn = _make_db()
    positions = _sample_positions(2)
    db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=1000)

    history = db.position_snapshot_history(conn, WALLET)
    assert len(history) == 2
    # Should be dicts, not Row objects
    assert isinstance(history[0], dict)


def test_position_snapshot_history_filtered_by_condition():
    conn = _make_db()
    positions = _sample_positions(3)
    db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=1000)

    history = db.position_snapshot_history(conn, WALLET, condition_id="0xcond0001")
    assert len(history) == 1
    assert history[0]["condition_id"] == "0xcond0001"


def test_position_snapshot_history_respects_limit():
    conn = _make_db()
    positions = _sample_positions(5)
    db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=1000)

    history = db.position_snapshot_history(conn, WALLET, limit=2)
    assert len(history) == 2


def test_position_snapshot_pnl_percent_calculation():
    conn = _make_db()
    positions = [{
        "conditionId": "0xcond0001",
        "outcome": "Yes",
        "title": "Test",
        "avgPrice": 0.40,
        "curPrice": 0.60,
        "size": 100,
        "initialValue": 40,
        "cashPnl": 20,
    }]
    db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=1000)

    row = conn.execute("SELECT pnl_percent FROM position_snapshots").fetchone()
    # (0.60 - 0.40) / 0.40 * 100 = 50.0
    assert abs(row["pnl_percent"] - 50.0) < 0.01


def test_position_snapshot_resolved_flag():
    conn = _make_db()
    positions = [
        {"conditionId": "0xa", "outcome": "Yes", "avgPrice": 0.3, "curPrice": 0.99, "size": 10},
        {"conditionId": "0xb", "outcome": "Yes", "avgPrice": 0.3, "curPrice": 0.50, "size": 10},
        {"conditionId": "0xc", "outcome": "Yes", "avgPrice": 0.3, "curPrice": 0.005, "size": 10},
    ]
    db.insert_position_snapshots(conn, wallet=WALLET, positions=positions, snapshot_ts=1000)

    rows = conn.execute(
        "SELECT condition_id, resolved FROM position_snapshots ORDER BY condition_id"
    ).fetchall()
    assert rows[0]["resolved"] == 1  # 0xa: 0.99 >= 0.99
    assert rows[1]["resolved"] == 0  # 0xb: 0.50, not resolved
    assert rows[2]["resolved"] == 1  # 0xc: 0.005 <= 0.01


def test_prune_position_snapshots_keeps_recent_and_daily_history():
    conn = _make_db()
    positions = _sample_positions(1)
    now_ts = 1_700_000_000
    # 6 snapshot timestamps across 4 UTC days.
    snapshot_times = [
        now_ts - 5 * 86400,
        now_ts - 2 * 86400 - 3600,
        now_ts - 2 * 86400,
        now_ts - 1 * 86400 - 3600,
        now_ts - 1 * 86400,
        now_ts,
    ]
    for ts in snapshot_times:
        tweaked = _sample_positions(1)
        tweaked[0]["curPrice"] = 0.50 + (ts % 1000) / 10000.0
        db.insert_position_snapshots(conn, wallet=WALLET, positions=tweaked, snapshot_ts=ts)

    pruned = db.prune_position_snapshots(
        conn,
        WALLET,
        keep_recent_snapshots=2,
        keep_daily_days=3,
        now_ts=now_ts,
    )

    assert pruned == 3
    rows = conn.execute(
        "SELECT DISTINCT snapshot_ts FROM position_snapshots WHERE wallet = ? ORDER BY snapshot_ts DESC",
        (WALLET,),
    ).fetchall()
    kept = [r["snapshot_ts"] for r in rows]
    assert kept == [
        now_ts,
        now_ts - 1 * 86400,
        now_ts - 2 * 86400,
    ]


def test_research_wallet_activity_max_ts_empty():
    conn = _make_db()
    assert db.research_wallet_activity_max_ts(conn, WALLET) == 0


# ---------------------------------------------------------------------------
# Position-building cluster detection
# ---------------------------------------------------------------------------

def _insert_activity(conn, wallet, condition_id, side, outcome, title, ts, price=0.10, size=100, usdc_size=10.0):
    """Insert a single followed_wallet_activity row for testing."""
    import hashlib, json
    key = hashlib.sha256(f"{wallet}{condition_id}{ts}{side}{price}{size}".encode()).hexdigest()[:32]
    conn.execute(
        """INSERT OR IGNORE INTO followed_wallet_activity
           (activity_key, wallet, transaction_hash, condition_id, activity_type,
            side, outcome, title, slug, event_slug, price, size, usdc_size,
            activity_ts, source_endpoint, raw_json)
           VALUES (?, ?, '', ?, 'TRADE', ?, ?, ?, '', '', ?, ?, ?, ?, 'test', '{}')""",
        (key, wallet, condition_id, side, outcome, title, price, size, usdc_size, ts),
    )
    conn.commit()


def test_cluster_detection_twap_build():
    conn = _make_db()
    base_ts = 1700000000
    # 15 buys on the same market within one day = twap_build
    for i in range(15):
        _insert_activity(conn, WALLET, "0xcond_iran", "BUY", "Yes",
                         "Iran ceasefire", base_ts + i * 60, usdc_size=100.0)

    clusters = db.research_position_building_clusters(conn, WALLET)
    assert len(clusters) == 1
    assert clusters[0]["label"] == "twap_build"
    assert clusters[0]["trade_count"] == 15
    assert clusters[0]["total_usdc"] == 1500.0


def test_cluster_detection_position_add():
    conn = _make_db()
    base_ts = 1700000000
    # 5 buys = position_add (3-9 range)
    for i in range(5):
        _insert_activity(conn, WALLET, "0xcond_test", "BUY", "No",
                         "Test market", base_ts + i * 300, usdc_size=50.0)

    clusters = db.research_position_building_clusters(conn, WALLET)
    assert len(clusters) == 1
    assert clusters[0]["label"] == "position_add"
    assert clusters[0]["trade_count"] == 5


def test_cluster_detection_trimming():
    conn = _make_db()
    base_ts = 1700000000
    # 4 sells = trimming
    for i in range(4):
        _insert_activity(conn, WALLET, "0xcond_sell", "SELL", "Yes",
                         "Selling market", base_ts + i * 120, usdc_size=200.0)

    clusters = db.research_position_building_clusters(conn, WALLET)
    assert len(clusters) == 1
    assert clusters[0]["label"] == "trimming"


def test_cluster_detection_twap_exit():
    conn = _make_db()
    base_ts = 1700000000
    # 12 sells = twap_exit
    for i in range(12):
        _insert_activity(conn, WALLET, "0xcond_exit", "SELL", "No",
                         "Exit market", base_ts + i * 60, usdc_size=500.0)

    clusters = db.research_position_building_clusters(conn, WALLET)
    assert len(clusters) == 1
    assert clusters[0]["label"] == "twap_exit"
    assert clusters[0]["trade_count"] == 12


def test_cluster_detection_min_trades_filter():
    conn = _make_db()
    base_ts = 1700000000
    # Only 2 trades — below default min_trades=3
    for i in range(2):
        _insert_activity(conn, WALLET, "0xcond_small", "BUY", "Yes",
                         "Small", base_ts + i * 60)

    clusters = db.research_position_building_clusters(conn, WALLET)
    assert len(clusters) == 0

    # With min_trades=2, should appear
    clusters = db.research_position_building_clusters(conn, WALLET, min_trades=2)
    assert len(clusters) == 1


def test_cluster_detection_empty_wallet():
    conn = _make_db()
    clusters = db.research_position_building_clusters(conn, WALLET)
    assert clusters == []


# ---------------------------------------------------------------------------
# wallet_has_open_signals tests
# ---------------------------------------------------------------------------


def test_wallet_has_open_signals_false_when_empty():
    conn = _make_db()
    assert db.wallet_has_open_signals(conn, WALLET) is False


def test_wallet_has_open_signals_true_when_open():
    conn = _make_db()
    conn.execute(
        """INSERT INTO position_signals
           (wallet, condition_id, outcome, is_open, analyzed_ts)
           VALUES (?, ?, ?, 1, 1700000000)""",
        (WALLET, "cond1", "yes"),
    )
    conn.commit()
    assert db.wallet_has_open_signals(conn, WALLET) is True


def test_wallet_has_open_signals_false_when_closed():
    conn = _make_db()
    conn.execute(
        """INSERT INTO position_signals
           (wallet, condition_id, outcome, is_open, analyzed_ts)
           VALUES (?, ?, ?, 0, 1700000000)""",
        (WALLET, "cond1", "yes"),
    )
    conn.commit()
    assert db.wallet_has_open_signals(conn, WALLET) is False

import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.add_wallet(conn, "u1", "alice")
    m.add_wallet(conn, "u2", "bob")
    conn.execute("UPDATE memecoin_wallets SET real_approved=1")
    conn.commit()
    return conn


def _observation(event_id, *, network="4663", uid="u1", side="buy", remaining=100.0,
                 observed_at="2026-07-16T10:00:00+00:00"):
    body = {} if remaining is None else {"currentSizeUsd": remaining}
    return SimpleNamespace(
        upstream_event_id=event_id,
        raw_type="large_buy" if side == "buy" else "large_sell",
        normalized_side=side,
        source_user_id=uid,
        source_handle="alice" if uid == "u1" else "bob",
        network_id=network,
        mint="0xSame",
        observed_at=observed_at,
        raw_payload={"body": body},
    )


def test_feed_reconciliation_preserves_explicit_positive_and_zero():
    from sniper.services.memecoin_holder_evidence import reconcile_feed_observations

    conn = _conn()
    reconcile_feed_observations(conn, [_observation("e1", remaining=125.0)], {"u1", "u2"})
    row = conn.execute(
        "SELECT * FROM memecoin_holder_evidence WHERE network_id='4663' AND mint='0xSame' "
        "AND source_user_id='u1'",
    ).fetchone()
    assert row["evidence_state"] == "positive" and row["remaining_usd"] == 125.0
    assert row["clock_domain"] == "observed_real_utc"

    reconcile_feed_observations(conn, [
        _observation("e2", side="sell", remaining=0.0,
                     observed_at="2026-07-16T10:00:01+00:00"),
    ], {"u1", "u2"})
    row = conn.execute(
        "SELECT * FROM memecoin_holder_evidence WHERE network_id='4663' AND mint='0xSame' "
        "AND source_user_id='u1'",
    ).fetchone()
    assert row["evidence_state"] == "zero" and row["remaining_usd"] == 0.0


def test_sell_without_remaining_invalidates_positive_to_unknown():
    from sniper.services.memecoin_holder_evidence import reconcile_feed_observations

    conn = _conn()
    reconcile_feed_observations(conn, [_observation("e1", remaining=125.0)], {"u1"})
    reconcile_feed_observations(conn, [
        _observation("e2", side="sell", remaining=None,
                     observed_at="2026-07-16T10:00:01+00:00"),
    ], {"u1"})
    row = conn.execute("SELECT * FROM memecoin_holder_evidence").fetchone()
    assert row["evidence_state"] == "unknown"
    assert row["remaining_usd"] is None
    assert row["invalidation_reason"] == "sell_missing_remaining"


def test_holder_evidence_is_exact_chain_keyed():
    from sniper.services.memecoin_holder_evidence import reconcile_feed_observations

    conn = _conn()
    reconcile_feed_observations(conn, [
        _observation("base", network="8453", remaining=10.0),
        _observation("robin", network="4663", remaining=20.0),
    ], {"u1"})
    rows = conn.execute(
        "SELECT network_id, remaining_usd FROM memecoin_holder_evidence ORDER BY network_id",
    ).fetchall()
    assert [(r["network_id"], r["remaining_usd"]) for r in rows] == [
        ("4663", 20.0), ("8453", 10.0),
    ]


def test_stale_or_unknown_evidence_never_returns_positive_holder():
    from sniper.services.memecoin_holder_evidence import (
        fresh_positive_holders, reconcile_feed_observations,
    )

    conn = _conn()
    reconcile_feed_observations(conn, [
        _observation("e1", uid="u2", remaining=100.0,
                     observed_at="2026-07-16T10:00:00+00:00"),
    ], {"u2"})
    assert fresh_positive_holders(
        conn, network_id="4663", mint="0xSame", exclude_user_id="u1",
        now_iso="2026-07-16T10:01:00+00:00", max_age_s=120, min_remaining_usd=1,
    )[0]["source_user_id"] == "u2"
    assert fresh_positive_holders(
        conn, network_id="4663", mint="0xSame", exclude_user_id="u1",
        now_iso="2026-07-16T10:02:01+00:00", max_age_s=120, min_remaining_usd=1,
    ) == []


def test_follow_sell_and_failed_poll_invalidate_without_new_request():
    from sniper.services.memecoin_holder_evidence import (
        invalidate_wallet_evidence, reconcile_feed_observations, reconcile_follow_poll,
    )

    conn = _conn()
    reconcile_feed_observations(conn, [_observation("e1", remaining=100.0)], {"u1"})
    swaps = [{
        "id": "s1", "inTokenAddress": "0xSame",
        "outTokenAddress": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "networkId": "4663", "createdAt": "2026-07-16T10:00:10+00:00",
        "humanUsdAmountOut": 50.0, "inHumanAmount": 10.0,
    }]
    reconcile_follow_poll(conn, "u1", swaps, "2026-07-16T10:00:11+00:00")
    row = conn.execute("SELECT * FROM memecoin_holder_evidence").fetchone()
    assert row["evidence_state"] == "unknown"
    assert row["invalidation_reason"] == "follow_sell_missing_remaining"
    assert row["successful_poll_at"] == "2026-07-16T10:00:11+00:00"
    assert row["observed_at"] == "2026-07-16T10:00:11+00:00"
    assert row["source_event_at"] == "2026-07-16T10:00:10+00:00"
    assert row["source_timestamp_trust"] == "claimed_untrusted"

    reconcile_feed_observations(conn, [
        _observation("e2", remaining=80.0, observed_at="2026-07-16T10:00:12+00:00"),
    ], {"u1"})
    invalidate_wallet_evidence(
        conn, "u1", "2026-07-16T10:00:13+00:00", "follow_poll_failed",
    )
    row = conn.execute("SELECT * FROM memecoin_holder_evidence").fetchone()
    assert row["evidence_state"] == "unknown"
    assert row["invalidation_reason"] == "follow_poll_failed"


def test_follow_replay_uses_stable_fact_hash_across_poll_clocks():
    from sniper.services.memecoin_holder_evidence import reconcile_follow_poll

    conn = _conn()
    swap = {
        "id": "stable-swap", "outTokenAddress": "0xSame",
        "inTokenAddress": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "networkId": "4663", "createdAt": "2026-07-16T10:00:10+00:00",
        "humanUsdAmountIn": 50.0, "outHumanAmount": 10.0,
        "currentSizeUsd": 75.0,
    }

    reconcile_follow_poll(conn, "u1", [swap], "2026-07-28T05:40:08+00:00")
    reconcile_follow_poll(conn, "u1", [swap], "2026-07-28T05:41:08+00:00")

    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_holder_evidence_history",
    ).fetchone()[0] == 1
    row = conn.execute("SELECT * FROM memecoin_holder_evidence").fetchone()
    assert row["observed_at"] == "2026-07-28T05:41:08+00:00"
    assert row["successful_poll_at"] == "2026-07-28T05:41:08+00:00"


def test_unknown_evidence_updates_current_without_appending_history():
    from sniper.services.memecoin_holder_evidence import reconcile_follow_poll

    conn = _conn()
    swap = {
        "id": "unknown-swap", "inTokenAddress": "0xSame",
        "outTokenAddress": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "networkId": "4663", "createdAt": "2026-07-16T10:00:10+00:00",
        "humanUsdAmountOut": 50.0, "inHumanAmount": 10.0,
    }

    reconcile_follow_poll(conn, "u1", [swap], "2026-07-28T05:40:08+00:00")

    row = conn.execute("SELECT * FROM memecoin_holder_evidence").fetchone()
    assert row["evidence_state"] == "unknown"
    assert conn.execute(
        "SELECT COUNT(*) FROM memecoin_holder_evidence_history",
    ).fetchone()[0] == 0


def test_holder_poll_success_update_uses_source_user_index():
    conn = _conn()
    indexes = {
        row[1] for row in conn.execute("PRAGMA index_list(memecoin_holder_evidence)")
    }
    assert "idx_mc_holder_evidence_user" in indexes
    plan = conn.execute(
        "EXPLAIN QUERY PLAN UPDATE memecoin_holder_evidence "
        "SET successful_poll_at=?, updated_at=? WHERE source_user_id=?",
        ("2026-07-28T05:40:08+00:00", "2026-07-28T05:40:08+00:00", "u1"),
    ).fetchall()
    assert any("idx_mc_holder_evidence_user" in row[3] for row in plan)


def test_older_sell_cannot_overwrite_newer_positive_evidence():
    from sniper.services.memecoin_holder_evidence import reconcile_feed_observations

    conn = _conn()
    reconcile_feed_observations(conn, [
        _observation("new", remaining=100.0, observed_at="2026-07-16T10:01:00+00:00"),
        _observation("old", side="sell", remaining=None,
                     observed_at="2026-07-16T10:00:00+00:00"),
    ], {"u1"})
    row = conn.execute("SELECT * FROM memecoin_holder_evidence").fetchone()
    assert row["evidence_state"] == "positive"
    assert row["upstream_event_id"] == "new"


def test_reconciliation_failure_rolls_back_its_local_transaction(monkeypatch):
    from sniper.services import memecoin_holder_evidence as evidence

    conn = _conn()
    original = m.upsert_holder_evidence

    def fail_after_write(*args, **kwargs):
        original(*args, **{**kwargs, "commit": False})
        raise RuntimeError("forced holder write failure")

    monkeypatch.setattr(m, "upsert_holder_evidence", fail_after_write)
    with pytest.raises(RuntimeError, match="forced holder write failure"):
        evidence.reconcile_feed_observations(
            conn, [_observation("e1", remaining=100.0)], {"u1"},
        )
    assert conn.in_transaction is False
    assert conn.execute("SELECT COUNT(*) FROM memecoin_holder_evidence").fetchone()[0] == 0

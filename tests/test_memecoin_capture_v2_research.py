import sqlite3

import pytest

from scripts.research import memecoin_capture_v2_manifest as manifest
from scripts.research import memecoin_conditioning_study as study
from scripts.research import normalize_clock_domains as clocks
from sniper.persistence import db_memecoin as m


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _legacy_event(conn, event_id, created_at, received_at):
    conn.execute(
        "INSERT INTO memecoin_events(fomo_event_id, source_user_id, source_handle, mint, ticker, "
        "side, created_at, received_at) VALUES (?,?,?,?,?,?,?,?)",
        (event_id, "u", "h", f"M-{event_id}", "T", "buy", created_at, received_at),
    )


def test_clock_classification_is_non_destructive_idempotent_and_preserves_unknown():
    conn = _conn()
    _legacy_event(conn, "host", "2026-07-10T00:00:00+00:00", "2026-07-10T07:00:00+00:00")
    _legacy_event(conn, "real", "2026-07-10T00:00:00+00:00", "2026-07-10T00:01:00+00:00")
    _legacy_event(conn, "amb", "2026-07-10T00:00:00+00:00", "2026-07-10T03:00:00+00:00")
    conn.commit()

    dry = clocks.classify_legacy_clock_rows(conn, offset_seconds=7 * 3600, apply=False)
    assert dry["memecoin_events.received_at"]["classifiable"] == 2
    assert conn.execute(
        "SELECT clock_domain FROM memecoin_events WHERE fomo_event_id='host'",
    ).fetchone()[0] == "legacy_unknown"

    clocks.classify_legacy_clock_rows(conn, offset_seconds=7 * 3600, apply=True)
    rows = {row["fomo_event_id"]: dict(row) for row in conn.execute(
        "SELECT fomo_event_id, received_at, received_at_normalized, clock_domain "
        "FROM memecoin_events WHERE fomo_event_id IN ('host','real','amb')",
    )}
    assert rows["host"] == {
        "fomo_event_id": "host", "received_at": "2026-07-10T07:00:00+00:00",
        "received_at_normalized": "2026-07-10T00:00:00+00:00",
        "clock_domain": "legacy_host_wall",
    }
    assert rows["real"]["clock_domain"] == "observed_real_utc"
    assert rows["real"]["received_at_normalized"] == "2026-07-10T00:01:00+00:00"
    assert rows["amb"]["clock_domain"] == "legacy_unknown"
    assert rows["amb"]["received_at_normalized"] is None
    again = clocks.classify_legacy_clock_rows(conn, offset_seconds=7 * 3600, apply=True)
    assert again["memecoin_events.received_at"]["updated"] == 0


def test_outcome_clock_classification_uses_join_without_rewriting_buy_time():
    conn = _conn()
    _legacy_event(conn, "e", "2026-07-10T00:00:00+00:00", "2026-07-10T07:00:00+00:00")
    m.create_signal_outcome(
        conn, fomo_event_id="e", source_user_id="u", handle="h", mint="M", ticker="T",
        buy_ts="2026-07-10T07:00:00+00:00", ref_price=1.0, their_size_usd=1000,
        now_iso="2026-07-10T07:00:00+00:00", network_id="solana",
    )
    clocks.classify_legacy_clock_rows(conn, offset_seconds=7 * 3600, apply=True)
    row = conn.execute(
        "SELECT buy_ts, buy_ts_normalized, clock_domain, capture_version "
        "FROM memecoin_signal_outcomes WHERE fomo_event_id='e'",
    ).fetchone()
    assert tuple(row) == (
        "2026-07-10T07:00:00+00:00", "2026-07-10T00:00:00+00:00",
        "legacy_host_wall", 1,
    )


def _feed(conn, event_id, observed_at, source_ts, *, version=2, network="solana", mint="M"):
    conn.execute(
        "INSERT INTO memecoin_feed_observations_v2(upstream_event_id, raw_type, normalized_side, "
        "network_id, mint, observed_at, source_ts, raw_json, capture_version) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (event_id, "large_buy", "buy", network, mint, observed_at, source_ts, "{}", version),
    )


def test_replay_order_is_information_time_then_source_then_id():
    conn = _conn()
    _feed(conn, "z", "2026-07-11T00:00:01+00:00", "2026-07-11T00:00:00+00:00")
    _feed(conn, "b", "2026-07-11T00:00:00+00:00", "2026-07-10T23:59:59+00:00")
    _feed(conn, "a", "2026-07-11T00:00:00+00:00", "2026-07-10T23:59:59+00:00")
    assert [row["upstream_event_id"] for row in manifest.replay_feed_rows(conn)] == ["a", "b", "z"]


def test_manifest_keeps_missing_and_budget_gaps_in_denominators():
    conn = _conn()
    _feed(conn, "e", "2026-07-11T00:00:00+00:00", "2026-07-10T23:59:59+00:00")
    conn.execute(
        "INSERT INTO memecoin_quote_sweeps_v2(sweep_id, observed_at, eligible_count, "
        "requested_count, captured_count, budget_skipped_count) VALUES ('s',?,3,2,1,1)",
        ("2026-07-11T00:01:00+00:00",),
    )
    conn.execute(
        "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, observed_at, "
        "price, quality, contexts) VALUES ('s','solana','M',?,1.2,'ratio_outlier','signal')",
        ("2026-07-11T00:01:00+00:00",),
    )
    outcome_id = "o"
    conn.execute(
        "INSERT INTO memecoin_signal_outcomes_v2(outcome_id, upstream_event_id, network_id, mint, "
        "signal_observed_at, ref_price, target_horizons_json, expected_marks, observed_marks, "
        "missed_marks, status, created_at) VALUES (?,?,?,?,?,1.0,'[60,300]',2,1,1,'partial',?)",
        (outcome_id, "e", "solana", "M", "2026-07-11T00:00:00+00:00",
         "2026-07-11T00:00:00+00:00"),
    )
    conn.executemany(
        "INSERT INTO memecoin_horizon_marks_v2(outcome_id,target_s,target_at,status) VALUES (?,?,?,?)",
        [(outcome_id, 60, "2026-07-11T00:01:00+00:00", "observed"),
         (outcome_id, 300, "2026-07-11T00:05:00+00:00", "missed")],
    )
    conn.commit()
    result = manifest.build_capture_manifest(conn)
    assert result["horizons"] == {"total": 2, "observed": 1, "missed": 1, "abandoned": 0,
                                   "pending": 0, "coverage": 0.5}
    assert result["sweeps"]["eligible"] == 3
    assert result["sweeps"]["budget_skipped"] == 1
    assert result["quotes"]["quality_excluded"] == 1
    assert result["distinct_chain_coins"] == 1


def test_version_guard_rejects_mixed_rows_and_study_defaults_to_v2():
    conn = _conn()
    _feed(conn, "v2", "2026-07-11T00:00:00+00:00", "2026-07-11T00:00:00+00:00")
    _feed(conn, "v1", "2026-07-11T00:00:01+00:00", "2026-07-11T00:00:00+00:00", version=1)
    with pytest.raises(ValueError, match="mixed capture versions"):
        manifest.assert_capture_version(conn, "memecoin_feed_observations_v2", 2)

    conn.execute("DELETE FROM memecoin_feed_observations_v2 WHERE upstream_event_id='v1'")
    conn.execute(
        "INSERT INTO memecoin_signal_outcomes_v2(outcome_id,upstream_event_id,network_id,mint,"
        "signal_observed_at,ref_price,target_horizons_json,expected_marks,observed_marks,status,"
        "created_at) VALUES ('o','v2','solana','M',?,1.0,'[60]',1,1,'complete',?)",
        ("2026-07-11T00:00:00+00:00", "2026-07-11T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO memecoin_horizon_marks_v2(outcome_id,target_s,target_at,observed_at,price,"
        "quality,status) VALUES ('o',60,?,?,1.2,'ok','observed')",
        ("2026-07-11T00:01:00+00:00", "2026-07-11T00:01:00+00:00"),
    )
    conn.commit()
    sightings, _positions = study.fetch(conn, capture_version=2)
    assert len(sightings) == 1 and sightings[0]["peak"] == pytest.approx(20.0)

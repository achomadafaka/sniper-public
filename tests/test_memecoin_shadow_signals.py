import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_shadow_signals import (
    evaluate_shadow_signals,
    list_shadow_signals,
)


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _seed_episode(conn, *, episode="ep1", event="e1", network="4663", mint="0xabc",
                  observed="2026-07-17T10:00:10+00:00", lag=10.0, armed=True,
                  source_price=1.0, executable_price=1.0, source="global_feed",
                  uid="u1", handle="alice"):
    conn.execute(
        "INSERT INTO memecoin_token_episodes(episode_id, chain_id, token_address, "
        "started_ts, first_signal_id, consensus_cluster_count, status, last_signal_ts, "
        "first_source_price, first_executable_price) VALUES(?,?,?,?,?,1,'open',?,?,?)",
        (episode, network, mint, observed, event, observed, source_price, executable_price),
    )
    conn.execute(
        "INSERT INTO memecoin_episode_signals(episode_id, upstream_event_id, observed_at, "
        "source_price, executable_price) VALUES(?,?,?,?,?)",
        (episode, event, observed, source_price, executable_price),
    )
    conn.execute(
        "INSERT INTO memecoin_feed_observations_v2(upstream_event_id, raw_type, "
        "normalized_side, source_user_id, source_handle, network_id, mint, ticker, "
        "their_price, source_ts, observed_at, lag_s, was_curated, was_real_approved, "
        "raw_json, capture_version, clock_domain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event, "buy", "buy", uid, handle, network, mint, "T", source_price,
         "2026-07-17T10:00:00+00:00", observed, lag, 1, int(armed), "{}", 2,
         "observed_real_utc"),
    )
    conn.execute(
        "INSERT INTO memecoin_events(fomo_event_id, source_user_id, source_handle, mint, "
        "ticker, side, their_price, created_at, received_at, lag_s, signal_source, "
        "network_id, clock_domain) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event, uid, handle, mint, "T", "buy", source_price,
         "2026-07-17T10:00:00+00:00", observed, lag, source, network,
         "observed_real_utc"),
    )
    conn.commit()


def _quote(conn, *, sweep="s1", network="4663", mint="0xabc",
           observed="2026-07-17T10:00:30+00:00", price=1.1, mcap=800_000.0,
           liquidity=50_000.0, quality="ok"):
    conn.execute(
        "INSERT INTO memecoin_quote_observations_v2(sweep_id, network_id, mint, "
        "observed_at, price, liquidity, market_cap, quality, contexts, capture_version) "
        "VALUES(?,?,?,?,?,?,?,?,?,2)",
        (sweep, network, mint, observed, price, liquidity, mcap, quality, "[]"),
    )
    conn.commit()


def test_clean_sub_1m_episode_becomes_immutable_candidate():
    conn = _conn()
    _seed_episode(conn)
    _quote(conn)

    assert evaluate_shadow_signals(conn, "2026-07-17T10:00:40+00:00") == {
        "created": 1, "waiting": 0, "collisions": 0,
    }
    row = dict(conn.execute("SELECT * FROM memecoin_shadow_signals").fetchone())
    assert row["rating"] == "candidate"
    assert json.loads(row["reason_codes_json"]) == []
    assert row["network_id"] == "4663"
    assert row["origin_event_id"] == "e1"
    assert row["signal_source"] == "global_feed"
    assert row["clock_domain"] == "observed_real_utc"

    before = dict(row)
    assert evaluate_shadow_signals(conn, "2026-07-17T10:05:00+00:00")["created"] == 0
    assert dict(conn.execute("SELECT * FROM memecoin_shadow_signals").fetchone()) == before


def test_quote_deadline_waits_then_records_explicit_reject():
    conn = _conn()
    _seed_episode(conn)

    result = evaluate_shadow_signals(conn, "2026-07-17T10:01:00+00:00")
    assert result == {"created": 0, "waiting": 1, "collisions": 0}
    assert conn.execute("SELECT COUNT(*) FROM memecoin_shadow_signals").fetchone()[0] == 0

    result = evaluate_shadow_signals(conn, "2026-07-17T10:02:00+00:00")
    assert result["created"] == 1
    row = conn.execute(
        "SELECT rating, reason_codes_json, quote_price FROM memecoin_shadow_signals"
    ).fetchone()
    assert row["rating"] == "reject"
    assert json.loads(row["reason_codes_json"]) == ["quote_unavailable"]
    assert row["quote_price"] is None


@pytest.mark.parametrize(
    "kwargs, expected_rating, expected_reason",
    [
        ({"mcap": 2_000_000.0}, "watch", "entry_mcap_1m_5m"),
        ({"mcap": 35_000_000.0}, "reject", "entry_mcap_ge_30m"),
        ({"price": 2.1}, "reject", "episode_runup_ge_100pct"),
        ({"liquidity": None}, "watch", "liquidity_unknown"),
        ({"liquidity": 0.0}, "reject", "liquidity_zero"),
    ],
)
def test_shadow_bands_are_deterministic(kwargs, expected_rating, expected_reason):
    conn = _conn()
    _seed_episode(conn)
    _quote(conn, **kwargs)
    evaluate_shadow_signals(conn, "2026-07-17T10:00:40+00:00")
    row = conn.execute(
        "SELECT rating, reason_codes_json FROM memecoin_shadow_signals"
    ).fetchone()
    assert row["rating"] == expected_rating
    assert expected_reason in json.loads(row["reason_codes_json"])


def test_unarmed_or_late_wallet_is_watch_and_consensus_never_erases_warning():
    conn = _conn()
    _seed_episode(conn, armed=False, lag=75.0)
    conn.execute(
        "UPDATE memecoin_token_episodes SET consensus_cluster_count=3 WHERE episode_id='ep1'"
    )
    conn.commit()
    _quote(conn)
    evaluate_shadow_signals(conn, "2026-07-17T10:00:40+00:00")
    row = conn.execute(
        "SELECT rating, consensus_at_signal, reason_codes_json FROM memecoin_shadow_signals"
    ).fetchone()
    assert row["rating"] == "watch"
    assert row["consensus_at_signal"] == 1
    assert json.loads(row["reason_codes_json"]) == ["wallet_not_armed", "claimed_lag_ge_60s"]


def test_unusable_probe_waits_for_a_usable_quote_inside_the_window():
    conn = _conn()
    _seed_episode(conn)
    _quote(conn, quality="probe_unpriced", price=None,
           observed="2026-07-17T10:00:20+00:00")
    assert evaluate_shadow_signals(conn, "2026-07-17T10:00:40+00:00")["waiting"] == 1
    assert conn.execute("SELECT COUNT(*) FROM memecoin_shadow_signals").fetchone()[0] == 0

    _quote(conn, sweep="s2", quality="probe_ok", price=1.1,
           observed="2026-07-17T10:00:50+00:00")
    assert evaluate_shadow_signals(conn, "2026-07-17T10:01:00+00:00")["created"] == 1
    row = conn.execute(
        "SELECT rating, quote_observed_at FROM memecoin_shadow_signals"
    ).fetchone()
    assert row["rating"] == "candidate"
    assert row["quote_observed_at"] == "2026-07-17T10:00:50+00:00"


def test_only_unusable_quote_becomes_reject_after_deadline():
    conn = _conn()
    _seed_episode(conn)
    _quote(conn, quality="probe_unpriced", price=None,
           observed="2026-07-17T10:00:20+00:00")
    assert evaluate_shadow_signals(conn, "2026-07-17T10:02:00+00:00")["created"] == 1
    row = conn.execute(
        "SELECT rating, reason_codes_json FROM memecoin_shadow_signals"
    ).fetchone()
    assert row["rating"] == "reject"
    assert json.loads(row["reason_codes_json"]) == ["quote_unusable"]


def test_same_mint_on_two_chains_produces_two_coin_scoped_signals():
    conn = _conn()
    _seed_episode(conn, episode="ep4663", event="e4663", network="4663")
    _seed_episode(conn, episode="ep8453", event="e8453", network="8453")
    _quote(conn, sweep="s4663", network="4663")
    _quote(conn, sweep="s8453", network="8453")
    result = evaluate_shadow_signals(conn, "2026-07-17T10:00:40+00:00")
    assert result["created"] == 2
    assert [tuple(row) for row in conn.execute(
        "SELECT network_id, mint FROM memecoin_shadow_signals ORDER BY network_id"
    )] == [("4663", "0xabc"), ("8453", "0xabc")]


def test_consensus_at_signal_excludes_a_prior_episode():
    conn = _conn()
    conn.execute(
        "INSERT INTO memecoin_feed_observations_v2(upstream_event_id, raw_type, "
        "normalized_side, source_user_id, network_id, mint, observed_at, "
        "was_real_approved, raw_json) VALUES('old','buy','buy','old-user','4663',"
        "'0xabc','2026-07-17T09:00:00+00:00',1,'{}')"
    )
    conn.commit()
    _seed_episode(conn)
    _quote(conn)
    evaluate_shadow_signals(conn, "2026-07-17T10:00:40+00:00")
    assert conn.execute(
        "SELECT consensus_at_signal FROM memecoin_shadow_signals"
    ).fetchone()[0] == 1


def test_exact_event_identity_mismatch_is_rejected_not_reassigned():
    conn = _conn()
    _seed_episode(conn)
    conn.execute(
        "UPDATE memecoin_feed_observations_v2 SET network_id='8453' WHERE upstream_event_id='e1'"
    )
    conn.commit()
    _quote(conn)
    evaluate_shadow_signals(conn, "2026-07-17T10:00:40+00:00")
    row = conn.execute(
        "SELECT network_id, rating, reason_codes_json FROM memecoin_shadow_signals"
    ).fetchone()
    assert row["network_id"] == "4663"
    assert row["rating"] == "reject"
    assert "origin_identity_mismatch" in json.loads(row["reason_codes_json"])


def test_list_filters_chain_and_lane_without_writes():
    conn = _conn()
    _seed_episode(conn, source="follow_activity")
    _quote(conn)
    evaluate_shadow_signals(conn, "2026-07-17T10:00:40+00:00")
    changes = conn.total_changes
    assert list_shadow_signals(conn, chain="4663", source="follow_activity")[0]["rating"] == "candidate"
    assert list_shadow_signals(conn, chain="8453") == []
    assert conn.total_changes == changes

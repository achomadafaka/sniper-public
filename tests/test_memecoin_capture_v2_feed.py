import sqlite3

from sniper.feeds import fomo_feed
from sniper.persistence import db_memecoin as m


OBSERVED = "2026-07-11T12:00:05+00:00"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def test_non_trade_feed_type_is_retained_without_copy_event():
    raw = {
        "id": "listing-1",
        "type": "new_token_listing",
        "networkId": "1399811149",
        "tokenAddress": "MintX",
        "createdAt": "2026-07-11T12:00:00Z",
        "body": {"ticker": "X", "marketCap": "125000"},
    }
    observation = fomo_feed.normalize_feed_observation(raw, OBSERVED)
    assert observation.event is None
    assert observation.raw_type == "new_token_listing"
    assert observation.network_id == "solana"
    assert observation.source_ts == "2026-07-11T12:00:00+00:00"
    assert observation.lag_s == 5.0

    conn = _conn()
    assert m.insert_feed_observation_v2(
        conn, observation, was_curated=False, was_real_approved=False,
    ) is True
    row = dict(conn.execute("SELECT * FROM memecoin_feed_observations_v2").fetchone())
    assert row["raw_type"] == "new_token_listing"
    assert row["normalized_side"] is None
    assert row["capture_version"] == 2
    assert row["clock_domain"] == "observed_real_utc"
    assert '"type":"new_token_listing"' in row["raw_json"]


def test_trade_observation_carries_optional_normalized_copy_event():
    raw = {
        "id": "buy-1", "type": "large_buy", "userId": "u1",
        "networkId": "8453", "tokenAddress": "0xabc",
        "createdAt": "2026-07-11T12:00:00Z",
        "body": {"userHandle": "alice", "ticker": "A", "price": "0.01",
                 "marketCap": "1000000", "currentSizeUsd": "5000"},
    }
    observation = fomo_feed.normalize_feed_observation(raw, OBSERVED)
    assert observation.event is not None
    assert observation.event.side == "buy"
    assert observation.normalized_side == "buy"
    assert observation.source_user_id == "u1"


def test_capture_time_cohort_flags_do_not_change_with_roster():
    conn = _conn()
    raw = {"id": "e1", "type": "large_buy", "userId": "u1", "tokenAddress": "M",
           "createdAt": "2026-07-11T12:00:00Z", "body": {}}
    observation = fomo_feed.normalize_feed_observation(raw, OBSERVED)
    m.insert_feed_observation_v2(conn, observation, was_curated=False, was_real_approved=False)

    m.add_wallet(conn, "u1", "alice")
    m.set_wallet_real_approved(conn, "u1", True, force=True)
    row = conn.execute(
        "SELECT was_curated, was_real_approved FROM memecoin_feed_observations_v2 WHERE upstream_event_id='e1'",
    ).fetchone()
    assert tuple(row) == (0, 0)


def test_upstream_and_synthetic_ids_deduplicate_redelivery():
    conn = _conn()
    raw = {"type": "price_since_listing", "tokenAddress": "M",
           "createdAt": "2026-07-11T12:00:00Z", "body": {"change": 10}}
    first = fomo_feed.normalize_feed_observation(raw, OBSERVED)
    second = fomo_feed.normalize_feed_observation(dict(raw), "2026-07-11T12:00:06+00:00")
    assert first.upstream_event_id.startswith("v2-synth-")
    assert first.upstream_event_id == second.upstream_event_id
    assert m.insert_feed_observation_v2(conn, first, False, False) is True
    assert m.insert_feed_observation_v2(conn, second, True, True) is False
    assert conn.execute("SELECT COUNT(*) FROM memecoin_feed_observations_v2").fetchone()[0] == 1


def test_invalid_source_timestamp_is_preserved_raw_not_fabricated():
    observation = fomo_feed.normalize_feed_observation(
        {"id": "bad-ts", "type": "manual", "createdAt": "not-a-time", "body": {}},
        OBSERVED,
    )
    assert observation.source_ts_raw == "not-a-time"
    assert observation.source_ts is None
    assert observation.lag_s is None

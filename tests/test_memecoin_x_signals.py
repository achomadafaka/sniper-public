import sqlite3

from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables


MINT = "0x1111111111111111111111111111111111111111"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    return conn


def _observation(conn, pid, actor, observed, *, network="4663", mint=MINT,
                 author="poptyedev", role="tier2_source", relationship="repost"):
    obs = conn.execute(
        """
        INSERT INTO memecoin_x_card_observations
          (capture_run_id,canonical_post_id,author_handle,actor_handle,relationship,
           observed_at,clock_domain,parser_version,raw_card_json)
        VALUES ('r',?,?,?,?,?,'observed_real_utc',1,'{}')
        """,
        (pid, author, actor, relationship, observed),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO memecoin_x_forward_posts
          (canonical_post_id,author_handle,author_role_at_capture,first_observed_at,
           last_observed_at,parser_version,clock_domain)
        VALUES (?,?,?,?,?,1,'observed_real_utc')
        """,
        (pid, author, role, observed, observed),
    )
    conn.execute(
        """
        INSERT INTO memecoin_x_coin_mentions
          (canonical_post_id,network_id,mint,extraction_method,eligible,parser_version,
           source_observation_id)
        VALUES (?,?,?,'contract_text',1,1,?)
        """,
        (pid, network, mint, obs),
    )
    conn.execute(
        """
        INSERT INTO memecoin_x_relationships
          (canonical_post_id,actor_handle,actor_role_at_obs,actor_is_tier1_at_obs,
           relationship,observed_at,clock_domain,source_observation_id)
        VALUES (?,?,'tier1_trader',1,?,?,'observed_real_utc',?)
        """,
        (pid, actor, relationship, observed, obs),
    )
    conn.commit()
    return obs


def _events(conn, observation_id):
    from sniper.analysis.memecoin_x.signals import events_for_observation

    return events_for_observation(conn, observation_id=observation_id)


def test_exact_capture_and_tier2_scout_keep_robinhood_and_base_separate():
    conn = _conn()
    robinhood = _observation(conn, "p1", "zinceth", "2026-07-13T00:00:00+00:00")
    base = _observation(conn, "p2", "zinceth", "2026-07-13T00:01:00+00:00",
                        network="8453")
    rh_events = _events(conn, robinhood)
    base_events = _events(conn, base)
    assert {event["event_type"] for event in rh_events} == {"capture_tag", "tier2_scout"}
    assert {event["network_id"] for event in rh_events} == {"4663"}
    assert {event["network_id"] for event in base_events} == {"8453"}
    assert all(event["mint"] == MINT for event in rh_events + base_events)


def test_fast_consensus_uses_second_distinct_actor_and_inclusive_60_minute_boundary():
    conn = _conn()
    first = _observation(conn, "p1", "zinceth", "2026-07-13T00:00:00+00:00")
    second = _observation(conn, "p2", "goyimpnl", "2026-07-13T01:00:00+00:00")
    third = _observation(conn, "p3", "binkieeefomo", "2026-07-13T01:01:00+00:00")
    assert not any(event["event_type"] == "fast_consensus" for event in _events(conn, first))
    consensus = next(event for event in _events(conn, second)
                     if event["event_type"] == "fast_consensus")
    assert consensus["evidence_observation_ids"] == [first, second]
    assert consensus["actor_handles"] == ["zinceth", "goyimpnl"]
    assert consensus["relationship_counts"] == {"repost": 2}
    assert not any(event["event_type"] == "fast_consensus" for event in _events(conn, third))


def test_consensus_rejects_60_minutes_plus_one_second_and_same_actor_repeat():
    conn = _conn()
    _observation(conn, "p1", "zinceth", "2026-07-13T00:00:00+00:00")
    same = _observation(conn, "p2", "zinceth", "2026-07-13T00:59:59+00:00")
    assert not any(event["event_type"] == "fast_consensus" for event in _events(conn, same))

    late_conn = _conn()
    _observation(late_conn, "p1", "zinceth", "2026-07-13T00:00:00+00:00")
    late = _observation(late_conn, "p2", "goyimpnl", "2026-07-13T01:00:01+00:00")
    assert not any(event["event_type"] == "fast_consensus"
                   for event in _events(late_conn, late))


def test_official_context_never_enters_consensus():
    conn = _conn()
    _observation(conn, "p1", "zinceth", "2026-07-13T00:00:00+00:00",
                 author="vladtenev", role="official_context")
    second = _observation(conn, "p2", "goyimpnl", "2026-07-13T00:01:00+00:00",
                          author="vladtenev", role="official_context")
    events = _events(conn, second)
    assert {event["event_type"] for event in events} == {"capture_tag", "official_context"}


def test_different_exact_coins_never_form_consensus():
    conn = _conn()
    _observation(conn, "p1", "zinceth", "2026-07-13T00:00:00+00:00")
    second = _observation(
        conn, "p2", "goyimpnl", "2026-07-13T00:59:59+00:00",
        mint="0x2222222222222222222222222222222222222222")
    assert not any(event["event_type"] == "fast_consensus"
                   for event in _events(conn, second))


def test_ineligible_or_wrong_clock_observation_emits_nothing():
    conn = _conn()
    obs = _observation(conn, "p1", "zinceth", "2026-07-13T00:00:00+00:00")
    conn.execute("UPDATE memecoin_x_coin_mentions SET eligible=0")
    assert _events(conn, obs) == []
    conn.execute("UPDATE memecoin_x_coin_mentions SET eligible=1")
    conn.execute("UPDATE memecoin_x_card_observations SET clock_domain='legacy_unknown'")
    assert _events(conn, obs) == []

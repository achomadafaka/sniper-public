import sqlite3

import pytest

from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables


AS_OF = "2026-07-14T00:00:00+00:00"
SINCE = "2026-07-13T00:00:00+00:00"
MINT = "0x1111111111111111111111111111111111111111"


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    conn.executemany(
        "INSERT INTO memecoin_x_accounts(handle,role,active,needs_review) VALUES (?,?,1,0)",
        [("poptyedev", "tier2_source"), ("vladtenev", "official_context")],
    )
    return conn


def _call(conn, pid, author, role, network, mint, observed, *, posted="2040-01-01T00:00:00Z"):
    obs = conn.execute(
        """
        INSERT INTO memecoin_x_card_observations
          (capture_run_id,canonical_post_id,author_handle,actor_handle,relationship,
           observed_at,clock_domain,parser_version,full_text,text_complete,posted_at,
           extracted_urls,raw_card_json)
        VALUES ('capture',?,?,?,'direct',?,'observed_real_utc',1,'',1,?,'[]','{}')
        """,
        (pid, author, author, observed, posted),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO memecoin_x_forward_posts
          (canonical_post_id,author_handle,author_role_at_capture,first_observed_at,
           last_observed_at,parser_version,clock_domain,posted_at)
        VALUES (?,?,?,?,?,1,'observed_real_utc',?)
        """,
        (pid, author, role, observed, observed, posted),
    )
    mention = conn.execute(
        """
        INSERT INTO memecoin_x_coin_mentions
          (canonical_post_id,network_id,mint,extraction_method,ambiguous,eligible,
           parser_version,source_observation_id)
        VALUES (?,?,?,'contract_text',0,1,1,?)
        """,
        (pid, network, mint, obs),
    ).lastrowid
    conn.execute(
        """
        INSERT INTO memecoin_x_relationships
          (canonical_post_id,actor_handle,actor_role_at_obs,actor_is_tier1_at_obs,
           relationship,observed_at,clock_domain,source_observation_id)
        VALUES (?,?,?,?,'direct',?,'observed_real_utc',?)
        """,
        (pid, author, role, 1 if role == "tier1_trader" else 0, observed, obs),
    )
    return {"pid": pid, "obs": obs, "mention": mention, "network": network, "mint": mint,
            "observed": observed, "role": role}


def _relationship(conn, call, actor, relationship, observed):
    conn.execute(
        """
        INSERT INTO memecoin_x_relationships
          (canonical_post_id,actor_handle,actor_role_at_obs,actor_is_tier1_at_obs,
           relationship,observed_at,clock_domain,source_observation_id)
        VALUES (?,?,'tier1_trader',1,?,?,'observed_real_utc',?)
        """,
        (call["pid"], actor, relationship, observed, call["obs"]),
    )


def _buy(conn, event, user, buyer, observed):
    conn.execute(
        """
        INSERT INTO memecoin_x_fomo_buyer_snapshots
          (fomo_upstream_event_id,buying_source_user_id,source_handle_at_obs,
           buyer_x_handle_at_obs,tier1_at_obs,fomo_observed_at,created_at)
        VALUES (?,?,?,?,1,?,?)
        """,
        (event, user, buyer, buyer, observed, observed),
    )


def _link(conn, call, event, user, observed, lane):
    conn.execute(
        """
        INSERT INTO memecoin_x_forward_links
          (canonical_post_id,coin_mention_id,fomo_upstream_event_id,buying_source_user_id,
           network_id,mint,x_observed_at,x_clock_domain,fomo_observed_at,fomo_clock_domain,
           lead_seconds,evidence_lane,tier1_at_obs,source_role_at_obs,eligible)
        VALUES (?,?,?,?,?,?,?,'observed_real_utc',?,'observed_real_utc',300,?,1,?,1)
        """,
        (call["pid"], call["mention"], event, user, call["network"], call["mint"],
         call["observed"], observed, lane, call["role"]),
    )


def _window(conn):
    from sniper.analysis.memecoin_x.read_models import build_window

    conn.commit()
    return build_window(conn, since=SINCE, as_of=AS_OF, label="24h")


def test_source_rows_keep_calls_lanes_buyers_relationships_and_networks_separate():
    conn = _conn()
    call_a = _call(conn, "p1", "poptyedev", "tier2_source", "4663", MINT,
                   "2026-07-13T01:00:00+00:00")
    _call(conn, "p2", "poptyedev", "tier2_source", "4663", MINT,
          "2026-07-13T02:00:00+00:00")
    call_c = _call(conn, "p3", "poptyedev", "tier2_source", "8453", MINT,
                   "2026-07-13T03:00:00+00:00")
    _relationship(conn, call_a, "zinceth", "repost", "2026-07-13T01:01:00+00:00")
    _relationship(conn, call_a, "goyimpnl", "quote", "2026-07-13T01:02:00+00:00")
    _buy(conn, "e1", "u1", "zinceth", "2026-07-13T01:05:00+00:00")
    _buy(conn, "e2", "u1", "zinceth", "2026-07-13T03:05:00+00:00")
    _link(conn, call_a, "e1", "u1", "2026-07-13T01:05:00+00:00", "amplified")
    _link(conn, call_c, "e2", "u1", "2026-07-13T03:05:00+00:00", "coincidence")

    rows = _window(conn)["sources"]
    robinhood = next(row for row in rows if row["network_id"] == "4663")
    base = next(row for row in rows if row["network_id"] == "8453")
    assert robinhood == {
        "handle": "poptyedev", "role": "tier2_source", "network_id": "4663",
        "exact_calls": 2, "followed_calls": 1, "follow_through_rate": 0.5,
        "amplified_links": 1, "coincidence_links": 0, "distinct_tier1_buyers": 1,
        "direct_count": 2, "repost_count": 1, "quote_count": 1,
        "reply_count": 0,
    }
    assert base["exact_calls"] == base["followed_calls"] == 1
    assert base["coincidence_links"] == 1 and base["amplified_links"] == 0
    assert base["distinct_tier1_buyers"] == 1


def test_official_and_unknown_are_quarantined_from_ranked_sources():
    conn = _conn()
    _call(conn, "official", "vladtenev", "official_context", "4663", MINT,
          "2026-07-13T01:00:00+00:00")
    _call(conn, "unknown", "newalpha", None, "4663", MINT,
          "2026-07-13T02:00:00+00:00")
    result = _window(conn)
    assert result["sources"] == []
    assert result["official_context"][0]["handle"] == "vladtenev"
    assert result["suggestions"][0]["handle"] == "newalpha"
    assert result["suggestions"][0]["needs_review"] is True


def test_actor_cooccurrence_is_raw_and_does_not_change_source_order():
    conn = _conn()
    first = _call(conn, "p1", "poptyedev", "tier2_source", "4663", MINT,
                  "2026-07-13T01:00:00+00:00")
    second = _call(conn, "p2", "poptyedev", "tier2_source", "4663",
                   "0x2222222222222222222222222222222222222222",
                   "2026-07-13T02:00:00+00:00")
    for call in (first, second):
        _relationship(conn, call, "zinceth", "repost", "2026-07-13T03:00:00+00:00")
        _relationship(conn, call, "goyimpnl", "quote", "2026-07-13T03:01:00+00:00")
    result = _window(conn)
    assert result["sources"][0]["handle"] == "poptyedev"
    assert result["actor_cooccurrence"] == [{
        "actor_a": "goyimpnl", "actor_b": "zinceth", "shared_exact_coins": 2,
        "actor_a_exact_coins": 2, "actor_b_exact_coins": 2,
    }]
    assert "score" not in result["sources"][0]


def test_relationship_before_exact_identity_is_not_counted_by_post_fallback():
    conn = _conn()
    call = _call(conn, "p1", "poptyedev", "tier2_source", "4663", MINT,
                 "2026-07-13T02:00:00+00:00")
    conn.execute(
        """
        INSERT INTO memecoin_x_relationships
          (canonical_post_id,actor_handle,actor_role_at_obs,actor_is_tier1_at_obs,
           relationship,observed_at,clock_domain,source_observation_id)
        VALUES (?,'zinceth','tier1_trader',1,'repost',
                '2026-07-13T01:00:00+00:00','observed_real_utc',NULL)
        """,
        (call["pid"],),
    )
    result = _window(conn)
    assert result["sources"][0]["repost_count"] == 0
    assert result["actor_cooccurrence"] == []


def test_coverage_exposes_gaps_clock_failures_and_none_for_zero_buy_denominator():
    conn = _conn()
    conn.executemany(
        """
        INSERT INTO memecoin_x_capture_runs
          (run_id,parser_version,started_at,finished_at,clock_domain,status)
        VALUES (?,1,?,?,?,?)
        """,
        [
            ("ok", "2026-07-13T01:00:00+00:00", "2026-07-13T01:01:00+00:00",
             "observed_real_utc", "ok"),
            ("gap", "2026-07-13T02:00:00+00:00", "2026-07-13T02:01:00+00:00",
             "legacy_unknown", "coverage_gap"),
        ],
    )
    conn.execute(
        """
        INSERT INTO memecoin_x_capture_diagnostics
          (capture_run_id,observed_at,clock_domain,diagnostic_code,parser_version)
        VALUES ('gap','2026-07-13T02:00:00+00:00','legacy_unknown','coverage_gap',1)
        """
    )
    coverage = _window(conn)["coverage"]
    assert coverage == {
        "capture_runs": 2, "healthy_clock_runs": 1, "coverage_gap_runs": 1,
        "eligible_observations": 0, "diagnostic_observations": 1,
        "eligible_tier1_buys": 0, "linked_tier1_buys": 0, "buy_coverage_rate": None,
    }


def test_posted_at_and_retrospective_rows_never_affect_forward_model():
    from sniper.analysis.memecoin_x.read_models import build_source_graph

    conn = _conn()
    _call(conn, "p1", "poptyedev", "tier2_source", "4663", MINT,
          "2026-07-13T01:00:00+00:00", posted="1999-01-01T00:00:00Z")
    before = build_source_graph(conn, as_of=AS_OF)
    conn.execute("UPDATE memecoin_x_forward_posts SET posted_at='2099-01-01T00:00:00Z'")
    conn.execute(
        """
        INSERT INTO memecoin_x_backfill_candidates
          (post_id,author,posted_at,network_id,mint,evidence_mode)
        VALUES ('retro','zinceth','1999-01-01','4663',?,'retrospective')
        """,
        (MINT,),
    )
    assert build_source_graph(conn, as_of=AS_OF) == before


@pytest.mark.parametrize("since,as_of", [
    ("2026-07-13", AS_OF), (SINCE, "2026-07-14"), (AS_OF, SINCE),
])
def test_window_rejects_naive_or_reversed_bounds(since, as_of):
    from sniper.analysis.memecoin_x.read_models import build_window

    with pytest.raises(ValueError):
        build_window(_conn(), since=since, as_of=as_of, label="24h")

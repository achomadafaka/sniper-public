import sqlite3

from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables, seed_roster
from sniper.analysis.memecoin_x import roster as R
from sniper.analysis.memecoin_x.correlator import build_forward_links

NOW = "2026-07-13T00:00:00+00:00"
X0 = "2026-07-13T12:00:00+00:00"
SOL = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"


def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(c); seed_roster(c, R.INITIAL_ROSTER, now_iso=NOW)
    return c


def _seed_mention(conn, pid, author, net, mint, x_obs, *, actor=None, rel=None, rel_obs=None,
                  clock="observed_real_utc"):
    role = conn.execute(
        "SELECT role FROM memecoin_x_accounts WHERE handle=?", (author,)).fetchone()
    role = role["role"] if role else None
    cur = conn.execute("""
        INSERT INTO memecoin_x_card_observations
            (capture_run_id, canonical_post_id, author_handle, actor_handle, relationship,
             observed_at, clock_domain, parser_version, full_text, text_complete,
             posted_at_raw, posted_at, extracted_urls, raw_card_json)
        VALUES ('run',?,?,?, 'direct', ?, ?, 1, '', 1, NULL, NULL, '[]', '{}')
    """, (pid, author, author, x_obs, clock))
    obs_id = cur.lastrowid
    conn.execute("""
        INSERT INTO memecoin_x_forward_posts
            (canonical_post_id, url, author_handle, author_role_at_capture, full_text,
             text_complete, first_observed_at, last_observed_at, parser_version, clock_domain)
        VALUES (?,?,?,?, '', 1, ?, ?, 1, ?)
    """, (pid, f"https://x.com/{author}/status/{pid}", author, role, x_obs, x_obs, clock))
    conn.execute("""
        INSERT INTO memecoin_x_coin_mentions
            (canonical_post_id, network_id, mint, ticker, extraction_method, ambiguous,
             eligible, parser_version, source_observation_id)
        VALUES (?,?,?, NULL, 'contract_text', 0, 1, 1, ?)
    """, (pid, net, mint, obs_id))
    if actor and rel:
        conn.execute("""
            INSERT INTO memecoin_x_relationships
                (canonical_post_id, actor_handle, actor_role_at_obs, actor_is_tier1_at_obs,
                 relationship, observed_at, clock_domain, source_observation_id)
            VALUES (?,?, 'tier1_trader', 1, ?, ?, 'observed_real_utc', ?)
        """, (pid, actor, rel, rel_obs, obs_id))
    conn.commit()


def _buy(handle, mint, observed_at, net="solana", clock="observed_real_utc", uid=None, eid="E1"):
    return {"upstream_event_id": eid, "source_handle": handle, "source_user_id": uid or handle,
            "network_id": net, "mint": mint, "observed_at": observed_at, "clock_domain": clock,
            "capture_version": 2, "normalized_side": "buy", "was_real_approved": 1}


def _links(conn):
    return conn.execute("SELECT * FROM memecoin_x_forward_links").fetchall()


def test_coincidence_link_when_buyer_did_not_amplify():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)          # Tier-2 source posts
    res = build_forward_links(c, [_buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")])
    assert res == {"created": 1, "upgraded": 0, "amplified": 0, "coincidence": 1}
    ln = _links(c)[0]
    assert ln["evidence_lane"] == "coincidence" and ln["eligible"] == 1
    assert abs(ln["lead_seconds"] - 300) < 1 and ln["buyer_relationship"] is None


def test_amplified_link_when_buyer_reposted_before_buy():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0,
                  actor="zinceth", rel="repost", rel_obs="2026-07-13T12:01:00+00:00")
    res = build_forward_links(c, [_buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")])
    assert res == {"created": 1, "upgraded": 0, "amplified": 1, "coincidence": 0}
    assert _links(c)[0]["evidence_lane"] == "amplified"
    assert _links(c)[0]["buyer_relationship"] == "repost"


def test_self_source_and_buyer_is_skipped():
    c = _conn()
    _seed_mention(c, "P1", "zinceth", "solana", SOL, X0)            # buyer posted it themselves
    res = build_forward_links(c, [_buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")])
    assert res["created"] == 0 and _links(c) == []


def test_horizon_excludes_late_buy():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)
    res = build_forward_links(c, [_buy("zinceth", SOL, "2026-07-13T19:00:01+00:00")])  # >6h
    assert res["created"] == 0


def test_non_tier1_buyer_skipped():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)
    res = build_forward_links(c, [_buy("some_rando", SOL, "2026-07-13T12:05:00+00:00")])
    assert res["created"] == 0


def test_legacy_clock_fomo_buy_skipped():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)
    res = build_forward_links(c, [_buy("zinceth", SOL, "2026-07-13T12:05:00+00:00",
                                       clock="legacy_unknown")])
    assert res["created"] == 0


def test_official_source_is_stored_but_ineligible():
    c = _conn()
    _seed_mention(c, "P1", "vladtenev", "solana", SOL, X0)          # official-context source
    res = build_forward_links(c, [_buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")])
    ln = _links(c)[0]
    assert ln["eligible"] == 0 and ln["exclusion_reason"] == "official_context_source"
    assert res["coincidence"] == 0 and res["amplified"] == 0


def test_idempotent_rerun_no_duplicate():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)
    buys = [_buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")]
    build_forward_links(c, buys)
    res2 = build_forward_links(c, buys)
    assert res2["created"] == 0 and len(_links(c)) == 1


def test_late_pre_buy_amplification_upgrades_existing_coincidence():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)
    buys = [_buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")]
    build_forward_links(c, buys)
    obs_id = c.execute(
        "SELECT id FROM memecoin_x_card_observations WHERE canonical_post_id='P1'").fetchone()[0]
    c.execute("""
        INSERT INTO memecoin_x_relationships
            (canonical_post_id, actor_handle, actor_role_at_obs, actor_is_tier1_at_obs,
             relationship, observed_at, clock_domain, source_observation_id)
        VALUES ('P1','zinceth','tier1_trader',1,'repost',
                '2026-07-13T12:01:00+00:00','observed_real_utc',?)
    """, (obs_id,))
    c.commit()

    result = build_forward_links(c, buys)
    link = _links(c)[0]
    assert result["created"] == 0 and result["upgraded"] == 1
    assert link["evidence_lane"] == "amplified"
    assert link["buyer_relationship"] == "repost"


def test_direct_or_pre_mention_relationship_does_not_amplify():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0,
                  actor="zinceth", rel="direct", rel_obs="2026-07-13T12:01:00+00:00")
    result = build_forward_links(
        c, [_buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")])
    assert result["coincidence"] == 1
    assert _links(c)[0]["evidence_lane"] == "coincidence"

    c2 = _conn()
    _seed_mention(c2, "P2", "poptyedev", "solana", SOL, X0,
                  actor="zinceth", rel="repost", rel_obs="2026-07-13T11:59:59+00:00")
    build_forward_links(c2, [_buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")])
    assert _links(c2)[0]["evidence_lane"] == "coincidence"


def test_correlator_rejects_non_v2_sell_or_unapproved_input():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)
    bad = []
    for field, value in (("capture_version", 1), ("normalized_side", "sell"),
                         ("was_real_approved", 0)):
        buy = _buy("zinceth", SOL, "2026-07-13T12:05:00+00:00",
                   eid=f"bad-{field}")
        buy[field] = value
        bad.append(buy)
    result = build_forward_links(c, bad)
    assert result["created"] == 0
    assert _links(c) == []


def test_robinhood_4663_does_not_cross_match_base_for_same_evm_address():
    c = _conn()
    mint = "0xabcdef0000000000000000000000000000000005"
    _seed_mention(c, "P1", "hooddotfun", "4663", mint, X0)
    base = _buy("zinceth", mint, "2026-07-13T12:05:00+00:00", net="8453", eid="base")
    hood = _buy("zinceth", mint, "2026-07-13T12:06:00+00:00", net="4663", eid="hood")
    assert build_forward_links(c, [base])["created"] == 0
    assert build_forward_links(c, [hood])["created"] == 1
    assert _links(c)[0]["network_id"] == "4663"


def test_correlator_rejects_blank_direct_input_identity():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)
    buy = _buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")
    for field in ("upstream_event_id", "source_user_id", "source_handle", "network_id", "mint"):
        bad = {**buy, field: " ", "upstream_event_id": f"blank-{field}"}
        if field == "upstream_event_id":
            bad[field] = " "
        assert build_forward_links(c, [bad])["created"] == 0
    assert _links(c) == []


def test_buyer_snapshot_prevents_current_roster_from_reinterpreting_buy():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)
    buy = _buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")
    assert build_forward_links(c, [buy])["created"] == 1
    snapshot = c.execute("""
        SELECT buyer_x_handle_at_obs, tier1_at_obs
        FROM memecoin_x_fomo_buyer_snapshots
    """).fetchone()
    assert tuple(snapshot) == ("zinceth", 1)

    c.execute("DELETE FROM memecoin_x_forward_links")
    c.execute("""
        UPDATE memecoin_x_accounts
        SET role='suggested', fomo_handle='renamed'
        WHERE handle='zinceth'
    """)
    c.commit()
    assert build_forward_links(c, [buy])["created"] == 1
    assert _links(c)[0]["tier1_at_obs"] == 1


def test_buy_time_approval_not_current_role_sets_tier1_snapshot():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)
    c.execute("UPDATE memecoin_x_accounts SET role='suggested' WHERE handle='zinceth'")
    c.commit()
    buy = _buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")
    assert build_forward_links(c, [buy])["created"] == 1
    assert _links(c)[0]["tier1_at_obs"] == 1


def test_unresolved_first_buy_snapshot_cannot_be_reinterpreted_by_later_roster_edit():
    c = _conn()
    _seed_mention(c, "P1", "poptyedev", "solana", SOL, X0)
    c.execute("UPDATE memecoin_x_accounts SET fomo_handle=NULL WHERE handle='zinceth'")
    c.commit()
    buy = _buy("zinceth", SOL, "2026-07-13T12:05:00+00:00")
    assert build_forward_links(c, [buy])["created"] == 0
    snapshot = c.execute("""
        SELECT buyer_x_handle_at_obs, tier1_at_obs
        FROM memecoin_x_fomo_buyer_snapshots
    """).fetchone()
    assert snapshot["buyer_x_handle_at_obs"] is None
    assert snapshot["tier1_at_obs"] == 1

    c.execute("UPDATE memecoin_x_accounts SET fomo_handle='zinceth' WHERE handle='zinceth'")
    c.commit()
    assert build_forward_links(c, [buy])["created"] == 0
    assert _links(c) == []

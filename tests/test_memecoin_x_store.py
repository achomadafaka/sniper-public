import json
import sqlite3
from copy import deepcopy

import pytest
from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables

X_TABLES = [
    "memecoin_x_accounts", "memecoin_x_capture_runs", "memecoin_x_forward_posts",
    "memecoin_x_card_observations", "memecoin_x_relationships", "memecoin_x_coin_mentions",
    "memecoin_x_forward_links", "memecoin_x_backfill_candidates",
    "memecoin_x_capture_diagnostics", "memecoin_x_fomo_buyer_snapshots",
    "memecoin_x_correlator_runs",
    "memecoin_x_signal_runs", "memecoin_x_signal_events",
    "memecoin_x_notification_outbox", "memecoin_x_notification_attempts",
    "memecoin_x_llm_jobs", "memecoin_x_llm_annotations", "memecoin_x_llm_watermarks",
]


def _mem():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def test_ensure_creates_all_tables_idempotently():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    ensure_memecoin_x_tables(conn)  # second call must not raise
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    for t in X_TABLES:
        assert t in names, f"missing table {t}"
    buyer_x = next(row for row in conn.execute(
        "PRAGMA table_info(memecoin_x_fomo_buyer_snapshots)")
        if row[1] == "buyer_x_handle_at_obs")
    assert buyer_x[3] == 0  # unresolved first observation is stored, never inferred later


def test_backfill_candidates_rejects_non_retrospective():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    # CHECK(evidence_mode='retrospective') must reject anything else
    try:
        conn.execute(
            "INSERT INTO memecoin_x_backfill_candidates (post_id, evidence_mode) "
            "VALUES ('p1', 'forward')")
        conn.commit()
        assert False, "expected CHECK constraint to reject non-retrospective row"
    except sqlite3.IntegrityError:
        pass


def test_relationship_unique_identity():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    row = ("post1", "zinceth", "tier1_trader", 1, "repost",
           "2026-07-13T00:00:00+00:00", "run1", "observed_real_utc", None, None)
    cols = ("canonical_post_id, actor_handle, actor_role_at_obs, actor_is_tier1_at_obs, "
            "relationship, observed_at, capture_run_id, clock_domain, referenced_author, "
            "referenced_post_id")
    q = f"INSERT INTO memecoin_x_relationships ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)"
    conn.execute(q, row); conn.commit()
    try:
        conn.execute(q, row); conn.commit()
        assert False, "expected UNIQUE(canonical_post_id, actor_handle, relationship)"
    except sqlite3.IntegrityError:
        pass


def test_coin_mention_identity_is_null_safe():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    row = ("post1", None, None, "WIF", "ticker_only", 0, 0, 1)
    sql = """INSERT INTO memecoin_x_coin_mentions
             (canonical_post_id, network_id, mint, ticker, extraction_method,
              ambiguous, eligible, parser_version)
             VALUES (?,?,?,?,?,?,?,?)"""
    conn.execute(sql, row)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, row)


def test_account_handle_identity_is_case_insensitive():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    conn.execute("INSERT INTO memecoin_x_accounts (handle, role) VALUES (?, ?)",
                 ("_oht_", "tier1_trader"))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO memecoin_x_accounts (handle, role) VALUES (?, ?)",
                     ("_OHT_", "tier1_trader"))


def test_capture_run_lifecycle_and_latest_completed_sentinel():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    snapshot = {"clock_domain": "observed_real_utc", "offset_s": 0.5,
                "offset_age_s": 30.0}
    assert X.start_capture_run(
        conn, run_id="r1", parser_version=1, started_at=NOW,
        clock_snapshot=snapshot, prior_sentinel="old") is True
    row = conn.execute("SELECT status, prior_sentinel, clock_domain FROM memecoin_x_capture_runs").fetchone()
    assert tuple(row) == ("running", "old", "observed_real_utc")
    X.finish_capture_run(
        conn, run_id="r1", finished_at="2026-07-13T00:01:00+00:00", status="ok",
        cards_seen=2, new_posts=1, new_relationships=1, parse_failures=0,
        newest_sentinel="new", sentinel_found=True, cards_scanned=9,
        stop_reason="sentinel_found", cadence_bucket="new_5_8m", scroll_count=2,
        dom_wait_ms=6500, reload_reason="healthy_ack", recovery_used=False,
        operator_postponed=True)
    scan = conn.execute(
        """SELECT cards_scanned, stop_reason, cadence_bucket, scroll_count,
                  dom_wait_ms, reload_reason, recovery_used, operator_postponed
           FROM memecoin_x_capture_runs WHERE run_id='r1'"""
    ).fetchone()
    assert tuple(scan) == (
        9, "sentinel_found", "new_5_8m", 2, 6500, "healthy_ack", 0, 1)
    assert X.latest_capture_sentinel(conn) == "new"

    X.start_capture_run(
        conn, run_id="r2", parser_version=1, started_at="2026-07-13T00:02:00+00:00",
        clock_snapshot=snapshot, prior_sentinel="new")
    X.finish_capture_run(
        conn, run_id="r2", finished_at="2026-07-13T00:03:00+00:00", status="error",
        cards_seen=0, new_posts=0, new_relationships=0, parse_failures=1,
        newest_sentinel=None, sentinel_found=None, error_detail="RuntimeError")
    assert X.latest_capture_sentinel(conn) == "new"


def test_capture_run_schema_includes_adaptive_pacing_audit_columns():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(memecoin_x_capture_runs)")}
    assert {
        "cadence_bucket", "scroll_count", "dom_wait_ms", "reload_reason",
        "recovery_used", "operator_postponed",
    } <= columns


def test_correlator_run_cursor_advances_only_on_completed_ok_runs():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    assert X.last_completed_fomo_id(conn) == 0
    assert X.start_correlator_run(
        conn, run_id="corr-ok", started_at=NOW, after_fomo_id=0) is True
    assert X.start_correlator_run(
        conn, run_id="corr-ok", started_at=NOW, after_fomo_id=0) is False
    X.finish_correlator_run(
        conn, run_id="corr-ok", finished_at="2026-07-13T00:01:00+00:00",
        status="ok", through_fomo_id=12, source_rows=12, eligible_buys=9,
        links_created=3, links_upgraded=1)
    assert X.last_completed_fomo_id(conn) == 12

    assert X.start_correlator_run(
        conn, run_id="corr-error", started_at="2026-07-13T00:02:00+00:00",
        after_fomo_id=12) is True
    X.finish_correlator_run(
        conn, run_id="corr-error", finished_at="2026-07-13T00:03:00+00:00",
        status="error", through_fomo_id=99, source_rows=4, eligible_buys=2,
        links_created=1, links_upgraded=0, error_detail="x" * 800)
    assert X.last_completed_fomo_id(conn) == 12
    row = conn.execute("""
        SELECT status, through_fomo_id, source_rows, eligible_buys, links_created,
               links_upgraded, length(error_detail)
        FROM memecoin_x_correlator_runs WHERE run_id='corr-error'
    """).fetchone()
    assert tuple(row) == ("error", 99, 4, 2, 1, 0, 500)


def test_correlator_run_rejects_invalid_status_and_double_finish():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    X.start_correlator_run(conn, run_id="corr", started_at=NOW, after_fomo_id=0)
    with pytest.raises(ValueError, match="invalid correlator run status"):
        X.finish_correlator_run(
            conn, run_id="corr", finished_at=NOW, status="running",
            through_fomo_id=0, source_rows=0, eligible_buys=0,
            links_created=0, links_upgraded=0)
    X.finish_correlator_run(
        conn, run_id="corr", finished_at=NOW, status="ok", through_fomo_id=0,
        source_rows=0, eligible_buys=0, links_created=0, links_upgraded=0)
    with pytest.raises(ValueError, match="correlator run is not running"):
        X.finish_correlator_run(
            conn, run_id="corr", finished_at=NOW, status="ok", through_fomo_id=0,
            source_rows=0, eligible_buys=0, links_created=0, links_upgraded=0)

def test_capture_diagnostic_allowlist_and_json_persistence():
    conn = _mem()
    ensure_memecoin_x_tables(conn)
    X.record_capture_diagnostic(
        conn, capture_run_id="r1", observed_at=NOW, clock_domain="observed_real_utc",
        diagnostic_code="missing_post_id", parser_version=1, card_index=2,
        details={"z": 1, "a": 2}, raw_card={"canonical_post_id": None})
    row = conn.execute("""
        SELECT diagnostic_code, card_index, details_json, raw_card_json
        FROM memecoin_x_capture_diagnostics
    """).fetchone()
    assert tuple(row) == (
        "missing_post_id", 2, '{"a": 2, "z": 1}', '{"canonical_post_id": null}')
    with pytest.raises(ValueError, match="unknown capture diagnostic"):
        X.record_capture_diagnostic(
            conn, capture_run_id="r1", observed_at=NOW, clock_domain="observed_real_utc",
            diagnostic_code="invented_code", parser_version=1)


from sniper.analysis.memecoin_x import normalize as nz
from sniper.analysis.memecoin_x import roster as R
import sniper.persistence.db_memecoin_x as X

NOW = "2026-07-13T00:00:00+00:00"


def _seeded():
    conn = _mem(); ensure_memecoin_x_tables(conn)
    X.seed_roster(conn, R.INITIAL_ROSTER, now_iso=NOW)
    return conn


def test_seed_roster_is_idempotent_and_sets_roles():
    conn = _seeded()
    X.seed_roster(conn, R.INITIAL_ROSTER, now_iso=NOW)  # again
    assert X.role_for(conn, "@ZincEth") == "tier1_trader"
    assert X.role_for(conn, "poptyedev") == "tier2_source"
    assert X.role_for(conn, "vladtenev") == "official_context"
    (n,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_accounts").fetchone()
    assert n == 35


def test_persist_card_idempotent_post_and_relationship():
    conn = _seeded()
    card = {"canonical_post_id": "1800000000000000001",
            "url": "https://x.com/zinceth/status/1800000000000000001",
            "author_handle": "zinceth", "actor_handle": "zinceth", "relationship": "direct",
            "full_text": "CA: 62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump $WIF",
            "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": None, "referenced_post_id": None}
    norm = nz.normalize_card(card)
    r1 = X.persist_card(conn, norm, observed_at=NOW, clock_domain="observed_real_utc",
                        capture_run_id="run1", now_iso=NOW)
    assert r1["new_post"] and r1["new_relationship"]
    r2 = X.persist_card(conn, norm, observed_at="2026-07-13T00:05:00+00:00",
                        clock_domain="observed_real_utc", capture_run_id="run1", now_iso=NOW)
    assert not r2["new_post"] and not r2["new_relationship"]
    # A delivery older than the current last-seen must not regress it.
    X.persist_card(conn, norm, observed_at="2026-07-13T00:02:00+00:00",
                   clock_domain="observed_real_utc", capture_run_id="run0", now_iso=NOW)
    (posts,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_posts").fetchone()
    (rels,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_relationships").fetchone()
    assert posts == 1 and rels == 1
    (mentions,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_coin_mentions").fetchone()
    assert mentions == 2  # exact CA + ticker, each deduped across all three deliveries
    # last_observed_at advanced on re-delivery
    row = conn.execute("SELECT first_observed_at, last_observed_at, author_role_at_capture "
                       "FROM memecoin_x_forward_posts").fetchone()
    assert row["first_observed_at"] == NOW
    assert row["last_observed_at"] == "2026-07-13T00:05:00+00:00"
    assert row["author_role_at_capture"] == "tier1_trader"
    raw = json.loads(conn.execute(
        "SELECT raw_card_json FROM memecoin_x_forward_posts").fetchone()[0])
    assert raw == norm["raw_card"]
    rel_seen = conn.execute(
        "SELECT observed_at FROM memecoin_x_relationships").fetchone()[0]
    assert rel_seen == NOW


def test_relationship_stamps_tier1_flag():
    conn = _seeded()
    card = {"canonical_post_id": "1800000000000000009", "url": "u",
            "author_handle": "blknoiz06", "actor_handle": "_OHT_", "relationship": "repost",
            "full_text": "x", "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": "blknoiz06", "referenced_post_id": "1"}
    X.persist_card(conn, nz.normalize_card(card), observed_at=NOW,
                   clock_domain="observed_real_utc", capture_run_id="run1", now_iso=NOW)
    rel = conn.execute("SELECT actor_role_at_obs, actor_is_tier1_at_obs "
                       "FROM memecoin_x_relationships").fetchone()
    assert rel["actor_role_at_obs"] == "tier1_trader"
    assert rel["actor_is_tier1_at_obs"] == 1


def test_diagnostic_card_persists_nothing():
    conn = _seeded()
    card = {"canonical_post_id": None, "url": "", "author_handle": "x", "actor_handle": "x",
            "relationship": "direct", "full_text": "hi", "text_complete": True,
            "posted_at_raw": None, "urls": [], "referenced_author": None,
            "referenced_post_id": None}
    r = X.persist_card(conn, nz.normalize_card(card), observed_at=NOW,
                       clock_domain="observed_real_utc", capture_run_id="run1", now_iso=NOW)
    assert not r["new_post"] and not r["new_relationship"]
    (posts,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_posts").fetchone()
    assert posts == 0


def test_complete_redelivery_uses_its_own_card_observation():
    conn = _seeded()
    base = {"canonical_post_id": "post-complete", "url": "u", "author_handle": "zinceth",
            "actor_handle": "zinceth", "relationship": "direct", "posted_at_raw": None,
            "urls": [], "referenced_author": None, "referenced_post_id": None}
    first = {**base, "full_text": "truncated…", "text_complete": False}
    complete = {**base,
                "full_text": "CA: 62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump",
                "text_complete": True}

    r1 = X.persist_card(conn, nz.normalize_card(first), observed_at=NOW,
                        clock_domain="observed_real_utc", capture_run_id="run1", now_iso=NOW)
    r2 = X.persist_card(conn, nz.normalize_card(complete),
                        observed_at="2026-07-13T00:01:00+00:00",
                        clock_domain="observed_real_utc", capture_run_id="run2", now_iso=NOW)

    assert r1["new_observation"] is True and r2["new_observation"] is True
    (observations,) = conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_card_observations").fetchone()
    assert observations == 2
    mention = conn.execute("""
        SELECT m.eligible, o.observed_at, o.raw_card_json
        FROM memecoin_x_coin_mentions m
        JOIN memecoin_x_card_observations o ON o.id=m.source_observation_id
    """).fetchone()
    assert mention["eligible"] == 1
    assert mention["observed_at"] == "2026-07-13T00:01:00+00:00"
    assert "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump" in json.loads(
        mention["raw_card_json"])["full_text"]
    canonical = conn.execute(
        "SELECT full_text, first_observed_at FROM memecoin_x_forward_posts").fetchone()
    assert canonical["full_text"] == "truncated…"
    assert canonical["first_observed_at"] == NOW


def test_incomplete_exact_mention_promotes_only_at_later_complete_observation():
    conn = _seeded()
    ca = "0xabcdef0000000000000000000000000000000005"
    base = {"canonical_post_id": "post-promote", "url": "u",
            "author_handle": "hooddotfun", "actor_handle": "hooddotfun",
            "relationship": "direct", "posted_at_raw": None, "urls": [],
            "referenced_author": None, "referenced_post_id": None,
            "full_text": f"Robinhood Chain CA: {ca}"}

    incomplete = {**base, "text_complete": False}
    complete = {**base, "text_complete": True}
    X.persist_card(conn, nz.normalize_card(incomplete), observed_at=NOW,
                   clock_domain="observed_real_utc", capture_run_id="run1", now_iso=NOW)
    first = conn.execute("""
        SELECT m.eligible, o.capture_run_id
        FROM memecoin_x_coin_mentions m
        JOIN memecoin_x_card_observations o ON o.id=m.source_observation_id
        WHERE m.mint=?
    """, (ca,)).fetchone()
    assert tuple(first) == (0, "run1")

    X.persist_card(conn, nz.normalize_card(complete),
                   observed_at="2026-07-13T00:01:00+00:00",
                   clock_domain="observed_real_utc", capture_run_id="run2", now_iso=NOW)
    promoted = conn.execute("""
        SELECT m.eligible, o.capture_run_id, o.observed_at
        FROM memecoin_x_coin_mentions m
        JOIN memecoin_x_card_observations o ON o.id=m.source_observation_id
        WHERE m.mint=?
    """, (ca,)).fetchone()
    assert tuple(promoted) == (1, "run2", "2026-07-13T00:01:00+00:00")

    X.persist_card(conn, nz.normalize_card(incomplete),
                   observed_at="2026-07-13T00:02:00+00:00",
                   clock_domain="observed_real_utc", capture_run_id="run3", now_iso=NOW)
    final = conn.execute("""
        SELECT m.eligible, o.capture_run_id
        FROM memecoin_x_coin_mentions m
        JOIN memecoin_x_card_observations o ON o.id=m.source_observation_id
        WHERE m.mint=?
    """, (ca,)).fetchone()
    assert tuple(final) == (1, "run2")


def test_second_actor_relationship_points_to_its_raw_observation():
    conn = _seeded()
    direct = {"canonical_post_id": "source-post", "url": "u", "author_handle": "blknoiz06",
              "actor_handle": "blknoiz06", "relationship": "direct", "full_text": "source",
              "text_complete": True, "posted_at_raw": None, "urls": [],
              "referenced_author": None, "referenced_post_id": None}
    repost = {**direct, "actor_handle": "_OHT_", "relationship": "repost",
              "referenced_author": "blknoiz06", "referenced_post_id": "source-post"}
    X.persist_card(conn, nz.normalize_card(direct), observed_at=NOW,
                   clock_domain="observed_real_utc", capture_run_id="run1", now_iso=NOW)
    X.persist_card(conn, nz.normalize_card(repost),
                   observed_at="2026-07-13T00:01:00+00:00",
                   clock_domain="observed_real_utc", capture_run_id="run2", now_iso=NOW)

    row = conn.execute("""
        SELECT r.actor_handle, r.observed_at, o.raw_card_json
        FROM memecoin_x_relationships r
        JOIN memecoin_x_card_observations o ON o.id=r.source_observation_id
        WHERE r.actor_handle='_oht_'
    """).fetchone()
    assert row["observed_at"] == "2026-07-13T00:01:00+00:00"
    assert json.loads(row["raw_card_json"])["actor_handle"] == "_OHT_"


def test_parser_correction_replaces_stale_mention_state():
    conn = _seeded()
    card = {"canonical_post_id": "parser-post", "url": "u", "author_handle": "zinceth",
            "actor_handle": "zinceth", "relationship": "direct",
            "full_text": "CA: 62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump",
            "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": None, "referenced_post_id": None}
    v1 = nz.normalize_card(card)
    v1["post"]["parser_version"] = 1
    X.persist_card(conn, v1, observed_at=NOW, clock_domain="observed_real_utc",
                   capture_run_id="run1", now_iso=NOW)
    v2 = deepcopy(v1)
    v2["post"]["parser_version"] = 2
    v2["mentions"][0]["eligible"] = False
    v2["mentions"][0]["ambiguous"] = True
    X.persist_card(conn, v2, observed_at="2026-07-13T00:02:00+00:00",
                   clock_domain="observed_real_utc", capture_run_id="run2", now_iso=NOW)

    rows = conn.execute("""
        SELECT m.parser_version, m.ambiguous, m.eligible, o.capture_run_id
        FROM memecoin_x_coin_mentions m
        JOIN memecoin_x_card_observations o ON o.id=m.source_observation_id
    """).fetchall()
    assert [tuple(row) for row in rows] == [(2, 1, 0, "run2")]


def test_parser_correction_can_remove_stale_mentions():
    conn = _seeded()
    card = {"canonical_post_id": "parser-remove", "url": "u", "author_handle": "zinceth",
            "actor_handle": "zinceth", "relationship": "direct", "full_text": "$WIF",
            "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": None, "referenced_post_id": None}
    v1 = nz.normalize_card(card)
    v1["post"]["parser_version"] = 1
    X.persist_card(conn, v1, observed_at=NOW, clock_domain="observed_real_utc",
                   capture_run_id="run1", now_iso=NOW)
    v2 = deepcopy(v1)
    v2["post"]["parser_version"] = 2
    v2["mentions"] = []
    X.persist_card(conn, v2, observed_at="2026-07-13T00:02:00+00:00",
                   clock_domain="observed_real_utc", capture_run_id="run2", now_iso=NOW)
    (mentions,) = conn.execute("SELECT COUNT(*) FROM memecoin_x_coin_mentions").fetchone()
    assert mentions == 0


def test_truncated_higher_parser_card_cannot_replace_complete_state():
    conn = _seeded()
    card = {"canonical_post_id": "parser-truncated", "url": "u",
            "author_handle": "zinceth", "actor_handle": "zinceth", "relationship": "direct",
            "full_text": "$WIF", "text_complete": True, "posted_at_raw": None, "urls": [],
            "referenced_author": None, "referenced_post_id": None}
    v1 = nz.normalize_card(card)
    v1["post"]["parser_version"] = 1
    X.persist_card(conn, v1, observed_at=NOW, clock_domain="observed_real_utc",
                   capture_run_id="run1", now_iso=NOW)
    v2 = deepcopy(v1)
    v2["post"]["parser_version"] = 2
    v2["post"]["text_complete"] = False
    v2["mentions"] = []
    X.persist_card(conn, v2, observed_at="2026-07-13T00:02:00+00:00",
                   clock_domain="observed_real_utc", capture_run_id="run2", now_iso=NOW)
    row = conn.execute(
        "SELECT parser_version, ticker FROM memecoin_x_coin_mentions").fetchone()
    assert tuple(row) == (1, "WIF")


def test_forward_link_stable_key_dedups_across_mention_rows():
    conn = _mem(); ensure_memecoin_x_tables(conn)
    cols = ("canonical_post_id, coin_mention_id, fomo_upstream_event_id, "
            "buying_source_user_id, network_id, mint")
    q = f"INSERT OR IGNORE INTO memecoin_x_forward_links ({cols}) VALUES (?,?,?,?,?,?)"
    conn.execute(q, ("p1", 10, "e1", "u1", "solana", "MINT1"))
    conn.execute(q, ("p1", 999, "e1", "u1", "solana", "MINT1"))   # diff mention row, same identity
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_links").fetchone()[0] == 1
    conn.execute(q, ("p1", 10, "e2", "u1", "solana", "MINT1"))    # diff fomo event -> distinct
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_links").fetchone()[0] == 2


_OLD_FL = """CREATE TABLE memecoin_x_forward_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_post_id TEXT NOT NULL,
    coin_mention_id INTEGER NOT NULL, fomo_upstream_event_id TEXT NOT NULL,
    buying_source_user_id TEXT NOT NULL, network_id TEXT, mint TEXT,
    x_observed_at TEXT, x_clock_domain TEXT, fomo_observed_at TEXT, fomo_clock_domain TEXT,
    lead_seconds REAL, evidence_lane TEXT, buyer_relationship TEXT,
    tier1_at_obs INTEGER, source_role_at_obs TEXT, eligible INTEGER NOT NULL DEFAULT 0,
    exclusion_reason TEXT,
    UNIQUE (coin_mention_id, fomo_upstream_event_id, buying_source_user_id))"""


def test_forward_link_migrates_off_old_coin_mention_key():
    conn = _mem()
    conn.execute(_OLD_FL)
    conn.execute("INSERT INTO memecoin_x_forward_links "
                 "(canonical_post_id, coin_mention_id, fomo_upstream_event_id, buying_source_user_id, network_id, mint) "
                 "VALUES ('p1', 10, 'e1', 'u1', 'solana', 'MINT1')")
    ensure_memecoin_x_tables(conn)  # must migrate
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='memecoin_x_forward_links'").fetchone()[0]
    assert "UNIQUE (coin_mention_id" not in sql
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='index' "
                        "AND name='uq_x_forward_link_identity'").fetchone() is not None
    assert conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_links").fetchone()[0] == 1  # row preserved


def test_buyer_snapshot_migration_preserves_rows_and_allows_unresolved_identity():
    conn = _mem()
    conn.execute("""
        CREATE TABLE memecoin_x_fomo_buyer_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fomo_upstream_event_id TEXT NOT NULL,
            buying_source_user_id TEXT NOT NULL,
            source_handle_at_obs TEXT NOT NULL,
            buyer_x_handle_at_obs TEXT COLLATE NOCASE NOT NULL,
            tier1_at_obs INTEGER NOT NULL CHECK (tier1_at_obs IN (0,1)),
            fomo_observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (fomo_upstream_event_id, buying_source_user_id)
        )
    """)
    conn.execute("""
        INSERT INTO memecoin_x_fomo_buyer_snapshots
            (fomo_upstream_event_id, buying_source_user_id, source_handle_at_obs,
             buyer_x_handle_at_obs, tier1_at_obs, fomo_observed_at, created_at)
        VALUES ('e1','u1','zinceth','zinceth',1,?,?)
    """, (NOW, NOW))
    ensure_memecoin_x_tables(conn)
    buyer_x = next(row for row in conn.execute(
        "PRAGMA table_info(memecoin_x_fomo_buyer_snapshots)")
        if row[1] == "buyer_x_handle_at_obs")
    assert buyer_x[3] == 0
    assert conn.execute(
        "SELECT buyer_x_handle_at_obs FROM memecoin_x_fomo_buyer_snapshots"
    ).fetchone()[0] == "zinceth"


def test_signal_schema_enforces_display_only_enums_and_logical_keys():
    conn = _mem(); ensure_memecoin_x_tables(conn)
    event = {
        "logical_key": "capture_tag:4663:mint:1", "event_type": "capture_tag",
        "network_id": "4663", "mint": "mint", "triggered_at": NOW,
        "source_handle": "poptyedev", "evidence_observation_ids": [1],
        "actor_handles": ["poptyedev"], "relationship_counts": {"direct": 1},
    }
    first = X.persist_signal_event(conn, event, created_at=NOW)
    second = X.persist_signal_event(conn, event, created_at=NOW)
    assert first == second
    row = conn.execute("SELECT * FROM memecoin_x_signal_events").fetchone()
    assert row["display_only"] == 1
    assert json.loads(row["evidence_observation_ids_json"]) == [1]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE memecoin_x_signal_events SET display_only=0")
    with pytest.raises(ValueError, match="event type"):
        X.persist_signal_event(conn, {**event, "logical_key": "bad", "event_type": "buy"},
                               created_at=NOW)


def test_signal_cursor_advances_only_after_ok_run_and_bounds_error():
    conn = _mem(); ensure_memecoin_x_tables(conn)
    assert X.start_signal_run(conn, run_id="failed", started_at=NOW,
                              after_observation_id=3) is True
    X.finish_signal_run(conn, run_id="failed", finished_at=NOW, status="error",
                        through_observation_id=99, events_created=0,
                        error_detail="x" * 900)
    assert X.last_completed_observation_id(conn) == 0
    assert len(conn.execute(
        "SELECT error_detail FROM memecoin_x_signal_runs WHERE run_id='failed'"
    ).fetchone()[0]) == 500
    assert X.start_signal_run(conn, run_id="ok", started_at=NOW,
                              after_observation_id=3) is True
    assert X.start_signal_run(conn, run_id="ok", started_at=NOW,
                              after_observation_id=3) is False
    X.finish_signal_run(conn, run_id="ok", finished_at=NOW, status="ok",
                        through_observation_id=11, events_created=2)
    assert X.last_completed_observation_id(conn) == 11


def test_notification_outbox_and_attempts_are_idempotent_and_audited():
    conn = _mem(); ensure_memecoin_x_tables(conn)
    event_id = X.persist_signal_event(conn, {
        "logical_key": "scout:4663:mint:1", "event_type": "tier2_scout",
        "network_id": "4663", "mint": "mint", "triggered_at": NOW,
        "source_handle": "poptyedev", "evidence_observation_ids": [1],
        "actor_handles": [], "relationship_counts": {},
    }, created_at=NOW)
    outbox_id = X.enqueue_signal_notification(
        conn, logical_key="telegram:scout:4663:mint:1", signal_event_id=event_id,
        channel="telegram", created_at=NOW)
    assert X.enqueue_signal_notification(
        conn, logical_key="telegram:scout:4663:mint:1", signal_event_id=event_id,
        channel="telegram", created_at=NOW) == outbox_id
    X.record_notification_attempt(conn, outbox_id=outbox_id, attempted_at=NOW,
                                  result="failed", latency_ms=2.5,
                                  bounded_error="e" * 900, final_state="pending")
    row = conn.execute("SELECT * FROM memecoin_x_notification_outbox").fetchone()
    assert row["attempts"] == 1 and row["state"] == "pending"
    assert len(row["bounded_error"]) == 500
    with pytest.raises(ValueError, match="channel"):
        X.enqueue_signal_notification(conn, logical_key="bad", signal_event_id=event_id,
                                      channel="email", created_at=NOW)


def _llm_job():
    return {
        "logical_key": "event:1:v1", "mode": "event", "as_of": NOW,
        "network_id": "4663", "mint": "0x1", "evidence_ids": [1],
        "input": {"event": 1}, "input_hash": "abc", "prompt_version": "p1",
        "provider": "fake", "model": "m", "model_version": "m1",
        "output_schema_version": "o1", "generation": {"temperature": 0},
    }


def test_llm_job_state_machine_and_immutable_running_input():
    conn = _mem(); ensure_memecoin_x_tables(conn)
    job_id = X.create_llm_job(conn, _llm_job(), created_at=NOW)
    assert X.create_llm_job(conn, _llm_job(), created_at=NOW) == job_id
    X.transition_llm_job(conn, job_id=job_id, from_state="pending", to_state="running",
                         at=NOW)
    with pytest.raises(ValueError, match="transition"):
        X.transition_llm_job(conn, job_id=job_id, from_state="running", to_state="accepted",
                             at=NOW, input_hash="changed")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE memecoin_x_llm_jobs SET input_hash='changed' WHERE id=?", (job_id,))
    X.transition_llm_job(conn, job_id=job_id, from_state="running", to_state="accepted",
                         at=NOW, validation_result="accepted")
    with pytest.raises(ValueError, match="transition"):
        X.transition_llm_job(conn, job_id=job_id, from_state="accepted", to_state="failed", at=NOW)


def test_llm_annotation_is_one_per_job_and_display_only():
    conn = _mem(); ensure_memecoin_x_tables(conn)
    job_id = X.create_llm_job(conn, _llm_job(), created_at=NOW)
    X.transition_llm_job(conn, job_id=job_id, from_state="pending", to_state="running", at=NOW)
    X.transition_llm_job(conn, job_id=job_id, from_state="running", to_state="accepted", at=NOW)
    annotation = {
        "summary": "summary", "intent": "analysis", "uncertainty": "limited",
        "risk_concerns": [], "claims": [{"text": "fact", "evidence_ids": [1],
                                           "network_id": "4663", "mint": "0x1"}],
    }
    first = X.persist_llm_annotation(conn, job_id=job_id, annotation=annotation, accepted_at=NOW)
    assert X.persist_llm_annotation(
        conn, job_id=job_id, annotation=annotation, accepted_at=NOW) == first
    row = conn.execute("SELECT * FROM memecoin_x_llm_annotations").fetchone()
    assert row["display_only"] == 1 and json.loads(row["claims_json"])[0]["text"] == "fact"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE memecoin_x_llm_annotations SET display_only=0")


def test_digest_watermark_requires_accepted_job_or_explicit_empty_completion():
    conn = _mem(); ensure_memecoin_x_tables(conn)
    job = {**_llm_job(), "logical_key": "digest:1:3", "mode": "digest",
           "input": {"after_event_id": 1, "through_event_id": 3}}
    job_id = X.create_llm_job(conn, job, created_at=NOW)
    with pytest.raises(ValueError, match="accepted"):
        X.commit_llm_digest_watermark(
            conn, through_event_id=3, committed_at=NOW, job_id=job_id)
    X.transition_llm_job(conn, job_id=job_id, from_state="pending", to_state="running", at=NOW)
    X.transition_llm_job(conn, job_id=job_id, from_state="running", to_state="accepted", at=NOW)
    X.commit_llm_digest_watermark(
        conn, through_event_id=3, committed_at=NOW, job_id=job_id)
    assert X.llm_digest_watermark(conn) == 3
    X.commit_llm_digest_watermark(
        conn, through_event_id=5, committed_at=NOW, empty=True)
    assert X.llm_digest_watermark(conn) == 5
    with pytest.raises(ValueError, match="explicit"):
        X.commit_llm_digest_watermark(conn, through_event_id=6, committed_at=NOW)

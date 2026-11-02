"""db_memecoin_x.py — tables + CRUD for the X source graph (display-only, read-only capture).

All ensure_* idempotent (CREATE TABLE IF NOT EXISTS). Forward evidence uses OUR observed
timestamps only; X posted_at is metadata. See
docs/superpowers/specs/2026-07-13-memecoin-x-source-graph-design.md.
"""
from __future__ import annotations

import json as _json
import sqlite3

from sniper.analysis.memecoin_x.normalize import normalize_handle


CAPTURE_DIAGNOSTIC_CODES = frozenset({
    "missing_article", "missing_post_id", "missing_author", "missing_actor",
    "missing_referenced_post_id", "invalid_relationship", "incomplete_text",
    "media_without_text_identity", "obfuscated_identity", "promoted_card",
    "wrong_feed", "unauthenticated", "challenge", "rate_limited", "parser_error",
    "coverage_gap", "page_unready", "profile_locked", "network_error", "capture_error",
})

CAPTURE_RUN_STATUSES = frozenset({
    "running", "ok", "no_new", "coverage_gap", "wrong_feed", "logged_out",
    "challenge", "rate_limited", "page_unready", "clock_unhealthy", "parser_error", "error",
})
SIGNAL_EVENT_TYPES = frozenset({
    "capture_tag", "fast_consensus", "tier2_scout", "official_context",
    "cashtag_mention", "cashtag_amplification", "cashtag_update",
    "cashtag_consensus", "capture_health",
})
NOTIFICATION_CHANNELS = frozenset({"dashboard", "telegram"})
NOTIFICATION_STATES = frozenset({"pending", "sent", "failed", "suppressed"})


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Add a nullable compatibility column to an existing local Phase-1 table."""
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def ensure_memecoin_x_tables(conn: sqlite3.Connection) -> None:
    # forward_links: migrate off the coin_mention_id unique key before Phase 3 writes links —
    # mention row ids can change on re-parse, so they are not a stable logical identity. Columns
    # are unchanged, so a rename + SELECT * copy (deduped by the new stable index) is lossless.
    _fl = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                       "AND name='memecoin_x_forward_links'").fetchone()
    _fl_migrate = bool(_fl and "UNIQUE (coin_mention_id" in (_fl[0] or ""))
    if _fl_migrate:
        conn.execute("ALTER TABLE memecoin_x_forward_links RENAME TO _mcx_forward_links_old")
    _bs = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                       "AND name='memecoin_x_fomo_buyer_snapshots'").fetchone()
    _bs_migrate = bool(
        _bs and "buyer_x_handle_at_obs TEXT COLLATE NOCASE NOT NULL" in (_bs[0] or ""))
    if _bs_migrate:
        conn.execute("ALTER TABLE memecoin_x_fomo_buyer_snapshots "
                     "RENAME TO _mcx_buyer_snapshots_old")
    _se = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                       "AND name='memecoin_x_signal_events'").fetchone()
    _se_sql = (_se[0] or "") if _se else ""
    _se_migrate = bool(
        _se and ("cashtag_mention" not in _se_sql
                 or "network_id TEXT NOT NULL" in _se_sql
                 or "identity_state" not in _se_sql
                 or "DEFAULT 'exact_coin'" not in _se_sql))
    if _se_migrate:
        conn.execute("ALTER TABLE memecoin_x_signal_events "
                     "RENAME TO _mcx_signal_events_old")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            handle TEXT COLLATE NOCASE UNIQUE NOT NULL,
            display_handle TEXT,
            x_user_id TEXT UNIQUE,
            profile_url TEXT,
            role TEXT NOT NULL CHECK (role IN
                ('tier1_trader','tier2_source','official_context','suggested')),
            fomo_source_user_id TEXT,
            fomo_handle TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            needs_review INTEGER NOT NULL DEFAULT 0,
            provenance TEXT,
            added_at TEXT,
            updated_at TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_capture_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            parser_version INTEGER NOT NULL,
            started_at TEXT, finished_at TEXT,
            clock_offset_s REAL, clock_offset_age_s REAL,
            clock_domain TEXT NOT NULL DEFAULT 'legacy_unknown',
            cards_seen INTEGER DEFAULT 0,
            cards_scanned INTEGER DEFAULT 0,
            stop_reason TEXT,
            new_posts INTEGER DEFAULT 0,
            new_relationships INTEGER DEFAULT 0,
            parse_failures INTEGER DEFAULT 0,
            prior_sentinel TEXT, newest_sentinel TEXT,
            sentinel_found INTEGER,
            status TEXT NOT NULL,
            error_detail TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_correlator_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            after_fomo_id INTEGER NOT NULL,
            through_fomo_id INTEGER,
            source_rows INTEGER NOT NULL DEFAULT 0,
            eligible_buys INTEGER NOT NULL DEFAULT 0,
            links_created INTEGER NOT NULL DEFAULT 0,
            links_upgraded INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK (status IN ('running','ok','error')),
            error_detail TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_signal_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT UNIQUE NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            after_observation_id INTEGER NOT NULL,
            through_observation_id INTEGER,
            events_created INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL CHECK (status IN ('running','ok','error')),
            error_detail TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logical_key TEXT UNIQUE NOT NULL,
            event_type TEXT NOT NULL CHECK (event_type IN
                ('capture_tag','fast_consensus','tier2_scout','official_context',
                 'cashtag_mention','cashtag_amplification','cashtag_update',
                 'cashtag_consensus','capture_health')),
            identity_state TEXT NOT NULL DEFAULT 'exact_coin' CHECK (identity_state IN
                ('ticker_only','exact_coin','not_applicable')),
            ticker TEXT,
            network_id TEXT,
            mint TEXT,
            triggered_at TEXT NOT NULL,
            source_handle TEXT,
            source_user_id TEXT,
            post_id TEXT,
            edit_root_id TEXT,
            post_url TEXT,
            relationship TEXT CHECK (relationship IS NULL OR relationship IN
                ('direct','repost','quote','reply')),
            cashtag_source TEXT CHECK (cashtag_source IS NULL OR cashtag_source IN
                ('actor_text','referenced_post')),
            excerpt TEXT,
            x_created_at TEXT,
            consensus_count INTEGER NOT NULL DEFAULT 1,
            evidence_observation_ids_json TEXT NOT NULL,
            actor_handles_json TEXT NOT NULL,
            relationship_counts_json TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            display_only INTEGER NOT NULL DEFAULT 1 CHECK (display_only=1),
            created_at TEXT NOT NULL,
            CHECK (
                (identity_state='ticker_only' AND ticker IS NOT NULL
                    AND network_id IS NULL AND mint IS NULL)
                OR (identity_state='exact_coin' AND network_id IS NOT NULL AND mint IS NOT NULL)
                OR (identity_state='not_applicable' AND event_type='capture_health'
                    AND network_id IS NULL AND mint IS NULL)
            )
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_notification_outbox (
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
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_notification_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            outbox_id INTEGER NOT NULL,
            attempted_at TEXT NOT NULL,
            result TEXT NOT NULL CHECK (result IN ('sent','failed','suppressed')),
            latency_ms REAL,
            bounded_error TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_llm_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            logical_key TEXT UNIQUE NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('event','digest','on_request')),
            state TEXT NOT NULL CHECK (state IN
                ('pending','running','accepted','rejected','failed','timed_out',
                 'over_budget','stale')),
            as_of TEXT NOT NULL,
            network_id TEXT,
            mint TEXT,
            evidence_ids_json TEXT NOT NULL,
            input_json TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            model_version TEXT NOT NULL,
            output_schema_version TEXT NOT NULL,
            generation_json TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            token_input INTEGER,
            token_output INTEGER,
            budget_result TEXT,
            validation_result TEXT,
            bounded_error TEXT,
            raw_response_json TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_llm_annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER UNIQUE NOT NULL,
            summary TEXT NOT NULL,
            intent TEXT NOT NULL CHECK (intent IN
                ('promotion','warning','analysis','unknown')),
            uncertainty TEXT NOT NULL,
            risk_concerns_json TEXT NOT NULL,
            claims_json TEXT NOT NULL,
            accepted_at TEXT NOT NULL,
            display_only INTEGER NOT NULL DEFAULT 1 CHECK (display_only=1)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_llm_watermarks (
            mode TEXT PRIMARY KEY CHECK (mode IN ('digest')),
            last_event_id INTEGER NOT NULL,
            committed_at TEXT NOT NULL
        )""")
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS trg_x_llm_job_input_immutable
        BEFORE UPDATE OF mode, as_of, network_id, mint, evidence_ids_json, input_json,
                         input_hash, prompt_version, provider, model, model_version,
                         output_schema_version, generation_json
        ON memecoin_x_llm_jobs
        WHEN OLD.state <> 'pending'
        BEGIN
            SELECT RAISE(ABORT, 'llm job input immutable after running');
        END
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_forward_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_post_id TEXT UNIQUE NOT NULL,
            url TEXT,
            author_handle TEXT,
            author_role_at_capture TEXT,
            full_text TEXT,
            text_complete INTEGER,
            posted_at_raw TEXT,
            posted_at TEXT,
            first_observed_at TEXT NOT NULL,
            last_observed_at TEXT NOT NULL,
            capture_run_id TEXT,
            parser_version INTEGER NOT NULL,
            clock_domain TEXT NOT NULL DEFAULT 'legacy_unknown',
            extracted_urls TEXT,
            raw_card_json TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_card_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_run_id TEXT NOT NULL,
            canonical_post_id TEXT NOT NULL,
            author_handle TEXT,
            actor_handle TEXT NOT NULL,
            relationship TEXT NOT NULL CHECK (relationship IN
                ('direct','repost','quote','reply')),
            observed_at TEXT NOT NULL,
            clock_domain TEXT NOT NULL DEFAULT 'legacy_unknown',
            parser_version INTEGER NOT NULL,
            full_text TEXT,
            text_complete INTEGER,
            posted_at_raw TEXT,
            posted_at TEXT,
            extracted_urls TEXT,
            raw_card_json TEXT NOT NULL,
            UNIQUE (capture_run_id, canonical_post_id, actor_handle, relationship)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_post_id TEXT NOT NULL,
            actor_handle TEXT NOT NULL,
            actor_role_at_obs TEXT,
            actor_is_tier1_at_obs INTEGER,
            relationship TEXT NOT NULL CHECK (relationship IN
                ('direct','repost','quote','reply')),
            observed_at TEXT NOT NULL,
            capture_run_id TEXT,
            clock_domain TEXT NOT NULL DEFAULT 'legacy_unknown',
            referenced_author TEXT,
            referenced_post_id TEXT,
            source_observation_id INTEGER,
            UNIQUE (canonical_post_id, actor_handle, relationship)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_coin_mentions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_post_id TEXT NOT NULL,
            network_id TEXT,
            mint TEXT,
            ticker TEXT,
            extraction_method TEXT NOT NULL CHECK (extraction_method IN
                ('contract_text','dex_url','launchpad_url','ticker_only')),
            ambiguous INTEGER NOT NULL DEFAULT 0,
            eligible INTEGER NOT NULL DEFAULT 0,
            parser_version INTEGER NOT NULL,
            source_observation_id INTEGER
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_forward_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_post_id TEXT NOT NULL,
            coin_mention_id INTEGER NOT NULL,
            fomo_upstream_event_id TEXT NOT NULL,
            buying_source_user_id TEXT NOT NULL,
            network_id TEXT, mint TEXT,
            x_observed_at TEXT, x_clock_domain TEXT,
            fomo_observed_at TEXT, fomo_clock_domain TEXT,
            lead_seconds REAL,
            evidence_lane TEXT,
            buyer_relationship TEXT,
            tier1_at_obs INTEGER, source_role_at_obs TEXT,
            eligible INTEGER NOT NULL DEFAULT 0,
            exclusion_reason TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_fomo_buyer_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fomo_upstream_event_id TEXT NOT NULL,
            buying_source_user_id TEXT NOT NULL,
            source_handle_at_obs TEXT NOT NULL,
            buyer_x_handle_at_obs TEXT COLLATE NOCASE,
            tier1_at_obs INTEGER NOT NULL CHECK (tier1_at_obs IN (0,1)),
            fomo_observed_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (fomo_upstream_event_id, buying_source_user_id)
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_backfill_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id TEXT,
            author TEXT,
            posted_at TEXT,
            network_id TEXT, mint TEXT, ticker TEXT,
            discovery_query TEXT,
            related_fomo_event_id TEXT,
            evidence_mode TEXT NOT NULL DEFAULT 'retrospective'
                CHECK (evidence_mode = 'retrospective')
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_capture_diagnostics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            capture_run_id TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            clock_domain TEXT NOT NULL DEFAULT 'legacy_unknown',
            diagnostic_code TEXT NOT NULL,
            card_index INTEGER,
            parser_version INTEGER NOT NULL,
            canonical_post_id TEXT,
            article_fingerprint TEXT,
            details_json TEXT,
            raw_card_json TEXT
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_api_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1),
            revision INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            roster_hash TEXT,
            rule_hash TEXT,
            frontier_post_id TEXT,
            recovery_since_id TEXT,
            recovery_next_token TEXT,
            recovery_max_post_id TEXT,
            last_healthy_at TEXT,
            last_recovery_at TEXT,
            updated_at TEXT NOT NULL,
            bounded_detail TEXT
        )""")
    conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_api_state
            (singleton, revision, state, updated_at)
        VALUES (1, 0, 'stopped', '')
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memecoin_x_api_resources (
            utc_day TEXT NOT NULL,
            resource_id TEXT NOT NULL,
            source TEXT NOT NULL CHECK(source IN
                ('stream','recovery','reference','fixture')),
            counted_at TEXT NOT NULL,
            PRIMARY KEY(utc_day, resource_id)
        )""")
    _ensure_column(conn, "memecoin_x_accounts", "display_handle", "TEXT")
    _ensure_column(conn, "memecoin_x_accounts", "x_user_id", "TEXT")
    _ensure_column(conn, "memecoin_x_relationships", "source_observation_id", "INTEGER")
    _ensure_column(conn, "memecoin_x_coin_mentions", "source_observation_id", "INTEGER")
    _ensure_column(conn, "memecoin_x_capture_runs", "cards_scanned", "INTEGER DEFAULT 0")
    _ensure_column(conn, "memecoin_x_capture_runs", "stop_reason", "TEXT")
    _ensure_column(conn, "memecoin_x_capture_runs", "cadence_bucket", "TEXT")
    _ensure_column(conn, "memecoin_x_capture_runs", "scroll_count", "INTEGER")
    _ensure_column(conn, "memecoin_x_capture_runs", "dom_wait_ms", "INTEGER")
    _ensure_column(conn, "memecoin_x_capture_runs", "reload_reason", "TEXT")
    _ensure_column(conn, "memecoin_x_capture_runs", "recovery_used", "INTEGER")
    _ensure_column(conn, "memecoin_x_capture_runs", "operator_postponed", "INTEGER")
    if _se_migrate:
        conn.execute("""
            INSERT INTO memecoin_x_signal_events
                (id, logical_key, event_type, identity_state, ticker, network_id, mint,
                 triggered_at, source_handle, source_user_id, post_id, edit_root_id,
                 post_url, relationship, cashtag_source, excerpt, x_created_at,
                 consensus_count, evidence_observation_ids_json, actor_handles_json,
                 relationship_counts_json, details_json, display_only, created_at)
            SELECT id, logical_key, event_type, 'exact_coin', NULL, network_id, mint,
                   triggered_at, source_handle, NULL, NULL, NULL, NULL, NULL, NULL,
                   NULL, NULL, 1, evidence_observation_ids_json, actor_handles_json,
                   relationship_counts_json, '{}', display_only, created_at
            FROM _mcx_signal_events_old ORDER BY id
        """)
        conn.execute("DROP TABLE _mcx_signal_events_old")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_x_rel_post ON memecoin_x_relationships (canonical_post_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_x_accounts_user_id "
                 "ON memecoin_x_accounts(x_user_id) WHERE x_user_id IS NOT NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_x_rel_actor ON memecoin_x_relationships (actor_handle, observed_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_x_mentions_coin ON memecoin_x_coin_mentions (network_id, mint)")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_x_observations_post_time
        ON memecoin_x_card_observations (canonical_post_id, observed_at)
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_x_mention_identity
        ON memecoin_x_coin_mentions (
            canonical_post_id,
            COALESCE(network_id, ''),
            COALESCE(mint, ''),
            COALESCE(ticker, ''),
            extraction_method
        )""")
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS uq_x_forward_link_identity
        ON memecoin_x_forward_links (
            canonical_post_id, fomo_upstream_event_id, buying_source_user_id,
            COALESCE(network_id, ''), COALESCE(mint, '')
        )""")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_x_capture_diag_run_code
        ON memecoin_x_capture_diagnostics (capture_run_id, diagnostic_code)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_x_correlator_runs_status_cursor
        ON memecoin_x_correlator_runs (status, through_fomo_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_x_signal_events_type_time
        ON memecoin_x_signal_events (event_type, triggered_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_x_signal_events_coin_time
        ON memecoin_x_signal_events (network_id, mint, triggered_at)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_x_notification_outbox_state_time
        ON memecoin_x_notification_outbox (state, created_at)
    """)
    if _fl_migrate:
        conn.execute("INSERT OR IGNORE INTO memecoin_x_forward_links "
                     "SELECT * FROM _mcx_forward_links_old ORDER BY id")
        conn.execute("DROP TABLE _mcx_forward_links_old")
    if _bs_migrate:
        conn.execute("""
            INSERT OR IGNORE INTO memecoin_x_fomo_buyer_snapshots
                (id, fomo_upstream_event_id, buying_source_user_id, source_handle_at_obs,
                 buyer_x_handle_at_obs, tier1_at_obs, fomo_observed_at, created_at)
            SELECT id, fomo_upstream_event_id, buying_source_user_id, source_handle_at_obs,
                   buyer_x_handle_at_obs, tier1_at_obs, fomo_observed_at, created_at
            FROM _mcx_buyer_snapshots_old ORDER BY id
        """)
        conn.execute("DROP TABLE _mcx_buyer_snapshots_old")
    conn.commit()


def seed_roster(conn, roster, *, now_iso) -> None:
    for e in roster:
        handle = normalize_handle(e["handle"])
        conn.execute("""
            INSERT INTO memecoin_x_accounts (handle, display_handle, profile_url, role, fomo_handle,
                                             active, needs_review, provenance, added_at, updated_at)
            VALUES (?,?,?,?,?,1,0,'roster_seed',?,?)
            ON CONFLICT(handle) DO UPDATE SET
                display_handle=excluded.display_handle,
                profile_url=excluded.profile_url, role=excluded.role,
                fomo_handle=excluded.fomo_handle, active=1,
                updated_at=excluded.updated_at
        """, (handle, e.get("display_handle") or e["handle"],
              f"https://x.com/{handle}", e["role"],
              e.get("fomo_handle"), now_iso, now_iso))
    conn.commit()


def role_for(conn, handle):
    row = conn.execute("SELECT role FROM memecoin_x_accounts WHERE handle=?",
                       (normalize_handle(handle),)).fetchone()
    return row[0] if row else None


def active_api_roster(conn) -> list[dict]:
    rows = conn.execute("""
        SELECT x_user_id, handle, COALESCE(display_handle, handle) AS display_handle, role
        FROM memecoin_x_accounts
        WHERE active=1 AND x_user_id IS NOT NULL
          AND role IN ('tier1_trader','tier2_source','official_context')
        ORDER BY handle COLLATE NOCASE, x_user_id
    """).fetchall()
    return [dict(row) for row in rows]


_API_STATES = frozenset({
    "healthy", "reconnecting", "recovering", "coverage_gap", "clock_unhealthy",
    "auth_failed", "budget_exhausted", "database_unavailable", "stopped",
})
_API_STATE_FIELDS = frozenset({
    "state", "roster_hash", "rule_hash", "frontier_post_id", "recovery_since_id",
    "recovery_next_token", "recovery_max_post_id", "last_healthy_at",
    "last_recovery_at", "updated_at", "bounded_detail",
})


def api_capture_state(conn) -> dict:
    row = conn.execute(
        "SELECT * FROM memecoin_x_api_state WHERE singleton=1").fetchone()
    if row is None:
        raise ValueError("X API state is not initialized")
    return dict(row)


def update_api_capture_state(conn, *, expected_revision: int, commit=True, **updates) -> dict:
    unexpected = set(updates) - _API_STATE_FIELDS
    if unexpected:
        raise ValueError(f"invalid X API state fields: {sorted(unexpected)}")
    if "state" in updates and updates["state"] not in _API_STATES:
        raise ValueError(f"invalid X API state: {updates['state']}")
    if "updated_at" not in updates:
        raise ValueError("X API state update requires updated_at")
    values = {field: updates.get(field) for field in _API_STATE_FIELDS}
    if values["bounded_detail"] is not None:
        values["bounded_detail"] = str(values["bounded_detail"])[:500]
    assignments = ["revision=revision+1"]
    args = []
    for field in sorted(updates):
        assignments.append(f"{field}=?")
        args.append(values[field])
    args.extend((int(expected_revision),))
    cur = conn.execute(
        f"UPDATE memecoin_x_api_state SET {', '.join(assignments)} "
        "WHERE singleton=1 AND revision=?", args)
    if cur.rowcount != 1:
        conn.rollback()
        raise ValueError("stale X API state revision")
    if commit:
        conn.commit()
    return api_capture_state(conn)


def count_api_resource(
        conn, *, utc_day: str, resource_id: str, source: str,
        counted_at: str, commit=True) -> bool:
    if source not in {"stream", "recovery", "reference", "fixture"}:
        raise ValueError(f"invalid X API resource source: {source}")
    cur = conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_api_resources
            (utc_day, resource_id, source, counted_at)
        VALUES (?,?,?,?)
    """, (str(utc_day), str(resource_id), source, counted_at))
    if commit:
        conn.commit()
    return cur.rowcount == 1


def api_resource_count(conn, *, utc_day: str) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_api_resources WHERE utc_day=?",
        (str(utc_day),)).fetchone()[0])


def persist_api_event(
        conn, normalized, *, source, observed_at, clock_domain="corrected_utc",
        created_at=None) -> dict:
    """Persist one normalized API Post and its alert work without committing.

    The caller owns the transaction so resource accounting and frontier movement can
    commit with this evidence or roll back with it.
    """
    created_at = created_at or observed_at
    post_id = str(normalized["post_id"])
    actor_handle = str(normalized["actor_handle"])
    actor_user_id = str(normalized["actor_user_id"])
    relationship = str(normalized["relationship"])
    tags = sorted({str(tag).upper() for tag in normalized.get("cashtags") or []})
    tag_key = ",".join(tags)
    logical_key = (
        f"x-api:{actor_user_id}:{normalized['edit_root_id']}:{relationship}:{tag_key}"
    )
    existing = conn.execute(
        "SELECT id FROM memecoin_x_signal_events WHERE logical_key=?", (logical_key,)
    ).fetchone()

    raw_json = _json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_forward_posts
            (canonical_post_id,url,author_handle,author_role_at_capture,full_text,
             text_complete,posted_at_raw,posted_at,first_observed_at,last_observed_at,
             capture_run_id,parser_version,clock_domain,extracted_urls,raw_card_json)
        VALUES (?,?,?,?,?,1,?,?,?,?,?,2,?,?,?)
    """, (post_id, normalized.get("post_url"), actor_handle,
          normalized.get("actor_role"), normalized.get("text"),
          normalized.get("x_created_at"), normalized.get("x_created_at"),
          observed_at, observed_at, f"api:{source}:{post_id}", clock_domain,
          _json.dumps(normalized.get("urls") or []), raw_json))
    conn.execute("""
        UPDATE memecoin_x_forward_posts
        SET last_observed_at=? WHERE canonical_post_id=?
          AND julianday(?) > julianday(last_observed_at)
    """, (observed_at, post_id, observed_at))

    observation_run_id = f"api:{source}:{post_id}:{normalized['edit_root_id']}"
    conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_card_observations
            (capture_run_id,canonical_post_id,author_handle,actor_handle,relationship,
             observed_at,clock_domain,parser_version,full_text,text_complete,
             posted_at_raw,posted_at,extracted_urls,raw_card_json)
        VALUES (?,?,?,?,?,?,?,2,?,1,?,?,?,?)
    """, (observation_run_id, post_id, actor_handle, actor_handle, relationship,
          observed_at, clock_domain, normalized.get("text"),
          normalized.get("x_created_at"), normalized.get("x_created_at"),
          _json.dumps(normalized.get("urls") or []), raw_json))
    observation_id = conn.execute("""
        SELECT id FROM memecoin_x_card_observations
        WHERE capture_run_id=? AND canonical_post_id=? AND actor_handle=? AND relationship=?
    """, (observation_run_id, post_id, actor_handle, relationship)).fetchone()[0]
    conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_relationships
            (canonical_post_id,actor_handle,actor_role_at_obs,actor_is_tier1_at_obs,
             relationship,observed_at,capture_run_id,clock_domain,referenced_post_id,
             source_observation_id)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (post_id, actor_handle, normalized.get("actor_role"),
          1 if normalized.get("actor_role") == "tier1_trader" else 0,
          relationship, observed_at, observation_run_id, clock_domain,
          normalized.get("referenced_post_id"), observation_id))

    if existing is not None:
        return {"event_id": int(existing[0]), "events_created": 0,
                "logical_key": logical_key}

    prior = conn.execute("""
        SELECT details_json FROM memecoin_x_signal_events
        WHERE source_user_id=? AND edit_root_id=?
        ORDER BY id DESC LIMIT 1
    """, (actor_user_id, normalized["edit_root_id"])).fetchone()
    event_type = normalized["event_type"] if prior is None else "cashtag_update"
    prior_tags = set()
    if prior is not None:
        try:
            prior_tags = set((_json.loads(prior[0]) or {}).get("cashtags") or [])
        except (TypeError, ValueError):
            prior_tags = set()
    event = {
        "logical_key": logical_key,
        "event_type": event_type,
        "identity_state": normalized["identity_state"],
        "ticker": tags[0] if tags else None,
        "network_id": normalized.get("network_id"),
        "mint": normalized.get("mint"),
        "triggered_at": observed_at,
        "source_handle": actor_handle,
        "source_user_id": actor_user_id,
        "post_id": post_id,
        "edit_root_id": normalized["edit_root_id"],
        "post_url": normalized["post_url"],
        "relationship": relationship,
        "cashtag_source": normalized["cashtag_source"],
        "excerpt": normalized.get("excerpt"),
        "x_created_at": normalized.get("x_created_at"),
        "evidence_observation_ids": [int(observation_id)],
        "actor_handles": [actor_handle],
        "relationship_counts": {relationship: 1},
        "details": {
            "cashtags": tags,
            "added_cashtags": sorted(set(tags) - prior_tags),
            "source": source,
            "actor_user_id": actor_user_id,
            "referenced_post_id": normalized.get("referenced_post_id"),
        },
    }
    event_id = persist_signal_event(conn, event, created_at=created_at, commit=False)
    for index, ticker in enumerate(tags):
        exact = normalized["identity_state"] == "exact_coin" and index == 0
        conn.execute("""
            INSERT INTO memecoin_x_coin_mentions
                (canonical_post_id,network_id,mint,ticker,extraction_method,
                 ambiguous,eligible,parser_version,source_observation_id)
            VALUES (?,?,?,?,?,0,?,2,?)
        """, (post_id, normalized.get("network_id") if exact else None,
              normalized.get("mint") if exact else None, ticker,
              "contract_text" if exact else "ticker_only", 1 if exact else 0,
              observation_id))
    enqueue_signal_notification(
        conn, logical_key=f"dashboard:{logical_key}", signal_event_id=event_id,
        channel="dashboard", created_at=created_at, commit=False)
    enqueue_signal_notification(
        conn, logical_key=f"telegram:{logical_key}", signal_event_id=event_id,
        channel="telegram", created_at=created_at, commit=False)
    consensus_created = 0
    for ticker in tags:
        rows = conn.execute("""
            SELECT e.id,e.source_user_id,e.source_handle,e.relationship,e.post_url
            FROM memecoin_x_signal_events e, json_each(e.details_json,'$.cashtags') tag
            WHERE e.event_type IN ('cashtag_mention','cashtag_amplification','cashtag_update')
              AND tag.value=?
              AND julianday(e.triggered_at) >= julianday(?) - (60.0 / 1440.0)
              AND julianday(e.triggered_at) <= julianday(?)
            ORDER BY e.id
        """, (ticker, observed_at, observed_at)).fetchall()
        actors = {}
        relationship_counts = {}
        for row in rows:
            actors.setdefault(str(row["source_user_id"]), str(row["source_handle"]))
            relationship = str(row["relationship"] or "direct")
            relationship_counts[relationship] = relationship_counts.get(relationship, 0) + 1
        if len(actors) < 2:
            continue
        cluster_id = min(int(row["id"]) for row in rows)
        consensus_key = f"x-consensus:{ticker}:{cluster_id}"
        actor_handles = sorted(actors.values(), key=str.lower)
        existing_consensus = conn.execute(
            "SELECT id FROM memecoin_x_signal_events WHERE logical_key=?",
            (consensus_key,)).fetchone()
        if existing_consensus is not None:
            conn.execute("""
                UPDATE memecoin_x_signal_events
                SET consensus_count=?, actor_handles_json=?, relationship_counts_json=?
                WHERE id=?
            """, (len(actors), _json.dumps(actor_handles),
                  _json.dumps(relationship_counts, sort_keys=True),
                  int(existing_consensus[0])))
            continue
        consensus_id = persist_signal_event(conn, {
            "logical_key": consensus_key,
            "event_type": "cashtag_consensus",
            "identity_state": "ticker_only",
            "ticker": ticker,
            "triggered_at": observed_at,
            "source_handle": actor_handle,
            "source_user_id": actor_user_id,
            "post_id": post_id,
            "post_url": normalized["post_url"],
            "consensus_count": len(actors),
            "actor_handles": actor_handles,
            "relationship_counts": relationship_counts,
            "details": {"cashtags": [ticker], "cluster_first_event_id": cluster_id,
                        "window_minutes": 60},
        }, created_at=created_at, commit=False)
        enqueue_signal_notification(
            conn, logical_key=f"dashboard:{consensus_key}",
            signal_event_id=consensus_id, channel="dashboard",
            created_at=created_at, commit=False)
        enqueue_signal_notification(
            conn, logical_key=f"telegram:{consensus_key}",
            signal_event_id=consensus_id, channel="telegram",
            created_at=created_at, commit=False)
        consensus_created += 1
    return {"event_id": event_id, "events_created": 1 + consensus_created,
            "logical_key": logical_key}


def record_capture_run(conn, run) -> None:
    conn.execute("""
        INSERT INTO memecoin_x_capture_runs
            (run_id, parser_version, started_at, finished_at, clock_offset_s, clock_offset_age_s,
             clock_domain, cards_seen, new_posts, new_relationships, parse_failures,
             prior_sentinel, newest_sentinel, sentinel_found, status, error_detail)
        VALUES (:run_id,:parser_version,:started_at,:finished_at,:clock_offset_s,:clock_offset_age_s,
                :clock_domain,:cards_seen,:new_posts,:new_relationships,:parse_failures,
                :prior_sentinel,:newest_sentinel,:sentinel_found,:status,:error_detail)
    """, {**{k: None for k in (
        "finished_at", "clock_offset_s", "clock_offset_age_s", "prior_sentinel",
        "newest_sentinel", "sentinel_found", "error_detail")},
        "cards_seen": 0, "new_posts": 0, "new_relationships": 0, "parse_failures": 0,
        "clock_domain": "legacy_unknown", **run})
    conn.commit()


def start_correlator_run(conn, *, run_id, started_at, after_fomo_id) -> bool:
    cur = conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_correlator_runs
            (run_id, started_at, after_fomo_id, status)
        VALUES (?,?,?,'running')
    """, (run_id, started_at, int(after_fomo_id)))
    conn.commit()
    return cur.rowcount == 1


def finish_correlator_run(
        conn, *, run_id, finished_at, status, through_fomo_id, source_rows,
        eligible_buys, links_created, links_upgraded, error_detail=None) -> None:
    if status not in {"ok", "error"}:
        raise ValueError(f"invalid correlator run status: {status}")
    bounded_error = None if error_detail is None else str(error_detail)[:500]
    cur = conn.execute("""
        UPDATE memecoin_x_correlator_runs
        SET finished_at=?, through_fomo_id=?, source_rows=?, eligible_buys=?,
            links_created=?, links_upgraded=?, status=?, error_detail=?
        WHERE run_id=? AND status='running'
    """, (finished_at, int(through_fomo_id), int(source_rows), int(eligible_buys),
          int(links_created), int(links_upgraded), status, bounded_error, run_id))
    if cur.rowcount != 1:
        conn.rollback()
        raise ValueError(f"correlator run is not running: {run_id}")
    conn.commit()


def last_completed_fomo_id(conn) -> int:
    row = conn.execute("""
        SELECT MAX(through_fomo_id) FROM memecoin_x_correlator_runs
        WHERE status='ok' AND finished_at IS NOT NULL
    """).fetchone()
    return int(row[0] or 0)


def start_signal_run(conn, *, run_id, started_at, after_observation_id) -> bool:
    cur = conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_signal_runs
            (run_id, started_at, after_observation_id, status)
        VALUES (?,?,?,'running')
    """, (run_id, started_at, int(after_observation_id)))
    conn.commit()
    return cur.rowcount == 1


def finish_signal_run(
        conn, *, run_id, finished_at, status, through_observation_id,
        events_created, error_detail=None) -> None:
    if status not in {"ok", "error"}:
        raise ValueError(f"invalid signal run status: {status}")
    error = None if error_detail is None else str(error_detail)[:500]
    cur = conn.execute("""
        UPDATE memecoin_x_signal_runs
        SET finished_at=?, through_observation_id=?, events_created=?, status=?, error_detail=?
        WHERE run_id=? AND status='running'
    """, (finished_at, int(through_observation_id), int(events_created), status, error, run_id))
    if cur.rowcount != 1:
        conn.rollback()
        raise ValueError(f"signal run is not running: {run_id}")
    conn.commit()


def last_completed_observation_id(conn) -> int:
    row = conn.execute("""
        SELECT MAX(through_observation_id) FROM memecoin_x_signal_runs
        WHERE status='ok' AND finished_at IS NOT NULL
    """).fetchone()
    return int(row[0] or 0)


def persist_signal_event(conn, event, *, created_at, commit=True) -> int:
    event_type = str(event.get("event_type") or "")
    if event_type not in SIGNAL_EVENT_TYPES:
        raise ValueError(f"invalid signal event type: {event_type}")
    identity_state = event.get("identity_state")
    if identity_state is None:
        identity_state = "not_applicable" if event_type == "capture_health" else "exact_coin"
    details = event.get("details", {})
    conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_signal_events
            (logical_key, event_type, identity_state, ticker, network_id, mint,
             triggered_at, source_handle, source_user_id, post_id, edit_root_id,
             post_url, relationship, cashtag_source, excerpt, x_created_at,
             consensus_count,
             evidence_observation_ids_json, actor_handles_json,
             relationship_counts_json, details_json, display_only, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)
    """, (event["logical_key"], event_type, identity_state, event.get("ticker"),
          event.get("network_id"), event.get("mint"), event["triggered_at"],
          event.get("source_handle"), event.get("source_user_id"), event.get("post_id"),
          event.get("edit_root_id"), event.get("post_url"), event.get("relationship"),
          event.get("cashtag_source"), event.get("excerpt"), event.get("x_created_at"),
          int(event.get("consensus_count") or 1),
          _json.dumps(event.get("evidence_observation_ids", []), sort_keys=True),
          _json.dumps(event.get("actor_handles", []), sort_keys=True),
          _json.dumps(event.get("relationship_counts", {}), sort_keys=True),
          _json.dumps(details, sort_keys=True, separators=(",", ":")), created_at))
    row = conn.execute(
        "SELECT id FROM memecoin_x_signal_events WHERE logical_key=?",
        (event["logical_key"],)).fetchone()
    if commit:
        conn.commit()
    return int(row[0])


def enqueue_signal_notification(
        conn, *, logical_key, signal_event_id, channel, created_at, commit=True) -> int:
    if channel not in NOTIFICATION_CHANNELS:
        raise ValueError(f"invalid notification channel: {channel}")
    conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_notification_outbox
            (logical_key, signal_event_id, channel, state, created_at)
        VALUES (?,?,?,'pending',?)
    """, (logical_key, int(signal_event_id), channel, created_at))
    row = conn.execute(
        "SELECT id FROM memecoin_x_notification_outbox WHERE logical_key=?",
        (logical_key,)).fetchone()
    if commit:
        conn.commit()
    return int(row[0])


def record_notification_attempt(
        conn, *, outbox_id, attempted_at, result, latency_ms=None,
        bounded_error=None, final_state=None) -> None:
    if result not in {"sent", "failed", "suppressed"}:
        raise ValueError(f"invalid notification result: {result}")
    state = final_state or result
    if state not in NOTIFICATION_STATES:
        raise ValueError(f"invalid notification state: {state}")
    error = None if bounded_error is None else str(bounded_error)[:500]
    conn.execute("""
        INSERT INTO memecoin_x_notification_attempts
            (outbox_id, attempted_at, result, latency_ms, bounded_error)
        VALUES (?,?,?,?,?)
    """, (int(outbox_id), attempted_at, result, latency_ms, error))
    cur = conn.execute("""
        UPDATE memecoin_x_notification_outbox
        SET attempts=attempts+1, state=?, last_attempt_at=?,
            sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END,
            bounded_error=?
        WHERE id=?
    """, (state, attempted_at, state, attempted_at, error, int(outbox_id)))
    if cur.rowcount != 1:
        conn.rollback()
        raise ValueError(f"notification outbox row not found: {outbox_id}")
    conn.commit()


_LLM_TRANSITIONS = {
    "pending": frozenset({"running", "over_budget"}),
    "running": frozenset({
        "pending", "accepted", "rejected", "failed", "timed_out", "over_budget", "stale",
    }),
}
_LLM_RESULT_FIELDS = frozenset({
    "token_input", "token_output", "budget_result", "validation_result",
    "bounded_error", "raw_response_json",
})


def create_llm_job(conn, job, *, created_at, commit=True) -> int:
    if job.get("mode") not in {"event", "digest", "on_request"}:
        raise ValueError(f"invalid llm mode: {job.get('mode')}")
    conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_llm_jobs
            (logical_key, mode, state, as_of, network_id, mint, evidence_ids_json,
             input_json, input_hash, prompt_version, provider, model, model_version,
             output_schema_version, generation_json, created_at)
        VALUES (?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (job["logical_key"], job["mode"], job["as_of"], job.get("network_id"),
          job.get("mint"), _json.dumps(job.get("evidence_ids", []), sort_keys=True),
          _json.dumps(job["input"], sort_keys=True, separators=(",", ":")),
          job["input_hash"], job["prompt_version"], job["provider"], job["model"],
          job["model_version"], job["output_schema_version"],
          _json.dumps(job.get("generation", {}), sort_keys=True, separators=(",", ":")),
          created_at))
    row = conn.execute(
        "SELECT id FROM memecoin_x_llm_jobs WHERE logical_key=?",
        (job["logical_key"],)).fetchone()
    if commit:
        conn.commit()
    return int(row[0])


def transition_llm_job(
        conn, *, job_id, from_state, to_state, at, commit=True, **updates) -> None:
    if to_state not in _LLM_TRANSITIONS.get(from_state, frozenset()):
        raise ValueError(f"illegal llm state transition: {from_state}->{to_state}")
    unexpected = set(updates) - _LLM_RESULT_FIELDS
    if unexpected:
        raise ValueError(f"transition cannot modify immutable fields: {sorted(unexpected)}")
    values = {key: updates.get(key) for key in _LLM_RESULT_FIELDS}
    if values["bounded_error"] is not None:
        values["bounded_error"] = str(values["bounded_error"])[:500]
    if values["raw_response_json"] is not None and not isinstance(
            values["raw_response_json"], str):
        values["raw_response_json"] = _json.dumps(
            values["raw_response_json"], sort_keys=True, separators=(",", ":"))
    started_at = at if to_state == "running" else None
    finished_at = at if to_state not in {"running", "pending"} else None
    attempts_delta = 1 if to_state == "running" else 0
    cur = conn.execute("""
        UPDATE memecoin_x_llm_jobs
        SET state=?, attempts=attempts+?, started_at=COALESCE(?, started_at),
            finished_at=?, token_input=COALESCE(?,token_input),
            token_output=COALESCE(?,token_output), budget_result=COALESCE(?,budget_result),
            validation_result=COALESCE(?,validation_result),
            bounded_error=COALESCE(?,bounded_error),
            raw_response_json=COALESCE(?,raw_response_json)
        WHERE id=? AND state=?
    """, (to_state, attempts_delta, started_at, finished_at,
          values["token_input"], values["token_output"], values["budget_result"],
          values["validation_result"], values["bounded_error"], values["raw_response_json"],
          int(job_id), from_state))
    if cur.rowcount != 1:
        conn.rollback()
        raise ValueError(f"llm job transition lost claim: {job_id} {from_state}")
    if commit:
        conn.commit()


def persist_llm_annotation(conn, *, job_id, annotation, accepted_at, commit=True) -> int:
    row = conn.execute("SELECT state FROM memecoin_x_llm_jobs WHERE id=?", (int(job_id),)).fetchone()
    if row is None or row[0] != "accepted":
        raise ValueError("llm annotation requires accepted job")
    conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_llm_annotations
            (job_id, summary, intent, uncertainty, risk_concerns_json, claims_json,
             accepted_at, display_only)
        VALUES (?,?,?,?,?,?,?,1)
    """, (int(job_id), annotation["summary"], annotation["intent"],
          annotation["uncertainty"],
          _json.dumps(annotation.get("risk_concerns", []), sort_keys=True),
          _json.dumps(annotation.get("claims", []), sort_keys=True), accepted_at))
    result = conn.execute(
        "SELECT id FROM memecoin_x_llm_annotations WHERE job_id=?", (int(job_id),)).fetchone()
    if commit:
        conn.commit()
    return int(result[0])


def llm_digest_watermark(conn) -> int:
    row = conn.execute(
        "SELECT last_event_id FROM memecoin_x_llm_watermarks WHERE mode='digest'").fetchone()
    return int(row[0] or 0) if row else 0


def commit_llm_digest_watermark(
        conn, *, through_event_id, committed_at, job_id=None, empty=False,
        commit=True) -> None:
    if not empty:
        if job_id is None:
            raise ValueError("digest completion must be accepted or explicitly empty")
        row = conn.execute(
            "SELECT mode,state FROM memecoin_x_llm_jobs WHERE id=?", (int(job_id),)).fetchone()
        if row is None or row["mode"] != "digest" or row["state"] != "accepted":
            raise ValueError("digest watermark requires accepted digest job")
    current = llm_digest_watermark(conn)
    if int(through_event_id) < current:
        raise ValueError("digest watermark cannot move backward")
    conn.execute("""
        INSERT INTO memecoin_x_llm_watermarks (mode,last_event_id,committed_at)
        VALUES ('digest',?,?)
        ON CONFLICT(mode) DO UPDATE SET
          last_event_id=excluded.last_event_id, committed_at=excluded.committed_at
    """, (int(through_event_id), committed_at))
    if commit:
        conn.commit()


def start_capture_run(
        conn, *, run_id, parser_version, started_at, clock_snapshot, prior_sentinel) -> bool:
    """Create the audit row before card persistence. False means the run already exists."""
    if conn.execute(
            "SELECT 1 FROM memecoin_x_capture_runs WHERE run_id=?", (run_id,)).fetchone():
        return False
    conn.execute("""
        INSERT INTO memecoin_x_capture_runs
            (run_id, parser_version, started_at, clock_offset_s, clock_offset_age_s,
             clock_domain, prior_sentinel, status)
        VALUES (?,?,?,?,?,?,?,'running')
    """, (run_id, parser_version, started_at, clock_snapshot.get("offset_s"),
          clock_snapshot.get("offset_age_s"),
          clock_snapshot.get("clock_domain", "legacy_unknown"), prior_sentinel))
    conn.commit()
    return True


def finish_capture_run(
        conn, *, run_id, finished_at, status, cards_seen, new_posts, new_relationships,
        parse_failures, newest_sentinel, sentinel_found, error_detail=None,
        cards_scanned=None, stop_reason=None, cadence_bucket=None, scroll_count=None,
        dom_wait_ms=None, reload_reason=None, recovery_used=None,
        operator_postponed=None) -> None:
    if status not in CAPTURE_RUN_STATUSES or status == "running":
        raise ValueError(f"invalid capture run status: {status}")
    cur = conn.execute("""
        UPDATE memecoin_x_capture_runs
        SET finished_at=?, status=?, cards_seen=?, cards_scanned=?, stop_reason=?,
            new_posts=?, new_relationships=?, parse_failures=?, newest_sentinel=?,
            sentinel_found=?, error_detail=?, cadence_bucket=?, scroll_count=?, dom_wait_ms=?,
            reload_reason=?, recovery_used=?, operator_postponed=?
        WHERE run_id=? AND status='running'
    """, (finished_at, status, int(cards_seen),
          int(cards_seen if cards_scanned is None else cards_scanned), stop_reason,
          int(new_posts), int(new_relationships), int(parse_failures), newest_sentinel,
          None if sentinel_found is None else (1 if sentinel_found else 0),
          error_detail, cadence_bucket, scroll_count, dom_wait_ms, reload_reason,
          None if recovery_used is None else (1 if recovery_used else 0),
          None if operator_postponed is None else (1 if operator_postponed else 0), run_id))
    if cur.rowcount != 1:
        conn.rollback()
        raise ValueError(f"capture run is not running: {run_id}")
    conn.commit()


def record_capture_diagnostic(
        conn, *, capture_run_id, observed_at, clock_domain, diagnostic_code,
        parser_version, card_index=None, canonical_post_id=None, article_fingerprint=None,
        details=None, raw_card=None, commit=True) -> None:
    if diagnostic_code not in CAPTURE_DIAGNOSTIC_CODES:
        raise ValueError(f"unknown capture diagnostic: {diagnostic_code}")
    conn.execute("""
        INSERT INTO memecoin_x_capture_diagnostics
            (capture_run_id, observed_at, clock_domain, diagnostic_code, card_index,
             parser_version, canonical_post_id, article_fingerprint, details_json, raw_card_json)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (capture_run_id, observed_at, clock_domain, diagnostic_code, card_index,
          parser_version, canonical_post_id, article_fingerprint,
          _json.dumps(details, sort_keys=True) if details is not None else None,
          _json.dumps(raw_card, sort_keys=True) if raw_card is not None else None))
    if commit:
        conn.commit()


def latest_capture_sentinel(conn):
    row = conn.execute("""
        SELECT newest_sentinel FROM memecoin_x_capture_runs
        WHERE status IN ('ok','no_new') AND finished_at IS NOT NULL
          AND newest_sentinel IS NOT NULL
        ORDER BY id DESC LIMIT 1
    """).fetchone()
    return row[0] if row else None


def _persist_observation(
        conn, post, rel, raw_card, *, observed_at, clock_domain, capture_run_id):
    before = conn.total_changes
    conn.execute("""
        INSERT OR IGNORE INTO memecoin_x_card_observations
            (capture_run_id, canonical_post_id, author_handle, actor_handle, relationship,
             observed_at, clock_domain, parser_version, full_text, text_complete,
             posted_at_raw, posted_at, extracted_urls, raw_card_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (capture_run_id, post["canonical_post_id"], post["author_handle"],
          rel["actor_handle"], rel["relationship"], observed_at, clock_domain,
          post["parser_version"], post["full_text"], 1 if post["text_complete"] else 0,
          post["posted_at_raw"], post["posted_at"], _json.dumps(post["urls"]),
          _json.dumps(raw_card)))
    new_observation = conn.total_changes > before
    row = conn.execute("""
        SELECT id FROM memecoin_x_card_observations
        WHERE capture_run_id=? AND canonical_post_id=? AND actor_handle=? AND relationship=?
    """, (capture_run_id, post["canonical_post_id"], rel["actor_handle"],
          rel["relationship"])).fetchone()
    return row[0], new_observation


def persist_card(
        conn, normalized, *, observed_at, clock_domain, capture_run_id, now_iso,
        commit=True) -> dict:
    if not normalized.get("eligible_for_forward"):
        return {"new_post": False, "new_relationship": False, "new_observation": False}
    post = normalized["post"]
    rel = normalized["relationship"]
    author_role = role_for(conn, post["author_handle"])
    actor_role = role_for(conn, rel["actor_handle"])

    cur = conn.execute("SELECT 1 FROM memecoin_x_forward_posts WHERE canonical_post_id=?",
                       (post["canonical_post_id"],)).fetchone()
    new_post = cur is None
    if new_post:
        conn.execute("""
            INSERT INTO memecoin_x_forward_posts
                (canonical_post_id, url, author_handle, author_role_at_capture, full_text,
                 text_complete, posted_at_raw, posted_at, first_observed_at, last_observed_at,
                 capture_run_id, parser_version, clock_domain, extracted_urls, raw_card_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (post["canonical_post_id"], post["url"], post["author_handle"], author_role,
              post["full_text"], 1 if post["text_complete"] else 0,
              post["posted_at_raw"], post["posted_at"],
              observed_at, observed_at, capture_run_id, post["parser_version"], clock_domain,
              _json.dumps(post["urls"]), _json.dumps(normalized["raw_card"])))
    else:
        conn.execute("""
            UPDATE memecoin_x_forward_posts
            SET last_observed_at=?
            WHERE canonical_post_id=?
              AND julianday(?) > julianday(last_observed_at)
        """, (observed_at, post["canonical_post_id"], observed_at))

    observation_id, new_observation = _persist_observation(
        conn, post, rel, normalized["raw_card"], observed_at=observed_at,
        clock_domain=clock_domain, capture_run_id=capture_run_id)

    is_tier1 = 1 if actor_role == "tier1_trader" else 0
    rel_key = (rel["canonical_post_id"], rel["actor_handle"], rel["relationship"])
    new_rel = conn.execute("""
        SELECT 1 FROM memecoin_x_relationships
        WHERE canonical_post_id=? AND actor_handle=? AND relationship=?
    """, rel_key).fetchone() is None
    if new_rel:
        conn.execute("""
            INSERT INTO memecoin_x_relationships
                (canonical_post_id, actor_handle, actor_role_at_obs, actor_is_tier1_at_obs,
                 relationship, observed_at, capture_run_id, clock_domain,
                 referenced_author, referenced_post_id, source_observation_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (rel["canonical_post_id"], rel["actor_handle"], actor_role, is_tier1,
              rel["relationship"], observed_at, capture_run_id, clock_domain,
              rel["referenced_author"], rel["referenced_post_id"], observation_id))
    else:
        conn.execute("""
            UPDATE memecoin_x_relationships
            SET referenced_author=COALESCE(referenced_author, ?),
                referenced_post_id=COALESCE(referenced_post_id, ?),
                source_observation_id=COALESCE(source_observation_id, ?)
            WHERE canonical_post_id=? AND actor_handle=? AND relationship=?
        """, (rel["referenced_author"], rel["referenced_post_id"], observation_id, *rel_key))

    parser_version = int(post["parser_version"])
    row = conn.execute("""
        SELECT MAX(parser_version) FROM memecoin_x_coin_mentions WHERE canonical_post_id=?
    """, (post["canonical_post_id"],)).fetchone()
    existing_parser_version = row[0] if row else None
    parser_upgrade = (existing_parser_version is not None
                      and parser_version > existing_parser_version)
    parser_state_applicable = (existing_parser_version is None
                               or parser_version == existing_parser_version
                               or (parser_upgrade and post["text_complete"]))
    if parser_state_applicable:
        if existing_parser_version is not None and parser_version > existing_parser_version:
            conn.execute("""
                DELETE FROM memecoin_x_coin_mentions
                WHERE canonical_post_id=? AND parser_version < ?
            """, (post["canonical_post_id"], parser_version))
        for m in normalized["mentions"]:
            values = (post["canonical_post_id"], m["network_id"], m["mint"], m["ticker"],
                      m["extraction_method"], 1 if m["ambiguous"] else 0,
                      1 if m["eligible"] else 0, parser_version, observation_id)
            conn.execute("""
                INSERT OR IGNORE INTO memecoin_x_coin_mentions
                    (canonical_post_id, network_id, mint, ticker, extraction_method,
                     ambiguous, eligible, parser_version, source_observation_id)
                VALUES (?,?,?,?,?,?,?,?,?)
            """, values)
            if post["text_complete"] and m["eligible"]:
                conn.execute("""
                    UPDATE memecoin_x_coin_mentions
                    SET eligible=1, ambiguous=?, source_observation_id=?
                    WHERE canonical_post_id=? AND network_id IS ? AND mint IS ? AND ticker IS ?
                      AND extraction_method=? AND parser_version=? AND eligible=0
                """, (1 if m["ambiguous"] else 0, observation_id,
                      post["canonical_post_id"], m["network_id"], m["mint"], m["ticker"],
                      m["extraction_method"], parser_version))
            conn.execute("""
                UPDATE memecoin_x_coin_mentions
                SET source_observation_id=COALESCE(source_observation_id, ?)
                WHERE canonical_post_id=? AND network_id IS ? AND mint IS ? AND ticker IS ?
                  AND extraction_method=? AND parser_version=?
            """, (observation_id, post["canonical_post_id"], m["network_id"], m["mint"],
                  m["ticker"], m["extraction_method"], parser_version))
    if commit:
        conn.commit()
    return {"new_post": new_post, "new_relationship": new_rel,
            "new_observation": new_observation}

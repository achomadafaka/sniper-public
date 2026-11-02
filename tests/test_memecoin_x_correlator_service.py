import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from sniper.analysis.memecoin_x import roster as R
from sniper.analysis.memecoin_x.correlator import build_forward_links
from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables, seed_roster
from sniper.services.memecoin_x_correlator import run_once


NOW = "2026-07-13T12:10:00+00:00"
SOL = "62YE1d4sRArBQzR5bdbxsx2k9LV3MdPV4xMC4Di2pump"


def _x_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    seed_roster(conn, R.INITIAL_ROSTER, now_iso=NOW)
    cur = conn.execute("""
        INSERT INTO memecoin_x_card_observations
            (capture_run_id, canonical_post_id, author_handle, actor_handle, relationship,
             observed_at, clock_domain, parser_version, full_text, text_complete,
             extracted_urls, raw_card_json)
        VALUES ('run','post','poptyedev','poptyedev','direct',
                '2026-07-13T12:00:00+00:00','observed_real_utc',1,'',1,'[]','{}')
    """)
    conn.execute("""
        INSERT INTO memecoin_x_forward_posts
            (canonical_post_id, author_handle, author_role_at_capture, full_text,
             text_complete, first_observed_at, last_observed_at, parser_version, clock_domain)
        VALUES ('post','poptyedev','tier2_source','',1,
                '2026-07-13T12:00:00+00:00','2026-07-13T12:00:00+00:00',1,
                'observed_real_utc')
    """)
    conn.execute("""
        INSERT INTO memecoin_x_coin_mentions
            (canonical_post_id, network_id, mint, extraction_method, ambiguous, eligible,
             parser_version, source_observation_id)
        VALUES ('post','solana',?,'contract_text',0,1,1,?)
    """, (SOL, cur.lastrowid))
    conn.commit()
    return conn


def _fomo_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_memecoin_tables(conn)
    return conn


def _insert_buy(conn, event_id="buy-1", mint=SOL):
    conn.execute("""
        INSERT INTO memecoin_feed_observations_v2
            (upstream_event_id, raw_type, normalized_side, source_user_id, source_handle,
             network_id, mint, observed_at, was_curated, was_real_approved, raw_json,
             capture_version, clock_domain)
        VALUES (?, 'swap', 'buy', 'u1', 'zinceth', 'solana', ?,
                '2026-07-13T12:05:00+00:00',1,1,?,2,'observed_real_utc')
    """, (event_id, mint, json.dumps({"id": event_id})))
    conn.commit()


def test_run_once_pages_buys_persists_links_and_advances_cursor():
    x_conn = _x_conn()
    fomo_conn = _fomo_conn()
    _insert_buy(fomo_conn)
    result = run_once(
        x_conn, fomo_conn, run_id="corr-1", observed_at=NOW,
        page_size=1, max_pages=2)
    source_id = fomo_conn.execute(
        "SELECT id FROM memecoin_feed_observations_v2").fetchone()[0]
    assert result == {
        "run_id": "corr-1", "after_fomo_id": 0, "through_fomo_id": source_id,
        "source_rows": 1, "eligible_buys": 1, "links_created": 1,
        "links_upgraded": 0, "status": "ok",
    }
    assert x_conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_links").fetchone()[0] == 1
    assert x_conn.execute(
        "SELECT MAX(through_fomo_id) FROM memecoin_x_correlator_runs WHERE status='ok'"
    ).fetchone()[0] == source_id


def test_rerun_after_link_persistence_before_cursor_finish_is_idempotent():
    x_conn = _x_conn()
    fomo_conn = _fomo_conn()
    _insert_buy(fomo_conn)
    buy = dict(fomo_conn.execute("""
        SELECT id, upstream_event_id, source_user_id, source_handle, network_id, mint,
               observed_at, clock_domain, capture_version, normalized_side, was_real_approved
        FROM memecoin_feed_observations_v2
    """).fetchone())
    assert build_forward_links(x_conn, [buy])["created"] == 1
    result = run_once(x_conn, fomo_conn, run_id="corr-rerun", observed_at=NOW)
    assert result["links_created"] == 0
    assert x_conn.execute("SELECT COUNT(*) FROM memecoin_x_forward_links").fetchone()[0] == 1


def test_run_once_records_error_without_advancing_completed_cursor(monkeypatch):
    import sniper.services.memecoin_x_correlator as service

    x_conn = _x_conn()
    fomo_conn = _fomo_conn()
    _insert_buy(fomo_conn)
    monkeypatch.setattr(service, "build_forward_links", lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        service.run_once(x_conn, fomo_conn, run_id="corr-error", observed_at=NOW)
    row = x_conn.execute(
        "SELECT status, error_detail FROM memecoin_x_correlator_runs WHERE run_id='corr-error'"
    ).fetchone()
    assert row["status"] == "error" and "boom" in row["error_detail"]
    assert x_conn.execute(
        "SELECT COUNT(*) FROM memecoin_x_correlator_runs WHERE status='ok'").fetchone()[0] == 0


def test_cli_readonly_source_connection_rejects_writes(tmp_path):
    path = tmp_path / "fomo.db"
    sqlite3.connect(path).close()
    spec = importlib.util.spec_from_file_location(
        "run_memecoin_x_correlator", Path("scripts/run_memecoin_x_correlator.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = module._open_readonly(path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("CREATE TABLE forbidden (id INTEGER)")
    conn.close()

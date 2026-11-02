import json
import sqlite3

import pytest

from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables


def _fixture_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    relationships = ["direct", "repost", "quote", "reply"]
    networks = ["solana", "4663", "8453", None]
    for index in range(40):
        post_id = str(10_000 + index)
        run_id = f"run-{index}"
        observed_at = f"2026-07-14T00:{index:02d}:00+00:00"
        relationship = relationships[index % len(relationships)]
        network = networks[index % len(networks)]
        conn.execute(
            """
            INSERT INTO memecoin_x_forward_posts
              (canonical_post_id,url,author_handle,full_text,text_complete,first_observed_at,
               last_observed_at,capture_run_id,parser_version,clock_domain,extracted_urls,
               raw_card_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (post_id, f"https://x.com/source/status/{post_id}", f"author{index}",
             f"visible text {index}", 1, observed_at, observed_at, run_id, 7,
             "observed_real_utc", json.dumps([f"https://example.test/{index}"]), "{}"),
        )
        cursor = conn.execute(
            """
            INSERT INTO memecoin_x_card_observations
              (capture_run_id,canonical_post_id,author_handle,actor_handle,relationship,
               observed_at,clock_domain,parser_version,full_text,text_complete,extracted_urls,
               raw_card_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (run_id, post_id, f"author{index}", f"actor{index}", relationship,
             observed_at, "observed_real_utc", 7, f"visible text {index}", 1,
             json.dumps([f"https://example.test/{index}"]), "{}"),
        )
        observation_id = cursor.lastrowid
        if network is not None:
            conn.execute(
                """
                INSERT INTO memecoin_x_coin_mentions
                  (canonical_post_id,network_id,mint,ticker,extraction_method,ambiguous,
                   eligible,parser_version,source_observation_id)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (post_id, network, f"mint-{network}-{index}", f"T{index}",
                 "contract_text", 0, 1, 7, observation_id),
            )
        if index == 0:
            conn.execute(
                """
                INSERT INTO memecoin_x_capture_diagnostics
                  (capture_run_id,observed_at,clock_domain,diagnostic_code,parser_version,
                   canonical_post_id)
                VALUES (?,?,?,?,?,?)
                """,
                (run_id, observed_at, "observed_real_utc", "incomplete_text", 7, post_id),
            )
    conn.commit()
    return conn


def test_sampler_is_deterministic_stratified_and_complete(tmp_path):
    import scripts.sample_memecoin_x_audit as module

    conn = _fixture_db(tmp_path / "x.db")
    conn.execute(
        """
        INSERT INTO memecoin_x_capture_diagnostics
          (capture_run_id,observed_at,clock_domain,diagnostic_code,parser_version)
        VALUES ('run-0','2026-07-14T00:00:00+00:00','observed_real_utc',
                'missing_referenced_post_id',7)
        """
    )
    conn.commit()
    first = module.sample_audit_rows(conn, limit=30)
    second = module.sample_audit_rows(conn, limit=30)

    assert first == second
    assert len(first) == 30
    assert {row["relationship"] for row in first} == {"direct", "repost", "quote", "reply"}
    sampled_networks = {
        mention["network_id"] for row in first for mention in row["mentions"]
    }
    assert {"solana", "4663", "8453"} <= sampled_networks
    row = first[0]
    assert {
        "observation_id", "post_url", "canonical_post_id", "author_handle",
        "actor_handle", "relationship", "observed_at", "clock_domain", "full_text",
        "text_complete", "extracted_urls", "mentions", "parser_version",
        "diagnostic_codes", "raw_card_available",
    } <= row.keys()
    assert row["post_url"].startswith("https://x.com/")
    assert isinstance(row["mentions"], list)
    assert row["raw_card_available"] is True
    post_zero = next(item for item in first if item["canonical_post_id"] == "10000")
    assert post_zero["diagnostic_codes"] == ["incomplete_text"]


def test_sampler_caps_to_available_rows_and_rejects_bad_limit(tmp_path):
    import scripts.sample_memecoin_x_audit as module

    conn = _fixture_db(tmp_path / "x.db")
    assert len(module.sample_audit_rows(conn, limit=100)) == 40
    with pytest.raises(ValueError, match="limit must be positive"):
        module.sample_audit_rows(conn, limit=0)


def test_sampler_filters_acceptance_window_and_parser_version(tmp_path):
    import scripts.sample_memecoin_x_audit as module

    conn = _fixture_db(tmp_path / "x.db")
    rows = module.sample_audit_rows(
        conn, limit=30, parser_version=7,
        since="2026-07-14T00:30:00+00:00")
    assert len(rows) == 10
    assert all(row["parser_version"] == 7 for row in rows)
    assert all(row["observed_at"] >= "2026-07-14T00:30:00+00:00" for row in rows)
    assert module.sample_audit_rows(conn, limit=30, parser_version=2) == []

    with pytest.raises(ValueError, match="timezone-aware"):
        module.sample_audit_rows(conn, limit=30, since="2026-07-14")


def test_audit_connection_is_read_only(tmp_path):
    import scripts.sample_memecoin_x_audit as module

    path = tmp_path / "x.db"
    _fixture_db(path).close()
    conn = module._open_audit_connection(path)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM memecoin_x_card_observations")

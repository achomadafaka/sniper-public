import sqlite3

import pytest

from sniper.persistence import db_memecoin_x as X


AS_OF = "2026-07-14T00:00:00+00:00"
MINT = "0x1111111111111111111111111111111111111111"
VERSIONS = {
    "prompt_version": "p1", "model_version": "m1",
    "output_schema_version": "o1", "generation": {"temperature": 0},
}


def _conn(event_type="tier2_scout"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    X.ensure_memecoin_x_tables(conn)
    obs = conn.execute(
        """
        INSERT INTO memecoin_x_card_observations
          (capture_run_id,canonical_post_id,author_handle,actor_handle,relationship,
           observed_at,clock_domain,parser_version,full_text,text_complete,raw_card_json)
        VALUES ('r','p','poptyedev','zinceth','repost','2026-07-13T23:00:00+00:00',
                'observed_real_utc',1,'captured fact',1,'{}')
        """
    ).lastrowid
    conn.execute(
        """
        INSERT INTO memecoin_x_coin_mentions
          (canonical_post_id,network_id,mint,extraction_method,eligible,parser_version,
           source_observation_id)
        VALUES ('p','4663',?,'contract_text',1,1,?)
        """, (MINT, obs))
    event_id = X.persist_signal_event(conn, {
        "logical_key": f"{event_type}:4663:mint:1", "event_type": event_type,
        "network_id": "4663", "mint": MINT,
        "triggered_at": "2026-07-13T23:00:00+00:00", "source_handle": "poptyedev",
        "evidence_observation_ids": [obs], "actor_handles": ["zinceth"],
        "relationship_counts": {"repost": 1},
    }, created_at="2026-07-13T23:00:00+00:00")
    return conn, event_id


def test_event_queue_accepts_only_consensus_or_tier2_scout_and_dedupes():
    from sniper.services.memecoin_x_llm_queue import enqueue_event_job

    conn, event_id = _conn()
    first = enqueue_event_job(
        conn, signal_event_id=event_id, as_of=AS_OF, provider="fake", model="m",
        versions=VERSIONS)
    second = enqueue_event_job(
        conn, signal_event_id=event_id, as_of=AS_OF, provider="fake", model="m",
        versions=VERSIONS)
    assert first == second
    row = conn.execute("SELECT mode,state,input_hash,input_json FROM memecoin_x_llm_jobs").fetchone()
    assert row["mode"] == "event" and row["state"] == "pending" and len(row["input_hash"]) == 64

    capture_conn, capture_id = _conn("capture_tag")
    assert enqueue_event_job(
        capture_conn, signal_event_id=capture_id, as_of=AS_OF,
        provider="fake", model="m", versions=VERSIONS) is None


def test_digest_queues_only_new_events_after_committed_watermark():
    from sniper.services.memecoin_x_llm_queue import enqueue_digest_job

    empty = sqlite3.connect(":memory:"); empty.row_factory = sqlite3.Row
    X.ensure_memecoin_x_tables(empty)
    assert enqueue_digest_job(
        empty, as_of=AS_OF, provider="fake", model="m", versions=VERSIONS) is None

    conn, _ = _conn()
    job_id = enqueue_digest_job(
        conn, as_of=AS_OF, provider="fake", model="m", versions=VERSIONS)
    assert job_id is not None
    assert enqueue_digest_job(
        conn, as_of=AS_OF, provider="fake", model="m", versions=VERSIONS) == job_id
    job = conn.execute("SELECT input_json FROM memecoin_x_llm_jobs WHERE id=?", (job_id,)).fetchone()
    assert '"after_event_id":0' in job[0] and '"through_event_id":1' in job[0]


def test_on_request_requires_unique_request_id_and_frozen_inputs():
    from sniper.services.memecoin_x_llm_queue import enqueue_on_request_job

    conn, _ = _conn()
    kwargs = dict(
        request_id="operator-1", as_of=AS_OF, window="24h", network_id="4663",
        mint=MINT, provider="fake", model="m", versions=VERSIONS)
    first = enqueue_on_request_job(conn, **kwargs)
    assert enqueue_on_request_job(conn, **kwargs) == first
    with pytest.raises(ValueError, match="conflicting"):
        enqueue_on_request_job(conn, **{**kwargs, "window": "7d"})
    row = conn.execute("SELECT mode,as_of,network_id,mint FROM memecoin_x_llm_jobs").fetchone()
    assert tuple(row) == ("on_request", AS_OF, "4663", MINT)


def test_queue_rejects_missing_versions_or_naive_as_of():
    from sniper.services.memecoin_x_llm_queue import enqueue_event_job

    conn, event_id = _conn()
    with pytest.raises(ValueError):
        enqueue_event_job(
            conn, signal_event_id=event_id, as_of="2026-07-14", provider="fake",
            model="m", versions=VERSIONS)
    with pytest.raises(ValueError, match="version"):
        enqueue_event_job(
            conn, signal_event_id=event_id, as_of=AS_OF, provider="fake",
            model="m", versions={})

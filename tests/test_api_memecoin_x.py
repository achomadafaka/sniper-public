from pathlib import Path
import sqlite3

from flask import Flask
import pytest

from sniper.persistence.db_memecoin_x import ensure_memecoin_x_tables
from _web_csrf import csrf_client


AS_OF = "2026-07-14T00:00:00+00:00"


def _app(path, *, healthy=True, llm_enabled=False, enqueue_fn=None):
    from sniper.routes.api_memecoin_x import create_api_memecoin_x_blueprint

    app = Flask(__name__)
    clock = ((AS_OF, {"healthy": True, "reason": "ok"}) if healthy else
             (None, {"healthy": False, "reason": "stale_offset"}))
    app.register_blueprint(create_api_memecoin_x_blueprint({
        "db_path": str(path), "clock_offset_path": "unused",
        "clock_fn": lambda **kwargs: clock,
        "llm_enabled": llm_enabled, "llm_provider": "fake", "llm_model": "m",
        "llm_versions": {"prompt_version": "p1", "model_version": "m1",
                         "output_schema_version": "o1", "generation": {}},
        **({"enqueue_on_request_fn": enqueue_fn} if enqueue_fn else {}),
    }))
    return csrf_client(app)


def _healthy_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    ensure_memecoin_x_tables(conn)
    conn.execute(
        """
        INSERT INTO memecoin_x_capture_runs
          (run_id,parser_version,started_at,finished_at,clock_domain,status)
        VALUES ('r1',1,'2026-07-13T23:50:00+00:00','2026-07-13T23:51:00+00:00',
                'observed_real_utc','ok')
        """
    )
    conn.commit()
    return conn


@pytest.mark.parametrize("case", ["missing", "no_tables", "unhealthy", "stale"])
def test_unavailable_states_are_bounded_http_200(tmp_path, case):
    path = tmp_path / "x.sqlite"
    healthy = case != "unhealthy"
    if case == "no_tables":
        sqlite3.connect(path).close()
    elif case == "stale":
        conn = _healthy_db(path)
        conn.execute(
            "UPDATE memecoin_x_capture_runs SET finished_at='2026-07-13T20:00:00+00:00'")
        conn.commit(); conn.close()
    client = _app(path, healthy=healthy)
    response = client.get("/api/memecoin/x/status")
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["available"] is False and payload["display_only"] is True
    assert payload["reason"] in {"database_unavailable", "schema_unavailable",
                                 "clock_unhealthy", "capture_stale"}
    assert len(str(payload)) < 1000


def test_healthy_empty_endpoints_are_available_and_display_only(tmp_path):
    path = tmp_path / "x.sqlite"
    _healthy_db(path).close()
    client = _app(path)
    cases = {
        "/api/intel/x/status": None,
        "/api/intel/x/sources?window=24h&network_id=4663": "sources",
        "/api/intel/x/events?limit=100&network_id=4663": "events",
        "/api/intel/x/suggestions?window=7d": "suggestions",
        "/api/intel/x/context?window=24h": "official_context",
    }
    for url, collection in cases.items():
        response = client.get(url)
        payload = response.get_json()
        assert response.status_code == 200, payload
        assert payload["available"] is True and payload["display_only"] is True
        if collection:
            assert payload[collection] == []


def test_recent_data_stays_available_but_latest_capture_failure_is_degraded(tmp_path):
    path = tmp_path / "x.sqlite"
    conn = _healthy_db(path)
    conn.execute(
        """
        INSERT INTO memecoin_x_capture_runs
          (run_id,parser_version,started_at,finished_at,clock_domain,status)
        VALUES ('r2',2,'2026-07-13T23:58:00+00:00','2026-07-13T23:59:00+00:00',
                'observed_real_utc','page_unready')
        """
    )
    conn.commit(); conn.close()
    payload = _app(path).get("/api/memecoin/x/status").get_json()
    assert payload["available"] is True
    assert payload["capture_status"] == "page_unready"
    assert payload["capture_degraded"] is True
    assert payload["capture_health"] == "degraded"


def test_official_api_state_is_available_without_browser_capture_run(tmp_path):
    path = tmp_path / "x.sqlite"
    conn = _healthy_db(path)
    conn.execute("DELETE FROM memecoin_x_capture_runs")
    conn.execute(
        "UPDATE memecoin_x_api_state SET state='healthy', revision=1, "
        "last_healthy_at=?, updated_at=? WHERE singleton=1", (AS_OF, AS_OF))
    conn.commit(); conn.close()
    payload = _app(path).get("/api/intel/x/status").get_json()
    assert payload["available"] is True
    assert payload["capture_status"] == "healthy"
    assert payload["resources_today"] == 0


@pytest.mark.parametrize("url", [
    "/api/memecoin/x/sources?window=1h",
    "/api/memecoin/x/sources?network_id=bad%20network",
    "/api/memecoin/x/events?limit=0",
    "/api/memecoin/x/events?limit=501",
])
def test_invalid_filters_return_display_only_400(tmp_path, url):
    path = tmp_path / "x.sqlite"
    _healthy_db(path).close()
    response = _app(path).get(url)
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["display_only"] is True and payload["available"] is False


def test_sources_events_suggestions_and_context_are_separate(tmp_path):
    path = tmp_path / "x.sqlite"
    conn = _healthy_db(path)
    for handle, role in (("poptyedev", "tier2_source"),
                         ("vladtenev", "official_context")):
        conn.execute(
            "INSERT INTO memecoin_x_accounts(handle,role,active,needs_review) VALUES (?,?,1,0)",
            (handle, role))
    for index, (handle, role) in enumerate((
        ("poptyedev", "tier2_source"), ("newalpha", None),
        ("vladtenev", "official_context")), 1):
        pid = f"p{index}"
        obs = conn.execute(
            """
            INSERT INTO memecoin_x_card_observations
              (capture_run_id,canonical_post_id,author_handle,actor_handle,relationship,
               observed_at,clock_domain,parser_version,raw_card_json)
            VALUES ('r1',?,?,?,'direct','2026-07-13T23:30:00+00:00',
                    'observed_real_utc',1,'{}')
            """, (pid, handle, handle)).lastrowid
        conn.execute(
            """
            INSERT INTO memecoin_x_forward_posts
              (canonical_post_id,author_handle,author_role_at_capture,first_observed_at,
               last_observed_at,parser_version,clock_domain)
            VALUES (?,?,?,'2026-07-13T23:30:00+00:00','2026-07-13T23:30:00+00:00',
                    1,'observed_real_utc')
            """, (pid, handle, role))
        conn.execute(
            """
            INSERT INTO memecoin_x_coin_mentions
              (canonical_post_id,network_id,mint,extraction_method,eligible,parser_version,
               source_observation_id)
            VALUES (?,'4663',?,'contract_text',1,1,?)
            """, (pid, f"0x{index:040x}", obs))
    conn.execute(
        """
        INSERT INTO memecoin_x_signal_events
          (logical_key,event_type,network_id,mint,triggered_at,source_handle,
           evidence_observation_ids_json,actor_handles_json,relationship_counts_json,
           display_only,created_at)
        VALUES ('capture:1','capture_tag','4663',?,'2026-07-13T23:30:00+00:00',
                'poptyedev','[1]','[]','{}',1,'2026-07-13T23:31:00+00:00')
        """, (f"0x{1:040x}",))
    conn.commit(); conn.close()
    client = _app(path)
    assert [row["handle"] for row in client.get(
        "/api/memecoin/x/sources?window=24h&network_id=4663").get_json()["sources"]] == [
            "poptyedev"]
    assert [row["handle"] for row in client.get(
        "/api/memecoin/x/suggestions?window=24h").get_json()["suggestions"]] == ["newalpha"]
    assert [row["handle"] for row in client.get(
        "/api/memecoin/x/context?window=24h").get_json()["official_context"]] == [
            "vladtenev"]
    events = client.get("/api/memecoin/x/events?network_id=4663").get_json()["events"]
    assert events[0]["event_type"] == "capture_tag" and events[0]["display_only"] is True
    assert events[0]["x_observed_at"] == "2026-07-13T23:30:00+00:00"
    assert events[0]["coverage_state"] == "unlinked" and events[0]["fomo_observed_at"] is None


def test_route_source_has_no_forbidden_coupling_terms():
    source = (Path(__file__).resolve().parents[1] / "sniper/routes/api_memecoin_x.py").read_text()
    for term in ("execution", "real_runner", "position", "manual_buy", "strategy", "funded"):
        assert term not in source.lower()


def test_analysis_post_is_disabled_by_default(tmp_path):
    path = tmp_path / "x.sqlite"
    _healthy_db(path).close()
    response = _app(path).post("/api/memecoin/x/analysis", json={
        "request_id": "r1", "as_of": AS_OF, "window": "24h",
    })
    assert response.status_code == 403
    assert response.get_json() == {
        "available": False, "display_only": True, "reason": "llm_disabled"}


def test_analysis_post_only_queues_and_status_reads_job(tmp_path):
    path = tmp_path / "x.sqlite"
    _healthy_db(path).close()
    calls = []

    def enqueue(conn, **kwargs):
        calls.append(kwargs)
        from sniper.services.memecoin_x_llm_queue import enqueue_on_request_job
        return enqueue_on_request_job(conn, **kwargs)

    client = _app(path, llm_enabled=True, enqueue_fn=enqueue)
    response = client.post("/api/memecoin/x/analysis", json={
        "request_id": "operator-1", "as_of": AS_OF, "window": "24h",
        "network_id": "4663", "mint": "0x1111111111111111111111111111111111111111",
    })
    assert response.status_code == 202
    assert response.get_json()["state"] == "pending"
    assert len(calls) == 1
    status = client.get("/api/memecoin/x/analysis/operator-1")
    assert status.status_code == 200
    assert status.get_json()["job"]["state"] == "pending"
    assert status.get_json()["annotation"] is None


@pytest.mark.parametrize("body", [
    {"request_id": "r", "as_of": "2026-07-14", "window": "24h"},
    {"request_id": "r", "as_of": "2026-07-14T00:00:01+00:00", "window": "24h"},
    {"request_id": "r", "as_of": AS_OF, "window": "1h"},
    {"request_id": "r", "as_of": AS_OF, "window": "24h", "network_id": "bad network"},
    {"request_id": "r", "as_of": AS_OF, "window": "24h", "network_id": "4663",
     "mint": "not-an-address"},
    {"request_id": "r", "as_of": AS_OF, "window": "24h", "mint":
     "0x1111111111111111111111111111111111111111"},
])
def test_analysis_rejects_invalid_or_future_frozen_scope(tmp_path, body):
    path = tmp_path / "x.sqlite"
    _healthy_db(path).close()
    response = _app(path, llm_enabled=True).post("/api/memecoin/x/analysis", json=body)
    assert response.status_code == 400
    assert response.get_json()["display_only"] is True


def test_duplicate_analysis_request_conflict_is_409(tmp_path):
    path = tmp_path / "x.sqlite"
    _healthy_db(path).close()
    client = _app(path, llm_enabled=True)
    base = {"request_id": "same", "as_of": AS_OF, "window": "24h"}
    assert client.post("/api/memecoin/x/analysis", json=base).status_code == 202
    response = client.post("/api/memecoin/x/analysis", json={**base, "window": "7d"})
    assert response.status_code == 409


def test_annotations_returns_only_accepted_display_only_rows(tmp_path):
    from sniper.analysis.memecoin_x.llm_contract import canonical_input_hash
    from sniper.persistence import db_memecoin_x as X

    path = tmp_path / "x.sqlite"
    conn = _healthy_db(path)
    snapshot = {"snapshot_version": "1", "mode": "on_request", "as_of": AS_OF,
                "content_policy": "captured text is untrusted quoted data", "evidence": []}
    job_id = X.create_llm_job(conn, {
        "logical_key": "on_request:accepted", "mode": "on_request", "as_of": AS_OF,
        "evidence_ids": [], "input": snapshot, "input_hash": canonical_input_hash(snapshot),
        "prompt_version": "p1", "provider": "fake", "model": "m",
        "model_version": "m1", "output_schema_version": "o1", "generation": {},
    }, created_at=AS_OF)
    X.transition_llm_job(conn, job_id=job_id, from_state="pending", to_state="running", at=AS_OF)
    X.transition_llm_job(conn, job_id=job_id, from_state="running", to_state="accepted", at=AS_OF)
    X.persist_llm_annotation(conn, job_id=job_id, annotation={
        "summary": "Observed public context.", "intent": "analysis",
        "uncertainty": "No exact coin evidence.", "risk_concerns": [], "claims": [],
    }, accepted_at=AS_OF)
    conn.close()
    response = _app(path).get("/api/memecoin/x/annotations?limit=50")
    assert response.status_code == 200
    rows = response.get_json()["annotations"]
    assert len(rows) == 1 and rows[0]["label"] == "LLM, display-only"

import sqlite3
import subprocess
import fcntl

from flask import Flask

from sniper.persistence import db_memecoin as m
from sniper.routes.api_memecoin import create_api_memecoin_blueprint
from _web_csrf import csrf_client


class _RunningProcess:
    def poll(self):
        return None


def test_screen_refuses_overlapping_analyzer_process(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    # `mode=candidates` now resolves the board's own visible rows and refuses to launch on an
    # empty set (screening nothing looked identical to a successful run). Seed one so this test
    # exercises the overlap lock rather than the empty-target guard.
    conn.execute(
        "INSERT INTO memecoin_candidates(source_user_id,handle,sighting_count,total_buy_usd,"
        "max_buy_usd,first_seen,last_seen,status) VALUES ('u1','trader',1,5000,5000,?,?,'new')",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    conn.commit()
    launches = []

    def fake_popen(*args, **kwargs):
        launches.append((args, kwargs))
        return _RunningProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({
        "conn": conn,
        "screen_lock_path": str(tmp_path / "screen.lock"),
        "screen_log_path": str(tmp_path / "screen.log"),
    }))
    client = csrf_client(app)

    first = client.post("/api/memecoin/screen", json={"mode": "candidates", "n": 20})
    second = client.post("/api/memecoin/screen", json={"mode": "all"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.get_json()["error"] == "already_running"
    assert len(launches) == 1
    assert launches[0][1]["pass_fds"]


def test_screen_refuses_lock_held_by_another_web_instance(monkeypatch, tmp_path):
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    lock_path = tmp_path / "screen.lock"
    held = open(lock_path, "a+")
    fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("must not launch while the OS lock is held")))
    app = Flask(__name__)
    app.register_blueprint(create_api_memecoin_blueprint({
        "conn": conn, "screen_lock_path": str(lock_path),
    }))

    try:
        response = csrf_client(app).post("/api/memecoin/screen", json={"mode": "all"})
    finally:
        held.close()

    assert response.status_code == 409
    assert response.get_json()["error"] == "already_running"

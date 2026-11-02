"""The research pass must not be able to stall the funded loop or hold the ledger across a fetch.

2026-09-25 18:30Z: a host DNS stall held the inline pump/capture pass ~100 s inside getaddrinfo,
the funded loop missed its sweep, the watchdog killed the runner and a funded buy's retry window
died with it. The same pass had written to the funded connection before the fetch.
"""
import pathlib
import sqlite3
import time

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_capture_v2 as capture
from sniper.services import memecoin_research_capture as rc


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def test_no_transaction_is_open_while_the_capture_fetch_runs():
    conn = _conn()
    for i in range(3):                      # 3 cohorts, budget 1: the SKIPPED update runs first
        capture.register_capture_cohort(
            conn, network_id="solana", mint="M%d" % i, context="signal",
            observed_at="2026-09-25T18:30:00+00:00", expires_at="2026-09-26T00:00:00+00:00",
            priority=i)
    seen = []

    def stats(keys):
        seen.append(conn.in_transaction)
        return {}

    capture.capture_v2_pass(conn, "2026-09-25T18:30:30+00:00", stats_batch_fn=stats,
                            max_mints=1, sweep_id="s", period_s=30)
    assert seen == [False]


def test_the_blind_capture_fetch_also_runs_with_no_open_transaction():
    conn = _conn()
    for i in range(3):
        capture.register_capture_cohort(
            conn, network_id="4663", mint="0x%040d" % i, context="signal",
            observed_at="2026-09-25T18:30:00+00:00", expires_at="2026-09-26T00:00:00+00:00",
            priority=i)
    seen = []

    def prices(keys):
        seen.append(conn.in_transaction)
        return {}

    capture.capture_blind_pass(conn, "2026-09-25T18:30:30+00:00", price_batch_fn=prices,
                               max_mints=1, blind_chains=("4663",))
    assert seen == [False]


def test_the_funded_loop_no_longer_runs_the_research_pass():
    loop = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    assert "capture_v2_pass(" not in loop and "pump_capture_pass(" not in loop
    assert "ResearchCaptureWorker(" in loop


def test_transaction_span_is_measured_from_begin_to_commit():
    t = [0.0]
    meter = rc.TransactionSpanMeter(clock=lambda: t[0])
    for stmt, at in (("BEGIN ", 1.0), ("UPDATE x", 1.001), ("COMMIT", 1.004),
                     ("SELECT 1", 2.0), ("BEGIN ", 3.0), ("ROLLBACK", 3.250)):
        t[0] = at
        meter.trace(stmt)
    snap = meter.snapshot()
    assert snap["transaction_span_n"] == 2
    assert snap["transaction_span_ms_max"] == 250.0 and snap["transaction_span_ms_p50"] == 250.0


def test_a_failed_pass_is_counted_and_the_worker_keeps_going(monkeypatch):
    logs = []

    def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(rc, "research_capture_pass", boom)
    worker = rc.ResearchCaptureWorker(period_s=30, connect=_conn, now_iso=lambda: "T",
                                      log=logs.append)
    worker.run_once(_conn())
    monkeypatch.setattr(rc, "research_capture_pass", lambda *a, **k: None)
    worker.run_once(_conn())
    status = worker.status()
    assert status["failures"] == 1 and status["passes"] == 1
    assert status["last_error"] == "RuntimeError" and status["last_pass_at"] == "T"
    assert any("research capture pass failed" in line for line in logs)


def test_heartbeat_carries_the_research_capture_stamp(tmp_path):
    import json
    from sniper.services import memecoin_runner as run
    path = tmp_path / "hb.json"
    run.write_heartbeat({}, True, path=str(path), research_capture={
        "last_pass_at": "T", "passes": 3, "failures": 0, "transaction_span_ms_max": 4.2, "extra": 1})
    got = json.loads(path.read_text())["research_capture"]
    assert got["last_pass_at"] == "T" and got["transaction_span_ms_max"] == 4.2 and "extra" not in got


def test_a_dns_stall_cannot_hold_a_dexscreener_fetch_past_its_deadline(monkeypatch):
    from sniper.execution import solana_quote as q

    def stalled(*a, **k):
        time.sleep(2.0)                     # getaddrinfo ignoring the socket timeout
        return 200, {"pairs": []}, None

    monkeypatch.setattr(q, "_http", stalled)
    monkeypatch.setattr(q, "_DS_DEADLINE_S", 0.2)
    monkeypatch.setattr(q, "_ds_cooldown_until", [0.0])
    monkeypatch.setattr(q, "_ds_funded_cooldown_until", [0.0])
    started = time.monotonic()
    assert q.dexscreener_stats_batch_keyed([("solana", "M")]) == {}
    assert time.monotonic() - started < 1.0
    assert q.dexscreener_cooldown_active()            # the breaker tripped, no pile-up of calls

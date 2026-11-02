"""CPU-versus-wall diagnostics on entry-attempt traces (2026-09-26).

Slot-lane send 4029 spent 340 ms in `market_data` against 2-5 ms on seven other sends. That window
holds the committed shell-row INSERT (it takes the ledger write lock) plus classification, so the
trace now records thread CPU time beside wall time. These tests pin the three properties that
matter: the CPU figures never enter `_stages` (finish_entry_attempt rejects any extra key there,
which would lose every funded trace), an unmeasurable CPU reads as None and never as 0, and a
write-lock wait during the shell INSERT lands in the first substage as high wall with low CPU.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from sniper.persistence.db_memecoin import ensure_memecoin_tables
from sniper.services.memecoin_entry_trace import EntryAttemptTrace

NOW = "2026-09-26T22:00:00+00:00"


@dataclass
class FakeClock:
    elapsed: float = 100.0

    def perf_counter(self) -> float:
        return self.elapsed

    def advance(self, seconds: float) -> None:
        self.elapsed += seconds


@dataclass
class CpuClock(FakeClock):
    cpu: float = 5.0

    def thread_time(self) -> float:
        return self.cpu


def _event():
    return SimpleNamespace(
        fomo_event_id="event-cpu", source_user_id="wallet-1", source_handle="alice",
        network_id="solana", mint="Mint111", ticker="CPU", side="buy", their_price=0.25,
        their_market_cap=1_000_000.0, created_at="2026-09-26T21:59:59+00:00",
        received_at="2026-09-26T21:59:59.900000+00:00", clock_domain="observed_real_utc",
        signal_source="slot_lane")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    ensure_memecoin_tables(connection)
    return connection


def _payload(conn, attempt_id):
    row = conn.execute("SELECT trace_json FROM memecoin_entry_attempts WHERE attempt_id=?",
                       (attempt_id,)).fetchone()
    return json.loads(row["trace_json"])


def test_cpu_rides_beside_wall_and_never_enters_the_stage_columns(conn):
    clock = CpuClock()
    trace = EntryAttemptTrace.start(conn, _event(), NOW, clock)
    clock.advance(0.340)          # 340 ms of wall ...
    clock.cpu += 0.004            # ... of which 4 ms on this thread's CPU
    trace.mark_substage("attempt_row")
    trace.mark("market_data")
    clock.advance(0.010)
    clock.cpu += 0.009
    trace.mark("reservation", duration_ms=2.0)   # explicit duration: the CPU window is the gap

    assert trace.finish("refused", "test refusal") is True
    payload = _payload(conn, trace.attempt_id)
    for stage in payload["stages"].values():
        assert set(stage) == {"completed_at", "duration_ms", "measured"}
    cpu = payload["stage_cpu_ms"]
    assert cpu["market_data"]["cpu_ms"] == pytest.approx(4.0)
    assert cpu["market_data"]["wall_ms"] == pytest.approx(340.0)
    assert cpu["reservation"]["cpu_ms"] == pytest.approx(9.0)
    assert cpu["reservation"]["wall_ms"] == pytest.approx(10.0)
    assert payload["stages"]["reservation"]["duration_ms"] == pytest.approx(2.0)
    row = payload["substage_events"][0]
    assert row["name"] == "attempt_row"
    assert row["cpu_ms"] == pytest.approx(4.0)
    assert row["cpu_wall_ms"] == pytest.approx(340.0)


def test_a_clock_without_thread_time_records_none_not_zero(conn):
    clock = FakeClock()
    trace = EntryAttemptTrace.start(conn, _event(), NOW, clock)
    clock.advance(0.1)
    trace.mark_substage("attempt_row")
    trace.mark("market_data")
    assert trace.finish("refused", "test refusal") is True
    payload = _payload(conn, trace.attempt_id)
    assert payload["stage_cpu_ms"]["market_data"] == {"cpu_ms": None, "wall_ms": pytest.approx(100.0)}
    assert payload["substage_events"][0]["cpu_ms"] is None


def test_a_mark_taken_on_another_thread_has_no_cpu_delta(conn):
    trace = EntryAttemptTrace.start(conn, _event(), NOW, time)
    worker = threading.Thread(target=lambda: trace.mark("market_data"))
    worker.start()
    worker.join()
    assert trace._stage_cpu_ms["market_data"]["cpu_ms"] is None
    trace.mark("reservation")       # back on the starting thread, but the baseline is the worker's
    assert trace._stage_cpu_ms["reservation"]["cpu_ms"] is None


def test_a_caller_cannot_overwrite_the_cpu_fields(conn):
    trace = EntryAttemptTrace.start(conn, _event(), NOW, CpuClock())
    with pytest.raises(ValueError):
        trace.mark_substage("attempt_row", cpu_ms=0.0)


def test_a_write_lock_wait_lands_in_attempt_row_as_wall_not_cpu(tmp_path):
    """The runnable check for the lock hypothesis: another writer holds the ledger's write lock,
    the shell-row INSERT waits on SQLite's busy handler, and that wait shows up in the FIRST
    substage as high wall and low CPU. It proves the lock case CAN land there; it cannot tell a
    live stall from a GIL or scheduler wait, which also reads as low CPU."""
    path = str(tmp_path / "ledger.db")
    setup = sqlite3.connect(path)
    ensure_memecoin_tables(setup)
    setup.commit()
    setup.close()
    blocker = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    blocker.execute("PRAGMA journal_mode=WAL")
    blocker.execute("BEGIN IMMEDIATE")
    release = threading.Timer(0.40, lambda: blocker.execute("COMMIT"))
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        release.start()
        trace = EntryAttemptTrace.start(conn, _event(), NOW, time)
        trace.mark_substage("attempt_row")
        trace.mark("market_data")
    finally:
        release.join()
        blocker.close()
    row = trace._substage_events[0]
    assert row["name"] == "attempt_row"
    assert row["cpu_wall_ms"] >= 300.0
    assert row["cpu_ms"] is not None
    assert row["cpu_ms"] < 0.25 * row["cpu_wall_ms"]
    assert trace.finish("refused", "test refusal") is True
    conn.close()

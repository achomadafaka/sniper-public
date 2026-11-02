"""Rails behind an unknown-confirm partial (Alpha GO 2026-10-15, follow-up to the manual close).

real_exit_sweep skips a position whose de-risk claim or sig is set: claim_position_close would only
answer already_closing. So behind an unknown partial NO rail ran, the stop included, until the
recovery pass settled it (MEMECOIN_DERISK_RECOVERY_MIN_AGE_S = 120 s; 73882 waited exactly that).

Now the sweep offers such a position to RailDeriskSettler, at most once per 2 s, and moves on: no
RPC and no DB write on the exit tick. The settler's own thread does the one confirm read
(settle_derisk_before_close, the manual close's step) on its own connection; a settled row is swept
on the next tick. A not_found is never a clear. MEMECOIN_RAIL_CLOSE_SETTLES_PARTIAL=0 offers nothing.
"""
import json
import os
import pathlib
import sqlite3
import threading
import time

import pytest

from sniper.execution.exec_lock import serialize_executor
from sniper.execution.solana_exit_executor import execute_partial_exit
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as rr
from sniper.settings import memecoin as cfg

OPENED = "2026-10-15T13:10:00+00:00"
PARTIAL_AT = "2026-10-15T13:18:24+00:00"


def _at(sec):
    return "2026-10-15T13:19:%02d+00:00" % sec


class _Shared:
    """One in-memory ledger reachable from the settler thread: connect() hands out a proxy whose
    close() is a no-op, so the thread's per-item close never drops the shared database."""
    def __init__(self):
        self.c = sqlite3.connect(":memory:", check_same_thread=False)
        self.c.row_factory = sqlite3.Row
        m.ensure_memecoin_tables(self.c)

    def connect(self):
        shared = self.c

        class _Proxy:
            def __getattr__(self, name):
                return getattr(shared, name)

            def close(self):
                pass
        return _Proxy()


def _open_with_unknown_partial(c):
    pid = m.open_position(c, source_user_id="u1", source_handle="probablyinsider", mint="GRACEQ",
                          ticker="TKR", entry_price=1.0, entry_notional=50.0, entry_fill_source="chain",
                          entry_lag_s=0, their_size_usd=0.0, opened_at=OPENED, opened_recorded_at=OPENED,
                          strategy="alpha_b", execution_mode="real", network_id="solana")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=50.0, notional=50.0,
                  fill_source="chain", fraction=1.0, created_at=OPENED)
    m.set_position_derisked(c, pid, PARTIAL_AT)
    m.set_position_derisk_sig(c, pid, "PARTIAL_SIG")
    # The partial's own intent (the executor writes it before signing): 75% of a 50-token balance.
    intent = {"action": "derisk", "position_id": pid, "claim_id": "C1", "tx_identity": "PARTIAL_SIG",
              "fraction": 0.75, "balance_snapshot": 50.0}
    c.execute("INSERT INTO memecoin_exit_intents(claim_id, position_id, action, intent_json, status, "
              "created_at, updated_at) VALUES('C1', ?, 'derisk', ?, 'submitted_unknown', ?, ?)",
              (pid, json.dumps(intent), PARTIAL_AT, PARTIAL_AT))
    c.commit()
    return pid


def _ctx(c, exits, confirm):
    return {"exit_fn": lambda conn, pos, reason, now: exits.append(reason) or {"status": "closed"},
            "token_fn": lambda mint: 0.0,
            # an exact whole-balance quote at 0.40 per token: -60%, past the 50% stop
            "preview_exit_fn": lambda pos: {"expected_usdc": 0.40 * m.open_qty(c, pos["id"])},
            "confirm_once_fn": confirm,
            "derisk_fn": serialize_executor(execute_partial_exit, f"test-rail-settle-{os.getpid()}")}


def _sweep(c, contexts, settler, now_iso):
    rr.real_exit_sweep(c, contexts, price_fn=lambda mints: {"GRACEQ": {"price": 0.40}},
                       now_fn=lambda: now_iso, derisk_settler=settler)


@pytest.fixture(autouse=True)
def rails(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_RAIL_CLOSE_SETTLES_PARTIAL", True, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_UNQUOTABLE_EXIT_S", 86400.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 80.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 2)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SECONDS", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_INSTANT_GRACE_S", 0.0)


def _setup(state_fn):
    db, exits, reads = _Shared(), [], []

    def confirm(sig):
        reads.append(sig)
        return state_fn()
    contexts = {"solana": _ctx(db.c, exits, confirm)}
    clock = [100.0]
    settler = rr.RailDeriskSettler(contexts, connect=db.connect, now_fn=lambda: _at(1),
                                   clock=lambda: clock[0])
    return db.c, contexts, settler, exits, reads, clock


@pytest.mark.parametrize("state, sold", [("confirmed", True), ("failed", True)])
def test_a_stop_fires_on_the_next_tick_after_the_partial_settles(state, sold):
    c, contexts, settler, exits, reads, _clock = _setup(lambda: state)
    pid = _open_with_unknown_partial(c)
    _sweep(c, contexts, settler, _at(0))                   # -60%: the stop's price, but skipped
    assert exits == [] and reads == [] and settler.stats["offered"] == 1
    assert settler.run_once(timeout=0) == state
    _sweep(c, contexts, settler, _at(1))
    assert exits == ["b_stop"] and reads == ["PARTIAL_SIG"]
    assert m.get_position(c, pid)["derisk_sig"] is None


@pytest.mark.parametrize("state", ["not_found", "pending", "unavailable"])
def test_an_unknown_partial_is_never_cleared_and_the_rails_stay_off(state):
    c, contexts, settler, exits, _reads, _clock = _setup(lambda: state)
    pid = _open_with_unknown_partial(c)
    before = m.get_position(c, pid)
    _sweep(c, contexts, settler, _at(0))
    assert settler.run_once(timeout=0) == "unknown"
    assert m.get_position(c, pid) == before
    _sweep(c, contexts, settler, _at(1))
    assert exits == []


def test_the_tick_never_blocks_on_the_read():
    entered, release = threading.Event(), threading.Event()

    def blocked():
        entered.set()
        release.wait(5.0)
        return "pending"
    c, contexts, settler, exits, _reads, clock = _setup(blocked)
    _open_with_unknown_partial(c)
    settler.start()
    try:
        _sweep(c, contexts, settler, _at(0))
        assert entered.wait(2.0), "the settler thread never read"
        clock[0] += 3.0
        started = time.monotonic()
        _sweep(c, contexts, settler, _at(3))                  # the read is still blocked
        assert time.monotonic() - started < 0.5 and exits == []
        assert settler.stats["offered"] == 2
    finally:
        release.set()
        settler.stop()


def test_offers_are_one_per_two_seconds_per_position():
    c, contexts, settler, _exits, _reads, clock = _setup(lambda: "pending")
    pid = _open_with_unknown_partial(c)
    pos = m.get_position(c, pid)
    assert settler.offer(pos) is True
    clock[0] += 1.9
    assert settler.offer(pos) is False and settler.stats["throttled"] == 1
    clock[0] += 0.2
    assert settler.offer(pos) is True and settler.stats["offered"] == 2


def test_a_position_with_a_close_in_flight_is_never_offered():
    c, contexts, settler, _exits, _reads, _clock = _setup(lambda: "confirmed")
    pid = _open_with_unknown_partial(c)
    c.execute("UPDATE memecoin_positions SET close_claim_id='x' WHERE id=?", (pid,)); c.commit()
    _sweep(c, contexts, settler, _at(0))
    assert settler.stats["offered"] == 0


def test_the_knob_off_offers_nothing(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_RAIL_CLOSE_SETTLES_PARTIAL", False)
    c, contexts, settler, exits, reads, _clock = _setup(lambda: "confirmed")
    _open_with_unknown_partial(c)
    _sweep(c, contexts, settler, _at(0))
    assert settler.stats["offered"] == 0 and reads == [] and exits == []


def test_a_settle_error_is_counted_and_the_thread_keeps_serving():
    c, contexts, settler, _exits, _reads, clock = _setup(lambda: "confirmed")
    pid = _open_with_unknown_partial(c)
    settler.settle_fn = lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("locked"))
    settler.offer(m.get_position(c, pid))
    assert settler.run_once(timeout=0) is None and settler.stats["errors"] == 1
    settler.settle_fn = rr.settle_derisk_before_close
    clock[0] += 2.0
    settler.offer(m.get_position(c, pid))
    assert settler.run_once(timeout=0) == "confirmed"


def test_the_knob_defaults_on():
    import importlib
    assert importlib.reload(cfg).MEMECOIN_RAIL_CLOSE_SETTLES_PARTIAL is True \
        or "MEMECOIN_RAIL_CLOSE_SETTLES_PARTIAL" in os.environ


def test_every_runner_sweep_passes_the_settler():
    body = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    calls = body.split("real_exit_sweep(")[1:]
    assert len(calls) == 4
    for call in calls:
        assert "derisk_settler=_rail_settler" in call[:400], call[:200]
    assert "RailDeriskSettler(" in body and "_rail_settler.start()" in body


def test_the_settler_reports_in_the_heartbeat_with_counters_only(tmp_path, caplog):
    import json
    import logging
    from sniper.services import memecoin_runner as run
    c, contexts, settler, _exits, _reads, _clock = _setup(lambda: "confirmed")
    pid = _open_with_unknown_partial(c)
    settler.offer(m.get_position(c, pid))
    with caplog.at_level(logging.WARNING):
        assert settler.run_once(timeout=0) == "confirmed"
    assert any("close settle (rail): partial PARTIAL_SIG confirmed" in r.getMessage() for r in caplog.records)
    settler.stats["last_error"] = "https://hidden"
    path = tmp_path / "heartbeat.json"
    run.write_heartbeat({}, path=path, auth_ok=True, rail_derisk_settler=dict(settler.status(), url="x"))
    beat = json.loads(path.read_text())["rail_derisk_settler"]
    assert beat["offered"] == 1 and beat["confirmed"] == 1 and beat["alive"] is False and beat["queued"] == 0
    assert beat["last_error"] is None and "url" not in beat and "hidden" not in path.read_text()


def test_every_runner_heartbeat_carries_the_settler():
    body = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    assert body.count("rail_derisk_settler=(_rail_settler.status() if _rail_settler is not None else None)") == 2


def test_r4_a_full_queue_never_blocks_the_tick():
    """Gamma R4: sixteen queued offers and a stuck thread; the seventeenth returns at once, dropped."""
    c, contexts, settler, _exits, _reads, _clock = _setup(lambda: "pending")
    for pid in range(1, rr.RAIL_SETTLE_QUEUE + 1):
        assert settler.offer({"id": pid}) is True
    started = time.monotonic()
    assert settler.offer({"id": 999}) is False
    assert time.monotonic() - started < 0.05
    assert settler.stats["dropped"] == 1 and settler.stats["offered"] == rr.RAIL_SETTLE_QUEUE

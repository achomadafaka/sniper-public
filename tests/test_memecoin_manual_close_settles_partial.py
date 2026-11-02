"""A manual close behind an unknown-confirm partial (Alpha GO 2026-10-15, position 73882).

The operator's 75% partial LANDED but its confirm read 'unknown', so the derisk claim held the row;
the full close 8 s later was refused 'already_closing' and only the recovery pass settled the partial,
120 s later (MEMECOIN_DERISK_RECOVERY_MIN_AGE_S). Now the manual close does one confirm read of that
partial itself, no age gate, under the same execution lock as the recovery:

  confirmed -> book it unpriced (as the recovery does), then close the remainder in the same pass;
  failed    -> clear it, then close the whole balance;
  anything else -> change nothing, retry at +1 s for ~3 s, then refuse derisk_unconfirmed.

The manual path never clears on not_found and never counts a recheck hit: an unknown is never resold.
MEMECOIN_MANUAL_CLOSE_SETTLES_PARTIAL=0 is today's behaviour.
"""
import os
import sqlite3

import pytest

from sniper.execution.exec_lock import serialize_executor
from sniper.execution.solana_exit_executor import execute_partial_exit
from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_manual_exit_intents as xi
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

STAMP = "2026-10-15T13:18:24+00:00"
T0 = "2026-10-15T13:18:32+00:00"            # the operator's close, 8 s after the partial


def _at(sec):
    return "2026-10-15T13:18:%02d+00:00" % (32 + sec)


def _c():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c); return c


def _open_real(c, *, sig="PARTIAL_SIG", claimed=False):
    pid = m.open_position(c, source_user_id="u1", source_handle="probablyinsider", mint="HhCj9Mag",
                          ticker="HH", entry_price=0.0004, entry_notional=79.26,
                          entry_fill_source="chain", entry_lag_s=-1.0, their_size_usd=0.0,
                          opened_at="2026-10-15T13:10:00+00:00", strategy=cfg.MEMECOIN_REAL_POLICY,
                          execution_mode="real", network_id="solana")
    if claimed:
        m.claim_position_derisk(c, pid, STAMP)
    else:
        m.set_position_derisked(c, pid, STAMP)
    if sig:
        m.set_position_derisk_sig(c, pid, sig)
    return pid


def _pos(c, pid):
    return dict(c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone())


def _ctx(state, reads=None):
    def confirm(sig):
        if reads is not None:
            reads.append(sig)
        return state
    return {"solana": {"confirm_once_fn": confirm,
                       "derisk_fn": serialize_executor(execute_partial_exit, f"test-manual-settle-{os.getpid()}")}}


@pytest.fixture(autouse=True)
def knob(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MANUAL_CLOSE_SETTLES_PARTIAL", True, raising=False)


# --- the runner's settle step ---

def test_a_landed_partial_is_booked_unpriced_and_the_close_can_claim():
    c = _c(); pid = _open_real(c)
    assert m.claim_position_close(c, pid, T0, "manual") is None          # today's already_closing
    assert R.settle_derisk_for_manual_close(c, _ctx("confirmed"), _pos(c, pid), T0) == "confirmed"
    row = _pos(c, pid)
    assert row["derisk_sig"] is None and row["derisk_claim_id"] is None and row["derisked_at"] == STAMP
    fills = [dict(r) for r in c.execute("SELECT kind, price, notional, tx_sig FROM memecoin_fills "
                                        "WHERE position_id=? AND kind='derisk'", (pid,))]
    assert fills == [{"kind": "derisk", "price": None, "notional": None, "tx_sig": "PARTIAL_SIG"}]
    assert m.claim_position_close(c, pid, T0, "manual") is not None


def test_a_reverted_partial_is_cleared_so_the_close_sells_everything():
    c = _c(); pid = _open_real(c)
    assert R.settle_derisk_for_manual_close(c, _ctx("failed"), _pos(c, pid), T0) == "failed"
    row = _pos(c, pid)
    assert row["derisk_sig"] is None and row["derisked_at"] is None
    assert c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE kind='derisk'").fetchone()[0] == 0
    assert m.claim_position_close(c, pid, T0, "manual") is not None


@pytest.mark.parametrize("state", ["not_found", "pending", "unavailable", None])
def test_an_unknown_partial_is_never_cleared_or_counted(state):
    c = _c(); pid = _open_real(c)
    before = _pos(c, pid)
    assert R.settle_derisk_for_manual_close(c, _ctx(state), before, T0) == "unknown"
    after = _pos(c, pid)
    assert after == before                                  # no clear, no recheck hit, nothing
    assert m.claim_position_close(c, pid, T0, "manual") is None


def test_a_confirm_read_error_is_unknown():
    c = _c(); pid = _open_real(c)
    ctx = _ctx("confirmed")
    ctx["solana"]["confirm_once_fn"] = lambda sig: (_ for _ in ()).throw(TimeoutError("rpc"))
    assert R.settle_derisk_for_manual_close(c, ctx, _pos(c, pid), T0) == "unknown"
    assert _pos(c, pid)["derisk_sig"] == "PARTIAL_SIG"


def test_a_live_partial_executor_is_never_read_or_settled():
    c = _c(); pid = _open_real(c)
    reads = []
    ctx = _ctx("confirmed", reads)
    with ctx["solana"]["derisk_fn"].recovery_lock() as held:
        assert held
        assert R.settle_derisk_for_manual_close(c, ctx, _pos(c, pid), T0) == "unknown"
    assert reads == [] and _pos(c, pid)["derisk_sig"] == "PARTIAL_SIG"


def test_an_unsigned_claim_is_in_flight_and_never_read():
    c = _c(); pid = _open_real(c, sig=None, claimed=True)
    reads = []
    assert R.settle_derisk_for_manual_close(c, _ctx("confirmed", reads), _pos(c, pid), T0) == "unknown"
    assert reads == [] and _pos(c, pid)["derisk_claim_id"] is not None


def test_nothing_to_settle_or_the_knob_off_is_none(monkeypatch):
    c = _c()
    clean = m.open_position(c, source_user_id="u1", source_handle="h", mint="Clean", ticker="C",
                            entry_price=0.1, entry_notional=10.0, entry_fill_source="chain",
                            entry_lag_s=-1.0, their_size_usd=0.0, opened_at=T0,
                            strategy=cfg.MEMECOIN_REAL_POLICY, execution_mode="real", network_id="solana")
    reads = []
    assert R.settle_derisk_for_manual_close(c, _ctx("confirmed", reads), _pos(c, clean), T0) is None
    pid = _open_real(c)
    monkeypatch.setattr(cfg, "MEMECOIN_MANUAL_CLOSE_SETTLES_PARTIAL", False)
    assert R.settle_derisk_for_manual_close(c, _ctx("confirmed", reads), _pos(c, pid), T0) is None
    assert reads == [] and _pos(c, pid)["derisk_sig"] == "PARTIAL_SIG"


def test_the_knob_defaults_on():
    import importlib
    assert importlib.reload(cfg).MEMECOIN_MANUAL_CLOSE_SETTLES_PARTIAL is True \
        or "MEMECOIN_MANUAL_CLOSE_SETTLES_PARTIAL" in os.environ


# --- the drain ---

def _exit_fn(calls):
    """A full close the way the executors gate it: claim_position_close or already_closing."""
    def exit_fn(conn, pos, reason, now):
        calls.append(dict(pos))
        if m.claim_position_close(conn, pos["id"], now, reason) is None:
            return {"status": "already_closing"}
        return {"status": "closed", "sig": "CLOSE_SIG", "realized": None}
    return exit_fn


def _drain(c, ctx, calls, now):
    return xi.drain(c, _exit_fn(calls), lambda *a: {"status": "derisked"}, now_fn=lambda: now,
                    position_fn=m.get_position,
                    settle_fn=lambda conn, pos: R.settle_derisk_for_manual_close(conn, ctx, pos, now))


@pytest.mark.parametrize("action", ["close", "close_any_price"])
def test_a_manual_close_behind_a_landed_partial_closes_in_the_same_pass(action):
    c = _c(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action=action, now_iso=T0)
    calls = []
    assert _drain(c, _ctx("confirmed"), calls, T0) == [(iid, xi.DONE)]
    assert len(calls) == 1 and calls[0]["derisk_sig"] is None          # the re-read row, unblocked
    st = xi.read_status(c, iid)
    assert st["result_status"] == "closed" and st["sig"] == "CLOSE_SIG"


def test_an_unknown_partial_waits_three_seconds_then_refuses_without_selling():
    c = _c(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    calls, reads = [], []
    ctx = _ctx("not_found", reads)
    assert _drain(c, ctx, calls, T0) == [(iid, xi.QUEUED)]
    assert xi.read_status(c, iid)["not_before"] == _at(1)
    assert _drain(c, ctx, calls, _at(1)) == [(iid, xi.QUEUED)]
    assert _drain(c, ctx, calls, _at(2)) == [(iid, xi.QUEUED)]
    assert _drain(c, ctx, calls, _at(3)) == [(iid, xi.REFUSED)]
    st = xi.read_status(c, iid)
    assert st["result_status"] == "derisk_unconfirmed" and "PARTIAL_SIG" in st["reason"]
    assert calls == [] and len(reads) == 4 and _pos(c, pid)["derisk_sig"] == "PARTIAL_SIG"


def test_a_partial_that_lands_during_the_wait_closes_on_the_retry():
    c = _c(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    calls = []
    assert _drain(c, _ctx("pending"), calls, T0) == [(iid, xi.QUEUED)]
    assert _drain(c, _ctx("confirmed"), calls, _at(1)) == [(iid, xi.DONE)]
    assert len(calls) == 1 and xi.read_status(c, iid)["result_status"] == "closed"


def test_a_manual_partial_never_settles_the_previous_partial():
    c = _c(); pid = _open_real(c)
    xi.enqueue(c, position_id=pid, action="partial", fraction=0.5, now_iso=T0)
    reads = []
    _drain(c, _ctx("confirmed", reads), [], T0)
    assert reads == [] and _pos(c, pid)["derisk_sig"] == "PARTIAL_SIG"


def test_without_a_settle_step_the_close_is_refused_as_today():
    c = _c(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    calls = []
    xi.drain(c, _exit_fn(calls), lambda *a: None, now_fn=lambda: T0, position_fn=m.get_position)
    assert len(calls) == 1 and xi.read_status(c, iid)["result_status"] == "already_closing"


def test_the_runner_loop_passes_the_settle_step():
    import pathlib
    body = pathlib.Path("scripts/memecoin_runner_loop.py").read_text()
    call = body[body.index("_mei_done = _mei_drain("):body.index("for _iid, _istate in _mei_done:")]
    assert "settle_fn=" in call and "settle_derisk_for_manual_close" in call


def test_a_raising_settle_step_never_sells():
    """Gamma N6 on e6a48ba3d: a settle step that raises is an unknown, never a pass. Whatever the
    drain does with it, the close ends refused and the executor's claim gate holds the bag."""
    c = _c(); pid = _open_real(c)
    iid = xi.enqueue(c, position_id=pid, action="close", now_iso=T0)
    calls = []

    def boom(conn, pos):
        raise RuntimeError("settle exploded")
    for sec in (0, 1, 2, 3):
        xi.drain(c, _exit_fn(calls), lambda *a: None, now_fn=lambda s=sec: _at(s),
                 position_fn=m.get_position, settle_fn=boom)
    st = xi.read_status(c, iid)
    assert st["state"] == xi.REFUSED and st["result_status"] == "derisk_unconfirmed"
    assert calls == []                                   # the executor is never reached
    row = _pos(c, pid)
    assert row["is_open"] == 1 and row["close_claim_id"] is None and row["derisk_sig"] == "PARTIAL_SIG"

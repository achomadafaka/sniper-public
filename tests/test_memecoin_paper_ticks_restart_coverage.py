"""C02 (engineering review 2026-10-08): a restart must not erase persisted paper tick coverage.

live_s / blind_s / carried_s in paper_tick_json are LIFETIME totals: the coverage fraction that
decides whether a paper row is observed well enough depends on all of them. A new engine used to
start every position at zero and its first flush overwrote the stored totals. Now the first pass
restores them, and the time since the last accounted instant (`accounted_at`, written with every
flush) is BLIND: nothing observed the position while the engine was down. Rows from before this
change carry no accounted_at, so their downtime before the deploy stays uncounted (not invented).
"""
import json

from test_memecoin_paper_ticks import T0, _engine, _ledger, _open, _row, _tape


def _persist(c, pid, **fields):
    got = json.loads(_row(c, pid)["paper_tick_json"] or "{}")
    got.update(fields)
    c.execute("UPDATE memecoin_positions SET paper_tick_json=? WHERE id=?", (json.dumps(got), pid))
    c.commit()


def _cov(c, pid):
    got = json.loads(_row(c, pid)["paper_tick_json"])
    return got["live_s"], got["blind_s"], got["carried_s"], got.get("accounted_at")


def test_restored_totals_never_decrease_and_downtime_is_blind_even_with_a_fresh_tick(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    _persist(c, pid, live_s=120.0, blind_s=30.0, carried_s=10.0, accounted_at=T0 + 100.0)
    tape = _tape(tmp_path, received=T0 + 400.0, lag_observed=T0 + 400.0)
    eng, _ = _engine(c, tape, T0 + 400.0, iso="2026-10-08T07:06:40+00:00")
    eng.step()
    live, blind, carried, accounted = _cov(c, pid)
    assert live >= 120.0 and carried >= 10.0
    assert blind == 330.0                          # 30 stored + 300 s down; the fresh tick is not proof
    assert accounted == T0 + 400.0


def test_two_restarts_with_no_elapsed_time_add_nothing(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    _persist(c, pid, live_s=120.0, blind_s=30.0, carried_s=10.0, accounted_at=T0 + 100.0)
    for _ in range(2):
        eng, _ = _engine(c, None, T0 + 100.0)
        eng.step()
    assert _cov(c, pid)[:3] == (120.0, 30.0, 10.0)


def test_a_clock_reversal_subtracts_nothing(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    _persist(c, pid, live_s=120.0, blind_s=30.0, carried_s=10.0, accounted_at=T0 + 500.0)
    eng, _ = _engine(c, None, T0 + 400.0)
    eng.step()
    assert _cov(c, pid)[:3] == (120.0, 30.0, 10.0)


def test_malformed_or_absent_stored_totals_restore_as_zero_never_negative(tmp_path):
    c, m = _ledger()
    pid = _open(c, m)
    _persist(c, pid, live_s="abc", blind_s=-5.0, carried_s=float("nan"))
    c.execute("UPDATE memecoin_positions SET opened_at=NULL WHERE id=?", (pid,))
    eng, _ = _engine(c, None, T0 + 100.0)
    eng.step()
    live, blind, carried, accounted = _cov(c, pid)
    assert (live, blind, carried) == (0.0, 0.0, 0.0)     # no accounted_at: legacy downtime unknown
    assert accounted == T0 + 100.0


def test_a_new_position_counts_its_opening_delay_as_blind(tmp_path):
    """Gamma LOW: no accounted_at and no totals = never accounted, so the engine's first visit
    books the time since opened_at as blind (unbounded if the engine thread died alone)."""
    from datetime import datetime
    c, m = _ledger()
    pid = _open(c, m)
    opened = datetime.fromisoformat(_row(c, pid)["opened_at"]).timestamp()
    eng, _ = _engine(c, None, opened + 300.0)
    eng.step()
    assert _cov(c, pid)[1] == 300.0

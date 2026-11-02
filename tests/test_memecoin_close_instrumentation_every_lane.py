"""Every close records how far the position ran and when — not only the paper lanes.

Measured 2026-09-08 across 1041 funded positions (execution_mode='real'):

    entry_price / entry_notional / peak_price / closed_at / realized_pnl / exit_reason   100%
    time_to_peak_s                                                                       0%
    max_gain_pct                                                                         0%
    executable_peak_at                                                                   0%

The first two have a writer, `set_position_instrumentation`, and it is real — but its only callers
are the paper close in `memecoin_exits` and `_close_instrumentation` in the copy trader. The funded
runner calls neither, so the two fields that answer "how far did it run before it turned, and when"
were empty on every funded row we have. That is exactly what the exit-overshoot decomposition needs.

`executable_peak_at` was worse than empty. The runner stamped it only when a stall arm was
configured, while `exit_controller.executable_no_progress` returns early when it is None. Setting
MEMECOIN_NO_PROGRESS_WINDOW_S would have switched on a rail that could never fire, and nothing
would have said so.

The fix puts the derivation in `finalize_close`, which is the single statement every lane's close
runs through, rather than adding a call at each close site.
"""
import sqlite3

import pytest

import sniper.persistence.db_memecoin as m


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _open(c, *, entry_price=1.0, opened_at="2026-09-08T20:00:00+00:00", **kw):
    pid = m.open_position(c, source_user_id="u", source_handle="h", mint="M", ticker="T",
                          entry_price=entry_price, entry_notional=20.0, opened_at=opened_at,
                          strategy="s", entry_fill_source="test", entry_lag_s=0.0,
                          their_size_usd=100.0, **kw)
    return pid


def _row(c, pid):
    return c.execute("SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone()


def test_a_close_derives_time_to_peak_and_max_gain(conn):
    """THE REGRESSION: this is the funded path, which calls no instrumentation helper."""
    pid = _open(conn, entry_price=2.0)
    m.update_position_peak(conn, pid, 3.0, "2026-09-08T20:00:45+00:00", 0)
    m.claim_close(conn, pid)
    m.finalize_close(conn, pid, "2026-09-08T20:02:00+00:00", -1.0, exit_reason="b_stop")
    r = _row(conn, pid)
    assert r["time_to_peak_s"] == pytest.approx(45.0, abs=0.01)
    assert r["max_gain_pct"] == pytest.approx(50.0, abs=1e-6)


def test_an_explicit_instrumentation_write_is_not_recomputed(conn):
    """The paper lane writes these just before finalize_close. Its value must win."""
    pid = _open(conn, entry_price=2.0)
    m.update_position_peak(conn, pid, 3.0, "2026-09-08T20:00:45+00:00", 0)
    m.set_position_instrumentation(conn, pid, time_to_peak_s=11.0, max_gain_pct=99.0)
    m.claim_close(conn, pid)
    m.finalize_close(conn, pid, "2026-09-08T20:02:00+00:00", -1.0, exit_reason="b_stop")
    r = _row(conn, pid)
    assert (r["time_to_peak_s"], r["max_gain_pct"]) == (11.0, 99.0)


def test_a_zero_written_explicitly_survives(conn):
    """0.0 is a measurement (the peak was the entry), not a missing value. COALESCE keeps it."""
    pid = _open(conn, entry_price=2.0)
    m.set_position_instrumentation(conn, pid, time_to_peak_s=0.0, max_gain_pct=0.0)
    m.claim_close(conn, pid)
    m.finalize_close(conn, pid, "2026-09-08T20:02:00+00:00", 0.0, exit_reason="zero_qty_close")
    r = _row(conn, pid)
    assert (r["time_to_peak_s"], r["max_gain_pct"]) == (0.0, 0.0)


def test_missing_inputs_yield_null_not_a_fabricated_zero(conn):
    """A close with no peak recorded must stay UNKNOWN. A 0.0 here would read as 'never moved',
    which is a different claim from 'we did not observe it'."""
    pid = _open(conn, entry_price=0.0)
    conn.execute("UPDATE memecoin_positions SET peak_ts=NULL, peak_price=NULL WHERE id=?", (pid,))
    conn.commit()
    m.claim_close(conn, pid)
    m.finalize_close(conn, pid, "2026-09-08T20:02:00+00:00", 0.0, exit_reason="noquote")
    r = _row(conn, pid)
    assert r["time_to_peak_s"] is None and r["max_gain_pct"] is None
    assert r["closed_at"] == "2026-09-08T20:02:00+00:00"     # the close itself still lands


def test_a_peak_before_the_open_never_yields_a_negative_duration(conn):
    pid = _open(conn, entry_price=2.0, opened_at="2026-09-08T20:00:30+00:00")
    m.update_position_peak(conn, pid, 3.0, "2026-09-08T20:00:00+00:00", 0)
    m.claim_close(conn, pid)
    m.finalize_close(conn, pid, "2026-09-08T20:02:00+00:00", -1.0, exit_reason="b_stop")
    assert _row(conn, pid)["time_to_peak_s"] == 0.0


def test_a_loss_records_a_negative_max_gain(conn):
    """max_gain_pct is peak over entry. A position that never traded up must say so."""
    pid = _open(conn, entry_price=2.0)
    m.update_position_peak(conn, pid, 1.0, "2026-09-08T20:00:10+00:00", 0)
    m.claim_close(conn, pid)
    m.finalize_close(conn, pid, "2026-09-08T20:02:00+00:00", -10.0, exit_reason="b_stop")
    assert _row(conn, pid)["max_gain_pct"] == pytest.approx(-50.0)


def test_the_close_bookkeeping_itself_is_unchanged(conn):
    pid = _open(conn)
    m.claim_close(conn, pid)
    m.finalize_close(conn, pid, "2026-09-08T20:02:00+00:00", 4.25, exit_reason="real_tp",
                     recorded_at="2026-09-08T20:02:01+00:00")
    r = _row(conn, pid)
    assert (r["closed_at"], r["realized_pnl"], r["exit_reason"], r["closed_recorded_at"]) == \
        ("2026-09-08T20:02:00+00:00", 4.25, "real_tp", "2026-09-08T20:02:01+00:00")


# --- executable_peak_at ---------------------------------------------------------------------

def test_the_executable_peak_time_is_stamped_unconditionally(conn):
    pid = _open(conn)
    m.update_position_executable_peak(conn, pid, 1.5, peak_at="2026-09-08T20:00:30+00:00")
    r = _row(conn, pid)
    assert r["executable_peak_price"] == 1.5
    assert r["executable_peak_at"] == "2026-09-08T20:00:30+00:00"


def test_the_runner_no_longer_gates_the_stamp_on_a_stall_arm():
    """The whole defect in one assertion: the call site must not make peak_at conditional."""
    import pathlib
    src = pathlib.Path("sniper/services/memecoin_real_runner.py").read_text()
    i = src.index("m.update_position_executable_peak(")
    call = src[i:i + 240]
    assert "peak_at=now_iso" in call, call
    assert "if stall_rule is not None" not in call, call


def test_the_no_progress_rail_can_now_fire_when_the_knob_is_set():
    """It could not before: the rail returns early on a None peak time, and the peak time was
    only ever written for a stall arm — a different condition from the knob that reads it."""
    from sniper.analysis.exit_controller import ControllerState, executable_no_progress
    state = ControllerState(executable_peak_at="2026-09-08T20:00:00+00:00")
    assert executable_no_progress(state, now="2026-09-08T20:10:00+00:00",
                                  no_progress_window_s=300.0, quote_fresh=True) is True
    assert executable_no_progress(state, now="2026-09-08T20:01:00+00:00",
                                  no_progress_window_s=300.0, quote_fresh=True) is False
    # and with the knob unset (today's config) nothing fires, stamp or no stamp
    assert executable_no_progress(state, now="2026-09-08T20:10:00+00:00",
                                  no_progress_window_s=None, quote_fresh=True) is False

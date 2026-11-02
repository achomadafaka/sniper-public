"""PS tape WAL: a pinned WAL is named (holder pid), a free one is truncated before it counts toward the cap, and the
journal_size_limit shrinks the file after a reset. Reproduces the 2026-10-23 stall: one reader transaction pinned the WAL
to 16.5 GB and the worker reported 'restart required'."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_memecoin_desk_pumpswap_worker import W, worker

HOLDER = ("import sqlite3,sys,time;c=sqlite3.connect(sys.argv[1]);c.execute('BEGIN');"
          "c.execute('SELECT count(*) FROM pools').fetchone();print('ready',flush=True);time.sleep(60)")


def _fat_wal(w, rows=300):
    """Grow the WAL with committed writes (an open reader prevents the checkpoint that would reset it)."""
    c = w.files.conn("swaps_ps", time.time())
    c.execute("CREATE TABLE IF NOT EXISTS junk(id INTEGER PRIMARY KEY, x BLOB)")
    for _ in range(rows):
        c.execute("INSERT OR REPLACE INTO junk VALUES (1, randomblob(60000))")      # one live row, many WAL frames
        c.commit()
    return c


def _holder(path):
    p = subprocess.Popen([sys.executable, "-c", HOLDER, str(path)], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "ready"
    return p


@pytest.fixture
def w(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    w.cfg["ps_wal_try_gb"] = "0.00001"                                      # ~10 KB: any real WAL is "over"
    w.cfg["ps_hard_gb"] = "100"
    return w


def test_journal_size_limit_is_set_on_the_ps_tape(w):
    c = w.files.conn("swaps_ps", time.time())
    assert c.execute("PRAGMA journal_size_limit").fetchone()[0] == W.PS_WAL_LIMIT_BYTES


def test_a_free_wal_is_checkpointed_passive_and_a_pinned_one_is_named(w, tmp_path):
    alerts = []
    w.alert_fn = lambda kind, text: alerts.append((kind, text))
    _fat_wal(w)
    wal = tmp_path / "pumpswap" / "tape.db-wal"
    assert wal.stat().st_size > 1e6
    holder = _holder(tmp_path / "pumpswap" / "tape.db")
    try:
        _fat_wal(w, 50)
        size, pinners = w.ps_wal_relief(time.time())
        assert w.stats["ps_wal_pinned"] == 1 and size > 1e6 and not w.ps_wal_clean
        assert [p[0] for p in pinners] == [holder.pid]
        assert alerts and alerts[0][0] == "desk-universe-ps-wal" and str(holder.pid) in alerts[0][1]
        assert "not checkpointing" in alerts[0][1] and "restart" not in alerts[0][1]
        w.ps_wal_relief(time.time())
        assert len(alerts) == 1                                         # one page per 30 min, not one per check
    finally:
        holder.kill()
        holder.wait()
    size, pinners = w.ps_wal_relief(time.time())
    assert pinners == [] and w.stats["ps_wal_checkpointed"] == 1 and w.ps_wal_clean and size == 0       # PASSIVE: every frame flushed, nothing live left
    assert wal.stat().st_size > 1e6                                                                       # the file is NOT truncated (operator reclaims it): reusable space


def test_the_cap_stop_text_names_the_pin_instead_of_only_restart(w, tmp_path, monkeypatch):
    alerts = []
    w.alert_fn = lambda kind, text: alerts.append((kind, text))
    w.cfg["ps_hard_gb"] = "0.0001"
    monkeypatch.setattr(W.shutil, "disk_usage", lambda _: SimpleNamespace(free=500e9))
    _fat_wal(w)
    holder = _holder(tmp_path / "pumpswap" / "tape.db")
    try:
        _fat_wal(w, 50)
        w.check_disk()
    finally:
        holder.kill()
        holder.wait()
    stop = [a for a in alerts if a[0] == "desk-universe-ps-disk"]
    assert stop and f"pid {holder.pid}" in stop[0][1] and "WAL" in stop[0][1] and "stop that reader" in stop[0][1]
    assert not w.ps_capture and not w.stop                              # still a PS-only stop: physical cap holds


def test_no_attempt_below_the_threshold(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    w.cfg["ps_wal_try_gb"] = "5"
    _fat_wal(w, 20)
    w.ps_wal_relief(time.time())
    assert "ps_wal_checkpointed" not in w.stats and "ps_wal_pinned" not in w.stats


# ------------------------------------------------------------------------------------------------ auto-resume

def _spans(w):
    c = w.files.conn("ps_gap_spans", time.time())
    return c.execute("SELECT start_at, end_at, cause, ended_by FROM ps_gap_spans").fetchall()


def _gap_rows(w):
    return w.files.conn("gaps_ps", time.time()).execute("SELECT cause FROM gaps_ps").fetchall()


@pytest.fixture
def stopped(tmp_path, monkeypatch):
    """A worker whose PS capture just stopped at its cap because a reader pinned a fat WAL."""
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_EMISSIONS", "0")
    w = worker(tmp_path, monkeypatch)
    alerts = []
    w.alert_fn = lambda kind, text: alerts.append((kind, text))
    w.cfg.update(ps_wal_try_gb="0.00001", ps_hard_gb="0.02", ps_resume_hyst_gb="0.01", ps_resume_dwell_s="0",
                 min_free_gb="1")
    monkeypatch.setattr(W.shutil, "disk_usage", lambda _: SimpleNamespace(free=500e9))
    _fat_wal(w)
    holder = _holder(tmp_path / "pumpswap" / "tape.db")
    _fat_wal(w, 150)
    w.check_disk()
    assert not w.ps_capture and w.ps_stopped is not None, "setup: the pinned WAL must trip the cap"
    return w, holder, alerts, tmp_path


def test_a_pinned_wal_never_resumes_and_an_unpinned_one_resumes_with_exactly_one_gap_row(stopped):
    w, holder, alerts, _ = stopped
    curve_before = (list(w.ring), w.stop)
    for _ in range(3):
        w.check_disk()
    assert not w.ps_capture and w.stats["ps_capture_resumed"] == 0         # pin persists: no resume, ever
    assert not any(k == "desk-universe-ps-resume" for k, _t in alerts)
    holder.kill()
    holder.wait()
    w.check_disk()                                                          # truncate succeeds, live bytes fall under cap - hyst
    assert w.ps_capture and w.ps_stopped is None and w.stats["ps_capture_resumed"] == 1
    resume = [t for k, t in alerts if k == "desk-universe-ps-resume"]
    assert len(resume) == 1 and "resumed after" in resume[0]
    w.commit_files()
    assert _gap_rows(w) == [("disk_cap",)]                                  # ONE point row for the whole episode
    spans = _spans(w)
    assert len(spans) == 1 and spans[0][1] is not None and spans[0][1] >= spans[0][0] and spans[0][3] == "resume"
    w.check_disk()
    w.check_disk()
    assert w.stats["ps_capture_resumed"] == 1 and len(_spans(w)) == 1       # idempotent
    assert (list(w.ring), w.stop) == curve_before                           # the U curve producer is untouched


def test_dwell_and_hysteresis_hold_a_resume_back(stopped):
    w, holder, _alerts, _ = stopped
    holder.kill()
    holder.wait()
    w.cfg["ps_resume_dwell_s"] = "3600"
    w.check_disk()
    assert not w.ps_capture                                                 # min dwell not served
    w.cfg["ps_resume_dwell_s"] = "0"
    w.cfg["ps_resume_hyst_gb"] = "0.0199999"                               # must fall under ~0 GB: the tape's own pages are above it
    w.check_disk()
    assert not w.ps_capture
    w.cfg["ps_resume_hyst_gb"] = "0.01"
    w.check_disk()
    assert w.ps_capture


def test_resume_needs_the_free_disk_floor(stopped, monkeypatch):
    w, holder, _alerts, _ = stopped
    holder.kill()
    holder.wait()
    monkeypatch.setattr(W.shutil, "disk_usage", lambda _: SimpleNamespace(free=1.01e9))   # above min_free (1) but below min_free + cap (1.02)
    w.check_disk()
    assert not w.ps_capture and not w.stop


def test_flapping_is_blocked_after_the_hourly_resume_budget(stopped):
    w, holder, _alerts, _ = stopped
    holder.kill()
    holder.wait()
    w.cfg["ps_resume_max_per_h"] = "1"
    w.ps_resumes.append(time.time() - 60)                                   # one resume a minute ago already
    w.check_disk()
    assert not w.ps_capture and w.stats["ps_resume_flap_blocked"] == 1


def test_a_restart_during_a_disk_cap_stop_restores_the_stop_and_the_resume_goes_through_the_same_guards(stopped, tmp_path, monkeypatch):
    w, holder, _alerts, base = stopped
    holder.kill()
    holder.wait()
    open_spans = _spans(w)
    assert len(open_spans) == 1 and open_spans[0][1] is None                # stopped: the span is open
    w.files.commit()
    w2 = worker(base, monkeypatch)                                          # the restart: capture would come back from the env
    w2.cfg.update(ps_wal_try_gb="0.00001", ps_hard_gb="0.02", ps_resume_hyst_gb="0.01", ps_resume_dwell_s="3600", min_free_gb="1")
    assert not w2.ps_capture and w2.ps_stopped is not None and w2.ps_stopped["at"] == open_spans[0][0] and w2.stats["ps_stop_restored"] == 1   # Worker.__init__ restored it
    w2.close_open_ps_spans(time.time())
    assert _spans(w2)[0][1] is None and w2.ps_spans_closed                  # the stop is STILL OPEN: a restart is not a resume
    monkeypatch.setattr(W.shutil, "disk_usage", lambda _: SimpleNamespace(free=500e9))
    w2.check_disk()
    assert not w2.ps_capture                                                # the dwell (counted from the ORIGINAL stop) is not served
    w2.cfg["ps_resume_dwell_s"] = "0"
    w2.check_disk()
    assert w2.ps_capture and w2.ps_stopped is None and _spans(w2)[0][3] == "resume"
    w3 = worker(base, monkeypatch)                                          # a later restart finds a closed span and a recent resume
    assert w3.ps_capture and w3.ps_stopped is None and len(w3.ps_resumes) == 1       # the flap guard history survives the restart


def test_a_non_cap_open_span_is_closed_at_start_and_a_failed_close_is_retried(stopped, tmp_path, monkeypatch):
    w, holder, _alerts, base = stopped
    holder.kill()
    holder.wait()
    c = w.files.conn("ps_gap_spans", time.time())
    c.execute("INSERT INTO ps_gap_spans VALUES(5.0, NULL, 'stream', NULL)")
    c.commit()
    w2 = worker(base, monkeypatch)
    w2.restore_ps_stop(time.time())
    real = w2.files.conn
    monkeypatch.setattr(w2.files, "conn", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")))
    w2.close_open_ps_spans(time.time())
    assert not w2.ps_spans_closed and w2.stats["ps_span_errors"] >= 1       # not marked closed: it will be retried
    monkeypatch.setattr(w2.files, "conn", real)
    w2.close_open_ps_spans(time.time())
    assert w2.ps_spans_closed
    rows = {r[2]: r for r in _spans(w2)}
    assert rows["stream"][1] is not None and rows["stream"][3] == "restart" and rows["disk_cap"][1] is None


def test_span_writes_are_verified_queued_and_retried_and_a_resume_waits_for_a_durable_span_end(stopped, monkeypatch):
    w, holder, _alerts, _ = stopped
    holder.kill()
    holder.wait()
    w.ps_span_pending.clear()
    c = w.files.conn("ps_gap_spans", time.time())
    c.execute("DELETE FROM ps_gap_spans")                                   # the open row was never written (a failed insert): the close must record the span itself
    c.commit()
    real = w.files.conn
    monkeypatch.setattr(w.files, "conn", lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    w.resume_ps(time.time(), 0.0)
    assert not w.ps_capture and not w.ps_span_pending and w.stats["ps_span_errors"] >= 1      # resume deferred, the close is NOT left queued (the next resume queues it again)
    monkeypatch.setattr(w.files, "conn", real)
    w.check_disk()
    assert w.ps_capture and not w.ps_span_pending
    rows = _spans(w)
    assert len(rows) == 1 and rows[0][1] is not None and rows[0][3] == "resume"


def test_hysteresis_must_be_below_the_cap_and_the_defaults_are_compatible(tmp_path, monkeypatch):
    cfg = W.knobs({})
    assert 0 <= float(cfg["ps_resume_hyst_gb"]) < float(cfg["ps_hard_gb"])
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CAPTURE", "1")
    for bad in ("1", "2", "-0.1", "nan"):
        cfg = W.knobs({})
        cfg["ps_resume_hyst_gb"] = bad
        with pytest.raises(ValueError):
            W.Worker(cfg, base=tmp_path / bad, sol_price_fn=lambda: None, alert_fn=lambda *a: None)


def test_a_pin_blocks_the_resume_even_when_the_byte_count_alone_would_allow_it(stopped):
    w, holder, _alerts, _ = stopped
    try:
        w.cfg["ps_hard_gb"] = "5"                                           # bytes now far below cap - hysteresis
        for _ in range(3):
            w.check_disk()
        assert not w.ps_capture and w.stats["ps_capture_resumed"] == 0      # only the pin holds it back
    finally:
        holder.kill()
        holder.wait()
    w.check_disk()
    assert w.ps_capture


def test_the_routine_checkpoint_is_passive_and_never_truncates(w, tmp_path):
    seen = []
    c = _fat_wal(w, 40)
    c.set_trace_callback(lambda sql: seen.append(sql) if "wal_checkpoint" in sql else None)
    w.ps_wal_relief(time.time())
    assert seen and all("PASSIVE" in x for x in seen) and not any("TRUNCATE" in x or "RESTART" in x or "FULL" in x for x in seen)
    import inspect
    src = "".join(inspect.getsource(f) for f in (W.Worker.ps_wal_relief, W.Worker.maybe_resume_ps, W.Worker.check_disk))
    assert "wal_checkpoint(TRUNCATE)" not in src and "wal_checkpoint(RESTART)" not in src      # reclaiming the file is an operator action (the one-time open fold in DayFiles is main's, no other thread yet)


def _unreadable_restart(base, monkeypatch):
    """A restart whose ps_gap_spans read fails: the open disk_cap stop cannot be seen."""
    real = W.DayFiles.conn

    def conn(self, stream, ts):
        if stream in ("ps_gap_spans", "gaps_ps") and conn.broken:
            raise sqlite3.OperationalError("database is locked")
        return real(self, stream, ts)
    conn.broken = True
    monkeypatch.setattr(W.DayFiles, "conn", conn)
    return conn


def test_a_restore_read_error_keeps_capture_off_and_the_filter_subscribed_until_the_stop_is_read(stopped, monkeypatch):
    w, holder, _alerts, base = stopped
    holder.kill()
    holder.wait()
    w.files.commit()
    gate = _unreadable_restart(base, monkeypatch)
    w2 = worker(base, monkeypatch)
    assert not w2.ps_capture and w2.ps_restore_unread and w2.stats["ps_span_errors"] >= 1      # an unread stop is NOT "no stop"
    assert W.ps_filter_wanted(w2)                                                               # a later resume must still receive frames
    w2.close_open_ps_spans(time.time())
    assert not w2.ps_spans_closed                                                               # no span is closed while the stop is unread
    gate.broken = False
    monkeypatch.setattr(W.shutil, "disk_usage", lambda _: SimpleNamespace(free=500e9))
    w2.cfg.update(ps_hard_gb="0.02", ps_resume_dwell_s="3600", min_free_gb="1")
    w2.check_disk()                                                                             # the retry reads the OPEN disk_cap span
    assert not w2.ps_restore_unread and not w2.ps_capture and w2.ps_stopped is not None and w2.stats["ps_stop_restored"] == 1


def test_a_restore_that_finds_no_stop_after_a_read_error_resumes_capture_and_records_the_blackout(tmp_path, monkeypatch):
    w = worker(tmp_path, monkeypatch)
    w.files.conn("ps_gap_spans", time.time()).commit()
    w.files.commit()
    gate = _unreadable_restart(tmp_path, monkeypatch)
    w2 = worker(tmp_path, monkeypatch)
    assert not w2.ps_capture and w2.ps_restore_unread
    gate.broken = False
    monkeypatch.setattr(W.shutil, "disk_usage", lambda _: SimpleNamespace(free=500e9))
    w2.check_disk()
    assert w2.ps_capture and not w2.ps_restore_unread and w2.ps_stopped is None
    spans = [r for r in _spans(w2) if r[2] == "restore_unread"]
    assert len(spans) == 1 and spans[0][1] is not None and spans[0][3] == "restore_retry"      # the blackout is a durable gap span


def test_a_deferred_resume_does_not_leave_its_span_close_queued(stopped, monkeypatch):
    w, holder, _alerts, _base = stopped
    holder.kill()
    holder.wait()
    real = w._apply_span_op
    monkeypatch.setattr(w, "_apply_span_op", lambda op: False if op[0] == "close" else real(op))
    w.check_disk()                                                          # every guard holds, the span end cannot be made durable: the resume is deferred
    assert not w.ps_capture and w.ps_stopped is not None
    assert not [op for op in w.ps_span_pending if op[0] == "close"]         # nothing stays queued to close the stop later
    monkeypatch.setattr(w, "_apply_span_op", real)
    w.cfg["ps_resume_dwell_s"] = "3600"                                      # the guards no longer hold
    w.check_disk()
    assert not w.ps_capture and _spans(w)[0][1] is None                      # the stop span is still open: capture is still off


def test_a_stopped_capture_with_a_sub_threshold_wal_still_gets_its_wal_checkpointed_and_resumes(stopped):
    """RC#2 item 4. ps_wal_try_gb (default 2) can sit above the cap (default 1): a WAL under it was never checkpointed, kept counting toward the cap and
    blocked the resume for good once the reader that pinned it was gone."""
    w, holder, _alerts, _ = stopped
    holder.kill()
    holder.wait()
    w.cfg["ps_wal_try_gb"] = "2"                                            # far above this WAL and above the cap
    wal = w.base / "pumpswap" / "tape.db-wal"
    assert wal.exists() and wal.stat().st_size > 1e6
    w.check_disk()
    assert w.ps_capture and w.ps_stopped is None                             # the stopped check ran the PASSIVE checkpoint, the WAL stopped counting


def test_the_size_gate_sits_below_the_cap_so_a_wal_that_would_trip_it_is_checkpointed(w, tmp_path):
    _fat_wal(w, 20)
    holder = _holder(tmp_path / "pumpswap" / "tape.db")
    try:
        _fat_wal(w, 200)                                                     # a reader pins the WAL while it grows to ~12 MB
    finally:
        holder.kill()
        holder.wait()
    w.cfg.update(ps_wal_try_gb="2", ps_hard_gb="0.02")                       # try threshold far above the cap (the shipped defaults are 2 and 1)
    wal = tmp_path / "pumpswap" / "tape.db-wal"
    assert 10e6 < wal.stat().st_size < 2e9
    w.ps_wal_relief(time.time())
    assert w.ps_wal_clean and w.stats["ps_wal_checkpointed"] == 1


class _BusyCheckpoint:
    """A connection whose PASSIVE checkpoint could not run (another checkpoint or lock holder): SQLite answers (1, -1, -1)."""
    def __init__(self, real):
        self._real = real

    def execute(self, sql, *a):
        if "wal_checkpoint" in sql:
            return type("C", (), {"fetchone": staticmethod(lambda: (1, -1, -1))})()
        return self._real.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_a_checkpoint_that_did_not_run_is_not_a_clean_wal(w, tmp_path):
    """Delta: (busy=1, log=-1, ckpt=-1) means NOTHING was checkpointed; reading it as 'no frames pinned' would let a full WAL count as 0 and fail the cap open."""
    _fat_wal(w, 40)
    w.cfg["ps_wal_try_gb"] = "0.00001"
    w.files._open[("pumpswap", "all")] = _BusyCheckpoint(w.files._open[("pumpswap", "all")])
    wal, pinners = w.ps_wal_relief(time.time())
    assert not w.ps_wal_clean and wal > 0                                   # still counted toward the cap
    assert w.stats["ps_wal_checkpoint_busy"] == 1 and w.stats.get("ps_wal_checkpointed", 0) == 0


def test_a_resume_is_blocked_while_the_checkpoint_cannot_run(stopped):
    w, holder, _alerts, _ = stopped
    holder.kill()
    holder.wait()
    w.cfg.update(ps_wal_try_gb="2", ps_hard_gb="5", ps_resume_hyst_gb="1")      # the WAL is far below the try threshold and the cap: only the unknown checkpoint can block
    real = w.files._open[("pumpswap", "all")]
    w.files._open[("pumpswap", "all")] = _BusyCheckpoint(real)
    w.stats["ps_wal_pinned_last"] = 0                                        # the fixture's earlier real pin is gone: only the unknown checkpoint is left to block
    w.check_disk()
    assert not w.ps_capture and w.ps_stopped is not None                     # unknown WAL state: no resume
    w.files._open[("pumpswap", "all")] = real
    w.check_disk()
    assert w.ps_capture                                                      # the checkpoint runs again: resumes


class _RaisingCheckpoint(_BusyCheckpoint):
    def execute(self, sql, *a):
        if "wal_checkpoint" in sql:
            raise sqlite3.OperationalError("database is locked")
        return self._real.execute(sql, *a)


def test_a_resume_is_blocked_while_the_checkpoint_raises(stopped):
    """Zeta: the exception path left no 'unknown' flag, so below the try threshold the resume went through."""
    w, holder, _alerts, _ = stopped
    holder.kill()
    holder.wait()
    w.cfg.update(ps_wal_try_gb="2", ps_hard_gb="5", ps_resume_hyst_gb="1")
    real = w.files._open[("pumpswap", "all")]
    w.files._open[("pumpswap", "all")] = _RaisingCheckpoint(real)
    w.stats["ps_wal_pinned_last"] = 0
    w.check_disk()
    assert not w.ps_capture and w.ps_stopped is not None and not w.ps_wal_clean and w.stats["ps_wal_unknown_last"] == 1
    w.files._open[("pumpswap", "all")] = real
    w.check_disk()
    assert w.ps_capture


def test_a_resume_is_blocked_while_there_is_no_ps_connection_to_checkpoint(stopped):
    """Alpha: fail closed. 'Never happens' is the case that bites later."""
    w, holder, _alerts, _ = stopped
    holder.kill()
    holder.wait()
    w.cfg.update(ps_wal_try_gb="2", ps_hard_gb="5", ps_resume_hyst_gb="1")
    w.files.commit()
    w.files._open.pop(("pumpswap", "all")).close()                           # no connection: nothing can be checkpointed
    w.stats["ps_wal_pinned_last"] = 0
    w.check_disk()
    assert not w.ps_capture and w.ps_stopped is not None and not w.ps_wal_clean and w.stats["ps_wal_unknown_last"] == 1
    w.files.conn("swaps_ps", time.time())                                    # the connection is back
    w.check_disk()
    assert w.ps_capture


def test_the_passive_checkpoint_time_is_published_for_the_post_deploy_read(w):
    _fat_wal(w, 40)
    w.cfg["ps_wal_try_gb"] = "0.00001"
    w.ps_wal_relief(time.time())
    assert w.stats["ps_wal_checkpoint_ms_last"] >= 0 and w.stats["ps_wal_checkpoint_ms_max"] >= w.stats["ps_wal_checkpoint_ms_last"]
    assert "PASSIVE" in W.Worker.ps_wal_relief.__doc__ and "try a TRUNCATE" not in W.Worker.ps_wal_relief.__doc__      # the docstring describes the PASSIVE-only behaviour

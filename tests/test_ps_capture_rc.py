"""PS capture review items 2 and 6 (CORTEX-root on b2a4af569): readers see stop SPANS (incl. open ones), the census and pool-mint discovery are paged."""
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

from sniper.services import memecoin_desk_ps_tape as PT
from test_memecoin_desk_pumpswap_worker import W

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "research"))
spec = importlib.util.spec_from_file_location("census_hourly_rc", ROOT / "scripts/research/pumpswap_census_hourly.py")
CH = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CH)

H = 1_800_000_000 // 3600 * 3600


def make_tape(tmp_path, spans=True):
    files = W.DayFiles(tmp_path)
    c = files.conn("swaps_ps", H)
    if spans:
        files.conn("ps_gap_spans", H)               # a stop write creates the span table; a plain tape has none (test_desk_ps_capture_schema)
    return files, c


def add_swap(c, i, pool, t, status="unmapped", block_time=None):
    c.execute("INSERT INTO swaps_ps(pool, mint, quote_mint, slot, signature, ordinal, tx_index, is_buy, actor, block_time, received_at, map_status) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (pool, "m" + pool, PT.WSOL_MINT, i, "sig%d" % i, 0, 0, 1, "a", block_time or int(t), t, status))


def test_a_stop_span_overlapping_the_window_is_a_known_outage_even_when_no_point_gap_is_inside(tmp_path):
    files, c = make_tape(tmp_path)
    c.execute("INSERT INTO ps_gap_spans VALUES(100.0, 300.0, 'disk_cap', 'resume')")
    c.execute("INSERT INTO ps_gap_spans VALUES(1000.0, NULL, 'disk_cap', NULL)")             # still stopped: an OPEN span
    c.execute("INSERT INTO gaps_ps VALUES(5000.0, 'ws_gap', 1)")
    c.commit()
    assert PT.ps_gaps(tmp_path, 150.0, 250.0) == [150.0]                                       # stop [100,300] over [150,250]: an outage at the window start
    assert PT.ps_gaps(tmp_path, 250.0, 400.0) == [250.0, 300.0]                                # begins inside, ends inside: both edges
    assert PT.ps_gaps(tmp_path, 1500.0, 2000.0) == [1500.0]                                    # the open span runs through the whole window
    assert PT.ps_gaps(tmp_path, 4000.0, 6000.0) == [4000.0, 5000.0]                          # the open span (from 1000) still runs: window start + the point gap
    assert PT.ps_gaps(tmp_path, 400.0, 900.0) == []                                            # between the spans: clean
    assert PT.ps_gaps(tmp_path / "nowhere", 0.0, 1.0) == []                                    # no tape: no span claim


def test_the_census_row_lists_stop_spans_open_ones_included(tmp_path):
    files, c = make_tape(tmp_path)
    for i in range(30):
        add_swap(c, i, "P%d" % (i % 3), H + 100 + i * 100)                                      # 30 swaps over ~50 minutes, 3 pools
    add_swap(c, 99, "OUT", H - 10 * 3600)                                                       # an old swap outside every page
    c.execute("INSERT INTO ps_gap_spans VALUES(?, ?, 'disk_cap', 'resume')", (H + 500.0, H + 900.0))
    c.execute("INSERT INTO ps_gap_spans VALUES(?, NULL, 'disk_cap', NULL)", (H + 3000.0,))
    c.commit()
    seen = []
    c.set_trace_callback(lambda sql: seen.append(sql) if "FROM swaps_ps" in sql and "GROUP BY pool" in sql else None)
    facts = CH.hour_facts(c, H)
    assert facts["total_swaps"] == 30 and facts["distinct_pools"] == 3 and facts["swaps_per_pool_max"] == 10
    spans = [g for g in facts["gap_events"] if g.get("span")]
    assert [(g["start"], g["end"], g["open"]) for g in spans] == [(H + 500.0, H + 900.0, False), (H + 3000.0, None, True)]


def test_the_ps_filter_stays_subscribed_while_capture_is_armed_but_stopped(tmp_path, monkeypatch):
    from types import SimpleNamespace
    assert W.ps_filter_wanted(SimpleNamespace(census=False, ps_capture=False, ps_stopped={"at": 1.0}, ps_restore_unread=None)) is True
    assert W.ps_filter_wanted(SimpleNamespace(census=False, ps_capture=False, ps_stopped=None, ps_restore_unread=None)) is False
    assert W.ps_filter_wanted(SimpleNamespace(census=False, ps_capture=False, ps_stopped=None, ps_restore_unread=5.0)) is True       # an unread stop keeps the filter on
    assert W.ps_filter_wanted(SimpleNamespace(census=False, ps_capture=True, ps_stopped=None, ps_restore_unread=None)) is True


def test_the_older_two_column_span_table_is_read_not_refused(tmp_path):
    files, c = make_tape(tmp_path)
    c.execute("DROP TABLE ps_gap_spans")
    c.execute("CREATE TABLE ps_gap_spans(start_at REAL, end_at REAL)")                           # the shape main's fixtures and older tapes carry
    c.execute("INSERT INTO ps_gap_spans VALUES(100.0, 300.0)")
    c.commit()
    assert PT.ps_gap_spans(tmp_path, 150.0, 250.0) == [(100.0, 300.0, None, None)]
    assert PT.ps_gaps(tmp_path, 150.0, 250.0)[0] == 150.0
    spans = [g for g in CH.hour_facts(c, 0.0)["gap_events"] if g.get("span")]
    assert [(g["start"], g["end"]) for g in spans] == [(100.0, 300.0)]


def test_a_worker_upgrades_the_older_two_column_span_table_so_span_writes_land(tmp_path, monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker
    (tmp_path / "pumpswap").mkdir()
    c = sqlite3.connect(tmp_path / "pumpswap" / "tape.db")
    c.execute("CREATE TABLE ps_gap_spans(start_at REAL, end_at REAL)")
    c.execute("INSERT INTO ps_gap_spans VALUES(1.0, 2.0)")
    c.commit()
    c.close()
    w = worker(tmp_path, monkeypatch)
    w.write_ps_span(500.0)
    assert not w.ps_span_pending                                                             # the open row was written and verified
    rows = w.files.conn("ps_gap_spans", 500.0).execute("SELECT start_at, end_at, cause FROM ps_gap_spans ORDER BY start_at").fetchall()
    assert rows == [(1.0, 2.0, "legacy"), (500.0, None, "disk_cap")]


def test_a_plain_tape_restores_clean_and_never_gets_a_span_table_from_a_read(tmp_path, monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CAPTURE", "1")
    w = worker(tmp_path, monkeypatch)
    w.files.conn("swaps_ps", H).commit()
    w2 = worker(tmp_path, monkeypatch)                                                       # a restart on a tape that was never stopped
    w2.restore_ps_stop(H)
    w2.close_open_ps_spans(H)
    assert w2.ps_capture and w2.ps_restore_unread is None and w2.ps_stopped is None and w2.ps_spans_closed
    names = {r[0] for r in sqlite3.connect(tmp_path / "pumpswap" / "tape.db").execute("SELECT name FROM sqlite_master")}
    assert "ps_gap_spans" not in names


def test_every_nested_window_inside_a_stop_span_sees_the_outage(tmp_path):
    """RC#2 item 3. Nested windows [x, as_of] of ANY width overlap-test the stop spans (intervals), not sampled points."""
    from sniper.services.memecoin_desk_structure import gap_hit
    files, c = make_tape(tmp_path)
    c.execute("INSERT INTO ps_gap_spans VALUES(1000.0, NULL, 'disk_cap', NULL)")                 # an ongoing outage
    c.execute("INSERT INTO ps_gap_spans VALUES(100.0, 400.0, 'disk_cap', 'resume')")             # a closed one
    c.commit()
    gaps = PT.ps_point_gaps(tmp_path, 0.0, 5000.0) + PT.ps_gap_intervals(tmp_path, 0.0, 5000.0)
    assert gaps == [(100.0, 400.0), (1000.0, 5000.0)]
    for lo in range(1000, 4990, 3):                                                              # windows 1 s wide inside the ongoing outage, every 3 s
        assert gap_hit(gaps, lo, lo + 1), lo
    for lo in range(100, 399, 3):
        assert gap_hit(gaps, lo, lo + 1), lo
    assert not gap_hit(gaps, 401, 999)                                                           # between the outages: clean
    assert gap_hit(gaps, 399, 401) and gap_hit(gaps, 999, 1001)                                  # touching an edge counts
    assert not gap_hit([], 0, 1) and gap_hit([5.0, (7.0, 8.0)], 4, 6) and not gap_hit([5.0, (7.0, 8.0)], 5.5, 6.5)   # points and spans mix


def test_late_rows_inside_the_received_window_are_counted_and_the_window_is_stated(tmp_path):
    """RC#2 item 5. The old paged scan dropped a row stored late at a page edge. Main reads ONE bounded received_at window; a held-back write that lands up to
    RECEIVED_MARGIN_S after the hour still counts, one beyond it is outside the stated window (a lower bound, recorded on the row)."""
    files, c = make_tape(tmp_path)
    end = H + 3600
    add_swap(c, 1, "P0", H + 100)
    add_swap(c, 2, "P0", end - 5, block_time=int(end - 5))                                      # block time in the hour...
    c.execute("UPDATE swaps_ps SET received_at=? WHERE signature='sig2'", (end + 60.0,))         # ...stored a minute after the hour (held-back write)
    add_swap(c, 3, "P1", end - 10, block_time=int(end - 10))
    c.execute("UPDATE swaps_ps SET received_at=? WHERE signature='sig3'", (end + CH.C.RECEIVED_MARGIN_S - 1,))       # at the edge of the margin
    add_swap(c, 4, "P2", end - 20, block_time=int(end - 20))
    c.execute("UPDATE swaps_ps SET received_at=? WHERE signature='sig4'", (end + CH.C.RECEIVED_MARGIN_S + 5,))       # beyond it: outside the stated window
    c.commit()
    facts = CH.hour_facts(c, H)
    assert facts["total_swaps"] == 3 and facts["distinct_pools"] == 2                           # P2's late row is the stated lower bound, not a silent drop elsewhere
    assert CH._rwin(H, end) == (H - CH.C.RECEIVED_PRE_S, end + CH.C.RECEIVED_MARGIN_S)


def _structure_with_span(tmp_path, start, end, read_margin_s):
    import test_desk_ps_tape as D
    desk = D.curve(tmp_path)
    D.ps_tape(desk, D.ROWS)
    con = sqlite3.connect(desk / "pumpswap" / "tape.db")
    con.execute("CREATE TABLE ps_gap_spans(start_at REAL, end_at REAL, cause TEXT, ended_by TEXT, PRIMARY KEY(start_at, cause))")
    con.execute("INSERT INTO ps_gap_spans VALUES(?,?,?,?)", (D.NOW + start, None if end is None else D.NOW + end, "disk_cap", None))
    con.commit()
    con.close()
    return D.ST.structure_for(D.MINT, desk_dir=desk, as_of=D.NOW, read_margin_s=read_margin_s)


@pytest.mark.parametrize("margin", [0.0, 1.0, 2.0])
def test_an_ongoing_stop_blinds_the_shortest_window_through_the_real_structure_path(tmp_path, margin):
    """RC#2 item 3, Zeta's repro: the 15 s window is evaluated at as_of - read_margin while gaps are read through as_of; sampled points missed it."""
    out = _structure_with_span(tmp_path, -110.0, None, margin)
    assert out["windows"]["15s"] == {"state": "unknown", "reason": "tape_gap"}
    assert out["windows"]["60s"]["state"] == "unknown" and out["chart"] is None


def test_a_stop_that_began_after_the_evaluated_cut_or_ended_before_the_window_does_not_blind_it(tmp_path):
    began_after = _structure_with_span(tmp_path / "a", -0.5, None, 2.0)           # the window ends at NOW-2; the stop began at NOW-0.5
    assert began_after["windows"]["15s"].get("reason") != "tape_gap"
    ended_before = _structure_with_span(tmp_path / "b", -300.0, -200.0, 0.0)
    assert ended_before["windows"]["15s"].get("reason") != "tape_gap"


@pytest.mark.parametrize("ddl", ["CREATE TABLE ps_gap_spans(start_at REAL)", "CREATE TABLE ps_gap_spans(wrong REAL, other REAL)"])
def test_a_malformed_span_table_is_unknown_in_the_census_row_never_no_outage(tmp_path, ddl):
    files, c = make_tape(tmp_path, spans=False)
    c.execute(ddl)
    c.execute("INSERT INTO ps_gap_spans(%s) VALUES(?)" % ("start_at" if "start_at" in ddl else "wrong"), (H - 1.0,))
    c.commit()
    facts = CH.hour_facts(c, H)
    assert facts["gap_events"] == [] and facts["gap_spans_unreadable"]                           # empty events AND an explicit unknown flag


def test_a_missing_span_table_is_none_not_unknown_and_a_good_one_is_not_flagged(tmp_path):
    files, c = make_tape(tmp_path, spans=False)
    assert CH.hour_facts(c, H)["gap_spans_unreadable"] is None                                   # a tape from before spans existed: backward compatible
    files.conn("ps_gap_spans", H)
    c.execute("INSERT INTO ps_gap_spans VALUES(?, NULL, 'disk_cap', NULL)", (H + 10.0,))
    c.commit()
    facts = CH.hour_facts(c, H)
    assert facts["gap_spans_unreadable"] is None and [g for g in facts["gap_events"] if g.get("span")]


def test_the_span_reader_refuses_a_malformed_table_instead_of_reading_no_spans(tmp_path):
    files, c = make_tape(tmp_path, spans=False)
    c.execute("CREATE TABLE ps_gap_spans(start_at REAL)")
    c.execute("INSERT INTO ps_gap_spans VALUES(?)", (H - 1.0,))
    c.commit()
    with pytest.raises(PT.TapeUnreadable):
        PT.ps_gap_spans(tmp_path, H, H + 100)
    with pytest.raises(PT.TapeUnreadable):
        PT.ps_gaps(tmp_path, H, H + 100)


def _legacy_tape(tmp_path, rows):
    (tmp_path / "pumpswap").mkdir(exist_ok=True)
    c = sqlite3.connect(tmp_path / "pumpswap" / "tape.db")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE ps_gap_spans(start_at REAL, end_at REAL)")
    c.executemany("INSERT INTO ps_gap_spans VALUES(?,?)", rows)
    c.commit()
    c.close()


def test_startup_on_a_legacy_two_column_table_with_only_closed_spans_keeps_capture_on(tmp_path, monkeypatch):
    """Delta HOLD: the restore query named cause/ended_by before any upgrade ran, so a real legacy tape read as unreadable and capture stayed OFF."""
    from test_memecoin_desk_pumpswap_worker import worker
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CAPTURE", "1")
    _legacy_tape(tmp_path, [(1.0, 2.0)])
    w = worker(tmp_path, monkeypatch)                                                          # the real startup path, no span write first
    assert w.ps_capture and w.ps_restore_unread is None and w.ps_stopped is None
    w.close_open_ps_spans(H)
    assert w.ps_spans_closed
    rows = sqlite3.connect(tmp_path / "pumpswap" / "tape.db").execute("SELECT start_at, end_at FROM ps_gap_spans").fetchall()
    assert rows == [(1.0, 2.0)]                                                                # no span lost or rewritten


def test_startup_on_a_legacy_table_with_an_OPEN_span_of_unknown_cause_does_not_fail_open(tmp_path, monkeypatch):
    from test_memecoin_desk_pumpswap_worker import worker
    monkeypatch.setenv("MEMECOIN_DESK_PUMPSWAP_CAPTURE", "1")
    _legacy_tape(tmp_path, [(1.0, 2.0), (50.0, None)])
    w = worker(tmp_path, monkeypatch)
    assert not w.ps_capture and w.ps_stopped == {"at": 50.0, "cause": "legacy"} and w.ps_restore_unread is None
    w.close_open_ps_spans(H)
    rows = sqlite3.connect(tmp_path / "pumpswap" / "tape.db").execute("SELECT start_at, end_at, cause FROM ps_gap_spans ORDER BY start_at").fetchall()
    assert rows == [(1.0, 2.0, "legacy"), (50.0, None, "legacy")]                              # the unknown-cause stop is still OPEN: a restart is not a resume
    w.cfg.update(ps_resume_dwell_s="0", ps_hard_gb="100", ps_resume_hyst_gb="1", min_free_gb="0")
    w.resume_ps(H + 1000.0, 0.0)                                                               # a resume through the guards closes it
    assert w.ps_capture and w.ps_stopped is None
    rows = sqlite3.connect(tmp_path / "pumpswap" / "tape.db").execute("SELECT start_at, end_at, ended_by FROM ps_gap_spans WHERE start_at=50.0").fetchall()
    assert rows == [(50.0, H + 1000.0, "resume")]

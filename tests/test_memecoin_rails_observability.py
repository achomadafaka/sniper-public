"""A rail with no price must announce itself.

The detector this replaces counts sweeps that resolved nothing, so it is structurally blind to a
sweep that never ran — which on 2026-08-24 meant 87.1 minutes holding a funded position with an
active 35% stop and zero marks, worst window 926s on position 44624, and not one alert.
"""
from __future__ import annotations

import sqlite3

import pytest

from sniper.services import memecoin_rails_observability as ro

NOW = "2026-08-24T12:28:07+00:00"


def _conn(marks=()):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE memecoin_position_marks (mint TEXT, sweep_ts TEXT)")
    for mint, ts in marks:
        c.execute("INSERT INTO memecoin_position_marks VALUES (?,?)", (mint, ts))
    return c


@pytest.fixture(autouse=True)
def _clean():
    ro._ALERTED.clear()
    yield
    ro._ALERTED.clear()


def _pos(pid=1, mint="MINT"):
    return {"id": pid, "mint": mint}


# ------------------------------------------------------------------ age

def test_the_age_of_the_newest_mark_is_what_matters():
    c = _conn([("MINT", "2026-08-24T12:27:07+00:00"), ("MINT", "2026-08-24T12:20:00+00:00")])
    assert ro.newest_mark_age_s(c, mint="MINT", now_iso=NOW) == pytest.approx(60.0)


def test_a_coin_never_marked_is_unknown_not_zero():
    """None is a different fault from a fresh mark, and must not read as healthy."""
    assert ro.newest_mark_age_s(_conn(), mint="GHOST", now_iso=NOW) is None


# ------------------------------------------------------------------ THE 08-24 CASE

def test_position_44624_the_926_second_blind_window_fires():
    """THE REGRESSION CASE, built from the real incident. The sweep never ran, so nothing inside it
    could count; only a time check outside it sees this."""
    c = _conn([("PUMP44624", "2026-08-24T11:38:34+00:00")])
    now = "2026-08-24T11:54:00+00:00"                       # 926s later
    finds, worst, blind = ro.evaluate(
        c, now_iso=now, open_positions=[_pos(44624, "PUMP44624")],
        has_loss_rail_fn=lambda _p: True)
    assert blind == 1
    assert worst == pytest.approx(926.0, abs=1.0)
    assert finds[0]["position_id"] == 44624 and finds[0]["blind"] is True


def test_a_fresh_mark_is_not_blind():
    c = _conn([("MINT", "2026-08-24T12:27:57+00:00")])      # 10s old
    finds, worst, blind = ro.evaluate(c, now_iso=NOW, open_positions=[_pos()],
                                      has_loss_rail_fn=lambda _p: True)
    assert finds == [] and blind == 0 and worst == 0.0


def test_the_notice_band_is_reported_but_not_paged():
    c = _conn([("MINT", "2026-08-24T12:26:37+00:00")])      # 90s: past notice, under alert
    finds, _worst, blind = ro.evaluate(c, now_iso=NOW, open_positions=[_pos()],
                                       has_loss_rail_fn=lambda _p: True,
                                       notice_s=60.0, alert_s=120.0)
    assert len(finds) == 1 and finds[0]["blind"] is False and blind == 0


def test_a_position_with_no_loss_rail_is_not_paged():
    """A blind price misleads nothing when no rail reads it — paging is pure noise."""
    c = _conn([("MINT", "2026-08-24T11:00:00+00:00")])
    finds, _w, blind = ro.evaluate(c, now_iso=NOW, open_positions=[_pos()],
                                   has_loss_rail_fn=lambda _p: False)
    assert finds == [] and blind == 0


def test_a_flat_book_evaluates_nothing():
    finds, worst, blind = ro.evaluate(_conn(), now_iso=NOW, open_positions=[],
                                      has_loss_rail_fn=lambda _p: True)
    assert (finds, worst, blind) == ([], 0.0, 0)


def test_an_unmarked_coin_is_judged_by_the_position_age():
    """Otherwise every fresh entry pages during the seconds before its first mark lands."""
    c = _conn()
    finds, _w, blind = ro.evaluate(c, now_iso=NOW, open_positions=[_pos()],
                                   has_loss_rail_fn=lambda _p: True,
                                   opened_age_fn=lambda _p: 5.0)
    assert finds == [] and blind == 0
    finds, _w, blind = ro.evaluate(c, now_iso=NOW, open_positions=[_pos()],
                                   has_loss_rail_fn=lambda _p: True,
                                   opened_age_fn=lambda _p: 900.0)
    assert blind == 1


def test_runner_callback_reports_an_old_position_without_marks():
    import ast
    from pathlib import Path
    from sniper.services.memecoin_exits import _held_s

    tree = ast.parse((Path(__file__).parents[1] / "scripts/memecoin_runner_loop.py").read_text())
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "evaluate"
                and any(k.arg == "opened_age_fn" for k in n.keywords))
    callback = next(k.value for k in call.keywords if k.arg == "opened_age_fn")
    c = _conn()
    age_fn = eval(compile(ast.Expression(callback), "runner callback", "eval"),
                  {"_ro": ro, "conn": c, "_rd_now": NOW, "_held_s": _held_s})
    finds, age, blind = ro.evaluate(
        c, now_iso=NOW, open_positions=[{**_pos(), "opened_at": "2026-08-24T11:28:07+00:00"}],
        has_loss_rail_fn=lambda p: True, opened_age_fn=age_fn)
    assert blind == 1
    assert age == 3600


# ------------------------------------------------------------------ paging

def test_a_blind_position_pages_once_then_holds_its_tongue():
    """At a 120s pass an un-deduped alert fires every pass for the whole outage. That is how an
    alert gets muted, and then missed."""
    finds = [{"position_id": 1, "mint": "M", "age_s": 900.0, "blind": True}]
    sent = []
    assert ro.page_blind(finds, now_mono=0.0, alert_fn=sent.append, realert_s=300.0) == 1
    assert ro.page_blind(finds, now_mono=120.0, alert_fn=sent.append, realert_s=300.0) == 0
    assert ro.page_blind(finds, now_mono=240.0, alert_fn=sent.append, realert_s=300.0) == 0
    assert len(sent) == 1


def test_it_re_pages_once_the_cadence_elapses():
    finds = [{"position_id": 1, "mint": "M", "age_s": 900.0, "blind": True}]
    sent = []
    ro.page_blind(finds, now_mono=0.0, alert_fn=sent.append, realert_s=300.0)
    assert ro.page_blind(finds, now_mono=301.0, alert_fn=sent.append, realert_s=300.0) == 1
    assert len(sent) == 2


def test_recovery_clears_the_state_so_the_next_window_pages_immediately():
    finds = [{"position_id": 1, "mint": "M", "age_s": 900.0, "blind": True}]
    sent = []
    ro.page_blind(finds, now_mono=0.0, alert_fn=sent.append)
    ro.page_blind([], now_mono=10.0, alert_fn=sent.append)          # recovered
    assert ro.page_blind(finds, now_mono=20.0, alert_fn=sent.append) == 1
    assert len(sent) == 2


def test_an_alert_failure_cannot_raise_into_the_loop():
    """Observation doctrine: the instrument must never break the money path it watches."""
    def boom(_text):
        raise RuntimeError("telegram down")
    finds = [{"position_id": 1, "mint": "M", "age_s": 900.0, "blind": True}]
    assert ro.page_blind(finds, now_mono=0.0, alert_fn=boom) == 1     # counted, did not raise


def test_evaluate_never_raises_on_a_broken_position():
    class Broken(dict):
        def get(self, *_a, **_k):
            raise sqlite3.OperationalError("boom")
    finds, worst, blind = ro.evaluate(_conn(), now_iso=NOW, open_positions=[Broken()],
                                      has_loss_rail_fn=lambda _p: True)
    assert (finds, worst, blind) == ([], 0.0, 0)


# ------------------------------------------------------------------ the heartbeat whitelist

def test_the_snapshot_carries_the_numbers_an_operator_needs():
    snap = ro.snapshot(926.4, 1, 3)
    assert snap == {"worst_blind_age_s": 926.4, "blind_positions": 1, "checked_positions": 3}


def test_the_field_survives_to_the_written_heartbeat_file(tmp_path):
    """THE WHITELIST LESSON. The RPC pool's `degraded` was added to a snapshot on 2026-08-25 and
    dropped one layer up, so the operator read a number that could not change."""
    from sniper.services import memecoin_runner as run
    path = str(tmp_path / "hb.json")
    run.write_heartbeat({"events": 1, "opened": 0}, auth_ok=True, path=path,
                        rails_observability=ro.snapshot(926.4, 1, 3))
    got = run.read_heartbeat(path)
    assert got["rails_observability"]["worst_blind_age_s"] == 926.4
    assert got["rails_observability"]["blind_positions"] == 1


# ------------------------------------------------------------------ 2026-09-11: exact quotes count

def test_a_quiet_coin_with_fresh_exact_quotes_is_not_deaf():
    """QQQ 55669 (09-11): no swap -> no mark for 68 s, but the stop read an exact whole-bag quote
    every ~1.3 s. That position had a price; it must not page RAILS DEAF."""
    c = _conn([("QQQ", "2026-08-24T12:26:59+00:00")])       # newest mark 68 s old
    pos = dict(_pos(55669, "QQQ"), last_executable_at="2026-08-24T12:28:06+00:00")
    finds, worst, blind = ro.evaluate(c, now_iso=NOW, open_positions=[pos],
                                      has_loss_rail_fn=lambda _p: True, notice_s=15, alert_s=45)
    assert blind == 0 and finds == []


def test_stale_marks_and_a_stale_exact_quote_still_page():
    c = _conn([("QQQ", "2026-08-24T12:26:59+00:00")])
    pos = dict(_pos(55669, "QQQ"), last_executable_at="2026-08-24T12:27:00+00:00")   # 67 s old
    finds, worst, blind = ro.evaluate(c, now_iso=NOW, open_positions=[pos],
                                      has_loss_rail_fn=lambda _p: True, notice_s=15, alert_s=45)
    assert blind == 1 and worst == pytest.approx(67.0, abs=1.0)

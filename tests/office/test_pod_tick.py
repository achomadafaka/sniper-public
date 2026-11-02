"""Task 15 hook: the daily pods reallocation tick, exercised on a real ledger through the same fixtures as pod_floor."""
import logging

import pytest

from sniper.office import ledger as L, pod_floor as F, pod_tick as T
from .test_pod_floor import FX, NOW, RULE, book, close, conn, scored  # noqa: F401  (conn is a fixture)

FXFN = lambda: (FX, NOW - 5)


def tick(conn, epochs=("E1",), clock=lambda: NOW, fx=FXFN, **kw):
    return T.tick(conn, RULE, epoch_ids=epochs, clock=clock, fx=fx, **kw)


def seeded(conn):
    book(conn)
    scored(conn, {"pod1": 40.0, "pod2": -10.0, "pod3": 0.0})


def transfers(conn):
    return conn.execute("SELECT COUNT(*) FROM capital_flows WHERE kind LIKE 'transfer%'").fetchone()[0]


def test_one_booking_per_utc_day_and_every_later_call_is_a_replay(conn):
    seeded(conn)
    first = tick(conn)
    assert first["E1"][0] == "booked" and transfers(conn) == 2
    again = tick(conn, clock=lambda: NOW + 600)
    assert again["E1"][0] == "replayed" and transfers(conn) == 2


def test_unknown_clock_or_fx_books_nothing_and_says_why(conn):
    seeded(conn)
    assert tick(conn, clock=lambda: None) == {"skipped": "clock_unknown"}
    assert tick(conn, clock=lambda: float("nan")) == {"skipped": "clock_unknown"}
    assert tick(conn, fx=lambda: (None, None)) == {"E1": ["none"]}              # FX unknown: nothing planned, nothing guessed
    assert transfers(conn) == 0


def test_only_the_epochs_the_caller_names_are_touched(conn):
    seeded(conn)
    assert tick(conn, epochs=()) == {} and transfers(conn) == 0                  # a broken or non-evaluated epoch is simply not passed in
    assert tick(conn, epochs=("E9",)) == {"E9": ["error:rule_version_not_epoch_rule"]}


def test_one_epoch_failing_does_not_stop_the_others_and_is_logged(conn, caplog):
    seeded(conn)
    with caplog.at_level(logging.ERROR):
        out = tick(conn, epochs=("E9", "E1"))
    assert out["E9"][0].startswith("error:") and out["E1"][0] == "booked"
    assert any("pods tick failed" in r.message and "E9" in r.message for r in caplog.records)


def test_a_discovery_epoch_is_never_reallocated(conn):
    book(conn, kind="discovery")
    scored(conn, {"pod1": 40.0, "pod2": -10.0, "pod3": 0.0})
    assert tick(conn) == {"E1": ["none"]} and transfers(conn) == 0


def _boom(*_a):
    raise TimeoutError("shared sample unreadable")


def test_a_raising_or_malformed_dependency_never_reaches_the_floor_and_books_nothing(conn):
    seeded(conn)
    assert tick(conn, clock=_boom) == {"skipped": "clock_unknown"}
    assert tick(conn, fx=_boom) == {"skipped": "fx_unknown"}
    for bad in (None, 5, (FX,), (FX, NOW, 1), "ab", object()):
        assert tick(conn, fx=lambda bad=bad: bad) == {"skipped": "fx_unknown"}
    assert transfers(conn) == 0

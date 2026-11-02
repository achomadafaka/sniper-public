"""Desk pre-quote while JUDGE thinks (Alpha GO 2026-10-23, option A)."""
import sqlite3
import threading

from sniper.persistence import db_memecoin_desk as store
from sniper.services import memecoin_desk_prequote as Q


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def book(quote_fn, *, max_hints=2, clock=None, wall=None):
    clock = clock or Clock()
    return Q.PrequoteBook(quote_fn=quote_fn, clip_usd=25.0, max_hints=max_hints, max_age_s=1.5,
                          clock=clock, wall=wall or (lambda: 1000.0)), clock


def test_hint_table_round_trip_and_expiry():
    c = sqlite3.connect(":memory:")
    store.ensure_tables(c)
    store.write_prequote_hint(c, ticket_id="t1", network_id="solana", mint="M1", created_at=10, expires_at=20)
    assert store.live_prequote_hints(c, now=15, limit=5) == [("t1", "solana", "M1", 20.0)]
    store.write_prequote_hint(c, ticket_id="t2", network_id="solana", mint="M2", created_at=30, expires_at=40)
    assert c.execute("SELECT COUNT(*) FROM desk_prequote_hints").fetchone()[0] == 1     # t1 pruned on write
    assert [h[0] for h in store.live_prequote_hints(c, now=35, limit=5)] == ["t2"]


def test_fresh_quote_is_taken_only_for_same_ticket_mint_and_clip():
    calls = []
    b, clock = book(lambda mint, clip: calls.append((mint, clip)) or {"outAmount": "7", "mint": mint})
    b.sync([("t1", "solana", "M1", 2000.0)])
    b.refresh()
    held = b.for_ticket("t1", "M1")
    assert held.take(25.0) == {"outAmount": "7", "mint": "M1"} and calls == [("M1", 25.0)]
    assert held.take(18.75) is None                          # another clip quotes fresh
    assert b.for_ticket("t1", "OTHER") is None and b.for_ticket("t9", "M1") is None
    clock.t += 1.6
    assert held.take(25.0) is None                           # older than the early-quote max age


def test_refresh_requotes_aging_quotes_and_bounds_the_held_set():
    n = []
    b, clock = book(lambda mint, clip: n.append(mint) or {"outAmount": "1"}, max_hints=2)
    b.sync([("a", "solana", "A", 2000.0), ("b", "solana", "B", 2000.0), ("c", "solana", "C", 2000.0),
            ("d", "evm", "D", 2000.0)])
    b.refresh()
    assert sorted(n) == ["A", "B"]                           # at most max_hints, never more
    clock.t += 0.5
    b.refresh()
    assert len(n) == 2                                       # still fresh: no new call
    clock.t += 0.6                                           # 1.1 s > 0.7 x 1.5 s
    b.refresh()
    assert len(n) == 4


def test_expired_hints_are_dropped_and_errors_never_raise():
    b, _ = book(lambda mint, clip: (_ for _ in ()).throw(TimeoutError("jup")))
    b.sync([("t1", "solana", "M1", 999.0)])                  # already expired (wall 1000)
    b.refresh()
    assert b.for_ticket("t1", "M1") is None
    b.sync([("t2", "solana", "M2", 2000.0)])
    b.refresh()
    assert b.stats["quote_errors"] == 1 and b.for_ticket("t2", "M2") is None


def test_no_route_holds_none_so_the_executor_quotes_and_the_rung_still_runs():
    b, _ = book(lambda mint, clip: None)
    b.sync([("t1", "solana", "M1", 2000.0)])
    b.refresh()
    assert b.for_ticket("t1", "M1") is None


def test_run_forever_survives_a_failing_hint_read():
    b, _ = book(lambda mint, clip: {"outAmount": "1"})
    stop, seen = threading.Event(), []

    def hints():
        seen.append(1)
        if len(seen) == 1:
            raise sqlite3.OperationalError("no such table")
        stop.set()
        return [("t1", "solana", "M1", 2000.0)]
    Q.run_forever(b, hints, stop, poll_s=0)
    assert len(seen) == 2 and b.for_ticket("t1", "M1") is not None


def test_held_for_without_a_book_or_ticket_is_none():
    Q.install(None)
    assert Q.held_for("t1", "M1") is None
    b, _ = book(lambda mint, clip: {"outAmount": "1"})
    Q.install(b)
    assert Q.held_for(None, "M1") is None
    Q.install(None)


def test_the_leg_hands_a_desk_entry_its_held_quote_and_nothing_else():
    from sniper.services.memecoin_real_leg import desk_prequote
    b, _ = book(lambda mint, clip: {"outAmount": "1"})
    b.sync([("desk:U:1", "solana", "M1", 2000.0)])
    b.refresh()
    Q.install(b)
    try:
        ex = lambda conn, *, early_quote=None: None   # noqa: E731
        desk_ev = type("E", (), {"desk": {"ticket_id": "desk:U:1"}, "mint": "M1"})()
        assert desk_prequote(desk_ev, ex).take(25.0) == {"outAmount": "1"}
        assert desk_prequote(type("E", (), {"mint": "M1"})(), ex) is None             # copy / manual
        assert desk_prequote(desk_ev, lambda conn: None) is None                        # executor without the kwarg
        other = type("E", (), {"desk": {"ticket_id": "desk:U:1"}, "mint": "M2"})()
        assert desk_prequote(other, ex) is None                                         # mint mismatch
    finally:
        Q.install(None)


def test_a_failing_quote_is_retried_at_most_once_per_backoff_not_every_poll():
    n = []

    def boom(mint, clip):
        n.append(1)
        raise TimeoutError("jupiter 429")
    b, clock = book(boom)
    b.sync([("t1", "solana", "M1", 2000.0)])
    for _ in range(12):                                     # 3 s of 0.25 s polls
        b.refresh()
        clock.t += 0.25
    assert len(n) == 1 and b.for_ticket("t1", "M1") is None
    clock.t += Q.ERROR_BACKOFF_S
    b.refresh()
    assert len(n) == 2


def test_a_returned_call_leaves_no_live_hint():
    c = sqlite3.connect(":memory:")
    store.ensure_tables(c)
    store.write_prequote_hint(c, ticket_id="t1", network_id="solana", mint="M1", created_at=10, expires_at=99)
    store.clear_prequote_hint(c, "t1")
    assert store.live_prequote_hints(c, now=11, limit=5) == []


def test_pass_errors_are_logged_once_per_distinct_error(caplog):
    b, _ = book(lambda mint, clip: {"outAmount": "1"})
    stop, n = threading.Event(), []

    def hints():
        n.append(1)
        if len(n) >= 6:
            stop.set()
        raise sqlite3.OperationalError("no such table: desk_prequote_hints")
    with caplog.at_level("ERROR"):
        Q.run_forever(b, hints, stop, poll_s=0, clock=lambda: 0.0)
    assert b.stats["pass_errors"] == 6
    assert sum("desk prequote pass failed" in r.getMessage() for r in caplog.records) == 1


def test_a_held_quote_without_a_trace_goes_through_the_executor_s_early_quote_path():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("eq_t", Path(__file__).parent / "test_memecoin_entry_early_quote.py")
    T = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(T)
    clock = Clock()
    held = Q.HeldQuote("MINT", 12.0, dict(T.QUOTE), clock(), None, 1.5, clock)
    assert held.trace is None
    result, fresh = T._execute(held)
    assert result["status"] == "filled", result
    assert fresh == [], "a fresh held quote was not used"


def test_the_held_quote_s_completion_time_survives_the_ordinary_consumer():
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("eq_t2", Path(__file__).parent / "test_memecoin_entry_early_quote.py")
    T = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(T)
    stamp = "2026-07-15T12:00:01.250000+00:00"                # before the executor's now (12:00:02)
    b = Q.PrequoteBook(quote_fn=lambda mint, clip: dict(T.QUOTE), clip_usd=12.0, max_hints=1, max_age_s=1.5,
                       wall=lambda: 1000.0, observed_fn=lambda: stamp)
    b.sync([("t1", "solana", "MINT", 2000.0)])
    b.refresh()
    held = b.for_ticket("t1", "MINT")
    assert held.observed_at == stamp
    result, fresh = T._execute(held)
    assert result["status"] == "filled" and fresh == []
    assert result.get("quote_observed_at") == stamp, result.get("quote_observed_at")


def _cleared(age_s, n_tickets=1, max_hints=2):
    calls = []
    b, clock = book(lambda mint, clip: calls.append(mint) or {"outAmount": "1"}, max_hints=max_hints)
    hints = [("t%d" % i, "solana", "M%d" % i, 2000.0) for i in range(n_tickets)]
    b.sync(hints)
    b.refresh()
    b.sync([])                                   # the JUDGE call returned: hint cleared
    clock.t += age_s
    b.refresh()
    b.sync([])
    return b, calls


def test_a_cleared_hint_s_fresh_quote_is_still_taken_and_never_requoted():
    b, calls = _cleared(0.5)
    assert b.for_ticket("t0", "M0").take(25.0) == {"outAmount": "1"}
    assert calls == ["M0"]                       # cleared: no further quote


def test_a_cleared_hint_s_quote_past_max_age_is_not_taken_and_dropped():
    b, _ = _cleared(1.6)
    assert b.for_ticket("t0", "M0") is None and b._retained == {}


def test_retained_quotes_stay_bounded_by_max_hints():
    b, _ = _cleared(0.1, n_tickets=2, max_hints=2)
    b2, clock = book(lambda mint, clip: {"outAmount": "1"}, max_hints=2)
    for i in range(5):
        b2.sync([("x%d" % i, "solana", "N%d" % i, 2000.0)])
        b2.refresh()
        clock.t += 0.1
    b2.sync([])
    assert len(b2._retained) <= 2 and len(b._retained) <= 2

"""A revival watch is a delayed re-ask with no authority of its own.

The danger it must never become: a retry loop that eventually talks a gate into a bad entry. Every
test here pins a way it could acquire authority it should not have.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from sniper.services import memecoin_revival_watch as rw

LOOP = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "memecoin_runner_loop.py"
FLOOR = 0.10


@pytest.fixture(autouse=True)
def _clean():
    rw.WATCHES.clear()
    rw._SPENT.clear()
    yield
    rw.WATCHES.clear()
    rw._SPENT.clear()


class _Ev:
    def __init__(self, mint="MINT", eid="evt-1"):
        self.mint = mint
        self.fomo_event_id = eid


def _enq(watches, *, mint="MINT", eid="evt-1", now=0.0, **kw):
    return rw.enqueue(watches, event=_Ev(mint, eid), mint=mint, origin_event_id=eid,
                      refused_at="2026-08-26T12:00:00+00:00", mcap=50_000.0, needed=5000.0,
                      now=now, **kw)


# ------------------------------------------------------------------ what may be watched

def test_only_the_m5_floor_refusal_enqueues():
    """Every other refusal meant it. Re-asking a chase or premium gate is a retry loop."""
    assert rw.should_watch("m5_turnover_under_floor", mcap=50_000, max_mcap=100_000) is True
    for other in ("m5_turnover_over_ceiling", "turnover_under_floor", "skipped_chase",
                  "insufficient bankroll", "one position per coin", ""):
        assert rw.should_watch(other, mcap=50_000, max_mcap=100_000) is False, other


def test_the_scope_knob_bounds_by_mcap():
    assert rw.should_watch(rw.FLOOR_REASON, mcap=99_000, max_mcap=100_000) is True
    assert rw.should_watch(rw.FLOOR_REASON, mcap=150_000, max_mcap=100_000) is False
    assert rw.should_watch(rw.FLOOR_REASON, mcap=150_000, max_mcap=0) is True     # 0 = no scope


def test_an_unpriced_coin_is_out_of_scope_rather_than_admitted():
    assert rw.should_watch(rw.FLOOR_REASON, mcap=None, max_mcap=100_000) is False


# ------------------------------------------------------------------ the queue

def test_the_same_refusal_does_not_stack():
    w = {}
    assert _enq(w)[0] is True
    assert _enq(w)[0] is False
    assert len(w) == 1


def test_a_refusal_earns_exactly_ONE_re_ask_ever():
    """THE LOOP GUARD. A watch is removed before re-injection, so without a spent-key memory a
    re-injection that hits the floor again would arm a second watch, forever."""
    w = {}
    assert _enq(w, now=0.0)[0] is True
    w.clear()                                   # the watch cleared and re-injected
    assert _enq(w, now=1.0)[0] is False, "the same (mint, event) must never be re-watched"
    assert w == {}


def test_the_spent_memory_expires_so_it_cannot_grow_forever():
    w = {}
    _enq(w, now=0.0)
    w.clear()
    assert _enq(w, now=rw._SPENT_TTL_S + 10.0)[0] is True


def test_at_capacity_the_oldest_expiring_watch_is_evicted():
    w = {}
    for i in range(4):
        _enq(w, mint=f"M{i}", eid=f"e{i}", now=float(i), window_s=30.0, max_watches=4)
    ok, evicted = _enq(w, mint="M9", eid="e9", now=10.0, window_s=30.0, max_watches=4)
    assert ok is True
    assert evicted == ("M0", "e0"), "the least window left is the least useful"
    assert len(w) == 4


# ------------------------------------------------------------------ clearance

def test_clearance_recomputes_BOTH_sides():
    """Comparing fresh volume against the stale mcap would clear a coin whose cap merely fell."""
    assert rw.clears_floor({"volume_m5": 6000, "market_cap": 50_000}, FLOOR)[0] is True
    assert rw.clears_floor({"volume_m5": 4000, "market_cap": 50_000}, FLOOR)[0] is False
    # volume unchanged, mcap halved -> now clears. Recomputing both sides is what makes that visible.
    assert rw.clears_floor({"volume_m5": 4000, "market_cap": 25_000}, FLOOR)[0] is True


def test_missing_data_is_not_clearance():
    """The entry gate fails OPEN on absent volume; this is the opposite question."""
    assert rw.clears_floor({"market_cap": 50_000}, FLOOR)[0] is False
    assert rw.clears_floor({"volume_m5": 9999}, FLOOR)[0] is False
    assert rw.clears_floor(None, FLOOR)[0] is False


# ------------------------------------------------------------------ the pass

def test_clearance_re_injects_the_ORIGINAL_event_through_the_normal_hook():
    w = {}
    _enq(w, now=0.0, poll_s=10.0, window_s=30.0)
    seen = []
    tally = rw.poll(w, now=11.0, floor=FLOOR,
                    stats_fn=lambda _m: {"volume_m5": 9000, "market_cap": 50_000},
                    reinject_fn=seen.append)
    assert tally["cleared"] == 1
    assert len(seen) == 1 and seen[0].mint == "MINT" and seen[0].fomo_event_id == "evt-1"
    assert w == {}, "a cleared watch is finished, not left to fire twice"


def test_clearance_callback_can_hold_a_reask_until_the_live_gate_passes():
    w = {}
    _enq(w, now=0.0, poll_s=10.0, window_s=30.0)
    seen = []

    held = rw.poll(
        w, now=11.0, floor=FLOOR,
        stats_fn=lambda _m: {"volume_m5": 9000, "market_cap": 50_000},
        clears_fn=lambda _watch, _stats: False,
        reinject_fn=seen.append,
    )
    assert held["cleared"] == 0 and seen == [] and w

    cleared = rw.poll(
        w, now=21.0, floor=FLOOR,
        stats_fn=lambda _m: {"volume_m5": 9000, "market_cap": 50_000},
        clears_fn=lambda _watch, _stats: True,
        reinject_fn=seen.append,
    )
    assert cleared["cleared"] == 1 and len(seen) == 1 and w == {}


def test_a_clears_fn_that_swallows_its_own_recording_failure_still_clears():
    """The real clears_fn (scripts/memecoin_runner_loop.py:_rw_clears) writes a poll-evidence row
    as a side effect of computing the decision, wrapped in its own try/except. Without that wrap,
    a write failure (a locked funded ledger, WAL contention) would raise OUT of clears_fn, into
    poll()'s outer except here, which pops the watch, skips the re-inject and writes no outcome
    row for a clearance that was ALREADY DECIDED -- and the mint could never be re-armed. A
    recording failure must cost a row, never the decision."""
    w = {}
    _enq(w, now=0.0, poll_s=10.0, window_s=30.0)
    seen = []
    records = []

    def record_fn(watch, outcome, detail):
        records.append(outcome)

    def clears_fn(_watch, _stats):
        try:
            raise RuntimeError("database is locked")
        except RuntimeError:
            pass                          # a recording failure costs a row, never the decision
        return True

    tally = rw.poll(w, now=11.0, floor=FLOOR,
                    stats_fn=lambda _m: {"volume_m5": 9000, "market_cap": 50_000},
                    clears_fn=clears_fn, reinject_fn=seen.append, record_fn=record_fn)
    assert tally["cleared"] == 1
    assert len(seen) == 1 and seen[0].mint == "MINT", "the clearance still re-injects"
    assert records == [rw.OUTCOME_CLEARED], "the outcome is still recorded"
    assert w == {}, "the watch is finished normally, not lost to the outer except"


def test_the_full_gate_stack_still_decides_after_clearance():
    """The re-ask is a re-ask. A hook that refuses (chase, premium, bankroll) means NO ENTRY, and
    the watch must not treat its own clearance as an admission."""
    w = {}
    _enq(w, now=0.0)
    verdicts = []

    def refusing_hook(ev):
        verdicts.append({"status": "skipped_chase", "reason": "coin ran 3.4x"})

    rw.poll(w, now=11.0, floor=FLOOR,
            stats_fn=lambda _m: {"volume_m5": 9000, "market_cap": 50_000},
            reinject_fn=refusing_hook)
    assert verdicts[0]["status"] == "skipped_chase"
    assert w == {}, "the watch is spent either way; the hook owns the decision"


def test_a_window_that_closes_without_clearance_drops_cleanly():
    w = {}
    _enq(w, now=0.0, window_s=30.0)
    seen, recorded = [], []
    tally = rw.poll(w, now=31.0, floor=FLOOR, stats_fn=lambda _m: {"volume_m5": 1, "market_cap": 1},
                    reinject_fn=seen.append,
                    record_fn=lambda watch, outcome, detail: recorded.append(outcome))
    assert tally["expired"] == 1 and seen == [] and w == {}
    assert recorded == [rw.OUTCOME_EXPIRED]


def test_a_live_position_on_the_mint_supersedes_the_watch():
    """THE RACE. A new signal on the same mint goes through the normal path; the watch must not
    duplicate that entry, and must not be mistaken for its cause."""
    w = {}
    _enq(w, now=0.0)
    seen, recorded = [], []
    tally = rw.poll(w, now=11.0, floor=FLOOR,
                    stats_fn=lambda _m: {"volume_m5": 9999, "market_cap": 50_000},
                    reinject_fn=seen.append,
                    record_fn=lambda watch, outcome, detail: recorded.append(outcome),
                    superseded_fn=lambda _m: True)
    assert tally["superseded"] == 1
    assert seen == [], "must not re-inject onto a mint the book already holds"
    assert recorded == [rw.OUTCOME_SUPERSEDED]


def test_polling_respects_its_own_cadence():
    w = {}
    _enq(w, now=0.0, poll_s=10.0)
    reads = []
    rw.poll(w, now=5.0, floor=FLOOR, stats_fn=lambda m: reads.append(m) or {},
            reinject_fn=lambda _e: None)
    assert reads == [], "before the next poll is due, no provider call"
    rw.poll(w, now=10.5, floor=FLOOR, stats_fn=lambda m: reads.append(m) or {},
            reinject_fn=lambda _e: None)
    assert reads == ["MINT"]


def test_a_provider_exception_drops_the_watch_and_never_escapes():
    w = {}
    _enq(w, now=0.0)

    def boom(_m):
        raise TimeoutError("provider down")

    tally = rw.poll(w, now=11.0, floor=FLOOR, stats_fn=boom, reinject_fn=lambda _e: None)
    assert tally["cleared"] == 0 and w == {}


# ------------------------------------------------------------------ isolation

def test_the_service_reserves_nothing_and_takes_no_lock():
    src = pathlib.Path(rw.__file__).read_text()
    for forbidden in ("reserve_entry", "wallet_exec_lock", "release_pending", "execute_fn",
                      "memecoin_pending_entries", "MEMECOIN_RUN_LOCK"):
        assert forbidden not in src, f"a watch must not touch {forbidden}"


def test_the_poller_runs_off_the_fastlane_and_holds_no_lock():
    body = LOOP.read_text()
    start = body.index("# REVIVAL WATCH: poll on the ordinary loop cadence")
    block = body[start:body.index("[err] revival watch poll failed", start)]
    assert "wallet_exec_lock" not in block
    # It may READ reservation state (that is the supersede check); it may not CREATE one.
    assert "reserve_entry" not in block
    assert "INSERT INTO memecoin_pending_entries" not in block
    assert "real_hook(conn, e)" in block, "clearance must go through the NORMAL entry hook"


def _call_is_try_wrapped(source, func_name, call_attr):
    """True iff every `<obj>.<call_attr>(...)` call inside function `func_name` sits in a `try:`
    block's own body. A grep for the word "try" would pass a comment that only claims one."""
    func = next((n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.FunctionDef) and n.name == func_name), None)
    calls = [n for n in ast.walk(func) if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute) and n.func.attr == call_attr] if func else []
    tries = [n for n in ast.walk(func) if isinstance(n, ast.Try)] if func else []
    return bool(calls) and all(
        any(call in ast.walk(stmt) for t in tries for stmt in t.body) for call in calls)


def test_the_poll_record_write_is_try_wrapped_not_just_commented_as_one():
    """A comment saying "wrapped in try/except" is prose, not protection
    (feedback-source-scanning-guards-must-not-read-their-own-prose-2026-08-27). Gamma's finding:
    the recording write in `_rw_clears` must sit inside an actual `ast.Try`, or a future edit that
    deletes the wrap leaves every string-based test here green."""
    assert _call_is_try_wrapped(LOOP.read_text(), "_rw_clears", "record_revival_watch_poll")

    unwrapped = '''
def _rw_clears(watch, stats):
    evidence = {}
    m.record_revival_watch_poll(conn, mint=watch.get("mint"))
    return True
'''
    assert not _call_is_try_wrapped(unwrapped, "_rw_clears", "record_revival_watch_poll")

    decoy = '''
def _rw_clears(watch, stats):
    evidence = {}
    # wrapped in try/except so a recording failure never costs the decision
    m.record_revival_watch_poll(conn, mint=watch.get("mint"))
    return True
'''
    assert not _call_is_try_wrapped(decoy, "_rw_clears", "record_revival_watch_poll"), (
        "the comment claims a wrap that is not actually there")


def test_a_watch_reads_its_own_chain():
    """A 0x mint read without its chain lands on whatever DexScreener lists first, or nothing.
    ITEMS (4663) was read chainless and recorded as `solana` (2026-09-12)."""
    w = {}
    assert _enq(w, mint="0xabc", eid="e-chain", network_id="4663", coin_band="nano")[0]
    seen = []
    rw.poll(w, now=10.5, floor=FLOOR,
            stats_fn=lambda m, network_id=None: seen.append((m, network_id)) or {},
            reinject_fn=lambda _e: None)
    assert seen == [("0xabc", "4663")]

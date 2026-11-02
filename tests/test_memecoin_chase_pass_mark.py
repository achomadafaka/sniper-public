"""The display chase's PASS branch records its numbers, in memory only (entry capture P1, Alpha GO
2026-09-29). The chase columns were written only when refusing, so every fill carried NULL.

Pinned: the pass branch marks one trace substage (gap, cap, prices); that mark adds no SQL statement
and no quote; and a mark that raises changes nothing about the entry.
"""
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg


class _Ev:
    def __init__(self):
        self.mint = "PassMint"
        self.source_user_id = "u1"
        self.created_at = "2026-07-18T11:59:59+00:00"
        self.network_id = "solana"
        self.side = "buy"
        self.fomo_event_id = "event-pass"
        self.their_price = 1.0


class _Trace:
    """Records substages; every other trace call is a no-op."""
    attempt_id = None

    def __init__(self, fail_on=None):
        self.substages, self.fail_on = [], fail_on

    def mark_substage(self, name, **meta):
        if name == self.fail_on:
            raise RuntimeError("mark failed")
        self.substages.append((name, meta))

    def __getattr__(self, _name):
        return lambda *_a, **_k: None


def _run(monkeypatch, trace, *, cap=25.0):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CHASE_MAX_PCT", cap)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_FIRST_SEEN_CHASE_RATIO", 0.0)
    monkeypatch.setattr(R, "real_entry", lambda *_a, **_k: {"status": "rejected",
                                                             "reason": "test seam"})
    from sniper.execution import solana_real_seams
    monkeypatch.setattr(solana_real_seams, "jupiter_entry_quote",
                        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("quote on pass")))
    statements = []
    c.set_trace_callback(statements.append)
    result = R.real_entry_hook(
        c, _Ev(), {"solana": {}}, now_fn=lambda: "2026-07-18T12:00:00+00:00",
        trade_size=100.0, max_age_s=120.0, trace=trace,
        # The display chase runs on the stats path; the cached stats below are reused, so a call
        # here would be a second provider read.
        stats_fn=lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("duplicate stats call")),
        resolved_identity=R.ResolvedEntryIdentity("solana", "resolved", "test"),
        classification_context={"effective_mcap": 500_000.0, "assumed_micro": False,
                                "consensus_n_60m": 1,
                                "market_stats": {"price": 1.1, "liquidity": 5_000.0}})
    return result, statements


def test_the_pass_branch_records_its_gap_and_cap(monkeypatch):
    trace = _Trace()
    _run(monkeypatch, trace)
    marks = [meta for name, meta in trace.substages if name == "chase_display_check"]
    assert len(marks) == 1
    assert marks[0]["outcome"] == "passed" and marks[0]["cap_pct"] == 25.0
    assert marks[0]["gap_pct"] == pytest.approx(10.0) and marks[0]["our_price"] == pytest.approx(1.1)


def test_the_mark_adds_no_statement_and_a_failing_mark_changes_nothing(monkeypatch):
    marked, with_mark = _run(monkeypatch, _Trace())
    failed, without_mark = _run(monkeypatch, _Trace(fail_on="chase_display_check"))
    assert marked == failed
    assert with_mark == without_mark, "the mark must not add, drop or reorder a single SQL statement"


def test_the_pass_branch_runs_no_statement_the_chase_off_entry_does_not(monkeypatch):
    """The same traced entry with the display chase switched off (cap 0) differs only by the
    chase check and the mark. Any SQL the pass branch ran would show up as a difference."""
    on, with_chase = _run(monkeypatch, _Trace())
    off, without_chase = _run(monkeypatch, _Trace(), cap=0.0)
    assert on == off
    assert with_chase == without_chase

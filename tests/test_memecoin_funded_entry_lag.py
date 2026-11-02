"""The funded leg records its entry lag.

Both funded executors passed a literal `entry_lag_s=-1.0` to `open_position`, so every funded row
graded as missing-lag in `memecoin_entry_quality` and the funded leg had no entry-speed measurement
at all. The paper copy trader has always stored `ev.lag_s`; the funded leg now stores the same
quantity from the same source event, so the two legs are comparable.
"""
import inspect
import sqlite3

import pytest

from sniper.execution import evm_swap_executor, solana_swap_executor
from sniper.persistence import db_memecoin as m


def _conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c


def _event(c, event_id, lag):
    c.execute(
        "INSERT INTO memecoin_events (fomo_event_id, source_user_id, mint, side, lag_s,"
        " capture_version, clock_domain) VALUES (?,?,?,?,?,?,?)",
        (event_id, "u1", "MINT1", "buy", lag, 2, "observed_real_utc"))


def test_resolve_source_lag_reads_the_originating_event():
    c = _conn()
    _event(c, "evt-1", 43.22)
    assert m.resolve_source_lag_s(c, "evt-1") == pytest.approx(43.22)


def test_a_small_negative_lag_is_kept_as_measured():
    """Source clock ahead of ours is real and observed to -0.78s. Clamping it to 0 would hide the
    skew; readers already grade a negative lag as missing."""
    c = _conn()
    _event(c, "evt-neg", -0.78)
    assert m.resolve_source_lag_s(c, "evt-neg") == pytest.approx(-0.78)


def test_a_zero_lag_is_a_reading_not_an_absence():
    """Unlike a $0 size, a 0s lag is a legitimate measurement — the event and our read landed in
    the same second."""
    c = _conn()
    _event(c, "evt-0", 0.0)
    assert m.resolve_source_lag_s(c, "evt-0") == 0.0


def test_resolve_source_lag_is_none_when_unknown():
    c = _conn()
    _event(c, "evt-null", None)
    assert m.resolve_source_lag_s(c, "evt-null") is None
    assert m.resolve_source_lag_s(c, "missing") is None
    assert m.resolve_source_lag_s(c, None) is None


@pytest.mark.parametrize("module", [solana_swap_executor, evm_swap_executor])
def test_neither_funded_executor_still_writes_the_sentinel(module):
    """The regression guard. A funded entry runs behind RPC, quote and signing calls that a unit
    test cannot stand up, so the executor is checked at the source: the -1.0 literal is gone and
    the resolver is what supplies the column."""
    src = inspect.getsource(module)
    assert "entry_lag_s=-1.0" not in src
    assert "entry_lag_s=m.resolve_source_lag_s(conn, pending.get(\"origin_event_id\"))" in src

"""Tests for the coin-type lift artifact.

Every case is a defect a review found in an earlier revision of this script. The published claim
moved between 1.27x and 3.40x across four revisions, so the properties below are the only reason
any number it prints can be trusted.
"""
from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "coin_type_lift_artifact.py"
_spec = importlib.util.spec_from_file_location("coin_type_lift_artifact", SCRIPT)
art = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(art)

AS_OF = "2026-08-11T14:00:00+00:00"


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(
        """
        CREATE TABLE memecoin_signal_outcomes_v2(
            outcome_id TEXT, upstream_event_id TEXT, source_user_id TEXT, network_id TEXT,
            mint TEXT, signal_observed_at TEXT);
        CREATE TABLE memecoin_events(
            fomo_event_id TEXT, source_user_id TEXT, mint TEXT, side TEXT, is_first_buy INTEGER,
            created_at TEXT, their_market_cap REAL, network_id TEXT, received_at TEXT);
        CREATE TABLE memecoin_horizon_marks_v2(
            outcome_id TEXT, target_s INTEGER, price REAL, status TEXT, quality TEXT,
            observed_at TEXT, actual_age_s REAL, lateness_s REAL);
        """
    )
    return c


def signal(c, oid, mint, ts, *, uid="uid-1", mcap=500_000.0):
    c.execute("INSERT INTO memecoin_signal_outcomes_v2 VALUES (?,?,?,'solana',?,?)",
              (oid, f"ev-{oid}", uid, mint, ts))
    c.execute("INSERT INTO memecoin_events VALUES (?,?,?,'buy',1,?,?,?,?)",
              (f"ev-{oid}", uid, mint, ts, mcap, "solana", ts))


def prior_buy(c, mint, uid, received, *, created=None, net="solana"):
    c.execute("INSERT INTO memecoin_events VALUES (?,?,?,'buy',1,?,?,?,?)",
              (f"p-{mint}-{uid}-{net}", uid, mint, created or received, 500_000.0, net, received))


def marks(c, oid, p60, p300, *, observed="2026-08-11T13:00:00+00:00", late=0.0,
          status="observed", quality="ok", age_ok=True):
    for t, p in ((60, p60), (300, p300)):
        if p is None:
            continue
        c.execute("INSERT INTO memecoin_horizon_marks_v2 VALUES (?,?,?,?,?,?,?,?)",
                  (oid, t, p, status, quality, observed, t if age_ok else t - 1, late))


def test_consensus_uses_receipt_time_chain_scoped_and_strictly_before(conn):
    """An earlier revision added 60s to the signal before counting, to align with the price
    baseline. That leaks: the decision happens at signal time and cannot see the next minute.
    Correcting it moved the published cell from 36 mints to 20."""
    ts = "2026-08-11T12:00:00+00:00"
    for i in range(4):                       # 4 RECEIVED strictly before -> in the 4-8 band
        prior_buy(conn, "MintA", f"early{i}", "2026-08-11T11:00:00+00:00")
    for i in range(6):                       # 6 received within the next 60s -> must NOT count
        prior_buy(conn, "MintA", f"late{i}", "2026-08-11T12:00:30+00:00")
    # created BEFORE the decision but received AFTER it -> knowledge we did not have
    prior_buy(conn, "MintA", "delayed", "2026-08-11T12:04:00+00:00",
              created="2026-08-11T11:10:00+00:00")
    # same mint on another chain -> chains are never pooled
    prior_buy(conn, "MintA", "otherchain", "2026-08-11T11:05:00+00:00", net="4663")
    signal(conn, "a", "MintA", ts)
    marks(conn, "a", 1.0, 1.5)
    eps = art.load(conn, AS_OF, "mint")
    assert len(eps) == 1
    assert eps[0]["cons"] == 4, ("leak: future receipts, late-received buys, or another chain's "
                                 "buyers reached a decision-time feature")


def test_episode_is_selected_before_its_marks_are_validated(conn):
    """Marks vanish when a coin dies, so taking the earliest MARKED signal selects on outcome."""
    signal(conn, "first", "MintA", "2026-08-11T12:00:00+00:00")
    signal(conn, "later", "MintA", "2026-08-11T12:30:00+00:00")
    marks(conn, "later", 1.0, 5.0)           # only the later signal has marks
    assert art.load(conn, AS_OF, "mint") == [], "the unmarked first signal must drop, not swap"


def test_the_earliest_signal_wins_when_it_has_marks(conn):
    signal(conn, "first", "MintA", "2026-08-11T12:00:00+00:00")
    signal(conn, "later", "MintA", "2026-08-11T12:30:00+00:00")
    marks(conn, "first", 1.0, 1.1)
    marks(conn, "later", 1.0, 9.9)
    eps = art.load(conn, AS_OF, "mint")
    assert len(eps) == 1 and eps[0]["ret"] == pytest.approx(10.0)


@pytest.mark.parametrize("kwargs", [
    {"observed": "2026-08-11T15:00:00+00:00"},          # observed after as_of
    {"late": art.LATE_TARGET_S + 1},                    # staler than the bound
    {"status": "missed"},
    {"quality": None},
    {"age_ok": False},                                  # sampled before its target age
])
def test_unfresh_marks_are_rejected(conn, kwargs):
    signal(conn, "a", "MintA", "2026-08-11T12:00:00+00:00")
    marks(conn, "a", 1.0, 1.5, **kwargs)
    assert art.load(conn, AS_OF, "mint") == []


def test_the_null_is_hypergeometric_not_binomial():
    """The cell is a SUBSET of the pool, so the pool rate is not an independent base rate and a
    binomial p overstates significance."""
    import math
    k, n, K, N = 8, 36, 14, 120
    p_hyper = art.hypergeom_ge(k, n, K, N)
    p_binom = sum(math.comb(n, i) * (K / N) ** i * (1 - K / N) ** (n - i) for i in range(k, n + 1))
    assert p_hyper != pytest.approx(p_binom)
    assert 0.0 <= p_hyper <= 1.0
    # a cell holding exactly its share of the pool's hits is unremarkable
    assert art.hypergeom_ge(1, 10, 10, 100) > 0.5


def test_hypergeometric_edges():
    assert art.hypergeom_ge(0, 5, 3, 50) == pytest.approx(1.0)
    assert art.hypergeom_ge(5, 5, 5, 5) == pytest.approx(1.0)
    assert art.hypergeom_ge(3, 3, 3, 100) < 0.01


def test_mint_unit_collapses_the_signal_unit_inflation(conn):
    """One coin bought by three wallets is ONE observation with ONE price path, not three."""
    for i in range(3):
        signal(conn, f"s{i}", "MintA", f"2026-08-11T12:0{i}:00+00:00", uid=f"w{i}")
        marks(conn, f"s{i}", 1.0, 1.5)
    assert len(art.load(conn, AS_OF, "mint")) == 1
    assert len(art.load(conn, AS_OF, "signal")) == 3

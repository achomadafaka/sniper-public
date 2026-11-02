"""The post-take-profit follow-up instrument.

Every one of these fails QUIETLY if broken. An age anchored on the wrong clock still produces a
series; a provider error filed as `no_route` still produces a quality; a window with no pre-exit
anchor still produces a number. The failures are all wrong ANSWERS rather than errors.
"""
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from sniper.persistence import db_memecoin_captures as store
from sniper.services import memecoin_tp_continuation_capture as ptf

EXIT = "2026-08-29T10:00:00+00:00"


def _row(**kw):
    base = {"id": 7, "mint": "MINT", "network_id": "solana", "closed_at": EXIT,
            "close_sig": "SIG", "close_exit_reason": "real_tp",
            "qty": 1000.0, "notional": 36.83, "price": 0.0001}
    base.update(kw)
    return base


def _plan(now_iso=EXIT, **kw):
    return ptf.schedule(_row(**kw), decimals=6, now_iso=now_iso)


# ---------------------------------------------------------------- the clock

def test_age_is_measured_from_the_EXIT_not_from_when_we_noticed():
    """A poller detects a close seconds late. Anchoring on capture start would relabel a late
    first look as t=0 and move every observation earlier than it happened — and the 3-second
    window candidate is exactly where that lie would land."""
    late = (datetime.fromisoformat(EXIT) + timedelta(seconds=4.2)).isoformat()
    plan = _plan(now_iso=late)
    assert plan["detection_lag_s"] == pytest.approx(4.2, abs=0.01)
    obs = ptf.observation(plan, quote={"outAmount": "1000000"}, now_iso=late,
                          now=datetime.fromisoformat(late))
    assert obs["age_s"] == pytest.approx(4.2, abs=0.01), "age must not restart at zero"


def test_a_capture_detected_past_the_horizon_is_refused():
    late = (datetime.fromisoformat(EXIT) + timedelta(seconds=61)).isoformat()
    assert _plan(now_iso=late) is None


# ---------------------------------------------------------------- quality (prereg §3)

def test_a_provider_error_is_not_filed_as_no_route():
    """Different facts. `no_route` is a real market state that ends a window; a provider error is
    our own blindness. Merged, our outage reads as the coin dying — the direction that favours the
    hypothesis."""
    plan = _plan()
    now = datetime.fromisoformat(EXIT)
    assert ptf.observation(plan, quote=None, now_iso=EXIT, now=now)["quality"] == ptf.NO_ROUTE
    failed = ptf.observation(plan, quote=None, now_iso=EXIT, now=now, failure=RuntimeError("x"))
    assert failed["quality"] == ptf.PROVIDER_ERROR


def test_any_non_ok_mark_censors_the_whole_window():
    plan = _plan()
    assert ptf.terminal(plan, [1.0, 2.0], qualities=[ptf.OK, ptf.OK])["censored"] == 0
    assert ptf.terminal(plan, [1.0, 2.0], qualities=[ptf.OK, ptf.NO_ROUTE])["censored"] == 1
    assert ptf.terminal(plan, [1.0], qualities=[ptf.PROVIDER_ERROR])["censored"] == 1


# ---------------------------------------------------------------- own impact is a JOIN

def test_the_module_does_not_copy_a_pre_exit_mark_it_does_not_own():
    """`memecoin_route_series` already carries a pre-exit observation for 100% of real_tp closes
    since 2026-08-29, keyed on position_id. A second copy here would be two owners for one number,
    and they drift.

    The post-exit side was worse than redundant: it could only ever be NULL, because the capture
    reads marks at SCHEDULE time — moments after the close, before any post-close mark exists.
    0 of 227 live rows carried one. A column that can only be empty reads as a measurement that
    came back empty, which is the more expensive kind of wrong.
    """
    assert not hasattr(ptf, "own_impact_marks")
    plan = _plan()
    assert not [k for k in plan if "exit_mark" in k]


# ---------------------------------------------------------------- the bag

def test_the_quote_is_sized_to_the_position_s_own_bag():
    """A fixed USD notional would answer an easier question. Impact is size-dependent, and the
    hypothesis is whether a REAL exit could have got more."""
    assert _plan()["exit_qty_atomic"] == str(1000 * 10 ** 6)
    assert _plan(qty=0.5)["exit_qty_atomic"] == str(500_000)


def test_an_exit_with_no_recoverable_bag_is_skipped_not_guessed():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE memecoin_positions (id INT, mint TEXT, network_id TEXT, "
                 "closed_at TEXT, close_sig TEXT, close_exit_reason TEXT, execution_mode TEXT)")
    conn.execute("CREATE TABLE memecoin_fills (position_id INT, tx_sig TEXT, qty REAL, "
                 "notional REAL, price REAL)")
    conn.execute("INSERT INTO memecoin_positions VALUES "
                 "(7,'MINT','solana',?,'SIG','real_tp','real')", (EXIT,))
    assert ptf.new_tp_exits(conn, since_iso="2026-08-01") == []


# ---------------------------------------------------------------- coverage

def test_coverage_records_the_hole_rather_than_averaging_over_it():
    plan = _plan()
    t = ptf.terminal(plan, [1.0, 2.0, 3.0, 20.0], qualities=[ptf.OK] * 4)
    assert t["n_obs"] == 4 and t["expected_obs"] == 60
    assert t["max_gap_s"] == pytest.approx(17.0)
    assert t["outcome"] == ptf.GAPPED
    assert t["first_age_s"] == pytest.approx(1.0)


def test_the_table_round_trips_an_observation_and_a_terminal_row():
    conn = store.connect(":memory:")
    plan = _plan()
    now = datetime.fromisoformat(EXIT) + timedelta(seconds=3)
    store.record_followup_observation(conn, ptf.observation(
        plan, quote={"outAmount": "40000000"}, now_iso=now.isoformat(), now=now))
    store.record_followup_terminal(conn, ptf.terminal(plan, [3.0], qualities=[ptf.OK]))
    rows = conn.execute("SELECT age_s, quote_value_usd, censored FROM "
                        "memecoin_post_tp_followups ORDER BY id").fetchall()
    assert rows[0][0] == pytest.approx(3.0) and rows[0][1] == pytest.approx(40.0)
    assert rows[1][2] == 0


def test_a_re_observed_second_does_not_duplicate_the_series():
    """A restarted worker must not write a second row the read would take as two ticks."""
    conn = store.connect(":memory:")
    plan = _plan()
    now = datetime.fromisoformat(EXIT) + timedelta(seconds=5)
    row = ptf.observation(plan, quote={"outAmount": "1000000"}, now_iso=now.isoformat(), now=now)
    store.record_followup_observation(conn, row)
    store.record_followup_observation(conn, row)
    n = conn.execute("SELECT COUNT(*) FROM memecoin_post_tp_followups").fetchone()[0]
    assert n == 1


def test_nine_decimal_token_quote_price_shares_the_exit_basis():
    """Gamma audit 2026-08-29: a hardcoded 1e6 in the unit-price derivation put quote_price 1000x
    off exit_price on nine-decimal tokens — implied quantity ratio exactly 1000.0 — which reads as
    instantaneous collapse and false-negatives the confirm window on every such token. The decimals
    must travel with the plan and price both sides."""
    from datetime import datetime, timezone
    from sniper.services import memecoin_tp_continuation_capture as cc
    exit_row = {"id": 7, "close_sig": "s", "network_id": "solana", "mint": "M",
                "exit_reason": "real_tp", "close_exit_reason": "real_tp",
                "closed_at": "2026-08-29T10:00:00+00:00",
                "qty": 1000.0, "price": 0.002, "notional": 2.0}
    plan = cc.schedule(exit_row, decimals=9, now_iso="2026-08-29T10:00:02+00:00")
    assert plan["token_decimals"] == 9
    now = datetime(2026, 8, 29, 10, 0, 3, tzinfo=timezone.utc)
    row = cc.observation(plan, quote={"outAmount": 2_000_000}, now_iso="x", now=now)
    # $2 back for 1000 tokens -> unit price $0.002, the SAME basis as exit_price
    assert row["quote_price"] is not None
    assert abs(row["quote_price"] / row["exit_price"] - 1.0) < 1e-9, row["quote_price"]

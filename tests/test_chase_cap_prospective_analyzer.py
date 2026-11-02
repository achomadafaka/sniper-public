"""The registered analyzer must do what the protocol says, before the cohort opens.

The retrospective `chase_cap_drift_bands.py` could not express this protocol: it reads closed
positions only, so a refused attempt is invisible to it and cannot be entered at 0.00 PnL, and it
compares low-drift mints against high-drift mints rather than policy arms against a control. A
protocol that cites an analyzer which cannot run it is not a pre-registration.

These tests pin the parts that decide a live money gate:
  - a blocked attempt scores 0.00 and is never dropped,
  - the 120th enrollment CLOSES the cohort, so a 121st attempt cannot join,
  - an open position blocks the read until the 14-day backstop, then censors,
  - censoring is arm-dependent and above 10% invalidates the read,
  - every enrolled attempt was decided at the control cap,
  - the permutation applies the same per-attempt rule the arm applies,
  - Holm runs over the four arm hypotheses, not five.
"""
import json
import sqlite3

import pytest

from scripts.research import chase_cap_prospective as P


START = "2026-08-20T00:00:00Z"


def _trace(name, gap_pct=None, reason=None, cap_pct=30.0):
    event = {"name": name, "cap_pct": cap_pct}
    if gap_pct is not None:
        event["gap_pct"] = gap_pct
    if reason is not None:
        event["reason"] = reason
    return json.dumps({"substage_events": [{"name": "token_safety"}, event]})


def _row(state, drift=5.0, pnl=10.0, mint="MINT1", when="2026-08-20T01:00:00Z"):
    """One attempt in the shape load_cohort produces."""
    return {"attempt_id": f"a-{mint}-{when}", "mint": mint, "handle": "h",
            "observed_at": when, "gate": "chase_quote_confirmation",
            "gate_reason": None, "cap_at_decision": 30.0, "drift_pct": drift,
            "terminal_status": "filled", "position_id": 1, "realized_pnl": pnl,
            "is_open": False, "state": state}


# --- the rule the retrospective tool could not express -------------------------------------------

def test_a_blocked_attempt_scores_zero_and_is_never_dropped():
    """Scoring only the fills would drop exactly the trades the cap stopped."""
    blocked = _row("blocked", drift=45.0, pnl=None)
    assert P.arm_pnl(blocked, 30.0) == 0.0
    assert P.arm_pnl(blocked, 10.0) == 0.0


def test_a_tighter_arm_converts_an_observed_fill_into_a_block():
    row = _row("closed", drift=22.0, pnl=50.0)
    assert P.arm_pnl(row, 30.0) == 50.0        # control admitted it
    assert P.arm_pnl(row, 25.0) == 50.0
    assert P.arm_pnl(row, 20.0) == 0.0         # a 20 cap would have refused it
    assert P.arm_pnl(row, 10.0) == 0.0


def test_a_losing_fill_the_cap_removes_is_credited_as_avoided_loss():
    row = _row("closed", drift=22.0, pnl=-40.0)
    assert P.arm_pnl(row, 30.0) == -40.0
    assert P.arm_pnl(row, 10.0) == 0.0


def test_an_unpriceable_attempt_is_scored_by_no_arm():
    """The gate stood down, so no cap acted. Inventing a 0.00 would fabricate a decision."""
    row = _row("closed", drift=None, pnl=25.0)
    assert P.arm_pnl(row, 10.0) is None


# --- the terminal rule ---------------------------------------------------------------------------

def _args(**kw):
    base = {"start": START, "shuffles": 200, "seed": 7}
    base.update(kw)
    return type("A", (), base)()


# --- classification off the real trace shape ------------------------------------------------------

def _db(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE memecoin_entry_attempts(
        attempt_id TEXT, origin_event_id TEXT, network_id TEXT, mint TEXT, source_handle TEXT,
        observed_at TEXT, terminal_status TEXT, source_drift_pct REAL, trace_json TEXT,
        position_id INTEGER)""")
    c.execute("CREATE TABLE memecoin_positions(id INTEGER PRIMARY KEY, is_open INT, "
              "realized_pnl REAL, closed_at TEXT)")
    for r in rows:
        c.execute("INSERT INTO memecoin_entry_attempts VALUES(?,?,?,?,?,?,?,?,?,?)", r["attempt"])
        if r.get("position"):
            c.execute("INSERT INTO memecoin_positions(id, is_open, realized_pnl) "
                      "VALUES(?,?,?)", r["position"])
    c.commit()
    return c


def _load(conn, start=START):
    """Drive the real query against an in-memory database."""
    import unittest.mock as mock
    with mock.patch.object(P.sqlite3, "connect", return_value=conn):
        return P.load_cohort(":memory:", start)


def test_a_refusal_loads_as_blocked():
    conn = _db([{"attempt": ("a1", "ev1", "solana", "M1", "h", "2026-08-20T01:00:00Z",
                             "refused", 36.06, _trace("chase_quote_confirmation", gap_pct=36.06),
                             None)}])
    rows = _load(conn)
    assert len(rows) == 1
    assert rows[0]["state"] == "blocked" and rows[0]["drift_pct"] == pytest.approx(36.06)


def test_an_attempt_that_never_reached_the_gate_is_not_eligible():
    conn = _db([{"attempt": ("a1", "ev1", "solana", "M1", "h", "2026-08-20T01:00:00Z",
                             "aborted", None, json.dumps({"substage_events": [
                                 {"name": "token_safety"}]}), None)}])
    assert _load(conn) == []


def test_a_stand_down_is_eligible_and_keeps_its_reason():
    """The residual fail-open must be counted, not silently absent."""
    conn = _db([{"attempt": ("a1", "ev1", "solana", "M1", "h", "2026-08-20T01:00:00Z",
                             "filled", None,
                             _trace("chase_quote_skipped", reason="unpriceable_quote"), 7),
                 "position": (7, 0, 12.5)}])
    rows = _load(conn)
    assert rows[0]["gate_reason"] == "unpriceable_quote"
    assert P.arm_pnl(rows[0], 10.0) is None


def test_an_attempt_before_the_start_instant_is_excluded():
    conn = _db([{"attempt": ("a1", "ev1", "solana", "M1", "h", "2026-08-19T23:59:59Z",
                             "refused", 40.0, _trace("chase_quote_confirmation", gap_pct=40.0),
                             None)}])
    assert _load(conn) == []


# --- Holm over every registered arm


# --- cohort cutoff --------------------------------------------------------------------------------

def _seq(n, state="closed", drift=5.0, pnl=10.0, start_hour=1):
    return [_row(state, drift=drift, pnl=pnl, mint=f"M{i}",
                 when=f"2026-08-20T{start_hour + i // 60:02d}:{i % 60:02d}:00Z")
            for i in range(n)]


def test_the_cohort_closes_at_the_stop_count():
    """Loading every later attempt would analyse 121+ and let the sample grow while it is read."""
    enrolled, out, closed_at = P.apply_cohort_cutoff(_seq(P.STOP_COUNT + 25))
    assert len(enrolled) == P.STOP_COUNT
    assert len([r for r in out if r["cohort"] == "out_of_cohort"]) == 25
    assert closed_at == enrolled[-1]["observed_at"]


def test_an_aborted_attempt_never_consumes_a_slot():
    rows = _seq(5) + [_row("aborted", mint=f"A{i}", when=f"2026-08-20T02:{i:02d}:00Z")
                      for i in range(50)]
    enrolled, out, closed_at = P.apply_cohort_cutoff(sorted(rows, key=lambda r: r["observed_at"]))
    assert len(enrolled) == 5 and closed_at is None
    assert len([r for r in out if r["cohort"] == "aborted"]) == 50


def test_an_incomplete_cohort_is_not_closed():
    _enrolled, _out, closed_at = P.apply_cohort_cutoff(_seq(10))
    assert closed_at is None


# --- the terminal backstop ------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone


def test_before_the_backstop_an_open_position_is_left_open():
    rows = _seq(P.STOP_COUNT - 1) + [_row("open", mint="OPEN1", when="2026-08-20T03:00:00Z")]
    enrolled, _out, closed_at = P.apply_cohort_cutoff(rows)
    backstop, censored = P.apply_terminal_backstop(
        enrolled, closed_at, now=datetime(2026, 8, 21, tzinfo=timezone.utc))
    assert censored == 0
    assert backstop == P._iso(closed_at) + timedelta(days=P.TERMINAL_BACKSTOP_DAYS)
    assert any(r["state"] == "open" for r in enrolled)


def test_at_the_backstop_an_open_position_is_censored():
    rows = _seq(P.STOP_COUNT - 1) + [_row("open", mint="OPEN1", when="2026-08-20T03:00:00Z")]
    enrolled, _out, closed_at = P.apply_cohort_cutoff(rows)
    _backstop, censored = P.apply_terminal_backstop(
        enrolled, closed_at, now=datetime(2026, 9, 20, tzinfo=timezone.utc))
    assert censored == 1
    assert [r["state"] for r in enrolled].count("censored") == 1


def test_censoring_is_arm_dependent():
    """A tight arm BLOCKS a still-open position, so its PnL under that arm is a known 0.00.
    A loose arm admits it and inherits the unknown. Reporting one censor count for every arm
    would overstate what the tight arms do not know."""
    rows = [_row("censored", drift=22.0, pnl=None, mint="OPEN1")]
    assert P.arm_pnl(rows[0], 10.0) == 0.0          # 10 refuses a 22% drift -> known
    assert P.arm_pnl(rows[0], 30.0) is None         # 30 admits it -> unknown
    assert P.censored_by_arm(rows, 10.0) == 0
    assert P.censored_by_arm(rows, 30.0) == 1


def test_the_read_is_blocked_while_a_position_is_open(capsys):
    rows = _seq(P.STOP_COUNT - 1) + [_row("open", mint="OPEN1", when="2026-08-20T03:00:00Z")]
    assert P.report(rows, _args(), now=datetime(2026, 8, 21, tzinfo=timezone.utc)) == 2
    assert "NOT TERMINAL" in capsys.readouterr().out


def test_too_much_censoring_invalidates_the_read(capsys):
    n_open = int(P.STOP_COUNT * P.MAX_CENSORED_FRACTION) + 5
    rows = _seq(P.STOP_COUNT - n_open) + [
        _row("open", drift=5.0, mint=f"O{i}", when=f"2026-08-20T04:{i:02d}:00Z")
        for i in range(n_open)]
    assert P.report(rows, _args(), now=datetime(2026, 9, 20, tzinfo=timezone.utc)) == 2
    assert "CENSORED-INCONCLUSIVE" in capsys.readouterr().out


# --- the decision cap ------------------------------------------------------------------------------

def test_an_attempt_decided_at_another_cap_invalidates_the_run(capsys):
    """The reconstruction assumes the live cap was the control. An attempt decided at 15 has a
    different admission set, so no arm can be rebuilt from it."""
    rows = _seq(P.STOP_COUNT)
    rows[3]["cap_at_decision"] = 15.0
    assert P.report(rows, _args(), now=datetime(2026, 9, 20, tzinfo=timezone.utc)) == 2
    assert "WRONG DECISION CAP" in capsys.readouterr().out


# --- the permutation unit --------------------------------------------------------------------------

def test_the_permutation_uses_the_per_attempt_rule_not_a_mint_median():
    """A mint with attempts on both sides of the cap is where the two differ. The observed arm
    blocks the 40% attempt and keeps the 2% one; a mint-median rule would have moved both."""
    rows = [_row("closed", drift=2.0, pnl=10.0, mint="BOTH", when="2026-08-20T01:00:00Z"),
            _row("closed", drift=22.0, pnl=-90.0, mint="BOTH", when="2026-08-20T01:01:00Z"),
            _row("closed", drift=3.0, pnl=5.0, mint="LOW", when="2026-08-20T01:02:00Z")]
    # BOTH has median drift 12, so a mint-median rule at cap 10 zeroes BOTH of its attempts.
    # The exact per-attempt rule zeroes only the 22% one, which the control still admits.
    assert P.arm_pnl(rows[0], 10.0) == 10.0
    assert P.arm_pnl(rows[1], 10.0) == 0.0
    assert P.arm_pnl(rows[1], 30.0) == -90.0
    effect, p, band = P.permutation(rows, 10.0, 30.0, shuffles=200, seed=7)
    assert band == 1                                  # exactly one ATTEMPT in (10, 30]
    assert 0.0 < p <= 1.0


def test_an_arm_that_blocks_nothing_cannot_reject():
    rows = [_row("closed", drift=2.0, pnl=5.0, mint=f"M{i}") for i in range(8)]
    _effect, p, band = P.permutation(rows, 10.0, 30.0, shuffles=50, seed=7)
    assert band == 0 and p == 1.0


def test_the_permuted_world_moves_whole_mints():
    """Clustering must survive the permutation: two attempts on one mint keep their drift vector
    together, so the shuffle cannot split a mint across the band."""
    rows = [_row("closed", drift=2.0, pnl=10.0, mint="A", when="2026-08-20T01:00:00Z"),
            _row("closed", drift=2.0, pnl=10.0, mint="A", when="2026-08-20T01:01:00Z"),
            _row("closed", drift=22.0, pnl=-50.0, mint="B", when="2026-08-20T01:02:00Z"),
            _row("closed", drift=22.0, pnl=-50.0, mint="B", when="2026-08-20T01:03:00Z")]
    effect, p, band = P.permutation(rows, 10.0, 30.0, shuffles=200, seed=7)
    assert band == 2                                  # both of B's attempts, never one
    assert effect != 0.0


# --- the hypothesis family -------------------------------------------------------------------------

def test_the_family_is_four_arms_not_five():
    """The control is not a test against itself. A synthetic p=1.0 for it makes a 5-test family,
    which is conservative rather than wrong-in-our-favour, but it is not the registered family."""
    assert P.CONTROL_CAP not in P.ARM_CAPS
    assert len(P.ARM_CAPS) == 4
    steps = P.holm({c: {"p": 0.001} for c in P.ARM_CAPS}, 0.05)
    assert len(steps) == 4
    assert steps[0][2] == pytest.approx(0.05 / 4)     # first-rank bar is alpha/4, not alpha/5


def test_holm_halts_at_the_first_retain():
    """Ranks are by ascending p: 0.005 clears 0.0125, then 0.03 misses 0.01667 and everything
    after it retains regardless of its own bar."""
    per_arm = {c: {"p": p} for c, p in zip(P.ARM_CAPS, (0.005, 0.03, 0.04, 0.045))}
    steps = P.holm(per_arm, 0.05)
    assert [rejected for _c, _p, _bar, rejected in steps] == [True, False, False, False]


def test_no_arm_is_looser_than_the_control():
    assert max(P.ARM_CAPS) < P.CONTROL_CAP


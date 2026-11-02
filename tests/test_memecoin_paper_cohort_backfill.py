"""The backfill may only re-stamp events it can PROVE already ran the current rails."""
import sqlite3

from scripts.backfill_memecoin_paper_cohort import plan
from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_policy import build_paper_cohort, resolve_paper_arm_policy
from sniper.settings import memecoin as settings


ARMS = ("control", "alpha_b")


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    return conn


def _event(conn, event, *, cohort="stale-per-coin-id", policy_json=None, arms=ARMS,
           fill_source="dexscreener", status="eligible", notional=100.0):
    for arm in arms:
        policy_id, version, resolved = resolve_paper_arm_policy(arm, network_id="solana")
        m.open_position(
            conn, source_user_id=f"W-{event}", source_handle=f"W-{event}", mint=f"M-{event}",
            ticker=f"M-{event}", entry_price=1.0, entry_notional=notional,
            entry_fill_source=fill_source, entry_lag_s=1.0, their_size_usd=1000.0,
            opened_at="2026-07-28T00:00:00+00:00", strategy=arm, origin_event_id=event,
            network_id="solana", assigned_policy_id=policy_id, assigned_policy_version=version,
            assigned_policy_json=(policy_json or resolved), paper_cohort_id=cohort,
            paper_cohort_json='{"arms":[]}', experiment_status=status,
        )


def test_backfill_restamps_an_event_that_already_ran_todays_rails():
    conn = _conn()
    _event(conn, "E1")
    rewrites, reasons = plan(conn, ARMS)
    assert len(rewrites) == 1 and reasons["rails_differ_from_today"] == 0
    _event_id, cohort_id, _json, ids = rewrites[0]
    assert cohort_id == build_paper_cohort(ARMS, notional_usd=100.0)[0]
    assert len(ids) == len(ARMS)


def test_backfill_refuses_an_event_whose_frozen_policy_is_not_todays():
    """A row opened under different rails is a different experiment — never re-labelled."""
    conn = _conn()
    _event(conn, "E1", policy_json='{"schema_version":1,"strategy":"x","params":{"tp_pct":1}}')
    rewrites, reasons = plan(conn, ARMS)
    assert rewrites == [] and reasons["rails_differ_from_today"] == 1


def test_backfill_refuses_incomplete_rosters_fomo_fills_and_split_notionals():
    conn = _conn()
    _event(conn, "PARTIAL", arms=("control",))
    _event(conn, "FOMO", fill_source="fomo")
    _event(conn, "MODIFIED", status="operator_modified")
    rewrites, reasons = plan(conn, ARMS)
    assert rewrites == []
    assert reasons["incomplete_roster"] == 1 and reasons["fomo_or_not_eligible"] == 2

    conn = _conn()
    for arm, notional in zip(ARMS, (100.0, 300.0)):
        _event(conn, "SPLIT", arms=(arm,), notional=notional)
    rewrites, reasons = plan(conn, ARMS)
    assert rewrites == [] and reasons["split_notional"] == 1


def test_backfill_is_idempotent(monkeypatch):
    conn = _conn()
    correct = build_paper_cohort(ARMS, notional_usd=100.0)[0]
    _event(conn, "E1", cohort=correct)
    rewrites, reasons = plan(conn, ARMS)
    assert rewrites == [] and reasons["already_correct"] == 1
    # and a rail change makes the same rows stop qualifying rather than silently re-stamping
    monkeypatch.setattr(settings, "MEMECOIN_B_STOP_PCT", 999.0)
    rewrites, reasons = plan(conn, ARMS)
    assert rewrites == [] and reasons["rails_differ_from_today"] == 1

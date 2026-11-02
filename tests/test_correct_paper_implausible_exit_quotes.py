"""The STANDARD paper P&L correction: exactly six rows, audited first, idempotent, dry run by default."""
import sqlite3

import pytest

from scripts import correct_paper_implausible_exit_quotes as fix
from sniper.persistence import db_memecoin as m


def _ledger(extra=()):
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    rows = [(t["position_id"], t["booked_realized_pnl"], t["exit_fill_id"], t["exit_fill_price"])
            for t in fix.TARGETS] + list(extra)
    for pid, pnl, fill_id, price in rows:
        c.execute("INSERT INTO memecoin_positions (id, source_user_id, source_handle, mint, ticker, "
                  "entry_price, entry_notional, entry_fill_source, entry_lag_s, their_size_usd, is_open, "
                  "opened_at, closed_at, realized_pnl, exit_reason, strategy, execution_mode, network_id) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?,?,?)",
                  (pid, "u", "h", fix.MINT, "STANDARD", 0.24, 100.0, "route_quote", 0.1, 0.0,
                   "2026-09-15T01:22:00+00:00", "2026-09-15T01:48:09+00:00", pnl, "real_tp",
                   "funded_clone", "paper", "4663"))
        c.execute("INSERT INTO memecoin_fills (id, position_id, kind, price, qty, notional, fill_source, "
                  "fraction, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (fill_id, pid, "bracket_exit", price, 415.0, price * 415.0, "route_quote", 1.0,
                   "2026-09-15T01:48:09+00:00"))
    c.commit()
    return c


def test_the_dry_run_names_all_six_changes_and_writes_nothing():
    c = _ledger()
    before = c.total_changes
    plan = fix.plan(c)
    assert [p["position_id"] for p in plan] == [57844, 57845, 57846, 57847, 57850, 57851]
    assert all(p["status"] == "will_correct" for p in plan)
    assert all(p["after"] == {"exit_reason": "real_tp_noquote", "realized_pnl": 0.0} for p in plan)
    assert c.total_changes == before
    assert not c.execute("SELECT name FROM sqlite_master WHERE name='memecoin_paper_pnl_corrections'").fetchall()


def test_apply_audits_first_then_corrects_and_is_idempotent():
    c = _ledger()
    assert fix.apply(c, now_iso="2026-09-17T12:00:00+00:00") == 6
    audit = c.execute("SELECT position_id, booked_exit_reason, booked_realized_pnl, booked_exit_fill_price, "
                      "corrected_exit_reason, corrected_realized_pnl FROM memecoin_paper_pnl_corrections "
                      "ORDER BY position_id").fetchall()
    assert [a["position_id"] for a in audit] == [t["position_id"] for t in fix.TARGETS]
    assert audit[0]["booked_realized_pnl"] == pytest.approx(803.9502818259565)
    rows = c.execute("SELECT exit_reason, realized_pnl FROM memecoin_positions WHERE id IN (57844,57851)").fetchall()
    assert all(r["exit_reason"] == "real_tp_noquote" and r["realized_pnl"] == 0.0 for r in rows)
    assert fix.apply(c, now_iso="2026-09-17T12:01:00+00:00") == 0
    assert all(p["status"] == "already_corrected" for p in fix.plan(c))
    assert c.execute("SELECT COUNT(*) FROM memecoin_paper_pnl_corrections").fetchone()[0] == 6


def test_a_row_that_no_longer_matches_its_booked_values_aborts_everything():
    c = _ledger()
    c.execute("UPDATE memecoin_positions SET realized_pnl=12.0 WHERE id=57850"); c.commit()
    assert [p["status"] for p in fix.plan(c)].count("guard_mismatch") == 1
    with pytest.raises(fix.GuardMismatch):
        fix.apply(c, now_iso="2026-09-17T12:00:00+00:00")
    assert c.execute("SELECT COUNT(*) FROM memecoin_positions WHERE exit_reason='real_tp'").fetchone()[0] == 6
    assert not c.execute("SELECT name FROM sqlite_master WHERE name='memecoin_paper_pnl_corrections'").fetchall() \
        or c.execute("SELECT COUNT(*) FROM memecoin_paper_pnl_corrections").fetchone()[0] == 0


def test_rows_outside_the_six_are_never_touched():
    c = _ledger(extra=[(53308, 673.47, 900001, 1.0)])      # UBER: flagged, not corrected
    fix.apply(c, now_iso="2026-09-17T12:00:00+00:00")
    other = c.execute("SELECT exit_reason, realized_pnl FROM memecoin_positions WHERE id=53308").fetchone()
    assert other["exit_reason"] == "real_tp" and other["realized_pnl"] == pytest.approx(673.47)


def test_after_apply_the_uncensored_totals_no_longer_carry_the_inflated_profit():
    c = _ledger()
    booked = sum(t["booked_realized_pnl"] for t in fix.TARGETS)
    assert m.closed_position_totals(c, mode="paper")["total_pnl"] == pytest.approx(booked)
    fix.apply(c, now_iso="2026-09-17T12:00:00+00:00")
    assert m.closed_position_totals(c, mode="paper")["total_pnl"] == pytest.approx(0.0)


def test_correction_excludes_rejected_fills_from_conservative_report():
    from datetime import datetime, timezone
    from sniper.services.memecoin_paper_report import _project_rows, _add_outcome
    c = _ledger(extra=[(53308, 673.47, 900001, 1.0)])
    for pid, in c.execute("SELECT id FROM memecoin_positions").fetchall():
        m.insert_fill(c, position_id=pid, kind="entry", price=0.24, qty=415,
                      notional=99.6, fill_source="route_quote", fraction=1,
                      created_at="2026-09-15T01:22:00+00:00")
    c.commit()
    fix.apply(c, now_iso="2026-09-17T12:00:00+00:00")
    before = c.total_changes
    rows = [_add_outcome(r, datetime.now(timezone.utc)) for r in _project_rows(c, "robin")]
    corrected = [r for r in rows if r["id"] != 53308]
    assert all(r["report_pnl"] is None for r in corrected)
    assert all(r["report_observed_exit_proceeds"] == 0 for r in corrected)
    assert all(r["report_booked_pnl"] == 0 for r in corrected)
    assert all(r["report_conservative_pnl"] == pytest.approx(-99.6) for r in corrected)
    other = next(r for r in rows if r["id"] == 53308)
    assert other["report_conservative_pnl"] == pytest.approx(673.47)
    assert c.total_changes == before
    assert c.execute("SELECT price FROM memecoin_fills WHERE id=116766").fetchone()[0] == fix.TARGETS[0]["exit_fill_price"]

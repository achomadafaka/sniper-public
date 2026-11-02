"""Every paper arm that can OPEN must also be able to CLOSE.

THE INCIDENT, 2026-08-29. `funded_clone` and `profile_matched_v1` were wired into the open path
(`resolve_paper_arm_policy`) and not into `sweep_exits`, which dispatches by an explicit
per-strategy list. 53 + 53 positions sat open, average age 97 minutes, zero closed all day —
against funded rails whose max hold is 780 SECONDS. The only thing that could ever have closed
them was the 14-day backstop.

It failed silently in the worst way available: entries accrued, the book grew, and the study
looked like it was running.
"""
import ast
import pathlib

import pytest

from sniper.services import memecoin_exits as EX
from sniper.services import memecoin_policy as PO

EXITS = pathlib.Path(EX.__file__)


def _swept_strategies():
    """Every strategy name `sweep_exits` actually dispatches on, read from its source."""
    from _source_guard import code_of
    tree = ast.parse(code_of(EXITS))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "sweep_exits")
    names = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "open_positions_by_strategy"):
            for a in node.args[1:]:
                if isinstance(a, ast.Constant):
                    names.add(a.value)
                elif isinstance(a, ast.Name):
                    names.update(EX.STUDY_ARMS)      # the loop variable over STUDY_ARMS
    return names


def _resolvable_arms():
    """Every arm the OPEN path can produce a policy for."""
    return set(PO._PAPER_REQUIRED_PARAMS)


def test_every_arm_that_can_open_can_also_close():
    """The property the incident violated, enumerated from BOTH sides rather than from a list in
    this file — a list here would need the same edit the sweep needed, and would be forgotten in
    the same moment."""
    missing = sorted(_resolvable_arms() - _swept_strategies())
    assert not missing, (
        f"arms that can open but are never swept for exits: {missing}. "
        "They will sit until the 14-day backstop.")


def test_the_study_arms_are_swept():
    assert set(EX.STUDY_ARMS) <= _swept_strategies()
    assert "funded_clone" in EX.STUDY_ARMS and "profile_matched_v1" in EX.STUDY_ARMS


def test_the_guard_would_have_caught_the_incident():
    """Proved in the advertised direction: with the study arms removed from the swept set, the
    cross-check goes red rather than passing on an empty difference."""
    swept = _swept_strategies() - set(EX.STUDY_ARMS)
    assert sorted(_resolvable_arms() - swept), "the guard must notice an unswept arm"


# ---------------------------------------------------------------- the rails they close on

def _pos(**kw):
    import json
    policy = {"tp_pct": 25.0, "stop_pct": 35.0, "catastrophe_pct": 80.0, "derisk_pct": 0.0,
              "trail_pct": 0.0, "trail_arm_pct": 0.0, "max_hold_s": 780.0,
              "unquotable_exit_s": 300.0, "stop_debounce_sweeps": 1}
    policy.update(kw.pop("policy", {}))
    base = {"id": 1, "opened_at": "2026-08-29T10:00:00+00:00", "entry_price": 1.0,
            "experiment_status": "eligible", "peak_price": 1.0,
            "assigned_policy_json": json.dumps({"exit_policy": policy, "params": {}})}
    base.update(kw)
    return base


def test_the_frozen_rails_are_read_from_the_exit_policy_not_from_params():
    """The study arms store their rails under `exit_policy`; the legacy arms use `params`. If the
    reader only looked at `params` every study rail would read as zero — which is a position that
    never closes, exactly the failure being fixed."""
    pos = _pos()
    assert EX._frozen_real_shadow_reason(pos, 1.30, "2026-08-29T10:00:10+00:00") == "real_tp"
    assert EX._frozen_real_shadow_reason(pos, 0.60, "2026-08-29T10:00:10+00:00") == "b_stop"


def test_the_funded_timecap_actually_bites_at_its_own_seconds():
    """780 seconds, not 14 days. A funded_clone position three hours old was the tell."""
    pos = _pos()
    assert EX._frozen_real_shadow_reason(pos, 1.05, "2026-08-29T10:12:00+00:00") is None
    assert EX._frozen_real_shadow_reason(pos, 1.05, "2026-08-29T10:14:00+00:00") == "b_timecap"


def test_a_fitted_take_profit_fires_at_the_arm_s_OWN_level():
    """profile_matched_v1's whole point is a per-wallet TP. If the sweep used a global one the arm
    would be indistinguishable from its baseline and the study would measure nothing."""
    pos = _pos(policy={"tp_pct": 22.8})
    assert EX._frozen_real_shadow_reason(pos, 1.23, "2026-08-29T10:00:10+00:00") == "real_tp"
    assert EX._frozen_real_shadow_reason(pos, 1.20, "2026-08-29T10:00:10+00:00") is None


# ---------------------------------------------------------------- the backlog boundary

def _study_conn():
    import json
    import sqlite3

    from sniper.persistence import db_memecoin as m
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    return c, m


def _open_study(c, m, *, opened_at, arm="funded_clone", mint="MINT"):
    import json
    policy = {"tp_pct": 25.0, "stop_pct": 35.0, "catastrophe_pct": 80.0, "derisk_pct": 0.0,
              "trail_pct": 0.0, "trail_arm_pct": 0.0, "max_hold_s": 780.0,
              "unquotable_exit_s": 300.0, "stop_debounce_sweeps": 1}
    pid = m.open_position(
        c, source_user_id="u1", source_handle="h", mint=mint, ticker="T", entry_price=1.0,
        entry_notional=100.0, entry_fill_source="chain", entry_lag_s=0.0, their_size_usd=0.0,
        opened_at=opened_at, strategy=arm, execution_mode="paper", network_id="solana")
    m.insert_fill(c, position_id=pid, kind="entry", price=1.0, qty=100.0, notional=100.0,
                  fill_source="chain", fraction=1.0, created_at=opened_at)
    # The params the arm's real assignment carries. Without them `paper_policy_is_valid` marks the
    # row `invalid_policy`, `paper_policy_value` then returns the fallback for EVERY rail, and the
    # position has no rails at all — it would ride to the 14-day backstop. The fixture has to be a
    # valid assignment or it tests the invalid path by accident.
    params = {"paper_max_hold_s": 1209600.0, "paper_unquotable_s": 300.0, "mirror_on_sell": False}
    c.execute("UPDATE memecoin_positions SET assigned_policy_json=?, experiment_status='eligible' "
              "WHERE id=?", (json.dumps({"exit_policy": policy, "params": params}), pid))
    c.commit()
    return pid


CUT = "2026-08-29T14:00:00+00:00"
NOW = "2026-08-29T14:00:30+00:00"


def test_a_backlog_position_closes_under_its_OWN_label_not_a_rail(monkeypatch):
    """A deploy-triggered mass closure at 4-hour ages must not enter exit-reason distributions,
    hold-duration stats or future rail comparisons as though the rails decided it. Nothing
    decided — nothing looked."""
    from sniper.settings import memecoin as S
    c, m = _study_conn()
    pid = _open_study(c, m, opened_at="2026-08-29T09:29:31+00:00")
    monkeypatch.setattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", CUT)
    EX.sweep_exits(c, {("solana", "MINT"): {"price": 0.9, "liquidity": 5000.0}}, NOW)
    row = c.execute("SELECT is_open, exit_reason, realized_pnl FROM memecoin_positions "
                    "WHERE id=?", (pid,)).fetchone()
    assert row["is_open"] == 0
    assert row["exit_reason"] == EX.SWEEP_CLOSURE
    assert row["realized_pnl"] is None, "NULL is the unknown; 0 would be a fabricated break-even"


def test_a_backlog_closure_books_NO_fill():
    """Any price stamped at these ages is an invention — the coin moved for hours after the rails
    would have exited."""
    c, m = _study_conn()
    pid = _open_study(c, m, opened_at="2026-08-29T09:29:31+00:00")
    EX._sweep_closure(c, dict(c.execute(
        "SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone()), NOW)
    exits = c.execute("SELECT COUNT(*) FROM memecoin_fills WHERE position_id=? AND kind!='entry'",
                      (pid,)).fetchone()[0]
    assert exits == 0


def test_a_NORMAL_post_deploy_position_still_closes_on_its_RAILS(monkeypatch):
    """The boundary must not swallow the arm it exists to protect. Opened after the cutoff, up
    30% against a 25% take-profit — that is a rail decision and must be labelled as one."""
    from sniper.settings import memecoin as S
    c, m = _study_conn()
    pid = _open_study(c, m, opened_at="2026-08-29T14:00:05+00:00")
    monkeypatch.setattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", CUT)
    EX.sweep_exits(c, {("solana", "MINT"): {"price": 1.30, "liquidity": 5000.0}}, NOW)
    row = c.execute("SELECT is_open, exit_reason, realized_pnl FROM memecoin_positions "
                    "WHERE id=?", (pid,)).fetchone()
    assert row["is_open"] == 0
    assert row["exit_reason"] != EX.SWEEP_CLOSURE
    assert "real_tp" in str(row["exit_reason"])
    assert row["realized_pnl"] is not None, "a rail close has a real outcome"


def test_an_empty_cutoff_is_inert(monkeypatch):
    """The default. Everywhere except the one deploy this exists for, every position takes its
    rails and the branch cannot fire."""
    from sniper.settings import memecoin as S
    c, m = _study_conn()
    pid = _open_study(c, m, opened_at="2026-08-29T09:29:31+00:00")
    monkeypatch.setattr(S, "MEMECOIN_STUDY_SWEEP_CLOSURE_BEFORE", "")
    EX.sweep_exits(c, {("solana", "MINT"): {"price": 1.30, "liquidity": 5000.0}}, NOW)
    row = c.execute("SELECT exit_reason FROM memecoin_positions WHERE id=?", (pid,)).fetchone()
    assert row["exit_reason"] != EX.SWEEP_CLOSURE


def test_the_label_is_shared_with_no_rail():
    """A reader grouping by exit_reason must not be able to pool these with a rail decision."""
    rails = {"real_tp", "b_stop", "catastrophe", "derisk", "real_trail", "b_timecap",
             "unquotable", "max_hold_cap", "mirror", "peak_stall", "vol_death"}
    assert EX.SWEEP_CLOSURE not in rails


def test_the_detail_puts_the_LABEL_FIRST():
    """The prefix contract the vol_death audit guard enforces: a stable head to group on, the
    drifting measurement after it."""
    c, m = _study_conn()
    pid = _open_study(c, m, opened_at="2026-08-29T09:29:31+00:00")
    r = EX._sweep_closure(c, dict(c.execute(
        "SELECT * FROM memecoin_positions WHERE id=?", (pid,)).fetchone()), NOW)
    assert r["detail"].startswith(EX.SWEEP_CLOSURE + ":")


def test_the_sweep_NEVER_gives_a_real_position_a_null_realized_pnl():
    """THE BREAKER READS THIS COLUMN. `daily_real_realized_pnl` is
    `COALESCE(SUM(realized_pnl), 0.0)` over execution_mode='real', and SQLite's SUM SKIPS NULLs
    outright — so a NULL-PnL real close is not "unknown loss" to the daily-loss breaker, it is
    ABSENT. The one query that stops the bleeding would read a real loss as no loss, silently.

    Today that safety is a property of the inventory (`sweep_exits` iterates paper positions and
    the study arms are paper), not of the code. This assertion makes the incidental explicit, so a
    future arm that opens in real mode cannot inherit the NULL path unnoticed.
    """
    import ast
    import pathlib
    from _source_guard import code_of

    tree = ast.parse(code_of(pathlib.Path(EX.__file__)))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_sweep_closure")
    src = ast.unparse(fn)
    assert "finalize_close" in src and "None" in src

    # and behaviourally: a real-mode position must not come out of the sweep with a NULL pnl
    c, m = _study_conn()
    pid = _open_study(c, m, opened_at="2026-08-29T09:29:31+00:00")
    c.execute("UPDATE memecoin_positions SET execution_mode='real' WHERE id=?", (pid,))
    c.commit()
    EX.sweep_exits(c, {("solana", "MINT"): {"price": 0.9, "liquidity": 5000.0}}, NOW)
    row = c.execute("SELECT execution_mode, is_open, realized_pnl FROM memecoin_positions "
                    "WHERE id=?", (pid,)).fetchone()
    assert row["execution_mode"] == "real"
    assert not (row["is_open"] == 0 and row["realized_pnl"] is None), (
        "a real position closed with a NULL realized_pnl is invisible to the daily-loss breaker")

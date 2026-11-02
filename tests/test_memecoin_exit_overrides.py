"""Per-position exit-control layer: operator/system overrides + the ExitPolicy fold.

Effective exit = operator override > system template > default. Operator rows may loosen;
target-mcap resolves to an effective % via the entry mcap→price scaling. The exit sweep will
consume `effective_exit_override` (deploy-gated, later); this pins the substrate + the pure fold.
"""
import sqlite3

from sniper.persistence.db_memecoin import (
    ensure_memecoin_tables,
    set_position_exit_override,
    clear_position_exit_override,
    effective_exit_override,
)
from sniper.services.memecoin_policy import ExitPolicy, apply_overrides


def _db():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_memecoin_tables(c)
    return c


def _policy():
    return ExitPolicy(tp_pct=60.0, stop_pct=-50.0, stop_debounce_sweeps=4,
                      derisk_trigger_pct=None, derisk_fraction=0.0, trail_mode="off",
                      max_hold_s=43200.0, mirror_sell_mode="on")


def test_set_and_read_operator_override():
    c = _db()
    set_position_exit_override(c, 101, "operator", tp_pct=30.0, sl_pct=-40.0, actor="me")
    eff = effective_exit_override(c, 101)
    assert eff is not None and eff["source"] == "operator"
    assert eff["tp_pct"] == 30.0 and eff["sl_pct"] == -40.0


def test_operator_wins_over_template():
    c = _db()
    set_position_exit_override(c, 101, "scalp", tp_pct=25.0, sl_pct=-30.0, actor="system")
    set_position_exit_override(c, 101, "operator", tp_pct=80.0, actor="me")   # I overrule
    eff = effective_exit_override(c, 101)
    assert eff["source"] == "operator" and eff["tp_pct"] == 80.0


def test_resetting_same_source_supersedes_not_duplicates():
    c = _db()
    set_position_exit_override(c, 101, "operator", tp_pct=30.0, actor="me")
    set_position_exit_override(c, 101, "operator", tp_pct=45.0, actor="me")   # move it up
    n_active = c.execute("SELECT COUNT(*) FROM memecoin_position_exit_overrides "
                         "WHERE position_id=101 AND source='operator' AND active=1").fetchone()[0]
    assert n_active == 1
    assert effective_exit_override(c, 101)["tp_pct"] == 45.0


def test_clear_falls_back_to_template_then_none():
    c = _db()
    set_position_exit_override(c, 101, "scalp", tp_pct=25.0, actor="system")
    set_position_exit_override(c, 101, "operator", tp_pct=80.0, actor="me")
    clear_position_exit_override(c, 101, "operator")
    assert effective_exit_override(c, 101)["source"] == "scalp"   # template resurfaces
    clear_position_exit_override(c, 101, "scalp")
    assert effective_exit_override(c, 101) is None


def test_apply_overrides_pct_folds_into_policy():
    ov = {"source": "operator", "tp_pct": 30.0, "sl_pct": -35.0,
          "tp_target_mcap": None, "sl_target_mcap": None, "trail_pct": None}
    p = apply_overrides(_policy(), ov, entry_price=0.001, entry_mcap=500_000, current_mcap=600_000)
    assert p.tp_pct == 30.0 and p.stop_pct == -35.0
    assert p.mirror_sell_mode == "on"   # untouched fields preserved


def test_apply_overrides_target_mcap_resolves_to_pct():
    # entry mcap 500k, TP at 1M mcap => +100%; SL at 300k mcap => -40%
    ov = {"source": "operator", "tp_pct": None, "sl_pct": None,
          "tp_target_mcap": 1_000_000, "sl_target_mcap": 300_000, "trail_pct": None}
    p = apply_overrides(_policy(), ov, entry_price=0.001, entry_mcap=500_000, current_mcap=500_000)
    assert abs(p.tp_pct - 100.0) < 1e-6
    assert abs(p.stop_pct - (-40.0)) < 1e-6


def test_apply_overrides_none_is_identity():
    p0 = _policy()
    assert apply_overrides(p0, None, entry_price=0.001, entry_mcap=500_000, current_mcap=500_000) == p0


def test_operator_may_loosen_stop():
    # operator authority: a WIDER stop than default is allowed (their intuition wins)
    ov = {"source": "operator", "tp_pct": None, "sl_pct": -80.0,
          "tp_target_mcap": None, "sl_target_mcap": None, "trail_pct": None}
    p = apply_overrides(_policy(), ov, entry_price=0.001, entry_mcap=500_000, current_mcap=500_000)
    assert p.stop_pct == -80.0   # not clamped to -50 default


def test_choose_entry_template_scalps_big_caps_only():
    from sniper.services.memecoin_policy import choose_entry_template
    from sniper.settings import memecoin as S
    name, ov = choose_entry_template(50_000_000)
    assert name == "scalp"
    assert ov["tp_pct"] == S.MEMECOIN_SCALP_TP_PCT and ov["sl_pct"] == -abs(S.MEMECOIN_SCALP_STOP_PCT)
    assert choose_entry_template(500_000) is None      # microcap -> default rails (rides)
    assert choose_entry_template(None) is None

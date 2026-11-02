"""Operator 2026-10-23, two orders: desk entries are exempt from the 24 h re-entry cooldown
(MEMECOIN_REAL_REENTRY_COOLDOWN_S) and from the losing re-entry block. Each has its own kill knob."""
import inspect

from sniper.services import memecoin_real_leg as leg
from sniper.services import memecoin_risk as risk
from sniper.settings import memecoin as cfg
from test_memecoin_cross_wallet_reentry import LILMET, _ago, _conn, _live_policy, _position  # noqa: F401


def _decide(c, *, cooldown, losing=True):
    ctx = risk.build_memecoin_risk_context(c, usdc_balance=300.0, sol_balance=5.0, day=_ago(0)[:10],
                                           include_reentry_cooldown=cooldown, include_losing_reentry=losing)
    return risk.memecoin_entry_allowed(ctx, mint=LILMET, trade_size=ctx.trade_size_usd)


def test_a_coin_closed_with_a_win_is_enterable_without_the_cooldown():
    c = _conn()
    _position(c, None, pnl=5.0, closed_s_ago=600)
    assert "re-entry" in (_decide(c, cooldown=True).reason or "")                 # today: cooled for 24 h
    assert "re-entry" not in (_decide(c, cooldown=False).reason or "")            # desk: allowed


def test_a_lost_close_is_blocked_by_the_losing_set_unless_it_is_exempt_too():
    c = _conn()
    _position(c, None, pnl=-5.0, closed_s_ago=600)
    decision = _decide(c, cooldown=False)
    assert not decision.allowed and "los" in (decision.reason or "")
    assert "re-entry" not in (_decide(c, cooldown=False, losing=False).reason or "")        # desk: both exempt


def test_only_a_desk_entry_with_its_knob_on_skips_the_losing_block(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_LOSING_REENTRY_EXEMPT", True, raising=False)
    assert leg.losing_reentry_applies("desk") is False and leg.losing_reentry_applies("follow_activity") is True
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_LOSING_REENTRY_EXEMPT", False)
    assert leg.losing_reentry_applies("desk") is True


def test_only_a_desk_entry_with_the_knob_on_skips_the_cooldown(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_REENTRY_COOLDOWN_EXEMPT", True, raising=False)
    assert leg.reentry_cooldown_applies("desk") is False
    assert leg.reentry_cooldown_applies("follow_activity") is True
    assert leg.reentry_cooldown_applies("manual") is True        # manual keeps its own include_reentry_policy=False path
    monkeypatch.setattr(cfg, "MEMECOIN_DESK_REENTRY_COOLDOWN_EXEMPT", False)
    assert leg.reentry_cooldown_applies("desk") is True


def test_both_hops_pass_the_same_switch():
    src = inspect.getsource(leg)
    assert src.count("include_reentry_cooldown=_apply_reentry_cooldown") == 2      # preflight and reservation

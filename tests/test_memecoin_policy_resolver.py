from sniper.services.memecoin_policy import resolve_policy, control_reason
from sniper.settings import memecoin as S


def test_control_policy_matches_live_constants():
    pol, rule = resolve_policy({"mcap": 5e5}, arm="control")
    assert pol.tp_pct == S.MEMECOIN_TAKE_PROFIT_PCT
    assert pol.stop_pct == -abs(S.MEMECOIN_STOP_LOSS_PCT)
    assert rule == "fixed_control_v1"


def test_control_reason_reproduces_live_thresholds():
    pol, _ = resolve_policy({"mcap": 5e5}, arm="control")
    assert control_reason(pol, pol.tp_pct + 1) == "take_profit"
    assert control_reason(pol, pol.stop_pct - 1) == "stop_loss"
    assert control_reason(pol, 0.0) is None


def test_real_arm_uses_real_rails():
    pol, _ = resolve_policy({"mcap": 5e5}, arm="alpha_b")
    assert pol.tp_pct == S.MEMECOIN_REAL_TP_PCT
    assert pol.stop_debounce_sweeps == S.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS


def test_acceptance_control_matches_live_reason_fn():
    from sniper.services.memecoin_brackets import control_exit_reason
    pol, _ = resolve_policy({}, arm="control")
    sl, tp = S.MEMECOIN_STOP_LOSS_PCT, S.MEMECOIN_TAKE_PROFIT_PCT
    grid = [-sl - 20, -sl - 0.1, -sl, -sl + 0.1, 0, tp - 0.1, tp, tp + 20]
    for pnl in grid:
        live = control_exit_reason(pnl, S.MEMECOIN_STOP_LOSS_PCT, S.MEMECOIN_TAKE_PROFIT_PCT)
        assert control_reason(pol, pnl) == live, f"drift at pnl={pnl}: {control_reason(pol,pnl)} != {live}"

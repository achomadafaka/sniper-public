"""Offline regressions for frozen EVM premium shadow measurements."""
import json

import pytest


def test_disabled_premium_cap_shadow_matches_live_cap_off(monkeypatch):
    from sniper.execution.evm_swap_executor import _premium_shadows
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE', 0)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_EXEC_PREMIUM', 0)
    shadow = _premium_shadows({'facts': {'display_price': 1}}, 1.1, 1)
    assert shadow['shadow_display_allow'] is True
    assert shadow['shadow_post_trade_allow_1_2'] is True



def test_premium_fixed_arms_do_not_follow_live_cap_configuration(monkeypatch):
    from sniper.execution.evm_swap_executor import _premium_shadows
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_EXEC_PREMIUM_POST_TRADE', 1.5)
    monkeypatch.setattr(cfg, 'MEMECOIN_REAL_MAX_EXEC_PREMIUM', 0)
    shadow = _premium_shadows({'facts': {'display_price': 1}}, 1.4, 1)
    assert shadow['shadow_post_trade_allow_1_2'] is False
    assert shadow['shadow_post_trade_allow_1_6'] is True
    assert shadow['shadow_display_allow'] is True
    assert shadow['shadow_display_cap'] is None
    assert shadow['shadow_display_reason'] == 'cap_off'
    assert shadow['shadow_live_post_trade_allow'] is True
    assert shadow['shadow_live_post_trade_reason'] == 'cap_off'



@pytest.mark.parametrize('field', ['exact', 'post', 'display'])
@pytest.mark.parametrize('bad', [float('inf'), float('nan'), -1, 0])
def test_nonfinite_or_unusable_premium_shadow_prices_are_unknown(field, bad):
    from sniper.execution.evm_swap_executor import _premium_shadows
    prices = dict(exact=1.1, post=1, display=1)
    prices[field] = bad
    shadow = _premium_shadows({'facts': {'display_price': prices['display']}}, prices['exact'], prices['post'])
    if field in ('exact', 'post'):
        assert shadow['shadow_post_trade_ratio'] is None
        assert shadow['shadow_post_trade_allow_1_2'] is None
    if field in ('exact', 'display'):
        assert shadow['shadow_display_ratio'] is None
        assert shadow['shadow_display_allow'] is None
    json.dumps(shadow, allow_nan=False)

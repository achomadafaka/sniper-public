from sniper.algos.polymarket_crypto5m.maker_regime import (
    MakerRegimeConfig,
    MakerRegimeInventory,
    MakerRegimeSnapshot,
    route_maker_regime,
)


def snap(**kw):
    data = {
        "fair": 0.50,
        "best_bid": 0.49,
        "best_ask": 0.51,
        "seconds_left": 180,
        "jump_flag": False,
        "divergence": 1.0,
        "vol_fast": 0.00002,
        "vol_slow": 0.00002,
        "confirmed_side": None,
        "direction_confidence": 0.0,
        "book_stale": False,
        "book_quarantined": False,
        "fee_conflict": False,
    }
    data.update(kw)
    return MakerRegimeSnapshot(**data)


def inv(**kw):
    data = {"token_inventory": 0.0, "opposite_inventory": 0.0, "max_shares": 50.0}
    data.update(kw)
    return MakerRegimeInventory(**data)


def test_hard_invalid_pulls_all_without_hard_cut():
    p = route_maker_regime(snap(fair=None), inv())
    assert p.regime_id == "hard_invalid"
    assert p.risk_action == "pull_all"
    assert p.bid_enabled is False
    assert p.ask_enabled is False
    assert p.can_hard_cut is False


def test_late_window_beats_jump_and_trend():
    p = route_maker_regime(
        snap(seconds_left=18, jump_flag=True, confirmed_side="Up", direction_confidence=0.8),
        inv(token_inventory=10),
    )
    assert p.regime_id == "late_window"
    assert p.exit_only is True
    assert p.bid_enabled is False
    assert p.ask_enabled is True


def test_inventory_saturated_beats_jump_and_disables_more_bids():
    p = route_maker_regime(snap(jump_flag=True), inv(token_inventory=50.0, max_shares=50.0))
    assert p.regime_id == "inventory_saturated"
    assert p.bid_enabled is False
    assert p.ask_enabled is True
    assert p.risk_action == "reduce_only"


def test_extreme_odds_uses_tiny_reduce_only_policy():
    p = route_maker_regime(snap(fair=0.91, best_bid=0.90, best_ask=0.92), inv(token_inventory=8))
    assert p.regime_id == "extreme_odds"
    assert p.bid_enabled is False
    assert p.ask_enabled is True
    assert p.clip_multiplier < 1.0
    assert p.margin_multiplier > 1.0


def test_severe_extreme_odds_pulls_when_flat():
    p = route_maker_regime(snap(fair=0.98, best_bid=0.97, best_ask=0.99), inv())
    assert p.regime_id == "extreme_odds"
    assert p.risk_action == "pull_all"
    assert p.bid_enabled is False
    assert p.ask_enabled is False


def test_jump_widens_and_shrinks_clip():
    p = route_maker_regime(snap(jump_flag=True, divergence=2.4), inv())
    assert p.regime_id == "jump"
    assert p.margin_multiplier > 1.0
    assert p.clip_multiplier < 1.0
    assert p.quote_ttl_ms < MakerRegimeConfig().base_quote_ttl_ms


def test_trend_confirmed_is_maker_posture_not_taker_signal():
    p = route_maker_regime(snap(confirmed_side="Up", direction_confidence=0.85), inv())
    assert p.regime_id == "trend_confirmed"
    assert p.risk_action == "widen"
    assert p.can_hard_cut is True


def test_low_confidence_trend_does_not_get_aggressive():
    p = route_maker_regime(snap(confirmed_side="Up", direction_confidence=0.52), inv())
    assert p.regime_id == "trend_confirmed"
    assert p.regime_confidence == "low"
    assert p.margin_multiplier > 1.0
    assert p.clip_multiplier < 1.0


def test_mid_chop_is_reduce_only_after_live_bleed():
    p = route_maker_regime(snap(fair=0.51), inv())
    assert p.regime_id == "mid_chop"
    assert p.margin_multiplier > 1.0
    assert p.clip_multiplier <= 0.25
    assert p.max_shares_multiplier <= 0.5
    assert p.risk_action == "pull_all"
    assert p.quote_ttl_ms < MakerRegimeConfig().base_quote_ttl_ms
    assert p.bid_enabled is False
    assert p.ask_enabled is False

    with_inventory = route_maker_regime(snap(fair=0.51), inv(token_inventory=8.0))
    assert with_inventory.regime_id == "mid_chop"
    assert with_inventory.risk_action == "reduce_only"
    assert with_inventory.bid_enabled is False
    assert with_inventory.ask_enabled is True


def test_normal_fallback_for_moderate_non_chop_state():
    p = route_maker_regime(snap(fair=0.68, best_bid=0.66, best_ask=0.70), inv())
    assert p.regime_id == "normal"
    assert p.policy_id == "maker-regime-v1"


def test_inventory_exposure_reports_complete_sets_and_directional_net():
    exposure = inv(token_inventory=30.0, opposite_inventory=12.0).exposure()
    assert exposure["gross_inventory"] == 42.0
    assert exposure["net_directional"] == 18.0
    assert exposure["complete_sets"] == 12.0

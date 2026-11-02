import json

from sniper.algos.polymarket_crypto5m import maker_regime_state as RS


def snapshot(**kw):
    data = {
        "campaign_id": "maker-regime-v1",
        "condition_id": "0xc",
        "window_start_ts": 1_000_000,
        "window_end_ts": 1_000_300,
        "fair": 0.51,
        "best_bid": 0.50,
        "best_ask": 0.52,
        "seconds_left": 120,
        "volatility_state": "calm",
        "direction_state": "unconfirmed",
        "book_health": "healthy",
        "token_inventory": 0.0,
        "opposite_inventory": 0.0,
        "max_shares": 50.0,
    }
    data.update(kw)
    return data


def test_start_episode_builds_market_level_state_with_moment_key():
    moment = RS.build_moment(snapshot(), token_inventory=0.0, max_shares=50.0)
    episode = RS.start_episode(
        campaign_id="maker-regime-v1",
        condition_id="0xc",
        window_start_ts=1_000_000,
        window_end_ts=1_000_300,
        regime_id="mid_chop",
        now_ts=1_000_120,
        moment=moment,
        regime_confidence="medium",
    )

    assert episode.episode_id.startswith("ep_")
    assert episode.current_regime == "mid_chop"
    assert episode.previous_regime is None
    assert episode.dwell_seconds == 0
    assert episode.moment_key["odds_bucket"] == "mid_chop"
    assert "fair" not in episode.moment_key
    assert episode.moment_features["fair"] == 0.51


def test_same_regime_extends_episode_and_dwell():
    moment = RS.build_moment(snapshot(), token_inventory=0.0, max_shares=50.0)
    episode = RS.start_episode("maker-regime-v1", "0xc", 1_000_000, 1_000_300,
                               "mid_chop", 1_000_120, moment)

    updated, transition = RS.advance_episode(
        episode, "mid_chop", now_ts=1_000_128, snapshot=snapshot(), config=RS.RegimeStateConfig()
    )

    assert transition is None
    assert updated.episode_id == episode.episode_id
    assert updated.current_regime == "mid_chop"
    assert updated.dwell_seconds == 8


def test_opportunity_candidate_is_debounced_before_transition():
    moment = RS.build_moment(snapshot(), token_inventory=0.0, max_shares=50.0)
    episode = RS.start_episode("maker-regime-v1", "0xc", 1_000_000, 1_000_300,
                               "mid_chop", 1_000_120, moment)

    updated, transition = RS.advance_episode(
        episode, "normal", now_ts=1_000_121, snapshot=snapshot(fair=0.67),
        config=RS.RegimeStateConfig(transition_debounce_ticks=3, transition_debounce_ms=3_000),
    )

    assert transition is None
    assert updated.current_regime == "mid_chop"
    assert updated.candidate_regime == "normal"
    assert updated.candidate_ticks == 1


def test_emergency_regime_interrupts_immediately():
    moment = RS.build_moment(snapshot(), token_inventory=0.0, max_shares=50.0)
    episode = RS.start_episode("maker-regime-v1", "0xc", 1_000_000, 1_000_300,
                               "mid_chop", 1_000_120, moment)

    updated, transition = RS.advance_episode(
        episode, "hard_invalid", now_ts=1_000_121, snapshot=snapshot(book_health="missing"),
        config=RS.RegimeStateConfig(),
    )

    assert transition is not None
    assert transition.from_regime == "mid_chop"
    assert transition.to_regime == "hard_invalid"
    assert transition.reason == "emergency_interrupt"
    assert updated.current_regime == "hard_invalid"
    assert updated.previous_regime == "mid_chop"


def test_moment_key_is_coarse_and_raw_features_are_separate():
    moment = RS.build_moment(
        snapshot(fair=0.83, best_bid=0.80, best_ask=0.86, seconds_left=18),
        token_inventory=50.0,
        max_shares=50.0,
    )

    assert moment.schema_version == 1
    assert moment.key == {
        "window_phase": "final_seconds",
        "odds_bucket": "extreme_high",
        "volatility_state": "calm",
        "inventory_state": "long_saturated",
        "spread_state": "wide",
        "direction_state": "unconfirmed",
        "book_health": "healthy",
    }
    assert "0.83" not in json.dumps(moment.key, sort_keys=True)
    assert moment.features["fair"] == 0.83
    assert moment.features["spread_price"] == 0.06


def test_profile_decision_is_side_level_and_log_only():
    moment = RS.build_moment(snapshot(), token_inventory=50.0, max_shares=50.0)
    episode = RS.start_episode("maker-regime-v1", "0xc", 1_000_000, 1_000_300,
                               "inventory_saturated", 1_000_120, moment)

    decision = RS.decide_profile(
        episode,
        token="down",
        token_inventory=50.0,
        max_shares=50.0,
        active_policy={"policy_id": "maker-regime-v1", "risk_action": "reduce_only"},
        now_ts=1_000_121,
    )

    assert decision.profile_decision_id.startswith("pd_")
    assert decision.episode_id == episode.episode_id
    assert decision.token == "down"
    assert decision.profile_id == "reduce_only"
    assert decision.profile_mode == "log_only"
    assert decision.base_policy_id == "maker-regime-v1"

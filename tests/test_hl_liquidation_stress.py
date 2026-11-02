from sniper.algos.hl_microstructure.liquidation import stress_from_positions


def test_stress_from_positions_scores_near_long_liquidation_as_short_pressure():
    rows = [{
        "asset": "SOL",
        "side": "long",
        "notional_usdc": 250_000,
        "distance_to_liquidation_pct": 0.8,
        "margin_mode": "isolated",
        "updated_ts_ms": 10_000,
    }]

    stress = stress_from_positions(rows, now_ms=11_000)

    assert stress["SOL"].direction == "short"
    assert stress["SOL"].score > 0.5
    assert stress["SOL"].notional_at_risk == 250_000
    assert stress["SOL"].stale is False


def test_stress_from_positions_rejects_stale_and_cross_margin_rows():
    rows = [
        {"asset": "BTC", "side": "short", "notional_usdc": 1_000_000, "distance_to_liquidation_pct": 0.5, "margin_mode": "isolated", "updated_ts_ms": 1_000},
        {"asset": "ETH", "side": "long", "notional_usdc": 1_000_000, "distance_to_liquidation_pct": 0.5, "margin_mode": "cross", "updated_ts_ms": 10_000},
    ]

    stress = stress_from_positions(rows, now_ms=700_000, max_age_ms=300_000)

    assert stress == {}

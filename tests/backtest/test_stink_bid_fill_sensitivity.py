from scripts.stink_bid_fill_sensitivity import summarize_audit_events


def test_summarize_audit_events_counts_strict_loose_and_price_cross():
    audit_events = [
        {
            "status": "filled",
            "reason": "runner_parity_strict_fill",
            "condition_id": "a",
            "pnl_usdc": 10.0,
            "loose_fills_seen": 1,
        },
        {
            "status": "no_fill",
            "reason": "only_loose_fills",
            "condition_id": "b",
            "loose_fills_seen": 2,
        },
        {
            "status": "no_fill",
            "reason": "no_fill_before_cutoff",
            "condition_id": "c",
            "loose_fills_seen": 0,
        },
    ]

    out = summarize_audit_events(audit_events)

    assert out["markets"] == 3
    assert out["strict_fills"] == 1
    assert out["loose_only_markets"] == 1
    assert out["price_cross_or_loose_markets"] == 2
    assert out["strict_pnl_usdc"] == 10.0


def test_summarize_audit_events_flags_gap_between_loose_and_strict():
    audit_events = [
        {"status": "no_fill", "reason": "only_loose_fills", "condition_id": "a", "loose_fills_seen": 1},
        {"status": "no_fill", "reason": "only_loose_fills", "condition_id": "b", "loose_fills_seen": 1},
        {"status": "filled", "reason": "runner_parity_strict_fill", "condition_id": "c", "pnl_usdc": -25},
    ]

    out = summarize_audit_events(audit_events)

    assert out["loose_to_strict_ratio"] == 3.0
    assert out["strict_fill_rate"] == 1 / 3

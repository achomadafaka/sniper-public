"""Far-whale magnet detector — §2 side semantics + §3 candidate gate (fwlm-v0)."""
from sniper.hyperlive import far_whale as FW


def _state(coin="ETH", liq="2950", szi="100", entry="3010"):
    return {"assetPositions": [{"position": {
        "coin": coin, "liquidationPx": liq, "szi": szi, "entryPx": entry}}]}


def test_whale_rows_extracts_position_side_from_szi_sign():
    rows = FW.whale_rows_from_clearinghouse(_state(szi="100"), "0xabc")
    assert len(rows) == 1
    r = rows[0]
    assert r["address"] == "0xabc"
    assert r["asset"] == "ETH"
    assert r["position_side"] == "long"
    assert r["liquidation_price"] == 2950.0
    assert r["position_notional"] == 100 * 3010.0
    short = FW.whale_rows_from_clearinghouse(_state(szi="-100"), "0xabc")[0]
    assert short["position_side"] == "short"
    assert short["position_notional"] == 100 * 3010.0   # abs(szi)


def test_whale_rows_skips_zero_or_missing():
    assert FW.whale_rows_from_clearinghouse({}, "0xabc") == []
    assert FW.whale_rows_from_clearinghouse(_state(liq=None), "0xabc") == []
    assert FW.whale_rows_from_clearinghouse(_state(szi="0"), "0xabc") == []


def _row(side="long", liq=2950.0, notional=2_000_000.0):
    szi = 100.0 if side == "long" else -100.0
    return {"address": "0xabc", "asset": "ETH", "liquidation_price": liq,
            "szi": szi, "position_side": side, "entry_px": 3010.0,
            "position_notional": notional}


def test_long_liquidation_is_below_mid_direction_down():
    # long liquidates BELOW -> direction down -> long_liquidation (§2 invariant)
    cand, reason = FW.evaluate_candidate(
        _row("long", liq=2950.0), 3000.0, spread_bps=2.0, staleness_ms=0,
        threshold_notional=1_000_000.0)
    assert reason is None
    assert cand["direction_to_liquidation"] == "down"
    assert cand["liquidation_type"] == "long_liquidation"
    assert round(cand["distance_bps"], 1) == round(50 / 3000 * 1e4, 1)  # ~166.7


def test_short_liquidation_is_above_mid_direction_up():
    cand, reason = FW.evaluate_candidate(
        _row("short", liq=3050.0), 3000.0, spread_bps=2.0, staleness_ms=0,
        threshold_notional=1_000_000.0)
    assert reason is None
    assert cand["direction_to_liquidation"] == "up"
    assert cand["liquidation_type"] == "short_liquidation"


def test_side_invariant_violation_dropped_not_coerced():
    # a "long" whose liq sits ABOVE mid is a source/schema anomaly (§2) -> drop
    cand, reason = FW.evaluate_candidate(
        _row("long", liq=3050.0), 3000.0, spread_bps=2.0, staleness_ms=0,
        threshold_notional=1_000_000.0)
    assert cand is None and reason == "anomaly_side_invariant"


def test_gate_distance_band_50_to_300():
    near, r1 = FW.evaluate_candidate(_row("long", liq=2999.0), 3000.0,
                                     spread_bps=2.0, staleness_ms=0,
                                     threshold_notional=1_000_000.0)
    far, r2 = FW.evaluate_candidate(_row("long", liq=2800.0), 3000.0,
                                    spread_bps=2.0, staleness_ms=0,
                                    threshold_notional=1_000_000.0)
    assert near is None and r1 == "out_of_band"     # ~3.3 bps < 50
    assert far is None and r2 == "out_of_band"      # ~667 bps > 300


def test_gate_threshold_spread_staleness_fail_closed():
    small, r1 = FW.evaluate_candidate(_row(notional=500_000.0), 3000.0,
                                      spread_bps=2.0, staleness_ms=0,
                                      threshold_notional=1_000_000.0)
    wide, r2 = FW.evaluate_candidate(_row(), 3000.0, spread_bps=6.0,
                                     staleness_ms=0, threshold_notional=1_000_000.0)
    nospread, r3 = FW.evaluate_candidate(_row(), 3000.0, spread_bps=None,
                                         staleness_ms=0, threshold_notional=1_000_000.0)
    stale, r4 = FW.evaluate_candidate(_row(), 3000.0, spread_bps=2.0,
                                      staleness_ms=6000, threshold_notional=1_000_000.0)
    assert (small, r1) == (None, "below_threshold")
    assert (wide, r2) == (None, "spread_wide")
    assert (nospread, r3) == (None, "spread_missing")   # unknown spread -> fail closed
    assert (stale, r4) == (None, "stale")


def test_frozen_params_match_spec_v0():
    assert FW.STUDY_VERSION == "fwlm-v0"
    assert (FW.DIST_MIN_BPS, FW.DIST_MAX_BPS) == (50.0, 300.0)
    assert (FW.TARGET_FRAC, FW.ADVERSE_FRAC) == (0.75, 0.50)
    assert FW.HORIZON_MS == 30 * 60_000
    assert FW.MIGRATE_MIN_FRAC == 0.25
    assert FW.MAX_EVENT_SHARE == 0.40
    assert FW.EPISODE_LIQ_TOL_BPS == 25.0
    assert FW.EPISODE_GAP_MS == 3_600_000
    assert FW.CAMPAIGN_CAP_DAYS == 60
    assert 8.5 < FW.FEE_BPS < 9.5


def test_side_invariant_short_below_mid_dropped():
    # a "short" whose liq sits BELOW mid is a source/schema anomaly (§2) -> drop, not coerce
    # (the symmetric partner of the long-above-mid case; closes the invariant's other corner)
    cand, reason = FW.evaluate_candidate(
        _row("short", liq=2950.0), 3000.0, spread_bps=2.0, staleness_ms=0,
        threshold_notional=1_000_000.0)
    assert cand is None and reason == "anomaly_side_invariant"

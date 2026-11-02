"""Phase 1D #2 — frozen forward-paper rules. Pure: a known-at-time ctx -> (side, reason). Immutable after
launch (manifest hash). Controls C0/C1; candidates R1/R2/R3. No Price-to-Beat rule. Binance is a predictor."""
import math

from sniper.algos.polymarket_crypto5m import forward_rules as FR


def test_manifest_hash_is_stable_and_frozen():
    assert FR.manifest_hash() == FR.manifest_hash()                # deterministic
    assert len(FR.manifest_hash()) == 64
    assert FR.manifest_hash() == FR.EXPECTED_MANIFEST_HASH         # frozen — any rule change must be a new version
    assert FR.CAMPAIGN_ID == "btc5m-forward-paper-v0"
    assert FR.CONFIG["primary_checkpoint_s"] == 240 and FR.CONFIG["latency_ms"] == 500


def test_c0_random_side_deterministic_and_balanced():
    a = FR.evaluate("C0", {"condition_id": "0xabc"})
    assert a[0] in ("Up", "Down")
    assert FR.evaluate("C0", {"condition_id": "0xabc"}) == a       # reproducible by (campaign, condition)
    sides = {FR.evaluate("C0", {"condition_id": f"0x{i:04x}"})[0] for i in range(50)}
    assert sides == {"Up", "Down"}                                 # both sides occur


def test_c1_favorite_by_midpoint():
    assert FR.evaluate("C1", {"up_midpoint": 0.62, "down_midpoint": 0.40})[0] == "Up"
    assert FR.evaluate("C1", {"up_midpoint": 0.40, "down_midpoint": 0.62})[0] == "Down"
    assert FR.evaluate("C1", {"up_midpoint": 0.50, "down_midpoint": 0.50})[0] is None   # tie
    assert FR.evaluate("C1", {"up_midpoint": None, "down_midpoint": 0.5})[0] is None     # unavailable


def test_r1_binance_30s_momentum():
    assert FR.evaluate("R1", {"binance_price_now": 101.0, "binance_price_30s_ago": 100.0})[0] == "Up"
    assert FR.evaluate("R1", {"binance_price_now": 99.0, "binance_price_30s_ago": 100.0})[0] == "Down"
    assert FR.evaluate("R1", {"binance_price_now": 100.0, "binance_price_30s_ago": 100.0})[0] is None  # flat
    assert FR.evaluate("R1", {"binance_price_now": None, "binance_price_30s_ago": 100.0})[0] is None   # missing


def test_r2_book_imbalance_thresholds():
    # up heavily bid, down heavily ask -> delta >= 0.20 -> Up
    up = FR.evaluate("R2", {"up_bid_notional": 800, "up_ask_notional": 200,
                            "down_bid_notional": 200, "down_ask_notional": 800})
    assert up[0] == "Up"
    dn = FR.evaluate("R2", {"up_bid_notional": 200, "up_ask_notional": 800,
                            "down_bid_notional": 800, "down_ask_notional": 200})
    assert dn[0] == "Down"
    flat = FR.evaluate("R2", {"up_bid_notional": 500, "up_ask_notional": 500,
                              "down_bid_notional": 500, "down_ask_notional": 500})
    assert flat[0] is None                                         # delta 0 < 0.20
    assert FR.evaluate("R2", {"up_bid_notional": None})[0] is None  # missing


def test_r3_agreement():
    up_ctx = {"binance_price_now": 101.0, "binance_price_30s_ago": 100.0,
              "up_bid_notional": 800, "up_ask_notional": 200,
              "down_bid_notional": 200, "down_ask_notional": 800}
    assert FR.evaluate("R3", up_ctx)[0] == "Up"                    # R1 Up + R2 Up -> Up
    disagree = {**up_ctx, "binance_price_now": 99.0}               # R1 Down, R2 Up
    assert FR.evaluate("R3", disagree)[0] is None


def test_all_rule_ids_present():
    assert set(FR.RULE_IDS) == {"C0", "C1", "R1", "R2", "R3"}
    for rid in FR.RULE_IDS:
        side, reason = FR.evaluate(rid, {"condition_id": "0x1"})  # sparse ctx -> graceful
        assert side in ("Up", "Down", None) and isinstance(reason, str)


def test_no_price_to_beat_rule_present():
    blob = str(FR.RULES).lower()
    assert "price_to_beat" not in blob and "distance" not in blob and "threshold_diffusion" not in blob

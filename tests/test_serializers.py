import unittest

from sniper.web.serializers import serialize_position_signal_row, serialize_wallet_row


class SerializerTests(unittest.TestCase):
    def test_serialize_wallet_row_common_fields(self) -> None:
        row = {
            "wallet": "0xabc",
            "position_count": 3,
            "pnl": 100.0,
            "max_position_value": 500.0,
            "top_open_position_title": "Top",
            "top_open_position_value": 450.0,
            "total_open_value": 900.0,
            "trade_count": 10,
            "low_buy_hits": 4,
            "low_buy_hits_14d": 2,
            "suspicion_score": 20.0,
            "insider_score": 70.0,
            "speculator_score": 15.0,
            "classification": "insider_candidate",
            "win_rate": 0.8,
            "closed_sample_size": 25,
            "high_winrate_qualified": 1,
            "account_age_days": 11.0,
            "is_fresh_account": 1,
            "position_signal_count": 2,
            "position_insider_candidate_count": 1,
            "position_speculator_count": 0,
            "max_position_insider_score": 74.0,
            "max_position_speculator_score": 10.0,
            "tier": "watch",
            "strategy_group": "single_thesis",
            "strategy_confidence": 80.0,
            "strategy_note": "note",
            "last_title": "Last",
        }
        identity = {"username": "user", "pseudonym": "pseudo", "profile_image": "img"}
        item = serialize_wallet_row(row, identity, extra_fields={"reason": "x"})
        self.assertEqual(item["wallet"], "0xabc")
        self.assertEqual(item["username"], "user")
        self.assertEqual(item["reason"], "x")
        self.assertIn("insider_score", item)
        self.assertIn("strategy_group", item)
        expected_keys = {
            "wallet",
            "username",
            "pseudonym",
            "profile_image",
            "position_count",
            "pnl",
            "max_position_value",
            "top_open_position_title",
            "top_open_position_value",
            "total_open_value",
            "trade_count",
            "avg_trades_per_position",
            "event_slug",
            "low_buy_hits",
            "low_buy_hits_14d",
            "suspicion_score",
            "insider_score",
            "speculator_score",
            "classification",
            "win_rate",
            "wins",
            "losses",
            "closed_sample_size",
            "high_winrate_qualified",
            "account_age_days",
            "is_fresh_account",
            "position_signal_count",
            "position_insider_candidate_count",
            "position_speculator_count",
            "max_position_insider_score",
            "max_position_speculator_score",
            "tier",
            "strategy_group",
            "strategy_confidence",
            "strategy_note",
            "last_title",
            "reason",
            "total_closed_count",
            "resolution_rate",
            "effective_resolved_wins",
            "effective_resolved_losses",
            "roi_percent",
            "qualifying_roi",
            "qualifying_pnl",
            "qualifying_capital",
            "wallet_edge_tier",
            "followability_cached",
            "wallet_quality_score",
            "wallet_quality_eligible",
            "wallet_quality_fail_reason",
            "market_maker_detected",
        }
        self.assertEqual(set(item.keys()), expected_keys)

    def test_serialize_position_signal_row_fields(self) -> None:
        row = {
            "wallet": "0xabc",
            "condition_id": "c1",
            "outcome": "Yes",
            "title": "Market",
            "event_slug": "event",
            "is_open": 1,
            "position_value": 2500,
            "position_share": 0.3,
            "position_count_wallet": 2,
            "entry_anchor_ts": 1770768000,
            "entry_price": 0.45,
            "entry_size": 1000,
            "entry_notional": 450,
            "current_price": 0.55,
            "current_size": 1000,
            "latest_trade_ts": 1770771600,
            "buys_5m": 1,
            "buys_1h": 2,
            "buys_24h": 3,
            "trades_5m": 1,
            "trades_1h": 2,
            "trades_24h": 4,
            "sells_5m": 0,
            "sells_1h": 1,
            "sells_24h": 1,
            "notional_5m": 20.0,
            "notional_1h": 60.0,
            "notional_24h": 120.0,
            "sell_notional_5m": 0.0,
            "sell_notional_1h": 10.0,
            "sell_notional_24h": 10.0,
            "buys_since_entry": 3,
            "sells_since_entry": 1,
            "buy_notional_since_entry": 250.0,
            "sell_notional_since_entry": 50.0,
            "net_notional_24h": 110.0,
            "net_notional_since_entry": 200.0,
            "last_trade_side": "BUY",
            "activity_state": "accumulating",
            "hold_hours": 2.5,
            "unique_sizes_5m": 1,
            "max_trades_60s": 2,
            "position_insider_score": 72.0,
            "position_speculator_score": 18.0,
            "position_classification": "position_insider_candidate",
            "signal_reason": "strong",
            "analyzed_ts": 1770771700,
        }
        identity = {"username": "user", "pseudonym": "pseudo", "profile_image": "img"}
        analysis = {
            "win_rate": 0.7,
            "closed_sample_size": 20,
            "account_age_days": 30.0,
            "is_fresh_account": 1,
            "tier": "watch",
            "classification": "insider_candidate",
            "position_count": 2,
            "strategy_group": "single_thesis",
            "strategy_confidence": 85,
        }
        followed = {"added_source": "manual", "label": "focus", "added_ts": 1770768000}
        item = serialize_position_signal_row(row, identity, analysis, followed_row=followed)
        self.assertEqual(item["wallet"], "0xabc")
        self.assertEqual(item["classification"], "position_insider_candidate")
        self.assertEqual(item["wallet_classification"], "insider_candidate")
        self.assertTrue(item["is_followed_position"])
        self.assertIn("entry_price", item)
        self.assertIn("current_price", item)
        expected_keys = {
            "wallet",
            "username",
            "pseudonym",
            "profile_image",
            "condition_id",
            "outcome",
            "title",
            "event_slug",
            "market_title",
            "market_event_slug",
            "market_end_date_iso",
            "market_volume_24h",
            "market_is_open",
            "is_open",
            "position_value",
            "position_share",
            "position_count_wallet",
            "entry_anchor_ts",
            "entry_anchor_at",
            "entry_price",
            "entry_size",
            "entry_notional",
            "current_price",
            "current_size",
            "current_amount",
            "latest_trade_ts",
            "latest_trade_at",
            "buys_5m",
            "buys_1h",
            "buys_24h",
            "trades_5m",
            "trades_1h",
            "trades_24h",
            "sells_5m",
            "sells_1h",
            "sells_24h",
            "notional_5m",
            "notional_1h",
            "notional_24h",
            "sell_notional_5m",
            "sell_notional_1h",
            "sell_notional_24h",
            "buys_since_entry",
            "sells_since_entry",
            "buy_notional_since_entry",
            "sell_notional_since_entry",
            "net_notional_24h",
            "net_notional_since_entry",
            "last_trade_side",
            "activity_state",
            "position_action",
            "hold_hours",
            "unique_sizes_5m",
            "max_trades_60s",
            "position_insider_score",
            "position_speculator_score",
            "position_classification",
            "signal_reason",
            "analyzed_ts",
            "analyzed_at",
            "is_followed_position",
            "followed_position_source",
            "followed_position_label",
            "followed_position_added_ts",
            "followed_position_added_at",
            "insider_score",
            "speculator_score",
            "classification",
            "win_rate",
            "wins",
            "losses",
            "closed_sample_size",
            "total_closed_count",
            "resolution_rate",
            "roi_percent",
            "account_age_days",
            "is_fresh_account",
            "wallet_tier",
            "wallet_classification",
            "wallet_position_count",
            "wallet_strategy_group",
            "wallet_strategy_confidence",
            "tier",
            "strategy_group",
            "end_date_iso",
            "qualifying_roi",
            "qualifying_pnl",
            "qualifying_capital",
            "paper_trade_outcome",
            "paper_trade_roi",
            "paper_trade_closed_at",
            "effective_copy_fraction",
            "wallet_numeric_tier",
            "wallet_confidence_score",
            "insider_score_trend",
            "wallet_quality_score",
            "wallet_quality_eligible",
            "wallet_quality_fail_reason",
            "market_maker_detected",
        }
        self.assertEqual(set(item.keys()), expected_keys)


class TestPositionAction(unittest.TestCase):
    def test_new_position(self):
        from sniper.web.serializers import _compute_position_action
        result = _compute_position_action(
            entry_price=0.10, current_price=0.12,
            buys_since_entry=2, sell_notional_since_entry=0,
            hold_hours=1.0, max_trades_60s=1,
        )
        self.assertEqual(result, "new_position")

    def test_twap_accumulation_down(self):
        from sniper.web.serializers import _compute_position_action
        result = _compute_position_action(
            entry_price=0.12, current_price=0.075,
            buys_since_entry=291, sell_notional_since_entry=0,
            hold_hours=4.45, max_trades_60s=7,
        )
        self.assertEqual(result, "twap_accumulation_down")

    def test_twap_new_build(self):
        from sniper.web.serializers import _compute_position_action
        result = _compute_position_action(
            entry_price=0.10, current_price=0.11,
            buys_since_entry=50, sell_notional_since_entry=0,
            hold_hours=2.0, max_trades_60s=5,
        )
        self.assertEqual(result, "twap_new_build")

    def test_position_building(self):
        from sniper.web.serializers import _compute_position_action
        result = _compute_position_action(
            entry_price=0.20, current_price=0.22,
            buys_since_entry=15, sell_notional_since_entry=0,
            hold_hours=48.0, max_trades_60s=1,
        )
        self.assertEqual(result, "position_building")

    def test_adding(self):
        from sniper.web.serializers import _compute_position_action
        result = _compute_position_action(
            entry_price=0.30, current_price=0.32,
            buys_since_entry=6, sell_notional_since_entry=0,
            hold_hours=24.0, max_trades_60s=1,
        )
        self.assertEqual(result, "adding")

    def test_zero_entry_price(self):
        from sniper.web.serializers import _compute_position_action
        result = _compute_position_action(
            entry_price=0, current_price=0.10,
            buys_since_entry=100, sell_notional_since_entry=0,
            hold_hours=1.0, max_trades_60s=10,
        )
        self.assertEqual(result, "new_position")


if __name__ == "__main__":
    unittest.main()

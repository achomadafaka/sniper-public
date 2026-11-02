"""
Tests for thesis-alert and heartbeat behavior in alerter.py.
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import alerter


class TestThesisAlertFormatting(unittest.TestCase):
    def test_dedupe_key_is_stable(self):
        item = {
            "wallet": "0xAbCd",
            "condition_id": "COND-1",
            "outcome": "YES",
        }
        key = alerter._thesis_signal_dedupe_key(item)
        self.assertEqual(key, "thesis_signal_sent:0xabcd:cond-1:yes")

    def test_watch_alert_contains_required_fields(self):
        item = {
            "wallet": "0x1234567890abcdef1234567890abcdef12345678",
            "condition_id": "0xcond",
            "outcome": "yes",
            "market_title": "Will X happen?",
            "market_category": "politics",
            "market_end_date_iso": "2030-01-01T00:00:00Z",
            "market_event_slug": "will-x-happen",
            "entry_price": 0.25,
            "position_value": 12500,
            "open_positions_for_wallet": 1,
            "closed_sample_size": 12,
            "win_rate": 0.75,
            "account_age_days": 8,
            "high_conviction": False,
        }
        payload = {
            "min_position_value": 10000,
            "high_conviction_min_position_value": 100000,
        }
        msg = alerter._format_thesis_signal_alert(item, payload)
        self.assertIn("THESIS SIGNAL", msg)
        self.assertIn("WATCH", msg)
        self.assertIn("politics", msg)
        self.assertIn("1 open", msg)
        self.assertIn("polymarket\\.com/event/will\\-x\\-happen", msg)

    def test_high_conviction_alert_tier(self):
        item = {
            "wallet": "0x1234567890abcdef1234567890abcdef12345678",
            "condition_id": "0xcond",
            "outcome": "yes",
            "market_title": "Will Y happen?",
            "market_category": "sports",
            "market_end_date_iso": "2030-01-01T00:00:00Z",
            "entry_price": 0.4,
            "position_value": 220000,
            "open_positions_for_wallet": 1,
            "closed_sample_size": 4,
            "win_rate": 0.5,
            "account_age_days": 10,
            "high_conviction": True,
        }
        payload = {
            "min_position_value": 10000,
            "high_conviction_min_position_value": 100000,
        }
        msg = alerter._format_thesis_signal_alert(item, payload)
        self.assertIn("HIGH CONVICTION", msg)
        self.assertIn("$100,000", msg)


class TestThesisHeartbeat(unittest.TestCase):
    def test_should_send_heartbeat_after_hour_when_not_sent_today(self):
        now = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)
        with patch.object(alerter, "THESIS_HEARTBEAT_ENABLED", True), \
             patch.object(alerter, "THESIS_HEARTBEAT_HOUR_UTC", 9), \
             patch.object(alerter, "_get_watermark_raw", return_value=None):
            self.assertTrue(alerter._should_send_thesis_heartbeat(now))

    def test_should_not_send_heartbeat_twice_same_day(self):
        now = datetime(2026, 3, 1, 10, 0, tzinfo=timezone.utc)

        def fake_get_watermark_raw(key: str):
            if key == alerter.ALERT_WATERMARK_KEY_THESIS_HEARTBEAT_YMD:
                return 20260301
            return None

        with patch.object(alerter, "THESIS_HEARTBEAT_ENABLED", True), \
             patch.object(alerter, "THESIS_HEARTBEAT_HOUR_UTC", 9), \
             patch.object(alerter, "_get_watermark_raw", side_effect=fake_get_watermark_raw):
            self.assertFalse(alerter._should_send_thesis_heartbeat(now))

    def test_heartbeat_message_contains_counts_and_next_check(self):
        strict_payload = {"count": 2, "high_conviction_count": 1}
        lookahead_payload = {
            "items": [
                {"condition_id": "a"},
                {"condition_id": "b"},
                {"condition_id": "a"},
            ]
        }
        next_check = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
        msg = alerter._format_thesis_heartbeat_message(
            strict_payload,
            lookahead_payload,
            next_check_at_utc=next_check,
        )
        self.assertIn("Thesis detector alive", msg)
        self.assertIn("Strict signals today: 2", msg)
        self.assertIn("High conviction today: 1", msg)
        self.assertIn("Markets resolving in 5d", msg)
        self.assertIn("2", msg)  # distinct market count
        self.assertIn("2026\\-03\\-01 12:00:00 UTC", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)

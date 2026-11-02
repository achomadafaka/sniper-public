import os
import tempfile
import unittest

from db import get_connection, init_db
from scripts.ops.health_check import get_telegram_health_summary


class TestTelegramHealthSummary(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.conn = get_connection(self.tmp.name)
        init_db(self.conn)

    def tearDown(self):
        self.conn.close()
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    def test_recent_telegram_alert_marks_ok_without_token(self):
        self.conn.execute(
            """
            INSERT INTO alerts_sent (alert_type, wallet, message, channel, sent_at)
            VALUES ('test_alert', '0xabc', 'hello', 'telegram', datetime('now', '-1 hour'))
            """
        )
        self.conn.commit()
        old = os.environ.get("TELEGRAM_BOT_TOKEN")
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            result = get_telegram_health_summary(self.tmp.name)
        finally:
            if old is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = old
        self.assertEqual(result["status"], "ok")
        self.assertIn("Recent Telegram alert:", result["detail"])

    def test_missing_token_and_no_recent_alerts_warns(self):
        old = os.environ.get("TELEGRAM_BOT_TOKEN")
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            result = get_telegram_health_summary(self.tmp.name)
        finally:
            if old is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = old
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["detail"], "Check Telegram config")

    def test_token_without_chat_id_warns(self):
        old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        old_chat = os.environ.get("TELEGRAM_CHAT_ID")
        os.environ["TELEGRAM_BOT_TOKEN"] = "test-token"
        os.environ.pop("TELEGRAM_CHAT_ID", None)
        try:
            result = get_telegram_health_summary(self.tmp.name)
        finally:
            if old_token is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = old_token
            else:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            if old_chat is not None:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat
            else:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["detail"], "Check Telegram config")

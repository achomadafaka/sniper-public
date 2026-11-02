import sqlite3
import time
import unittest

from db import get_market_outcome, init_db
from sniper.services.market_outcome_poller import (
    _iter_missing_lbt_condition_ids,
    _market_outcome_rows,
    _parse_any_ts,
    backfill_lbt_outcomes_once,
)


class MarketOutcomePollerTests(unittest.TestCase):
    def test_parse_any_ts_handles_iso_and_epoch(self) -> None:
        now = int(time.time())
        self.assertGreater(_parse_any_ts("2026-03-01T12:00:00Z"), 0)
        self.assertEqual(_parse_any_ts(now), now)
        self.assertEqual(_parse_any_ts(now * 1000), now)

    def test_market_outcome_rows_uses_winner_flag(self) -> None:
        market = {
            "closed": True,
            "condition_id": "0xabc",
            "closed_time": "2026-03-01T12:00:00Z",
            "tokens": [
                {"outcome": "Yes", "winner": True, "price": 1},
                {"outcome": "No", "winner": False, "price": 0},
            ],
        }
        rows = _market_outcome_rows(market)
        self.assertEqual(len(rows), 2)
        by_outcome = {row["outcome"]: row for row in rows}
        self.assertEqual(by_outcome["yes"]["resolved_price"], 1.0)
        self.assertEqual(by_outcome["no"]["resolved_price"], 0.0)

    def test_market_outcome_rows_fallbacks_to_price_thresholds(self) -> None:
        market = {
            "closed": True,
            "condition_id": "0xdef",
            "tokens": [
                {"outcome": "Yes", "price": 0.9999},
                {"outcome": "No", "price": 0.0001},
                {"outcome": "Maybe", "price": 0.55},
            ],
        }
        rows = _market_outcome_rows(market)
        self.assertEqual(len(rows), 2)
        by_outcome = {row["outcome"]: row for row in rows}
        self.assertEqual(by_outcome["yes"]["resolved_price"], 1.0)
        self.assertEqual(by_outcome["no"]["resolved_price"], 0.0)
        self.assertNotIn("maybe", by_outcome)

    def test_iter_missing_lbt_condition_ids_excludes_existing_outcomes(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            """
            INSERT INTO low_buy_trades
              (transaction_hash, proxy_wallet, condition_id, side, price, size, timestamp, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("tx-1", "w1", "0xabc", "BUY", 0.4, 100, int(time.time()), "Yes"),
        )
        conn.execute(
            """
            INSERT INTO low_buy_trades
              (transaction_hash, proxy_wallet, condition_id, side, price, size, timestamp, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("tx-2", "w1", "0xdef", "BUY", 0.4, 100, int(time.time()), "No"),
        )
        conn.execute(
            """
            INSERT INTO market_outcomes (condition_id, outcome, resolved_price, resolved_at)
            VALUES (?, ?, ?, ?)
            """,
            ("0xdef", "no", 1.0, int(time.time())),
        )
        conn.commit()
        missing = list(_iter_missing_lbt_condition_ids(conn))
        self.assertEqual(missing, ["0xabc"])
        conn.close()

    def test_backfill_lbt_outcomes_once_upserts_closed_market(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_db(conn)
        conn.execute(
            """
            INSERT INTO low_buy_trades
              (transaction_hash, proxy_wallet, condition_id, side, price, size, timestamp, outcome)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("tx-3", "w2", "0xfeed", "BUY", 0.42, 120, int(time.time()), "Yes"),
        )
        conn.commit()

        class _Resp:
            status_code = 200

            def __init__(self, payload):
                self._payload = payload
                self.content = b"1"

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        class _Session:
            def get(self, url, timeout):
                self.last_url = url
                return _Resp(
                    {
                        "closed": True,
                        "condition_id": "0xfeed",
                        "closed_time": "2026-03-01T12:00:00Z",
                        "tokens": [
                            {"outcome": "Yes", "winner": True, "price": 1},
                            {"outcome": "No", "winner": False, "price": 0},
                        ],
                    }
                )

        stats = backfill_lbt_outcomes_once(conn, _Session())
        self.assertEqual(stats["candidate_condition_ids"], 1)
        self.assertEqual(stats["looked_up"], 1)
        self.assertEqual(stats["closed_markets"], 1)
        self.assertEqual(stats["outcomes_upserted"], 2)

        row_yes = get_market_outcome(conn, "0xfeed", "yes")
        row_no = get_market_outcome(conn, "0xfeed", "no")
        self.assertIsNotNone(row_yes)
        self.assertIsNotNone(row_no)
        self.assertEqual(float(row_yes["resolved_price"]), 1.0)
        self.assertEqual(float(row_no["resolved_price"]), 0.0)
        conn.close()


if __name__ == "__main__":
    unittest.main()

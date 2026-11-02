import os, sys, sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sniper.persistence.db_utils import generate_ws_synthetic_hash, is_synthetic_ws_hash

class TestSyntheticHash:
    def test_format_starts_with_ws_prefix(self):
        h = generate_ws_synthetic_hash("0xabc", "cond1", "YES", "BUY", "BUY", 1700000000123, 0.45, 100.0)
        assert h.startswith("ws:")
        assert len(h) == 3 + 40  # "ws:" + 40-char sha1 hex

    def test_deterministic(self):
        args = ("0xabc", "cond1", "YES", "BUY", "BUY", 1700000000123, 0.45, 100.0)
        assert generate_ws_synthetic_hash(*args) == generate_ws_synthetic_hash(*args)

    def test_millisecond_precision_matters(self):
        h1 = generate_ws_synthetic_hash("0xabc", "cond1", "YES", "BUY", "BUY", 1700000000000, 0.45, 100.0)
        h2 = generate_ws_synthetic_hash("0xabc", "cond1", "YES", "BUY", "BUY", 1700000000001, 0.45, 100.0)
        assert h1 != h2

    def test_is_synthetic_true(self):
        assert is_synthetic_ws_hash("ws:abc123") is True

    def test_is_synthetic_false_real_hash(self):
        assert is_synthetic_ws_hash("0xdeadbeef") is False

    def test_is_synthetic_false_empty(self):
        assert is_synthetic_ws_hash("") is False


# ---------------------------------------------------------------------------
# Reconciliation test helpers
# ---------------------------------------------------------------------------

from sniper.persistence.db_utils import _normalize_wallet
from sniper.persistence.db_followed import upsert_followed_wallet_activity
from db import init_db


def _get_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_db(conn)
    return conn


def _ws_activity(wallet, condition_id, outcome, side, price, size, ts_ms):
    return {
        "transactionHash": generate_ws_synthetic_hash(
            wallet, condition_id, outcome, side, "BUY", ts_ms, price, size
        ),
        "conditionId": condition_id,
        "type": "BUY",
        "side": side,
        "outcome": outcome,
        "price": price,
        "size": size,
        "timestamp": ts_ms,
    }


def _real_activity(tx_hash, condition_id, outcome, side, price, size, ts_ms, **extra):
    d = {
        "transactionHash": tx_hash,
        "conditionId": condition_id,
        "type": "BUY",
        "side": side,
        "outcome": outcome,
        "price": price,
        "size": size,
        "timestamp": ts_ms,
        "title": "Test Market",
        "slug": "test-market",
    }
    d.update(extra)
    return d


def _count_rows(conn, wallet):
    return conn.execute(
        "SELECT COUNT(*) FROM followed_wallet_activity WHERE wallet = ?",
        (_normalize_wallet(wallet),),
    ).fetchone()[0]


def _get_row(conn, wallet):
    return conn.execute(
        "SELECT * FROM followed_wallet_activity WHERE wallet = ? LIMIT 1",
        (_normalize_wallet(wallet),),
    ).fetchone()


# ---------------------------------------------------------------------------
# Reconciliation tests
# ---------------------------------------------------------------------------


class TestWsReconciliation:
    """Tests for WS synthetic → real-hash reconciliation logic."""

    def test_ws_synthetic_inserts(self):
        """WS row inserts normally; tx_hash starts with ws:."""
        conn = _get_conn()
        ws = _ws_activity("0xAAA", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xAAA", activities=[ws])
        assert _count_rows(conn, "0xAAA") == 1
        row = _get_row(conn, "0xAAA")
        assert row["transaction_hash"].startswith("ws:")

    def test_real_hash_merges_synthetic(self):
        """Real row for the same trade merges into the synthetic row -> count stays 1."""
        conn = _get_conn()
        ws = _ws_activity("0xBBB", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xBBB", activities=[ws])
        assert _count_rows(conn, "0xBBB") == 1

        real = _real_activity("0xREAL123", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xBBB", activities=[real])
        assert _count_rows(conn, "0xBBB") == 1

    def test_merged_key_is_canonical(self):
        """After merge, tx_hash is the real one and title is enriched."""
        conn = _get_conn()
        ws = _ws_activity("0xCCC", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xCCC", activities=[ws])

        real = _real_activity("0xREAL123", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xCCC", activities=[real])

        row = _get_row(conn, "0xCCC")
        assert row["transaction_hash"] == "0xreal123"
        assert row["title"] == "Test Market"

    def test_real_hash_upsert_idempotent(self):
        """Same real row inserted twice -> count stays 1."""
        conn = _get_conn()
        real = _real_activity("0xREAL123", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xDDD", activities=[real])
        upsert_followed_wallet_activity(conn, wallet="0xDDD", activities=[real])
        assert _count_rows(conn, "0xDDD") == 1

    def test_no_match_inserts_new(self):
        """WS row + real row for DIFFERENT market -> count=2."""
        conn = _get_conn()
        ws = _ws_activity("0xEEE", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xEEE", activities=[ws])

        real = _real_activity("0xREAL999", "cond_OTHER", "Yes", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xEEE", activities=[real])
        assert _count_rows(conn, "0xEEE") == 2

    def test_ambiguous_no_auto_merge(self):
        """Two WS rows for same market (different ms), real arrives -> count=3 (no merge)."""
        conn = _get_conn()
        ws1 = _ws_activity("0xFFF", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000100)
        ws2 = _ws_activity("0xFFF", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000200)
        upsert_followed_wallet_activity(conn, wallet="0xFFF", activities=[ws1, ws2])
        assert _count_rows(conn, "0xFFF") == 2

        real = _real_activity("0xREALAMB", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xFFF", activities=[real])
        assert _count_rows(conn, "0xFFF") == 3

    def test_existing_real_hash_unchanged(self):
        """Pure real-hash insert is not synthetic."""
        conn = _get_conn()
        real = _real_activity("0xPURE", "cond1", "Yes", "BUY", 0.55, 200.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xGGG", activities=[real])
        row = _get_row(conn, "0xGGG")
        assert not row["transaction_hash"].startswith("ws:")

    def test_ws_reconnect_dedup(self):
        """Same WS activity inserted twice -> count=1 (idempotent)."""
        conn = _get_conn()
        ws = _ws_activity("0xHHH", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xHHH", activities=[ws])
        upsert_followed_wallet_activity(conn, wallet="0xHHH", activities=[ws])
        assert _count_rows(conn, "0xHHH") == 1

    def test_zero_price_rejected(self):
        """WS with price=0 is rejected."""
        conn = _get_conn()
        ws = _ws_activity("0xIII", "cond1", "Yes", "BUY", 0.0, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xIII", activities=[ws])
        assert _count_rows(conn, "0xIII") == 0

    def test_zero_size_rejected(self):
        """WS with size=0 is rejected."""
        conn = _get_conn()
        ws = _ws_activity("0xLLL", "cond1", "Yes", "BUY", 0.45, 0.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xLLL", activities=[ws])
        assert _count_rows(conn, "0xLLL") == 0

    def test_canonical_exists_cleans_one_synthetic(self):
        """Real exists, synthetic added, re-upsert real -> count=1."""
        conn = _get_conn()
        real = _real_activity("0xREALX", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xJJJ", activities=[real])
        assert _count_rows(conn, "0xJJJ") == 1

        ws = _ws_activity("0xJJJ", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000123)
        upsert_followed_wallet_activity(conn, wallet="0xJJJ", activities=[ws])
        assert _count_rows(conn, "0xJJJ") == 2

        upsert_followed_wallet_activity(conn, wallet="0xJJJ", activities=[real])
        assert _count_rows(conn, "0xJJJ") == 1

    def test_canonical_exists_multiple_synthetics_no_delete(self):
        """Real + 2 synthetics, re-upsert real -> count=3 (ambiguous, no auto-delete)."""
        conn = _get_conn()
        real = _real_activity("0xREALY", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000000)
        upsert_followed_wallet_activity(conn, wallet="0xKKK", activities=[real])

        ws1 = _ws_activity("0xKKK", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000100)
        ws2 = _ws_activity("0xKKK", "cond1", "Yes", "BUY", 0.45, 100.0, 1700000000200)
        upsert_followed_wallet_activity(conn, wallet="0xKKK", activities=[ws1, ws2])
        assert _count_rows(conn, "0xKKK") == 3

        upsert_followed_wallet_activity(conn, wallet="0xKKK", activities=[real])
        assert _count_rows(conn, "0xKKK") == 3

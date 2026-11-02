from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.poll_source_position_copy_targets import (  # noqa: E402
    _activity_key,
    _row_to_source_observation,
    poll_once,
)
from sniper.strategies.source_position_copy_v1 import (  # noqa: E402
    ensure_source_position_copy_tables,
)


def test_activity_key_prefers_transaction_hash_and_log_index():
    row = {"transactionHash": "0xabc", "logIndex": 7}
    assert _activity_key("0xwallet", row) == "0xabc:7"


def test_activity_key_falls_back_to_deterministic_fields():
    row = {"conditionId": "c", "asset": "a", "side": "BUY", "timestamp": 123, "price": 0.5, "size": 10}
    assert _activity_key("0xwallet", row) == "0xwallet:c:a:BUY:123:0.5:10"


def test_row_to_source_observation_normalizes_trade_with_tags():
    row = {
        "type": "TRADE",
        "conditionId": "cond",
        "asset": "asset",
        "outcome": "Yes",
        "side": "BUY",
        "price": 0.50,
        "size": 100,
        "timestamp": 1000,
        "title": "UFC fight",
        "eventSlug": "ufc-fight-night",
        "eventId": "event",
        "tags": ["Sports", "UFC"],
        "transactionHash": "0xabc",
        "logIndex": 1,
    }

    obs = _row_to_source_observation(wallet="0xWallet", row=row)

    assert obs is not None
    assert obs["activity_key"] == "0xabc:1"
    assert obs["wallet"] == "0xwallet"
    assert obs["side"] == "BUY"
    assert obs["usdc_size"] == 50
    assert obs["tags"] == ["sports", "ufc"]


def test_poll_once_dry_run_makes_no_fetch_calls(tmp_path):
    db_path = tmp_path / "paper.db"
    conn = sqlite3.connect(db_path)
    ensure_source_position_copy_tables(conn)
    conn.close()

    calls = []

    def fetcher(wallet, since_ts):
        calls.append((wallet, since_ts))
        return []

    summary = poll_once(
        db_path=db_path,
        wallets=["0xwallet"],
        since_minutes=30,
        enable_live_api=False,
        max_wallets=10,
        fetch_wallet_activity=fetcher,
    )

    assert calls == []
    assert summary["rows_inserted"] == 0
    assert summary["dry_run"] is True

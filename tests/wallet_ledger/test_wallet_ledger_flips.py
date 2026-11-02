"""Slice 4 Task 3 — reconstructed flip writer.

Reads `wallet_events` directly with the strict FIFO contract from spec §12:

* Group key `(condition_id, token_id)` — never outcome label.
* `ORDER BY ts ASC, event_row_ordinal ASC, event_uid ASC` for deterministic
  pairing.
* Dedup by `canonical_event_hash`; ambiguous canonical groups force
  `derivation_check_status = 'unverified'` on every flip whose buy or sell
  side touches the ambiguous group.
* `flip_pnl_collateral` is NULL when buy and sell collateral tokens differ.
* `flip_uid = sha256(wallet|method|version|buy_uid|sell_uid|matched_size)`
  with size formatted as `f"{size:.18f}"`.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from sniper.analysis.wallet_protocol.ledger.flips import (
    reconstruct_and_upsert_flips,
)
from sniper.analysis.wallet_protocol.ledger.schema import ensure_wallet_schema


WALLET = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
NORMALIZED_WALLET = "0xabcdef1234567890abcdef1234567890abcdef12"


def _connect(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect(str(tmp_path / "wallet.duckdb"))
    ensure_wallet_schema(conn)
    return conn


def _rows(
    conn: duckdb.DuckDBPyConnection, query: str, params: list | None = None
) -> list[dict]:
    result = conn.execute(query, params or [])
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _insert_event(
    conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str = NORMALIZED_WALLET,
    event_uid: str,
    canonical_event_hash: str,
    side: str,
    condition_id: str = "0xcid",
    token_id: str = "asset_yes",
    outcome_index: int | None = 0,
    outcome_label: str | None = "Yes",
    ts: int,
    event_row_ordinal: int = 0,
    price: float = 0.50,
    size_tokens: float = 100.0,
    collateral_amount: float | None = None,
    collateral_token: str | None = "pUSD",
    collateral_token_address: str | None = "0x...pusd",
    usd_equivalent_amount: float | None = None,
    ingest_status: str = "normalized",
    dedup_status: str = "canonical",
    source: str = "polymarket_data_api",
    source_version: str = "2026-05-02",
    capture_run_id: str = "run-1",
    captured_at: int = 1_700_000_100,
) -> None:
    if collateral_amount is None:
        collateral_amount = price * size_tokens
    if usd_equivalent_amount is None:
        usd_equivalent_amount = collateral_amount
    conn.execute(
        """
        INSERT INTO wallet_events (
            wallet, event_uid, source_event_uid, canonical_event_hash,
            event_row_ordinal, source_event_type, ts, condition_id, token_id,
            outcome_index, outcome_label, side, size_tokens,
            collateral_amount, collateral_token, collateral_token_address,
            usd_equivalent_amount, price, ingest_status, dedup_status,
            normalization_version, source, source_version, capture_run_id,
            captured_at
        )
        VALUES (?, ?, ?, ?, ?, 'TRADE', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            wallet,
            event_uid,
            event_uid,
            canonical_event_hash,
            event_row_ordinal,
            ts,
            condition_id,
            token_id,
            outcome_index,
            outcome_label,
            side,
            size_tokens,
            collateral_amount,
            collateral_token,
            collateral_token_address,
            usd_equivalent_amount,
            price,
            ingest_status,
            dedup_status,
            "activity-v1",
            source,
            source_version,
            capture_run_id,
            captured_at,
        ],
    )


def test_basic_buy_sell_pair_writes_one_flip_row(tmp_path: Path) -> None:
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
            price=0.40,
            size_tokens=100.0,
        )
        _insert_event(
            conn,
            event_uid="ev:sell",
            canonical_event_hash="hash-sell",
            side="SELL",
            ts=1_700_001_000,
            price=0.60,
            size_tokens=100.0,
        )
        n = reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        assert n == 1
        rows = _rows(conn, "SELECT * FROM wallet_reconstructed_flips")
        assert len(rows) == 1
        flip = rows[0]
        assert flip["wallet"] == NORMALIZED_WALLET
        assert flip["buy_event_uid"] == "ev:buy"
        assert flip["sell_event_uid"] == "ev:sell"
        assert flip["condition_id"] == "0xcid"
        assert flip["token_id"] == "asset_yes"
        assert flip["size_tokens"] == 100.0
        assert flip["buy_price"] == 0.40
        assert flip["sell_price"] == 0.60
        assert flip["flip_roi"] == (0.60 - 0.40) / 0.40
        assert flip["hold_duration_seconds"] == 1000
        assert flip["derivation_check_status"] == "ok"
        assert flip["reconstruction_method"] == "fifo_event_pair_v1"
        assert flip["reconstruction_version"] == "v1"


def test_fifo_partial_consumption_produces_correct_flip_size(
    tmp_path: Path,
) -> None:
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
            price=0.40,
            size_tokens=100.0,
        )
        _insert_event(
            conn,
            event_uid="ev:sell30",
            canonical_event_hash="hash-sell30",
            side="SELL",
            ts=1_700_001_000,
            price=0.60,
            size_tokens=30.0,
        )
        reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        rows = _rows(
            conn,
            "SELECT size_tokens, buy_event_uid, sell_event_uid, derivation_check_status FROM wallet_reconstructed_flips",
        )
        assert len(rows) == 1
        assert rows[0]["size_tokens"] == 30.0
        assert rows[0]["buy_event_uid"] == "ev:buy"
        assert rows[0]["sell_event_uid"] == "ev:sell30"
        assert rows[0]["derivation_check_status"] == "ok"


def test_fifo_ordering_uses_ts_then_row_ordinal_then_event_uid(
    tmp_path: Path,
) -> None:
    """Two rows with identical ts but different event_row_ordinal/event_uid:
    deterministic match means the same input order yields the same flip_uid
    every run.
    """
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy_a",
            canonical_event_hash="hash-buy-a",
            side="BUY",
            ts=1_700_000_000,
            event_row_ordinal=1,
            size_tokens=50.0,
            price=0.40,
        )
        _insert_event(
            conn,
            event_uid="ev:buy_b",
            canonical_event_hash="hash-buy-b",
            side="BUY",
            ts=1_700_000_000,  # same ts!
            event_row_ordinal=2,
            size_tokens=50.0,
            price=0.42,
        )
        _insert_event(
            conn,
            event_uid="ev:sell",
            canonical_event_hash="hash-sell",
            side="SELL",
            ts=1_700_001_000,
            size_tokens=50.0,
            price=0.60,
        )
        reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        rows = _rows(
            conn,
            "SELECT buy_event_uid, sell_event_uid FROM wallet_reconstructed_flips",
        )
        assert len(rows) == 1
        # event_row_ordinal=1 wins over event_row_ordinal=2 → ev:buy_a is consumed first.
        assert rows[0]["buy_event_uid"] == "ev:buy_a"


def test_group_key_is_condition_id_and_token_id_not_outcome_label(
    tmp_path: Path,
) -> None:
    """Same condition_id with same outcome label but different token_id —
    they MUST NOT match (multi-outcome markets reuse outcome strings).
    """
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy_yes",
            canonical_event_hash="hash-buy-yes",
            side="BUY",
            ts=1_700_000_000,
            token_id="asset_yes",
            outcome_label="Yes",
        )
        _insert_event(
            conn,
            event_uid="ev:sell_no",
            canonical_event_hash="hash-sell-no",
            side="SELL",
            ts=1_700_001_000,
            token_id="asset_no",
            outcome_label="Yes",  # label collision but token_id differs
        )
        n = reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        assert n == 0
        rows = _rows(conn, "SELECT COUNT(*) AS n FROM wallet_reconstructed_flips")
        assert rows[0]["n"] == 0


def test_canonical_hash_dedup_skips_duplicate_within_same_source(
    tmp_path: Path,
) -> None:
    """Two rows with identical canonical_event_hash and dedup_status='canonical'
    are accidental duplicates from a single source — only one should be matched
    so the flip stays 'ok'.
    """
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
        )
        _insert_event(
            conn,
            event_uid="ev:buy_dup",
            canonical_event_hash="hash-buy",  # same canonical_hash!
            side="BUY",
            ts=1_700_000_000,
            event_row_ordinal=2,
        )
        _insert_event(
            conn,
            event_uid="ev:sell",
            canonical_event_hash="hash-sell",
            side="SELL",
            ts=1_700_001_000,
        )
        reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        rows = _rows(
            conn,
            "SELECT buy_event_uid, derivation_check_status FROM wallet_reconstructed_flips",
        )
        assert len(rows) == 1
        # Either of the two duplicate rows could have been chosen; they share
        # canonical_event_hash by definition. The flip is 'ok' (not unverified)
        # because dedup_status was canonical, not ambiguous.
        assert rows[0]["buy_event_uid"] in ("ev:buy", "ev:buy_dup")
        assert rows[0]["derivation_check_status"] == "ok"


def test_ambiguous_canonical_group_marks_flip_unverified(tmp_path: Path) -> None:
    """Buy side has dedup_status='ambiguous' (two normalized rows with the
    same canonical_event_hash from different sources). The flip is still
    written but with derivation_check_status='unverified'.
    """
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy_pma",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
            source="polymarket_data_api",
            dedup_status="ambiguous",
        )
        _insert_event(
            conn,
            event_uid="ev:buy_goldsky",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
            event_row_ordinal=1,
            source="goldsky_subgraph",
            dedup_status="ambiguous",
        )
        _insert_event(
            conn,
            event_uid="ev:sell",
            canonical_event_hash="hash-sell",
            side="SELL",
            ts=1_700_001_000,
        )
        reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        rows = _rows(
            conn,
            "SELECT derivation_check_status, sell_event_uid FROM wallet_reconstructed_flips",
        )
        assert len(rows) == 1
        assert rows[0]["derivation_check_status"] == "unverified"
        assert rows[0]["sell_event_uid"] == "ev:sell"


def test_mixed_collateral_sets_flip_pnl_collateral_to_null(tmp_path: Path) -> None:
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
            price=0.40,
            size_tokens=100.0,
            collateral_amount=40.0,
            collateral_token="USDC.e",
            usd_equivalent_amount=40.0,
        )
        _insert_event(
            conn,
            event_uid="ev:sell",
            canonical_event_hash="hash-sell",
            side="SELL",
            ts=1_700_001_000,
            price=0.60,
            size_tokens=100.0,
            collateral_amount=60.0,
            collateral_token="pUSD",
            usd_equivalent_amount=60.0,
        )
        reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        rows = _rows(
            conn,
            """
            SELECT buy_collateral_token, sell_collateral_token,
                   flip_pnl_collateral, flip_pnl_usd_equivalent
            FROM wallet_reconstructed_flips
            """,
        )
        assert len(rows) == 1
        assert rows[0]["buy_collateral_token"] == "USDC.e"
        assert rows[0]["sell_collateral_token"] == "pUSD"
        assert rows[0]["flip_pnl_collateral"] is None
        assert rows[0]["flip_pnl_usd_equivalent"] == pytest.approx(20.0)


def test_flip_uid_includes_method_and_version(tmp_path: Path) -> None:
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
            size_tokens=100.0,
            price=0.40,
        )
        _insert_event(
            conn,
            event_uid="ev:sell",
            canonical_event_hash="hash-sell",
            side="SELL",
            ts=1_700_001_000,
            size_tokens=100.0,
            price=0.60,
        )
        reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
            reconstruction_version="v1",
        )
        reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
            reconstruction_version="v2",
        )
        rows = _rows(
            conn,
            "SELECT flip_uid, reconstruction_version FROM wallet_reconstructed_flips ORDER BY reconstruction_version",
        )
        assert len(rows) == 2
        assert rows[0]["reconstruction_version"] == "v1"
        assert rows[1]["reconstruction_version"] == "v2"
        assert rows[0]["flip_uid"] != rows[1]["flip_uid"]


def test_flip_uid_is_canonical_sha256_with_size_padding(tmp_path: Path) -> None:
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
            size_tokens=100.0,
            price=0.40,
        )
        _insert_event(
            conn,
            event_uid="ev:sell",
            canonical_event_hash="hash-sell",
            side="SELL",
            ts=1_700_001_000,
            size_tokens=100.0,
            price=0.60,
        )
        reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        rows = _rows(conn, "SELECT flip_uid FROM wallet_reconstructed_flips")
        expected = hashlib.sha256(
            f"{NORMALIZED_WALLET}|fifo_event_pair_v1|v1|ev:buy|ev:sell|{100.0:.18f}".encode()
        ).hexdigest()
        assert rows[0]["flip_uid"] == expected


def test_upsert_is_idempotent(tmp_path: Path) -> None:
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
        )
        _insert_event(
            conn,
            event_uid="ev:sell",
            canonical_event_hash="hash-sell",
            side="SELL",
            ts=1_700_001_000,
        )
        first = reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        second = reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_020_000,
        )
        assert first == 1
        assert second == 0
        rows = _rows(conn, "SELECT COUNT(*) AS n FROM wallet_reconstructed_flips")
        assert rows[0]["n"] == 1


def test_input_event_uids_json_lists_contributors_in_time_order(
    tmp_path: Path,
) -> None:
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
        )
        _insert_event(
            conn,
            event_uid="ev:sell",
            canonical_event_hash="hash-sell",
            side="SELL",
            ts=1_700_001_000,
        )
        reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        rows = _rows(
            conn, "SELECT input_event_uids_json FROM wallet_reconstructed_flips"
        )
        contributors = json.loads(rows[0]["input_event_uids_json"])
        assert contributors == ["ev:buy", "ev:sell"]


def test_rejected_events_are_ignored(tmp_path: Path) -> None:
    """Defense in depth — flip writer must skip rows with ingest_status != 'normalized'."""
    with _connect(tmp_path) as conn:
        _insert_event(
            conn,
            event_uid="ev:buy",
            canonical_event_hash="hash-buy",
            side="BUY",
            ts=1_700_000_000,
        )
        _insert_event(
            conn,
            event_uid="ev:sell_rejected",
            canonical_event_hash="hash-sell",
            side="SELL",
            ts=1_700_001_000,
            ingest_status="rejected",
        )
        n = reconstruct_and_upsert_flips(
            conn,
            wallet=WALLET,
            capture_run_id="run-1",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_010_000,
        )
        assert n == 0

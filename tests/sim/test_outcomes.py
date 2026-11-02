# tests/sim/test_outcomes.py
import json
import os
import tempfile

import pyarrow as pa
import pyarrow.parquet as pq

from sniper.sim.episodes.outcomes import MarketContext, resolve_market_context


def _write_lifecycle_parquet(dir_path: str, rows: list[dict]) -> None:
    """Write lifecycle rows to a Parquet file, mimicking compactor output."""
    os.makedirs(dir_path, exist_ok=True)
    table = pa.Table.from_pylist(rows)
    pq.write_table(table, os.path.join(dir_path, "2026-04-03.parquet"))


def _make_metadata_row(
    market_id: str = "pm:0x1",
    tokens: list[dict] | None = None,
    fees_enabled: bool = True,
    fee: str = "0.02",
    enable_order_book: bool = True,
) -> dict:
    if tokens is None:
        tokens = [
            {"token_id": "tok_yes", "label": "Yes", "index": 0},
            {"token_id": "tok_no", "label": "No", "index": 1},
        ]
    outcomes = [t.get("label") or t.get("outcome", f"outcome_{i}") for i, t in enumerate(tokens)]
    payload = {
        "question": "Will it happen?",
        "outcomes": outcomes,
        "tokens": tokens,
        "fees_enabled": fees_enabled,
        "enable_order_book": enable_order_book,
        "fee": fee,
    }
    return {
        "market_id": market_id,
        "capture_ts": "2026-04-03T12:00:03.000Z",
        "ingest_seq": 4,
        "event_type": "market_metadata",
        "market_type": "geopolitical",
        "payload": json.dumps(payload),
        "venue": "polymarket",
    }


def _make_resolved_row(
    market_id: str = "pm:0x1",
    outcome_prices: list[str] | None = None,
    resolved_ts: str = "2026-04-03T18:00:00.000Z",
) -> dict:
    if outcome_prices is None:
        outcome_prices = ["0.01", "0.99"]
    payload = {
        "outcome_prices": outcome_prices,
        "resolved_ts": resolved_ts,
    }
    return {
        "market_id": market_id,
        "capture_ts": "2026-04-03T18:00:00.000Z",
        "ingest_seq": 5,
        "event_type": "market_resolved",
        "market_type": "geopolitical",
        "payload": json.dumps(payload),
        "venue": "polymarket",
    }


# --- Happy path ---

def test_resolve_market_context_basic():
    """Resolve outcome_index and ticker from lifecycle Parquet."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        _write_lifecycle_parquet(lifecycle_dir, [
            _make_metadata_row(),
            _make_resolved_row(),
        ])

        ctx = resolve_market_context("pm:0x1", lifecycle_dir)

        assert ctx.market_id == "pm:0x1"
        assert ctx.question == "Will it happen?"
        assert ctx.outcomes == ["Yes", "No"]
        # asset_id (= token_id) → outcome_index
        assert ctx.asset_to_outcome_index == {"tok_yes": 0, "tok_no": 1}
        # asset_id → ticker (outcome label)
        assert ctx.asset_to_ticker == {"tok_yes": "Yes", "tok_no": "No"}
        # Fee metadata
        assert ctx.fees_enabled is True
        assert ctx.fee == "0.02"
        assert ctx.enable_order_book is True


def test_resolve_market_context_settlement():
    """Settlement extracted with clear winner."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        _write_lifecycle_parquet(lifecycle_dir, [
            _make_metadata_row(),
            _make_resolved_row(outcome_prices=["0.01", "0.99"]),
        ])

        ctx = resolve_market_context("pm:0x1", lifecycle_dir)

        assert ctx.settlement is not None
        assert ctx.settlement["outcome_prices"] == ["0.01", "0.99"]
        assert ctx.settlement["resolved_ts"] == "2026-04-03T18:00:00.000Z"
        # Derived: winning outcome
        assert ctx.winning_outcome == "No"  # index 1 has price > 0.99
        assert ctx.winning_outcome_index == 1


def test_resolve_fees_disabled():
    """When fees_enabled is False, fee field is not required."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        _write_lifecycle_parquet(lifecycle_dir, [
            _make_metadata_row(fees_enabled=False, fee=None),
            _make_resolved_row(),
        ])

        ctx = resolve_market_context("pm:0x1", lifecycle_dir)

        assert ctx.fees_enabled is False
        assert ctx.fee is None


def test_resolve_three_outcome_market():
    """Markets with 3+ outcomes index correctly."""
    tokens = [
        {"token_id": "tok_a", "label": "Trump", "index": 0},
        {"token_id": "tok_b", "label": "Biden", "index": 1},
        {"token_id": "tok_c", "label": "Other", "index": 2},
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        _write_lifecycle_parquet(lifecycle_dir, [
            _make_metadata_row(tokens=tokens),
            _make_resolved_row(outcome_prices=["0.99", "0.005", "0.005"]),
        ])

        ctx = resolve_market_context("pm:0x1", lifecycle_dir)

        assert ctx.asset_to_outcome_index == {"tok_a": 0, "tok_b": 1, "tok_c": 2}
        assert ctx.asset_to_ticker == {"tok_a": "Trump", "tok_b": "Biden", "tok_c": "Other"}
        assert ctx.winning_outcome == "Trump"
        assert ctx.winning_outcome_index == 0


def test_resolve_market_context_accepts_legacy_outcome_tokens():
    """Older lifecycle payloads with token.outcome still resolve correctly."""
    tokens = [
        {"token_id": "tok_yes", "outcome": "Yes"},
        {"token_id": "tok_no", "outcome": "No"},
    ]
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        _write_lifecycle_parquet(lifecycle_dir, [
            _make_metadata_row(tokens=tokens),
            _make_resolved_row(),
        ])

        ctx = resolve_market_context("pm:0x1", lifecycle_dir)

        assert ctx.asset_to_outcome_index == {"tok_yes": 0, "tok_no": 1}
        assert ctx.asset_to_ticker == {"tok_yes": "Yes", "tok_no": "No"}


# --- Error cases ---

def test_resolve_missing_metadata_raises():
    """No metadata event for the market → error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        _write_lifecycle_parquet(lifecycle_dir, [
            _make_resolved_row(),  # resolution but no metadata
        ])

        try:
            resolve_market_context("pm:0x1", lifecycle_dir)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "metadata" in str(e).lower()


def test_resolve_missing_resolution_raises():
    """No resolution event for the market → error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        _write_lifecycle_parquet(lifecycle_dir, [
            _make_metadata_row(),  # metadata but no resolution
        ])

        try:
            resolve_market_context("pm:0x1", lifecycle_dir)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "resolution" in str(e).lower() or "resolved" in str(e).lower()


def test_resolve_no_clear_winner_raises():
    """Ambiguous outcome_prices (no single price > 0.99) → error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        _write_lifecycle_parquet(lifecycle_dir, [
            _make_metadata_row(),
            _make_resolved_row(outcome_prices=["0.50", "0.50"]),
        ])

        try:
            resolve_market_context("pm:0x1", lifecycle_dir)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "winner" in str(e).lower() or "clear" in str(e).lower()


def test_resolve_empty_tokens_raises():
    """Metadata with empty tokens array → error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        _write_lifecycle_parquet(lifecycle_dir, [
            _make_metadata_row(tokens=[]),
            _make_resolved_row(),
        ])

        try:
            resolve_market_context("pm:0x1", lifecycle_dir)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "token" in str(e).lower()


def test_resolve_wrong_market_id_raises():
    """Requesting a market_id not present in the lifecycle data → error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        _write_lifecycle_parquet(lifecycle_dir, [
            _make_metadata_row(market_id="pm:0xother"),
            _make_resolved_row(market_id="pm:0xother"),
        ])

        try:
            resolve_market_context("pm:0x1", lifecycle_dir)
            assert False, "Should have raised ValueError"
        except ValueError as e:
            assert "pm:0x1" in str(e)


def test_resolve_multiple_dates():
    """Lifecycle data spanning multiple date partitions is handled."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")
        os.makedirs(lifecycle_dir, exist_ok=True)

        # Metadata on day 1
        meta_row = _make_metadata_row()
        meta_row["capture_ts"] = "2026-04-02T10:00:00.000Z"
        t1 = pa.Table.from_pylist([meta_row])
        pq.write_table(t1, os.path.join(lifecycle_dir, "2026-04-02.parquet"))

        # Resolution on day 2
        res_row = _make_resolved_row()
        res_row["capture_ts"] = "2026-04-03T18:00:00.000Z"
        t2 = pa.Table.from_pylist([res_row])
        pq.write_table(t2, os.path.join(lifecycle_dir, "2026-04-03.parquet"))

        ctx = resolve_market_context("pm:0x1", lifecycle_dir)
        assert ctx.market_id == "pm:0x1"
        assert ctx.settlement is not None


def test_resolve_picks_newest_metadata():
    """When multiple metadata rows exist, the newest by (capture_ts, ingest_seq) wins."""
    with tempfile.TemporaryDirectory() as tmpdir:
        lifecycle_dir = os.path.join(tmpdir, "market_lifecycle")

        # Old metadata with wrong question
        old_meta = _make_metadata_row()
        old_meta["capture_ts"] = "2026-04-01T10:00:00.000Z"
        old_meta["ingest_seq"] = 1
        old_payload = json.loads(old_meta["payload"])
        old_payload["question"] = "Old question"
        old_meta["payload"] = json.dumps(old_payload)

        # New metadata with correct question
        new_meta = _make_metadata_row()
        new_meta["capture_ts"] = "2026-04-03T10:00:00.000Z"
        new_meta["ingest_seq"] = 10
        new_payload = json.loads(new_meta["payload"])
        new_payload["question"] = "New question"
        new_meta["payload"] = json.dumps(new_payload)

        resolved = _make_resolved_row()

        _write_lifecycle_parquet(lifecycle_dir, [old_meta, new_meta, resolved])

        ctx = resolve_market_context("pm:0x1", lifecycle_dir)
        assert ctx.question == "New question"

"""Resolve asset_id → outcome_index from market lifecycle Parquet data."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import pyarrow.dataset as ds


@dataclass
class MarketContext:
    """Everything needed to enrich trades/quotes with outcome context."""

    market_id: str
    question: str
    outcomes: list[str]
    tokens: list[dict[str, Any]]
    asset_to_outcome_index: dict[str, int]
    asset_to_ticker: dict[str, str]
    fees_enabled: bool
    fee: Optional[str]
    enable_order_book: bool
    settlement: Optional[dict[str, Any]]

    @property
    def winning_outcome_index(self) -> Optional[int]:
        """Index of the winning outcome (price > 0.99), or None."""
        if self.settlement is None:
            return None
        prices = self.settlement.get("outcome_prices", [])
        for i, p in enumerate(prices):
            if float(p) >= 0.99:
                return i
        return None

    @property
    def winning_outcome(self) -> Optional[str]:
        """Label of the winning outcome, or None."""
        idx = self.winning_outcome_index
        if idx is not None and idx < len(self.outcomes):
            return self.outcomes[idx]
        return None


def resolve_market_context(market_id: str, lifecycle_dir: str) -> MarketContext:
    """Build a MarketContext from market_lifecycle Parquet files.

    Reads all Parquet files in lifecycle_dir, filters to the given market_id,
    extracts metadata and resolution payloads.

    Args:
        market_id: canonical market ID (e.g. "pm:0x1")
        lifecycle_dir: path to market_lifecycle/ directory containing dated Parquet files

    Returns:
        MarketContext with resolved outcome mappings

    Raises:
        ValueError: if metadata/resolution is missing or invalid
    """
    # Read all lifecycle parquet files for this market
    dataset = ds.dataset(lifecycle_dir, format="parquet")
    table = dataset.to_table(
        filter=ds.field("market_id") == market_id,
    )

    if len(table) == 0:
        raise ValueError(
            f"No lifecycle events found for market_id={market_id}"
        )

    # Separate metadata and resolution rows, picking the newest by (capture_ts, ingest_seq)
    event_types = table.column("event_type").to_pylist()
    payloads = table.column("payload").to_pylist()
    capture_tss = table.column("capture_ts").to_pylist()
    ingest_seqs = table.column("ingest_seq").to_pylist()

    metadata_payload = None
    resolution_payload = None
    metadata_key: tuple[str, int] = ("", -1)
    resolution_key: tuple[str, int] = ("", -1)

    for et, raw_payload, cts, iseq in zip(event_types, payloads, capture_tss, ingest_seqs):
        row_key = (cts or "", iseq or 0)
        payload = json.loads(raw_payload)
        if et == "market_metadata" and row_key >= metadata_key:
            metadata_payload = payload
            metadata_key = row_key
        elif et == "market_resolved" and row_key >= resolution_key:
            resolution_payload = payload
            resolution_key = row_key

    # --- Validate metadata ---
    if metadata_payload is None:
        raise ValueError(
            f"No market_metadata event found for market_id={market_id}"
        )

    tokens = metadata_payload.get("tokens", [])
    if not tokens:
        raise ValueError(
            f"Empty or missing tokens array in metadata for market_id={market_id}"
        )

    # --- Validate resolution ---
    if resolution_payload is None:
        raise ValueError(
            f"No market_resolved event found for market_id={market_id}"
        )

    outcome_prices = resolution_payload.get("outcome_prices", [])
    winners = [i for i, p in enumerate(outcome_prices) if float(p) >= 0.99]
    if len(winners) != 1:
        raise ValueError(
            f"No clear winner in outcome_prices={outcome_prices} for market_id={market_id}. "
            f"Expected exactly one price > 0.99, found {len(winners)}."
        )

    # --- Build lookups ---
    asset_to_outcome_index: dict[str, int] = {}
    asset_to_ticker: dict[str, str] = {}
    for i, token in enumerate(tokens):
        tid = str(token.get("token_id", ""))
        outcome_label = str(token.get("label") or token.get("outcome") or "")
        token_index = token.get("index")
        if isinstance(token_index, str) and token_index.isdigit():
            token_index = int(token_index)
        if not isinstance(token_index, int):
            token_index = i
        if tid:
            asset_to_outcome_index[tid] = token_index
            asset_to_ticker[tid] = outcome_label

    outcomes = metadata_payload.get("outcomes", [])
    fees_enabled = metadata_payload.get("fees_enabled", False)
    fee = metadata_payload.get("fee")
    enable_order_book = metadata_payload.get("enable_order_book", False)

    return MarketContext(
        market_id=market_id,
        question=metadata_payload.get("question", ""),
        outcomes=outcomes,
        tokens=tokens,
        asset_to_outcome_index=asset_to_outcome_index,
        asset_to_ticker=asset_to_ticker,
        fees_enabled=fees_enabled,
        fee=fee,
        enable_order_book=enable_order_book,
        settlement=resolution_payload,
    )

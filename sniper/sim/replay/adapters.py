"""Venue adapters — read episode directories into a common timeline format."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Optional

import pyarrow.parquet as pq


@dataclass
class TimelineEvent:
    """A single event in the replay timeline."""

    ts: str  # sort key (exchange_ts or capture_ts fallback)
    ingest_seq: int  # tiebreaker
    kind: str  # "quote" or "trade"
    ticker: str  # outcome label (e.g. "Yes", "No")
    data: dict[str, Any]  # raw row data


@dataclass
class EpisodeData:
    """Everything needed to replay one episode."""

    market_id: str
    metadata: dict[str, Any]
    settlement: dict[str, Any]
    timeline: list[TimelineEvent]  # sorted by (ts, ingest_seq)
    fee_fn_args: dict[str, Any]  # venue-specific fee config


def _sort_key(evt: TimelineEvent) -> tuple[str, int]:
    return (evt.ts, evt.ingest_seq)


class PolymarketAdapter:
    """Read a Polymarket episode directory into EpisodeData."""

    def load(self, episode_dir: str) -> EpisodeData:
        # Read metadata
        with open(os.path.join(episode_dir, "metadata.json")) as f:
            metadata = json.load(f)

        # Read settlement
        with open(os.path.join(episode_dir, "settlement.json")) as f:
            settlement = json.load(f)

        timeline: list[TimelineEvent] = []

        # Read quotes
        quotes_path = os.path.join(episode_dir, "quotes.parquet")
        if os.path.exists(quotes_path):
            quotes = pq.read_table(quotes_path)
            for i in range(len(quotes)):
                row = {col: quotes.column(col)[i].as_py() for col in quotes.column_names}
                ts = row.get("exchange_ts") or row.get("capture_ts", "")
                timeline.append(TimelineEvent(
                    ts=ts,
                    ingest_seq=row.get("ingest_seq", 0),
                    kind="quote",
                    ticker=row.get("ticker", ""),
                    data=row,
                ))

        # Read trades
        trades_path = os.path.join(episode_dir, "trades.parquet")
        if os.path.exists(trades_path):
            trades = pq.read_table(trades_path)
            for i in range(len(trades)):
                row = {col: trades.column(col)[i].as_py() for col in trades.column_names}
                ts = row.get("exchange_ts") or row.get("capture_ts", "")
                timeline.append(TimelineEvent(
                    ts=ts,
                    ingest_seq=row.get("ingest_seq", 0),
                    kind="trade",
                    ticker=row.get("ticker", ""),
                    data=row,
                ))

        # Sort by time
        timeline.sort(key=_sort_key)

        # Build fee config from metadata
        fee_fn_args = {
            "fees_enabled": metadata.get("fees_enabled", False),
            "token_fee_rates": metadata.get("token_fee_rates", {}),
            "default_fee": metadata.get("fee"),
        }

        return EpisodeData(
            market_id=metadata.get("market_id", ""),
            metadata=metadata,
            settlement=settlement,
            timeline=timeline,
            fee_fn_args=fee_fn_args,
        )

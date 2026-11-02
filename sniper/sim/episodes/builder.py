"""Build self-contained episode directories from normalized Parquet tables."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from sniper.sim.episodes.outcomes import MarketContext, resolve_market_context

LOG = logging.getLogger(__name__)


@dataclass
class EpisodeSummary:
    """Result of a successful episode build."""

    market_id: str
    episode_dir: str
    trade_count: int
    quote_count: int
    book_depth_count: int


def _sanitize_market_id(market_id: str) -> str:
    """Convert market_id to a filesystem-safe directory name.

    e.g. 'pm:0xabc123' → 'pm_0xabc123'
    """
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", market_id)


def _read_filtered(table_dir: str, market_id: str) -> Optional[pa.Table]:
    """Read all Parquet files in table_dir, filtered to market_id.

    Returns None if the directory doesn't exist or has no matching rows.
    """
    if not os.path.isdir(table_dir):
        return None
    try:
        dataset = ds.dataset(table_dir, format="parquet")
    except Exception:
        return None

    table = dataset.to_table(filter=ds.field("market_id") == market_id)
    if len(table) == 0:
        return None
    return table


def _sort_table(table: pa.Table) -> pa.Table:
    """Sort a table by (exchange_ts, capture_ts, ingest_seq).

    Null exchange_ts values use capture_ts as fallback via a synthetic sort key.
    """
    exchange_ts = table.column("exchange_ts")
    capture_ts = table.column("capture_ts")

    sort_key = pc.if_else(pc.is_null(exchange_ts), capture_ts, exchange_ts)
    table = table.append_column("_sort_ts", sort_key)

    indices = pc.sort_indices(table, sort_keys=[("_sort_ts", "ascending"), ("ingest_seq", "ascending")])
    table = table.take(indices)

    col_idx = table.column_names.index("_sort_ts")
    table = table.remove_column(col_idx)
    return table


def _enrich_table(table: pa.Table, ctx: MarketContext) -> pa.Table:
    """Add outcome_index and ticker columns based on asset_id lookup.

    Raises ValueError if any asset_id cannot be resolved.
    """
    asset_ids = table.column("asset_id").to_pylist()

    outcome_indices = []
    tickers = []
    for aid in asset_ids:
        if aid not in ctx.asset_to_outcome_index:
            raise ValueError(
                f"Cannot resolve outcome_index for asset_id={aid} in market {ctx.market_id}. "
                f"Known assets: {list(ctx.asset_to_outcome_index.keys())}"
            )
        outcome_indices.append(ctx.asset_to_outcome_index[aid])
        tickers.append(ctx.asset_to_ticker[aid])

    table = table.append_column("outcome_index", pa.array(outcome_indices, type=pa.int32()))
    table = table.append_column("ticker", pa.array(tickers, type=pa.string()))
    return table


def build_episode(
    market_id: str,
    normalized_dir: str,
    episodes_dir: str,
    fee_rates: Optional[dict[str, float]] = None,
) -> EpisodeSummary:
    """Build a self-contained episode directory for a resolved market.

    Args:
        market_id: canonical market ID (e.g. "pm:0x1")
        normalized_dir: path to compactor output (contains trades/, quotes_top/, etc.)
        episodes_dir: base path for episode output
        fee_rates: per-asset token fee rates (asset_id → max_fee_rate).
                   Required when market has fees_enabled=True.

    Returns:
        EpisodeSummary with counts and episode path.

    Raises:
        ValueError: if episode fails validation.
    """
    # 1. Resolve market context from lifecycle data
    lifecycle_dir = os.path.join(normalized_dir, "market_lifecycle")
    ctx = resolve_market_context(market_id, lifecycle_dir)

    # 2. Validate fee_rates for fee-enabled markets
    if ctx.fees_enabled:
        if fee_rates is None:
            raise ValueError(
                f"Market {market_id} has fees_enabled=True but no fee_rates provided. "
                f"Required asset_ids: {list(ctx.asset_to_outcome_index.keys())}"
            )
        missing = set(ctx.asset_to_outcome_index.keys()) - set(fee_rates.keys())
        if missing:
            raise ValueError(
                f"fee_rates missing for assets: {missing} in fee-enabled market {market_id}"
            )

    # 3. Read and filter trades
    trades_dir = os.path.join(normalized_dir, "trades")
    trades_table = _read_filtered(trades_dir, market_id)
    if trades_table is None or len(trades_table) == 0:
        raise ValueError(f"No trades found for market {market_id}")

    # 4. Read and filter quotes
    quotes_dir = os.path.join(normalized_dir, "quotes_top")
    quotes_table = _read_filtered(quotes_dir, market_id)
    if quotes_table is None or len(quotes_table) == 0:
        raise ValueError(f"No quotes found for market {market_id}")

    # 5. Read book_levels (optional)
    levels_dir = os.path.join(normalized_dir, "book_levels")
    levels_table = _read_filtered(levels_dir, market_id)

    # 6. Enrich with outcome_index + ticker
    trades_table = _enrich_table(trades_table, ctx)
    quotes_table = _enrich_table(quotes_table, ctx)
    if levels_table is not None:
        levels_table = _enrich_table(levels_table, ctx)

    # 7. Sort
    trades_table = _sort_table(trades_table)
    quotes_table = _sort_table(quotes_table)
    if levels_table is not None and "exchange_ts" not in levels_table.column_names:
        indices = pc.sort_indices(
            levels_table,
            sort_keys=[("capture_ts", "ascending"), ("ingest_seq", "ascending")],
        )
        levels_table = levels_table.take(indices)
    elif levels_table is not None:
        levels_table = _sort_table(levels_table)

    # 8. Create episode directory
    slug = _sanitize_market_id(market_id)
    episode_dir = os.path.join(episodes_dir, "polymarket", slug)
    os.makedirs(episode_dir, exist_ok=True)

    # 9. Write trades.parquet
    pq.write_table(trades_table, os.path.join(episode_dir, "trades.parquet"))

    # 10. Write quotes.parquet
    pq.write_table(quotes_table, os.path.join(episode_dir, "quotes.parquet"))

    # 11. Write book_depth.parquet (optional)
    book_depth_count = 0
    if levels_table is not None and len(levels_table) > 0:
        pq.write_table(levels_table, os.path.join(episode_dir, "book_depth.parquet"))
        book_depth_count = len(levels_table)

    # 12. Write metadata.json
    metadata = {
        "market_id": ctx.market_id,
        "question": ctx.question,
        "outcomes": ctx.outcomes,
        "tokens": ctx.tokens,
        "fees_enabled": ctx.fees_enabled,
        "fee": ctx.fee,
        "enable_order_book": ctx.enable_order_book,
    }
    if ctx.fees_enabled and fee_rates is not None:
        metadata["token_fee_rates"] = fee_rates

    with open(os.path.join(episode_dir, "metadata.json"), "w") as f:
        json.dump(metadata, f, indent=2)

    # 13. Write settlement.json
    settlement = {
        "market_id": ctx.market_id,
        "outcome": ctx.winning_outcome,
        "outcome_prices": ctx.settlement["outcome_prices"],
        "resolved_ts": ctx.settlement["resolved_ts"],
    }
    with open(os.path.join(episode_dir, "settlement.json"), "w") as f:
        json.dump(settlement, f, indent=2)

    LOG.info(
        "Built episode for %s: %d trades, %d quotes, %d book_depth → %s",
        market_id, len(trades_table), len(quotes_table), book_depth_count, episode_dir,
    )

    return EpisodeSummary(
        market_id=market_id,
        episode_dir=episode_dir,
        trade_count=len(trades_table),
        quote_count=len(quotes_table),
        book_depth_count=book_depth_count,
    )

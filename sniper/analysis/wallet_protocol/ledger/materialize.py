"""Slice 5 — `materialize_wallet_data` reader.

Reads ledger facts inside an `AnalysisWindow` and returns a
`MaterializedWalletData` bundle whose `to_analyze_wallet_kwargs()`
produces the exact kwargs the existing `analyze_wallet()` entrypoint
already accepts. No protocol-side signature changes.

Boundaries (spec §14, plan v4):
* No live API calls. The price-history closure reads from the local
  warehouse only.
* `peer_baseline_fn`, `leafs_by_cid`, `nav_lookup` stay `None` — these
  surfaces are out of scope for Slice 5 (see plan "Out Of Scope").
* `replay_surface_meta` carries the materialization marker
  `ledger_source = "duckdb_warehouse"` per surface so downstream agents
  can tell ledger-replayed reports apart from live-fetched ones.
* Open / closed positions selected via the as-of snapshot query
  (D6) — `is_latest` is reserved for warehouse-head queries.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import duckdb

from sniper.analysis.wallet_protocol.ledger.paths import ledger_paths
from sniper.analysis.wallet_protocol.ledger.price_history import (
    PriceBarsResult,
    PriceHistoryFn,
    make_ledger_price_history_fn,
)
from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow
from sniper.analysis.wallet_protocol.types import WalletData


PeerBaselineFn = Callable[[str, str, str], tuple[float, int] | None]


_LEDGER_SOURCE = "duckdb_warehouse"
# Mirrors `build_price_history_fn`'s default and the live CLOB endpoint's
# 60-second bar fidelity. Keeping the value local avoids importing the
# live module here (Slice 5 forbids live imports).
_PRICE_HISTORY_FIDELITY_MINUTES = 1


# ---------------------------------------------------------------------------
# Bundle dataclass
# ---------------------------------------------------------------------------


@dataclass
class MaterializedWalletData:
    """Output of `materialize_wallet_data`.

    Field names match `analyze_wallet()` kwargs verbatim so the bundle
    can be splatted in via `to_analyze_wallet_kwargs()` without any
    protocol-side changes.

    `get_price_bars` is a Slice 5/Task 6 placeholder kept here so the
    field exists on the bundle now; Task 6 will populate it. It is not
    consumed by `analyze_wallet`.
    """

    wallet_data: WalletData
    price_history_fn: PriceHistoryFn
    peer_baseline_fn: PeerBaselineFn | None
    leafs_by_cid: dict[str, list[str]] | None
    nav_lookup: dict[str, str] | None

    window: AnalysisWindow
    replay_surface_meta: dict[str, dict[str, Any]] = field(default_factory=dict)
    manifest_hashes: dict[str, str] = field(default_factory=dict)
    row_counts: dict[str, int] = field(default_factory=dict)

    # Diagnostics-only surface (Task 6 populates).
    get_price_bars: Callable[[str], PriceBarsResult] | None = None

    def to_analyze_wallet_kwargs(self) -> dict[str, Any]:
        """Return the exact set of kwargs `analyze_wallet()` accepts."""
        return {
            "pre_fetched_data": self.wallet_data,
            "price_history_fn": self.price_history_fn,
            "peer_baseline_fn": self.peer_baseline_fn,
            "leafs_by_cid": self.leafs_by_cid,
            "nav_lookup": self.nav_lookup,
        }


# ---------------------------------------------------------------------------
# Internal readers
# ---------------------------------------------------------------------------


def _connect_read_only(path: Path) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(path), read_only=True)


def _rows_to_dicts(result: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    columns = [col[0] for col in result.description]
    return [dict(zip(columns, row)) for row in result.fetchall()]


def _meta_for(
    *,
    surface: str,
    rows: Sequence[Mapping[str, Any]],
    captured_at_field: str,
    wallet_path: Path,
    window: AnalysisWindow,
) -> dict[str, Any]:
    """Build the per-surface `replay_surface_meta` entry described in D4."""
    captured = [
        int(row[captured_at_field])
        for row in rows
        if row.get(captured_at_field) is not None
    ]
    return {
        "ledger_source": _LEDGER_SOURCE,
        "wallet_path": str(wallet_path),
        "window_id": window.window_id,
        "capture_run_ids": list(window.capture_run_ids),
        "source_versions": dict(window.source_versions),
        "row_count": len(rows),
        "captured_at_min": min(captured) if captured else None,
        "captured_at_max": max(captured) if captured else None,
    }


def _read_events(
    wallet_conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    window: AnalysisWindow,
) -> list[dict[str, Any]]:
    """Read `wallet_events` filtered to the window + ingest_status='normalized'."""
    return _rows_to_dicts(
        wallet_conn.execute(
            """
            SELECT
                event_uid,
                source_event_uid,
                canonical_event_hash,
                event_row_ordinal,
                source_event_type,
                ts,
                block_number,
                condition_id,
                token_id,
                outcome_index,
                outcome_label,
                market_structure,
                side,
                size_tokens,
                collateral_amount,
                fee_collateral,
                collateral_token,
                collateral_token_address,
                collateral_display_unit,
                usd_equivalent_amount,
                price,
                transaction_hash,
                log_index,
                order_hash,
                maker,
                taker,
                ingest_status,
                dedup_status,
                normalization_notes_json,
                normalization_version,
                source,
                source_version,
                capture_run_id,
                captured_at
            FROM wallet_events
            WHERE wallet = ?
              AND ingest_status = 'normalized'
              AND ts BETWEEN ? AND ?
            ORDER BY ts ASC, event_row_ordinal ASC, event_uid ASC
            """,
            [wallet, window.start_ts, window.end_ts],
        )
    )


def _read_positions_as_of(
    wallet_conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    window: AnalysisWindow,
) -> list[dict[str, Any]]:
    """As-of snapshot per `position_uid` at `window.end_ts` (D6).

    `is_latest` is intentionally NOT used — it points to warehouse-head
    state, which can be wrong for an analysis window that ends before
    head.
    """
    return _rows_to_dicts(
        wallet_conn.execute(
            """
            WITH ranked AS (
                SELECT
                    position_uid,
                    condition_id,
                    token_id,
                    outcome_index,
                    outcome_label,
                    market_structure,
                    status,
                    is_latest,
                    opened_at,
                    opened_at_source,
                    closed_at,
                    closed_at_source,
                    avg_price,
                    size_tokens,
                    cost_basis_collateral,
                    total_bought_collateral,
                    total_sold_collateral,
                    realized_pnl_collateral,
                    unrealized_pnl_collateral,
                    current_price,
                    collateral_token,
                    collateral_token_address,
                    api_total_bought_raw,
                    api_initial_value_raw,
                    normalization_notes_json,
                    raw_position_json,
                    is_finalized,
                    finalized_at,
                    finalization_reason,
                    source,
                    source_version,
                    capture_run_id,
                    captured_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY position_uid
                        ORDER BY captured_at DESC, capture_run_id DESC
                    ) AS rn
                FROM wallet_positions
                WHERE wallet = ?
                  AND captured_at <= ?
            )
            SELECT * FROM ranked
            WHERE rn = 1
            ORDER BY position_uid
            """,
            [wallet, window.end_ts],
        )
    )


def _read_probes(
    wallet_conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    window: AnalysisWindow,
) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        wallet_conn.execute(
            """
            SELECT *
            FROM wallet_execution_probes
            WHERE wallet = ?
              AND fill_ts BETWEEN ? AND ?
            ORDER BY fill_ts ASC
            """,
            [wallet, window.start_ts, window.end_ts],
        )
    )


def _read_flips(
    wallet_conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    window: AnalysisWindow,
) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        wallet_conn.execute(
            """
            SELECT *
            FROM wallet_reconstructed_flips
            WHERE wallet = ?
              AND sell_ts BETWEEN ? AND ?
            ORDER BY sell_ts ASC, buy_ts ASC, flip_uid ASC
            """,
            [wallet, window.start_ts, window.end_ts],
        )
    )


def _read_manifests(
    wallet_conn: duckdb.DuckDBPyConnection,
    *,
    wallet: str,
    window: AnalysisWindow,
) -> list[dict[str, Any]]:
    return _rows_to_dicts(
        wallet_conn.execute(
            """
            SELECT *
            FROM analysis_price_manifest
            WHERE wallet = ?
              AND window_id = ?
            ORDER BY token_id, range_start_ts, range_end_ts
            """,
            [wallet, window.window_id],
        )
    )


def _read_price_points_meta(
    market_conn: duckdb.DuckDBPyConnection,
    *,
    window: AnalysisWindow,
    fidelity_minutes: int,
) -> list[dict[str, Any]]:
    """Pull captured price points inside the window for `replay_surface_meta`.

    The price closures themselves are built by `make_ledger_price_history_fn`
    with the same connection; this helper supplies the rows used to compute
    `prices` row counts, captured_at extents, and the latest-bar lookup
    dict that lives on `WalletData.price_history_lookups`.
    """
    return _rows_to_dicts(
        market_conn.execute(
            """
            SELECT token_id, fidelity_minutes, ts, price, source_version, captured_at
            FROM market_price_points
            WHERE point_status = 'captured'
              AND fidelity_minutes = ?
              AND ts BETWEEN ? AND ?
            ORDER BY token_id, ts
            """,
            [fidelity_minutes, window.start_ts, window.end_ts],
        )
    )


# ---------------------------------------------------------------------------
# Activity-row reconstruction (event_uid stamped for downstream joins)
# ---------------------------------------------------------------------------


def _activity_row_from_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Reconstruct a single Data API activity-row shape from a wallet_events row.

    Mirrors `events.upsert_activity_events`'s field map so downstream
    callers see the same keys they would on a live fetch. The `event_uid`
    column is preserved for FK joins back to `wallet_events`.
    """
    return {
        "event_uid": event["event_uid"],
        "type": event["source_event_type"],
        "side": event["side"],
        "conditionId": event["condition_id"],
        "asset": event["token_id"],
        "outcomeIndex": event["outcome_index"],
        "outcome": event["outcome_label"],
        "marketStructure": event["market_structure"],
        "market_structure": event["market_structure"],
        "outcome_index": event["outcome_index"],
        "token_id": event["token_id"],
        "timestamp": event["ts"],
        "ts": event["ts"],
        "blockNumber": event["block_number"],
        "price": event["price"],
        "size": event["size_tokens"],
        "usdcSize": event["collateral_amount"],
        "fee": event["fee_collateral"],
        "collateral_token": event["collateral_token"],
        "usd_equivalent_amount": event["usd_equivalent_amount"],
        "transactionHash": event["transaction_hash"],
        "logIndex": event["log_index"],
        "orderHash": event["order_hash"],
        "maker": event["maker"],
        "taker": event["taker"],
    }


# ---------------------------------------------------------------------------
# Position-row reconstruction (open / closed shape for analyze_wallet)
# ---------------------------------------------------------------------------


def _normalize_for_walletdata(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return the as-of position row decorated with the camelCase keys
    `analyze_wallet` consumers expect (mirrors `/positions` JSON).

    DuckDB column names are kept as `current_price`, `closed_at`, etc.
    so existing ledger-aware tests can still query them. Aliases that
    match the live API shape are added without removing the canonical
    names.
    """
    out = dict(row)
    out.setdefault("conditionId", row.get("condition_id"))
    out.setdefault("asset", row.get("token_id"))
    out.setdefault("outcomeIndex", row.get("outcome_index"))
    out.setdefault("outcome", row.get("outcome_label"))
    out.setdefault("avgPrice", row.get("avg_price"))
    out.setdefault("curPrice", row.get("current_price"))
    out.setdefault("realizedPnl", row.get("realized_pnl_collateral"))
    out.setdefault("openedAt", row.get("opened_at"))
    # Codex round 2 audit: analyze_wallet reads `size` for open positions
    # and `totalBought` for closed positions (per PR #78), but the alias
    # chain didn't expose either field. The ledger writer preserves the
    # live-API value as api_total_bought_raw (positions.py:189) and the
    # share count as size_tokens.
    if out.get("size") is None and row.get("size_tokens") is not None:
        out["size"] = row.get("size_tokens")
    # Codex round 3 audit: ONLY source totalBought from api_total_bought_raw
    # (shares, per Polymarket /closed-positions). Do NOT fall back to
    # total_bought_collateral — schema (positions.py:131) defines that as
    # cost basis in collateral/USDC, a DIFFERENT unit. analyze_wallet does
    # `avgPrice * totalBought`; a USDC fallback would double-count.
    if out.get("totalBought") is None and row.get("api_total_bought_raw") is not None:
        out["totalBought"] = row.get("api_total_bought_raw")
    # Polymarket's /closed-positions uses `timestamp` for the close ts;
    # Stage 1 (`_last_activity_ts`, `_recent_90d_fraction`) and the
    # held-branch CUSUM post-regime sort key both read it. The ledger
    # writer stores it as `closed_at`, so re-attach the live-shape alias.
    if row.get("closed_at") is not None:
        out.setdefault("timestamp", row.get("closed_at"))
    return out


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def materialize_wallet_data(
    *,
    wallet: str,
    window: AnalysisWindow,
    ledger_root: str | Path,
) -> MaterializedWalletData:
    """Read ledger facts for `wallet` in `window` and return the bundle.

    Args:
        wallet: 0x-prefixed wallet address. Normalized via `ledger_paths`.
        window: immutable `AnalysisWindow` identifying the materialization.
        ledger_root: directory containing `<wallet>.duckdb`,
            `market_data.duckdb`, and `index.duckdb`.

    Returns:
        `MaterializedWalletData` whose `to_analyze_wallet_kwargs()` is
        the analyze-wallet adapter.
    """
    paths = ledger_paths(ledger_root, wallet)
    wallet_lc = paths.wallet

    with _connect_read_only(paths.wallet_path) as wallet_conn, _connect_read_only(
        paths.market_data_path
    ) as market_conn:
        events = _read_events(wallet_conn, wallet=wallet_lc, window=window)
        positions = _read_positions_as_of(
            wallet_conn, wallet=wallet_lc, window=window
        )
        probes = _read_probes(wallet_conn, wallet=wallet_lc, window=window)
        flips = _read_flips(wallet_conn, wallet=wallet_lc, window=window)
        manifests = _read_manifests(wallet_conn, wallet=wallet_lc, window=window)
        price_points = _read_price_points_meta(
            market_conn,
            window=window,
            fidelity_minutes=_PRICE_HISTORY_FIDELITY_MINUTES,
        )
        price_history_fn, get_price_bars = make_ledger_price_history_fn(
            market_conn,
            window=window,
            fidelity_minutes=_PRICE_HISTORY_FIDELITY_MINUTES,
        )

    activity_rows = [_activity_row_from_event(ev) for ev in events]

    open_positions = [
        _normalize_for_walletdata(row)
        for row in positions
        if row["status"] == "open"
    ]
    closed_positions = [
        _normalize_for_walletdata(row)
        for row in positions
        if (
            isinstance(row["status"], str)
            and row["status"].startswith("closed_")
            and row["closed_at"] is not None
            and window.start_ts <= int(row["closed_at"]) <= window.end_ts
        )
    ]

    # `price_history_lookups` doubles as the window-level pricing surface
    # for downstream consumers that don't want to call the closure
    # directly. The contract is "latest bar per token within window".
    latest_per_token: dict[str, float | None] = {}
    for row in price_points:
        token_id = row.get("token_id")
        ts = row.get("ts")
        price = row.get("price")
        if not token_id or ts is None:
            continue
        if window.start_ts <= int(ts) <= window.end_ts:
            latest_per_token[str(token_id)] = (
                None if price is None else float(price)
            )

    replay_surface_meta = {
        "events": _meta_for(
            surface="events",
            rows=events,
            captured_at_field="captured_at",
            wallet_path=paths.wallet_path,
            window=window,
        ),
        "positions": _meta_for(
            surface="positions",
            rows=positions,
            captured_at_field="captured_at",
            wallet_path=paths.wallet_path,
            window=window,
        ),
        "probes": _meta_for(
            surface="probes",
            rows=probes,
            captured_at_field="captured_at",
            wallet_path=paths.wallet_path,
            window=window,
        ),
        "flips": _meta_for(
            surface="flips",
            rows=flips,
            captured_at_field="captured_at",
            wallet_path=paths.wallet_path,
            window=window,
        ),
        "prices": _meta_for(
            surface="prices",
            rows=price_points,
            captured_at_field="captured_at",
            wallet_path=paths.wallet_path,
            window=window,
        ),
        "manifests": _meta_for(
            surface="manifests",
            rows=manifests,
            captured_at_field="captured_at",
            wallet_path=paths.wallet_path,
            window=window,
        ),
    }

    row_counts = {surface: meta["row_count"] for surface, meta in replay_surface_meta.items()}
    manifest_hashes = {
        f"{row['token_id']}:{int(row['range_start_ts'])}:{int(row['range_end_ts'])}": row["content_hash"]
        for row in manifests
    }

    fetched_at = max(
        (int(ev["captured_at"]) for ev in events if ev.get("captured_at") is not None),
        default=window.end_ts,
    )

    wallet_data = WalletData(
        wallet=wallet_lc,
        closed_positions=closed_positions,
        open_positions=open_positions,
        activity_rows=activity_rows,
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=fetched_at,
        capital_ledger=None,
        portfolio_value_usdc=None,
        fetch_quality_flags=[],
        subgraph_fallback_used=False,
        price_history_lookups=latest_per_token,
        leafs_by_cid=None,
        nav_lookup=None,
        peer_baselines=None,
        replay_surface_meta=replay_surface_meta,
    )

    return MaterializedWalletData(
        wallet_data=wallet_data,
        price_history_fn=price_history_fn,
        peer_baseline_fn=None,
        leafs_by_cid=None,
        nav_lookup=None,
        window=window,
        replay_surface_meta=replay_surface_meta,
        manifest_hashes=manifest_hashes,
        row_counts=row_counts,
        get_price_bars=get_price_bars,
    )


__all__ = [
    "MaterializedWalletData",
    "PriceHistoryFn",
    "PeerBaselineFn",
    "materialize_wallet_data",
]

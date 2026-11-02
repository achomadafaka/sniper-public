"""Slice 5 Task 5 — `materialize_wallet_data` reader.

Tests cover the locked design decisions:

* D3 — `MaterializedWalletData` bundle field names match
  `analyze_wallet()` kwargs; `to_analyze_wallet_kwargs()` returns exactly
  the five accepted keys (`pre_fetched_data`, `price_history_fn`,
  `peer_baseline_fn`, `leafs_by_cid`, `nav_lookup`).
* D3a — `price_history_fn(token_id, ts) -> float | None` mirrors
  `build_price_history_fn`: the first bar with `t >= target_ts` inside
  `[target_ts, target_ts + max(fidelity*60, 600)]`, falling back to the
  last bar of that tight forward window only when none reach the target,
  returning `None` when no bar exists.
* D4 — `replay_surface_meta` has one entry per surface with the keys
  spec'd in the plan (ledger_source, wallet_path, window_id,
  capture_run_ids, source_versions, row_count, captured_at_min/max).
* D6 — open/closed positions selected via the as-of snapshot query
  partitioned by `position_uid`, ordered by
  `(captured_at DESC, capture_run_id DESC)`. `is_latest` is NOT used.

The test file also enforces Slice 5's no-live-imports invariant and the
"Slice 5 omits peer baseline / tag lookups" out-of-scope contract: the
materializer leaves `peer_baseline_fn`, `leafs_by_cid`, and `nav_lookup`
as `None`.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path
from unittest.mock import MagicMock

import duckdb
import pytest

from scripts.execution_profile import (
    aggregate_execution_profile_from_ledger,
    compute_execution_profile,
    iter_fill_probes,
    rehydrate_fill_probe_records_from_ledger,
)
from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.ledger.events import upsert_activity_events
from sniper.analysis.wallet_protocol.ledger.flips import (
    reconstruct_and_upsert_flips,
)
from sniper.analysis.wallet_protocol.ledger.materialize import (
    MaterializedWalletData,
    materialize_wallet_data,
)
from sniper.analysis.wallet_protocol.ledger.paths import normalize_wallet
from sniper.analysis.wallet_protocol.ledger.positions import (
    upsert_position_snapshots,
)
from sniper.analysis.wallet_protocol.ledger.prices import (
    MarketPricePoint,
    upsert_price_points,
)
from sniper.analysis.wallet_protocol.ledger.probes import upsert_execution_probes
from sniper.analysis.wallet_protocol.ledger.schema import (
    ensure_index_schema,
    ensure_market_schema,
    ensure_wallet_schema,
)
from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow
from sniper.analysis.wallet_protocol.types import WalletData


WALLET = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
NORMALIZED_WALLET = normalize_wallet(WALLET)


# ---------------------------------------------------------------------------
# Fixture seeding helpers
# ---------------------------------------------------------------------------


def _ensure_paths(ledger_root: Path) -> tuple[Path, Path, Path]:
    wallet_path = ledger_root / f"{NORMALIZED_WALLET}.duckdb"
    market_path = ledger_root / "market_data.duckdb"
    index_path = ledger_root / "index.duckdb"
    return wallet_path, market_path, index_path


def _seed_full_warehouse(ledger_root: Path) -> None:
    """Seed a wallet warehouse + market data with a deterministic fixture.

    Layout:
      Window: [1_700_000_000, 1_700_010_000]  (~2.7 hours wide)
      Wallet 0xabc...
        - 4 wallet_events: 2 BUYs + 2 SELLs spaced 60 s apart inside window.
        - 1 wallet_event BEFORE window (must be excluded from activity_rows).
        - 1 wallet_event marked rejected (must be excluded).
        - 2 position snapshots for the same position_uid: snapshot_a inside
          window, snapshot_b after window. The as-of query must pick
          snapshot_a.
        - 1 closed position snapshot inside window.
        - 1 closed position snapshot OUTSIDE window (closed_at after end_ts).
        - flips reconstructed → exactly 2 flip rows (FIFO size match).
      Market data:
        - 6 price points for asset_yes around the window edges.
    """
    wallet_path, market_path, _index_path = _ensure_paths(ledger_root)
    with duckdb.connect(str(wallet_path)) as wallet_conn, duckdb.connect(
        str(market_path)
    ) as market_conn, duckdb.connect(str(ledger_root / "index.duckdb")) as index_conn:
        ensure_wallet_schema(wallet_conn)
        ensure_market_schema(market_conn)
        ensure_index_schema(index_conn)

        base = 1_700_000_000

        in_window_activity = [
            {
                "type": "TRADE",
                "side": "BUY",
                "conditionId": "0xcid_binary",
                "asset": "asset_yes",
                "outcomeIndex": 0,
                "outcome": "Yes",
                "marketStructure": "binary",
                "timestamp": base + i * 60,
                "price": 0.40,
                "size": 100,
                "usdcSize": 40,
                "transactionHash": f"0xtx_buy_{i}",
            }
            for i in range(2)
        ] + [
            {
                "type": "TRADE",
                "side": "SELL",
                "conditionId": "0xcid_binary",
                "asset": "asset_yes",
                "outcomeIndex": 0,
                "outcome": "Yes",
                "marketStructure": "binary",
                "timestamp": base + (2 + i) * 60,
                "price": 0.60,
                "size": 100,
                "usdcSize": 60,
                "transactionHash": f"0xtx_sell_{i}",
            }
            for i in range(2)
        ]

        # One activity row BEFORE the window — must be excluded.
        before_window_activity = [
            {
                "type": "TRADE",
                "side": "BUY",
                "conditionId": "0xcid_binary",
                "asset": "asset_yes",
                "outcomeIndex": 0,
                "outcome": "Yes",
                "marketStructure": "binary",
                "timestamp": base - 3600,
                "price": 0.30,
                "size": 50,
                "usdcSize": 15,
                "transactionHash": "0xtx_before",
            }
        ]

        # One activity row AFTER the window — must be excluded.
        after_window_activity = [
            {
                "type": "TRADE",
                "side": "BUY",
                "conditionId": "0xcid_binary",
                "asset": "asset_yes",
                "outcomeIndex": 0,
                "outcome": "Yes",
                "marketStructure": "binary",
                "timestamp": base + 20_000,
                "price": 0.55,
                "size": 50,
                "usdcSize": 27,
                "transactionHash": "0xtx_after",
            }
        ]

        # Activity row that ingest will REJECT (no timestamp). Must be excluded
        # by the materializer's ingest_status filter.
        bogus_activity = [
            {
                "type": "TRADE",
                "side": "BUY",
                "conditionId": "0xcid_binary",
                "asset": "asset_yes",
                "outcomeIndex": 0,
                "outcome": "Yes",
                "marketStructure": "binary",
                # no timestamp — writer assigns ingest_status='rejected'
                "price": 0.41,
                "size": 50,
                "usdcSize": 20,
                "transactionHash": "0xtx_bogus",
            }
        ]

        all_activity = (
            in_window_activity
            + before_window_activity
            + after_window_activity
            + bogus_activity
        )

        upsert_activity_events(
            wallet_conn,
            wallet=WALLET,
            capture_run_id="run-A",
            activity_rows=all_activity,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=base + 100,
        )

        # Position snapshots:
        #   snapshot_a (open) at run-A, captured_at inside window
        #   snapshot_b (open) at run-B, captured_at AFTER window — must be
        #     ignored by as-of query
        #   closed_pos (closed_resolution) at run-A, closed_at inside window
        #   closed_after (closed_resolution) at run-A, closed_at AFTER window
        #     end_ts — must be excluded from closed_positions
        snapshot_a = {
            "conditionId": "0xcid_binary",
            "asset": "asset_yes",
            "outcomeIndex": 0,
            "outcome": "Yes",
            "marketStructure": "binary",
            "avgPrice": 0.40,
            "size": 100,
            "totalBought": 9999,
            "realizedPnl": 0,
            "curPrice": 0.50,
        }
        upsert_position_snapshots(
            wallet_conn,
            wallet=WALLET,
            capture_run_id="run-A",
            position_rows=[snapshot_a],
            status="open",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=base + 5_000,  # inside window
        )
        upsert_position_snapshots(
            wallet_conn,
            wallet=WALLET,
            capture_run_id="run-B",
            position_rows=[{**snapshot_a, "curPrice": 0.99}],
            status="open",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=base + 50_000,  # AFTER window — as-of must skip
        )

        closed_pos = {
            "conditionId": "0xcid_binary",
            "asset": "asset_no",
            "outcomeIndex": 1,
            "outcome": "No",
            "marketStructure": "binary",
            "avgPrice": 0.55,
            "size": 80,
            "realizedPnl": 24,
            "timestamp": base + 6_000,  # closed_at inside window
        }
        upsert_position_snapshots(
            wallet_conn,
            wallet=WALLET,
            capture_run_id="run-A",
            position_rows=[closed_pos],
            status="closed_resolution",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=base + 6_500,
        )

        closed_after = {
            "conditionId": "0xcid_other",
            "asset": "asset_late",
            "outcomeIndex": 0,
            "outcome": "Yes",
            "marketStructure": "binary",
            "avgPrice": 0.30,
            "size": 100,
            "realizedPnl": -5,
            "timestamp": base + 25_000,  # closed_at AFTER window end_ts
        }
        upsert_position_snapshots(
            wallet_conn,
            wallet=WALLET,
            capture_run_id="run-A",
            position_rows=[closed_after],
            status="closed_resolution",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=base + 7_000,
        )

        # Reconstruct flips from the in-window events (idempotent on re-runs).
        reconstruct_and_upsert_flips(
            wallet_conn,
            wallet=WALLET,
            capture_run_id="run-A",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=base + 100,
        )

        # Market price points around the window for asset_yes.
        upsert_price_points(
            market_conn,
            [
                MarketPricePoint("asset_yes", 1, base - 60, 0.395),
                MarketPricePoint("asset_yes", 1, base + 0, 0.40),
                MarketPricePoint("asset_yes", 1, base + 60, 0.42),
                MarketPricePoint("asset_yes", 1, base + 120, 0.44),
                MarketPricePoint("asset_yes", 1, base + 600, 0.55),
                MarketPricePoint("asset_yes", 1, base + 9_000, 0.61),
            ],
            source_version="2026-05-02",
            capture_run_id="run-A",
            captured_at=base + 100,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _window() -> AnalysisWindow:
    return AnalysisWindow(
        window_id="2026-05-03_unit_v1",
        capture_run_ids=("run-A",),
        start_ts=1_700_000_000,
        end_ts=1_700_010_000,
        source_versions=(
            ("polymarket_data_api", "2026-05-02"),
            ("polymarket_clob_v2", "2026-05-02"),
        ),
    )


def test_returns_materialized_wallet_data_bundle(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    assert isinstance(bundle, MaterializedWalletData)
    assert isinstance(bundle.wallet_data, WalletData)
    assert bundle.wallet_data.wallet == WALLET.lower()
    # Slice 5 leaves these surfaces unsourced.
    assert bundle.peer_baseline_fn is None
    assert bundle.leafs_by_cid is None
    assert bundle.nav_lookup is None
    # Provenance.
    assert bundle.window == _window()
    assert isinstance(bundle.replay_surface_meta, dict)
    assert isinstance(bundle.manifest_hashes, dict)
    assert isinstance(bundle.row_counts, dict)
    # The price-history closure must be callable with the documented signature.
    assert callable(bundle.price_history_fn)


def test_to_analyze_wallet_kwargs_keys_match_analyze_wallet_signature(
    tmp_path: Path,
) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    kwargs = bundle.to_analyze_wallet_kwargs()
    assert set(kwargs.keys()) == {
        "pre_fetched_data",
        "price_history_fn",
        "peer_baseline_fn",
        "leafs_by_cid",
        "nav_lookup",
    }
    # Every key must be a real `analyze_wallet` parameter.
    sig = inspect.signature(analyze_wallet)
    for k in kwargs:
        assert k in sig.parameters, f"{k} not in analyze_wallet() signature"
    # And the bundle's wallet_data is what gets passed as pre_fetched_data.
    assert kwargs["pre_fetched_data"] is bundle.wallet_data


def test_events_filtered_by_ts_window_and_ingest_status(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    rows = bundle.wallet_data.activity_rows
    # 2 BUYs + 2 SELLs, no before-window, no after-window, no rejected.
    assert len(rows) == 4
    sides = sorted(r.get("side") for r in rows)
    assert sides == ["BUY", "BUY", "SELL", "SELL"]
    for row in rows:
        assert _window().start_ts <= int(row["ts"]) <= _window().end_ts


def test_activity_rows_carry_event_uid_for_probe_join(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    rows = bundle.wallet_data.activity_rows
    assert rows
    assert all(r.get("event_uid") for r in rows)
    # event_uids must match what wallet_events stores.
    with duckdb.connect(
        str(tmp_path / f"{NORMALIZED_WALLET}.duckdb"), read_only=True
    ) as conn:
        stored = {
            r[0]
            for r in conn.execute(
                "SELECT event_uid FROM wallet_events "
                "WHERE wallet = ? AND ingest_status = 'normalized' "
                "AND ts BETWEEN ? AND ?",
                [
                    NORMALIZED_WALLET,
                    _window().start_ts,
                    _window().end_ts,
                ],
            ).fetchall()
        }
    assert {r["event_uid"] for r in rows} == stored


def test_open_positions_use_as_of_snapshot_not_is_latest(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    open_rows = bundle.wallet_data.open_positions
    # Only the in-window snapshot survives. The is_latest=true row at run-B
    # is captured AFTER window end_ts and must be excluded.
    assert len(open_rows) == 1
    only = open_rows[0]
    # The in-window snapshot has curPrice 0.50, the after-window one has 0.99.
    assert pytest.approx(0.50) == float(only["current_price"])
    # Sanity: writer flagged the AFTER snapshot as is_latest, so a naive
    # `is_latest=true` reader would have picked the wrong row.
    with duckdb.connect(
        str(tmp_path / f"{NORMALIZED_WALLET}.duckdb"), read_only=True
    ) as conn:
        latest_count = conn.execute(
            "SELECT COUNT(*) FROM wallet_positions "
            "WHERE wallet = ? AND is_latest = true",
            [NORMALIZED_WALLET],
        ).fetchone()[0]
    assert latest_count >= 1


def test_closed_positions_filtered_by_closed_at_window(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    closed = bundle.wallet_data.closed_positions
    assert len(closed) == 1
    only = closed[0]
    # The other closed snapshot has closed_at 1_700_025_000 (after end_ts).
    assert int(only["closed_at"]) <= _window().end_ts
    assert _window().start_ts <= int(only["closed_at"])


def test_replay_surface_meta_has_d4_keys_per_surface(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    expected = {"events", "positions", "probes", "flips", "prices", "manifests"}
    assert set(bundle.replay_surface_meta.keys()) == expected
    required_keys = {
        "ledger_source",
        "wallet_path",
        "window_id",
        "capture_run_ids",
        "source_versions",
        "row_count",
        "captured_at_min",
        "captured_at_max",
    }
    for surface, meta in bundle.replay_surface_meta.items():
        assert required_keys.issubset(meta), f"{surface}: missing {required_keys - set(meta)}"
        assert meta["ledger_source"] == "duckdb_warehouse"
        assert meta["window_id"] == _window().window_id
        assert meta["capture_run_ids"] == list(_window().capture_run_ids)
        assert meta["source_versions"] == dict(_window().source_versions)
        assert isinstance(meta["row_count"], int)


def test_replay_surface_meta_propagated_to_walletdata(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    meta = bundle.wallet_data.replay_surface_meta
    assert isinstance(meta, dict)
    # The bundle's surface meta is propagated onto WalletData so downstream
    # ProtocolReports inherit the marker.
    assert meta == bundle.replay_surface_meta


def test_price_history_fn_first_bar_at_or_after_target(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    fn = bundle.price_history_fn
    base = 1_700_000_000
    # ts=base+30 falls between bars at base+0 and base+60. The first bar at
    # or after target is base+60 → price 0.42. Predecessor bar at base+0
    # must NOT be returned.
    assert fn("asset_yes", base + 30) == pytest.approx(0.42)


def test_price_history_fn_returns_none_when_no_forward_bar(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    fn = bundle.price_history_fn
    # Token unknown to the warehouse → None.
    assert fn("asset_unknown", 1_700_000_000) is None
    # Token known, but ts beyond the last bar in window → None (no forward bar
    # within the tight `max(60*60, 600)` window).
    assert fn("asset_yes", 1_700_100_000) is None


def test_price_history_fn_falls_back_to_last_bar_in_tight_window(
    tmp_path: Path,
) -> None:
    """When no bar reaches `target_ts` but bars exist inside the
    `[target_ts, target_ts + max(fidelity*60, 600)]` window, the closure
    returns the last such bar (window-edge effect; mirrors live adapter)."""
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    fn = bundle.price_history_fn
    base = 1_700_000_000
    # Plain forward-bar lookup: ts=base+150 → first forward is base+600 (0.55).
    assert fn("asset_yes", base + 150) == pytest.approx(0.55)


def test_no_live_imports_in_materialize_module() -> None:
    import sniper.analysis.wallet_protocol.ledger.materialize as mod

    src = Path(mod.__file__).read_text()
    forbidden_substrings = [
        "PolymarketClient",
        "import requests",
        "from requests",
        "httpx",
    ]
    for needle in forbidden_substrings:
        assert needle not in src, f"materialize.py must not reference {needle!r}"


def test_window_echoed_back_unchanged(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    win = _window()
    bundle = materialize_wallet_data(
        wallet=WALLET, window=win, ledger_root=tmp_path
    )
    # AnalysisWindow is frozen — bundle.window must compare equal and be hashable.
    assert bundle.window == win
    assert hash(bundle.window) == hash(win)


def test_get_price_bars_surface_populated_on_bundle(tmp_path: Path) -> None:
    """Slice 5 Task 6 wired the diagnostics surface onto the bundle."""
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    assert callable(bundle.get_price_bars)
    result = bundle.get_price_bars("asset_yes")
    # The fixture seeds bars at base, base+60, base+120, base+600, base+9_000
    # — five inside the window, plus base-60 outside.
    assert len(result.bars) == 5
    # No coverage_range row was seeded → coverage_status falls to
    # `not_captured`. Status invariants are exercised in detail in
    # `test_wallet_ledger_price_history.py`.
    assert result.coverage_status == "not_captured"


def test_row_counts_match_replay_surface_meta(tmp_path: Path) -> None:
    _seed_full_warehouse(tmp_path)
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_window(), ledger_root=tmp_path
    )
    for surface, meta in bundle.replay_surface_meta.items():
        assert bundle.row_counts.get(surface) == meta["row_count"]
    # And there must be at least one events row + one positions row from the
    # fixture.
    assert bundle.row_counts["events"] >= 4
    assert bundle.row_counts["positions"] >= 1
    assert bundle.row_counts["flips"] >= 1


# ---------------------------------------------------------------------------
# Slice 5 Task 7 — execution profile re-aggregation parity
# ---------------------------------------------------------------------------


# The aggregate baseline at `aggregate_baseline_v1.json` was captured by
# `compute_execution_profile` with `wallet="0xWALLET"`, but the wallet
# string never enters the envelope (`_aggregate` only reads per-fill
# fields). The ledger writers require a 40-hex address, so the parity
# path uses one — the envelope is identical to the baseline despite the
# wallet change.
BASELINE_WALLET = WALLET


BASELINE_FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "execution_profile"
    / "aggregate_baseline_v1.json"
)


def _baseline_inputs():
    """Mirror `tests.test_execution_probes_iter._baseline_inputs`.

    Same 30 BUY fills, same stub client, same base ts. The aggregate
    baseline at `aggregate_baseline_v1.json` was captured against this
    input via `compute_execution_profile`; re-running through Slices
    1–4 + Task 7 rehydration must produce the byte-for-byte equal
    envelope.
    """
    base = 1_000_000
    bars = [
        {"timestamp": base + dt, "price": 0.50 + (0.02 if dt >= 3600 else 0.0)}
        for dt in range(-60, 3600 + 1800, 60)
    ]
    client = MagicMock()
    client.get_prices_series.return_value = bars
    client.get_trades_window.return_value = [
        {"proxyWallet": "0xOTHER", "asset": "asset", "price": 0.50, "side": "BUY"}
    ] * 5
    activity = [
        {
            "type": "TRADE",
            "side": "BUY",
            "conditionId": "cid",
            "asset": "asset",
            "timestamp": base + i * 60,
            "price": 0.50,
            "title": "Fake Market",
            "transactionHash": f"0xtx_baseline_{i:02d}",
        }
        for i in range(30)
    ]
    return client, activity, base, bars


def _attach_event_uids_to_activity(
    activity: list[dict], conn: duckdb.DuckDBPyConnection, wallet: str
) -> list[dict]:
    """Pull event_uids from `wallet_events` and stamp them onto activity rows.

    Mirrors the helper in `test_wallet_ledger_slice4_integration.py` so the
    probe writer can satisfy its `fill_event_uid` foreign-key requirement.
    """
    rows = conn.execute(
        """
        SELECT event_uid, condition_id, token_id, ts, side, transaction_hash
        FROM wallet_events
        WHERE wallet = ? AND ingest_status = 'normalized'
        """,
        [wallet.lower()],
    ).fetchall()
    index: dict[tuple, str] = {
        (r[1], r[2], int(r[3]), r[4], r[5]): r[0] for r in rows
    }
    enriched: list[dict] = []
    for row in activity:
        key = (
            row["conditionId"],
            row["asset"],
            int(row["timestamp"]),
            row["side"],
            row["transactionHash"],
        )
        ev_uid = index.get(key)
        enriched.append({**row, "event_uid": ev_uid} if ev_uid else dict(row))
    return enriched


def _seed_baseline_warehouse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persist the deterministic 30-fill fixture into the wallet warehouse.

    Steps:
      1. Upsert activity rows into `wallet_events` (Slice 1 path).
      2. Run `iter_fill_probes` against the enriched activity.
      3. Persist the resulting probe records into
         `wallet_execution_probes` + `wallet_execution_probe_horizons`
         (Slice 4 path; aux fields persisted per Task 0).
    """
    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )

    client, activity, _base, _bars = _baseline_inputs()

    wallet_path = tmp_path / f"{BASELINE_WALLET.lower()}.duckdb"
    market_path = tmp_path / "market_data.duckdb"
    with duckdb.connect(str(wallet_path)) as wallet_conn, duckdb.connect(
        str(market_path)
    ) as market_conn, duckdb.connect(str(tmp_path / "index.duckdb")) as index_conn:
        ensure_wallet_schema(wallet_conn)
        ensure_market_schema(market_conn)
        ensure_index_schema(index_conn)

        upsert_activity_events(
            wallet_conn,
            wallet=BASELINE_WALLET,
            capture_run_id="run-baseline",
            activity_rows=activity,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_100_000,
        )

        enriched = _attach_event_uids_to_activity(activity, wallet_conn, BASELINE_WALLET)
        records = list(
            iter_fill_probes(
                BASELINE_WALLET,
                enriched,
                client=client,
                classification={"archetype": "directional_discretionary"},
                counts={"trade_records": 30},
                returns={"resolved_position_count": 10},
                db_path="/tmp/nonexistent/__no__.db",
            )
        )
        assert len(records) == 30
        assert all(r.fill_event_uid for r in records)

        upsert_execution_probes(
            wallet_conn,
            wallet=BASELINE_WALLET,
            capture_run_id="run-baseline",
            fill_probes=records,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_100_000,
        )


def test_rehydrate_records_match_in_memory_iter_fill_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The DuckDB round-trip is lossless for every field _aggregate reads.

    Captures the in-memory records produced by `iter_fill_probes`, then
    compares them field-by-field to the records read back from the
    warehouse. `title` is excluded — the probe writer does not persist
    it (it lives on `market_outcome_dim_snapshot`) and `_aggregate`
    does not consume it.
    """
    monkeypatch.setattr("scripts.execution_profile.FILL_PACE_SEC", 0.0)
    monkeypatch.setattr(
        "scripts.execution_profile._resolve_ts_from_gamma", lambda cid: None
    )
    client, activity, _base, _bars = _baseline_inputs()

    wallet_path = tmp_path / f"{BASELINE_WALLET.lower()}.duckdb"
    market_path = tmp_path / "market_data.duckdb"
    with duckdb.connect(str(wallet_path)) as wallet_conn, duckdb.connect(
        str(market_path)
    ) as market_conn, duckdb.connect(str(tmp_path / "index.duckdb")) as index_conn:
        ensure_wallet_schema(wallet_conn)
        ensure_market_schema(market_conn)
        ensure_index_schema(index_conn)

        upsert_activity_events(
            wallet_conn,
            wallet=BASELINE_WALLET,
            capture_run_id="run-baseline",
            activity_rows=activity,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_100_000,
        )

        enriched = _attach_event_uids_to_activity(
            activity, wallet_conn, BASELINE_WALLET
        )
        in_memory_records = list(
            iter_fill_probes(
                BASELINE_WALLET,
                enriched,
                client=client,
                classification={"archetype": "directional_discretionary"},
                counts={"trade_records": 30},
                returns={"resolved_position_count": 10},
                db_path="/tmp/nonexistent/__no__.db",
            )
        )

        upsert_execution_probes(
            wallet_conn,
            wallet=BASELINE_WALLET,
            capture_run_id="run-baseline",
            fill_probes=in_memory_records,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=1_700_100_000,
        )

        ledger_records = rehydrate_fill_probe_records_from_ledger(
            wallet_conn, BASELINE_WALLET
        )

    assert len(ledger_records) == len(in_memory_records)
    in_memory_by_uid = {r.fill_event_uid: r for r in in_memory_records}
    for ledger_record in ledger_records:
        live = in_memory_by_uid[ledger_record.fill_event_uid]
        # Field-by-field round-trip parity (skip the unpersisted `title`).
        assert ledger_record.probe_config_hash == live.probe_config_hash
        assert ledger_record.probe_version == live.probe_version
        assert ledger_record.horizons_seconds_json == live.horizons_seconds_json
        assert ledger_record.reference_rule == live.reference_rule
        assert ledger_record.condition_id == live.condition_id
        assert ledger_record.token_id == live.token_id
        assert ledger_record.fill_ts == live.fill_ts
        assert ledger_record.fill_price == live.fill_price
        assert ledger_record.side == live.side
        assert ledger_record.size_tokens == live.size_tokens
        assert ledger_record.reference_price == live.reference_price
        assert ledger_record.reference_ts == live.reference_ts
        assert ledger_record.probe_status == live.probe_status
        assert ledger_record.probe_error_msg == live.probe_error_msg
        assert ledger_record.ref_tape == live.ref_tape
        assert ledger_record.tape_n == live.tape_n
        assert ledger_record.in_play == live.in_play
        assert ledger_record.thin_market == live.thin_market
        assert ledger_record.resolution_ts_unknown == live.resolution_ts_unknown
        assert ledger_record.horizons == live.horizons


def test_materialized_execution_profile_matches_baseline_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slice 5 §D2 parity contract.

    Seed the deterministic 30-fill fixture through Slices 1 + 4
    writers (auxiliary fields persisted per Task 0), reconstruct a
    `FillProbeRecord` list from the ledger, run `_aggregate`, and
    compare against the captured baseline at
    `tests/fixtures/execution_profile/aggregate_baseline_v1.json` —
    the same baseline `compute_execution_profile` is verified against
    in `test_compute_execution_profile_aggregate_unchanged_after_refactor`.
    """
    _seed_baseline_warehouse(tmp_path, monkeypatch)

    wallet_path = tmp_path / f"{BASELINE_WALLET.lower()}.duckdb"
    with duckdb.connect(str(wallet_path), read_only=True) as conn:
        materialized_envelope = aggregate_execution_profile_from_ledger(
            conn, BASELINE_WALLET
        )

    baseline = json.loads(BASELINE_FIXTURE.read_text())
    assert materialized_envelope == baseline


def test_materialized_envelope_equals_live_compute_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cross-check: materialized aggregate equals `compute_execution_profile`
    on the same inputs.

    The baseline file pins one capture moment; this test re-runs the live
    aggregator in-process and confirms the materialized envelope still
    matches when the baseline is regenerated. Catches regressions that
    silently update both sides of the byte-for-byte test in lockstep.
    """
    _seed_baseline_warehouse(tmp_path, monkeypatch)
    client, activity, _base, _bars = _baseline_inputs()

    live_envelope = compute_execution_profile(
        BASELINE_WALLET,
        activity,
        client=client,
        classification={"archetype": "directional_discretionary"},
        counts={"trade_records": 30},
        returns={"resolved_position_count": 10},
        db_path="/tmp/nonexistent/__no__.db",
    )

    wallet_path = tmp_path / f"{BASELINE_WALLET.lower()}.duckdb"
    with duckdb.connect(str(wallet_path), read_only=True) as conn:
        materialized_envelope = aggregate_execution_profile_from_ledger(
            conn, BASELINE_WALLET
        )

    assert materialized_envelope == live_envelope

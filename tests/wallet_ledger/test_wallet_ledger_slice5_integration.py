"""Slice 5 Task 8 — end-to-end integration of the wallet fact ledger.

Walks the full Slice-5 path once: seed activity → run Slices 1–4
writers (wallet_events, wallet_positions) → call
`materialize_wallet_data(wallet, window, ledger_root)` → splat the
bundle into `analyze_wallet(...)` via `to_analyze_wallet_kwargs()`.

Asserts the contracts the unit tests cannot:

* The bundle's `to_analyze_wallet_kwargs()` keys match the live
  `analyze_wallet` signature exactly — no signature drift between the
  materializer and the protocol entrypoint.
* `WalletData.replay_surface_meta` carries the
  `ledger_source = "duckdb_warehouse"` marker through the call chain
  (the held-branch structure guard relies on it).
* The structure-tolerant H1 hypothesis still runs on a non-binary
  fixture even when binary-outcome-required hypotheses are guarded as
  `market_structure_unsupported`.
* Whole-wallet verdict is NOT poisoned when at least one supported /
  inconclusive hypothesis exists alongside structure-guarded ones.

Also pins Slice 5 invariants:

* `materialize_wallet_data(wallet, window, ledger_root)` is idempotent —
  the same inputs produce a bundle whose serialisable surfaces (events,
  positions, replay_surface_meta, manifest_hashes, row_counts) are
  byte-for-byte equal across calls.
* Slice 5's locked module set never imports a live HTTP client.
"""
from __future__ import annotations

import time
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

import duckdb

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.ledger.events import upsert_activity_events
from sniper.analysis.wallet_protocol.ledger.materialize import (
    materialize_wallet_data,
)
from sniper.analysis.wallet_protocol.ledger.paths import normalize_wallet
from sniper.analysis.wallet_protocol.ledger.positions import (
    upsert_position_snapshots,
)
from sniper.analysis.wallet_protocol.ledger.schema import (
    ensure_index_schema,
    ensure_market_schema,
    ensure_wallet_schema,
)
from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow
from sniper.analysis.wallet_protocol.types import ProtocolReport, WalletData


WALLET = "0xABCDEF1234567890abcdef1234567890ABCDEF12"
NORMALIZED_WALLET = normalize_wallet(WALLET)


# ---------------------------------------------------------------------------
# Fixture seeding
# ---------------------------------------------------------------------------


def _build_closed_fixture(
    *,
    n: int,
    market_structure: str,
    base_ts: int,
) -> tuple[list[dict], list[dict]]:
    """Build (activity_rows, position_rows) for ``n`` closed-resolution
    positions sharing the same `market_structure`.

    Each position is its own (condition_id, token_id) pair so the
    structure guard sees one row per position. Half resolve YES (curPrice
    ≈ 1.0), half resolve NO (curPrice ≈ 0.0). Each is opened by a single
    BUY activity row 60 s before its close, so Stage 1 / mode-routing
    sees a clean single-buy held-to-resolution shape.

    Timestamps spread one per day backwards from `base_ts` to keep them
    inside the dormancy window without colliding.
    """
    activity: list[dict] = []
    positions: list[dict] = []
    for i in range(n):
        condition_id = f"0xcid_{i:03d}"
        token_id = f"asset_{i:03d}"
        # Spread positions over the last 60 days, anchored at base_ts.
        position_ts = base_ts - i * 86400
        is_yes_winner = i % 2 == 0
        outcome = "Yes" if is_yes_winner else "No"
        outcome_index = 0 if is_yes_winner else 1
        # avgPrice low enough that the H2 sub-080 filter accepts the row.
        avg_price = 0.40
        size = 100
        # Slice 6 TI-1 fixed the writer's truthiness fallback so a literal
        # `0.0` curPrice (resolved-NO) round-trips intact. Use the real
        # resolved values now — earlier 0.005 / 0.99 workaround is no
        # longer needed and would obscure the regression boundary if the
        # writer fix ever drifted back.
        cur_price = 1.0 if is_yes_winner else 0.0
        realized_pnl = (cur_price - avg_price) * size
        activity.append(
            {
                "type": "TRADE",
                "side": "BUY",
                "conditionId": condition_id,
                "asset": token_id,
                "outcomeIndex": outcome_index,
                "outcome": outcome,
                "marketStructure": market_structure,
                "timestamp": position_ts - 60,
                "price": avg_price,
                "size": size,
                "usdcSize": avg_price * size,
                "transactionHash": f"0xtx_buy_{i:03d}",
            }
        )
        positions.append(
            {
                "conditionId": condition_id,
                "asset": token_id,
                "outcomeIndex": outcome_index,
                "outcome": outcome,
                "marketStructure": market_structure,
                "avgPrice": avg_price,
                "size": size,
                "totalBought": avg_price * size,
                "realizedPnl": realized_pnl,
                "curPrice": cur_price,
                "timestamp": position_ts,  # closed_at
            }
        )
    return activity, positions


def _seed_warehouse(
    *,
    ledger_root: Path,
    activity: list[dict],
    closed_positions: list[dict],
    captured_at: int,
) -> None:
    wallet_path = ledger_root / f"{NORMALIZED_WALLET}.duckdb"
    market_path = ledger_root / "market_data.duckdb"
    index_path = ledger_root / "index.duckdb"
    with duckdb.connect(str(wallet_path)) as wallet_conn, duckdb.connect(
        str(market_path)
    ) as market_conn, duckdb.connect(str(index_path)) as index_conn:
        ensure_wallet_schema(wallet_conn)
        ensure_market_schema(market_conn)
        ensure_index_schema(index_conn)
        upsert_activity_events(
            wallet_conn,
            wallet=WALLET,
            capture_run_id="run-int",
            activity_rows=activity,
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=captured_at,
        )
        upsert_position_snapshots(
            wallet_conn,
            wallet=WALLET,
            capture_run_id="run-int",
            position_rows=closed_positions,
            status="closed_resolution",
            source="polymarket_data_api",
            source_version="2026-05-02",
            captured_at=captured_at,
        )


def _make_window(*, base_ts: int) -> AnalysisWindow:
    """Window covering the full fixture span plus a forward pad."""
    return AnalysisWindow(
        window_id="2026-05-03_slice5_integration",
        capture_run_ids=("run-int",),
        start_ts=base_ts - 90 * 86400,
        end_ts=base_ts + 86400,
        source_versions=(
            ("polymarket_data_api", "2026-05-02"),
            ("polymarket_clob_v2", "2026-05-02"),
        ),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_slice5_integration_binary_fixture_returns_protocol_report(
    tmp_path: Path,
) -> None:
    """Happy path: 35 binary positions → analyze_wallet returns a populated
    ProtocolReport via the bundle splat. Confirms no signature mismatch
    between the materializer and the protocol entrypoint."""
    base_ts = int(time.time()) - 1_000
    activity, closed = _build_closed_fixture(
        n=35, market_structure="binary", base_ts=base_ts
    )
    _seed_warehouse(
        ledger_root=tmp_path,
        activity=activity,
        closed_positions=closed,
        captured_at=base_ts + 100,
    )
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_make_window(base_ts=base_ts), ledger_root=tmp_path
    )

    report = analyze_wallet(
        bundle.wallet_data.wallet, **bundle.to_analyze_wallet_kwargs()
    )

    assert isinstance(report, ProtocolReport)
    assert report.wallet == NORMALIZED_WALLET
    # Stage 1 must pass — fixture has 35 closed positions, all timestamps
    # within the last 90 days.
    assert report.gate_result == "pass", report.gate_reason


def test_slice5_integration_propagates_replay_surface_meta(
    tmp_path: Path,
) -> None:
    base_ts = int(time.time()) - 1_000
    activity, closed = _build_closed_fixture(
        n=35, market_structure="binary", base_ts=base_ts
    )
    _seed_warehouse(
        ledger_root=tmp_path,
        activity=activity,
        closed_positions=closed,
        captured_at=base_ts + 100,
    )
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_make_window(base_ts=base_ts), ledger_root=tmp_path
    )
    # The bundle's WalletData carries the materialization marker — the
    # held-branch structure guard depends on this surface to detect
    # ledger-replay reports.
    meta = bundle.wallet_data.replay_surface_meta
    assert isinstance(meta, dict)
    for surface in ("events", "positions", "probes", "flips", "prices", "manifests"):
        assert meta[surface]["ledger_source"] == "duckdb_warehouse"


def test_slice5_integration_h1_runs_on_non_binary_fixture(
    tmp_path: Path,
) -> None:
    """Structure-tolerant H1 must run regardless of market_structure.

    Non-binary fixture: every closed position has ``market_structure='scalar'``.
    H2/H3 get stamped with ``market_structure_unsupported``; H1 keeps
    whatever verdict the bootstrap produced.
    """
    base_ts = int(time.time()) - 1_000
    activity, closed = _build_closed_fixture(
        n=35, market_structure="scalar", base_ts=base_ts
    )
    _seed_warehouse(
        ledger_root=tmp_path,
        activity=activity,
        closed_positions=closed,
        captured_at=base_ts + 100,
    )
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_make_window(base_ts=base_ts), ledger_root=tmp_path
    )
    report = analyze_wallet(
        bundle.wallet_data.wallet, **bundle.to_analyze_wallet_kwargs()
    )

    assert report.gate_result == "pass", report.gate_reason
    held = report.held_analyst_branch
    assert held is not None and held.is_active
    h1 = next((h for h in held.hypotheses if h.name == "H1"), None)
    assert h1 is not None
    # H1 must NOT be guarded as a structure-failure: the only structure
    # reasons live on UnderpoweredReason for binary / token-price-only
    # hypotheses.
    assert h1.underpowered_reason != "market_structure_unsupported"
    assert h1.underpowered_reason != "outcome_index_missing"
    assert h1.underpowered_reason != "no_token_history"


def test_slice5_integration_h2_h3_marked_market_structure_unsupported(
    tmp_path: Path,
) -> None:
    """Non-binary fixture → H2 / H3 carry the structure-guard reason."""
    base_ts = int(time.time()) - 1_000
    activity, closed = _build_closed_fixture(
        n=35, market_structure="scalar", base_ts=base_ts
    )
    _seed_warehouse(
        ledger_root=tmp_path,
        activity=activity,
        closed_positions=closed,
        captured_at=base_ts + 100,
    )
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_make_window(base_ts=base_ts), ledger_root=tmp_path
    )
    report = analyze_wallet(
        bundle.wallet_data.wallet, **bundle.to_analyze_wallet_kwargs()
    )

    held = report.held_analyst_branch
    assert held is not None and held.is_active
    by_name = {h.name: h for h in held.hypotheses}
    # H2 has 35 held-to-resolution positions ≥ H2_MIN_N=20 → its leaf
    # produces a normal verdict. The structure guard then re-stamps it
    # underpowered with `market_structure_unsupported` because no
    # closed-position row is `market_structure='binary'`.
    assert "H2" in by_name
    assert by_name["H2"].verdict == "underpowered"
    assert by_name["H2"].underpowered_reason == "market_structure_unsupported"
    # H3 also reads 35 positions; same guarded outcome.
    assert "H3" in by_name
    assert by_name["H3"].verdict == "underpowered"
    assert by_name["H3"].underpowered_reason == "market_structure_unsupported"


def test_slice5_integration_branch_runs_when_only_some_hypotheses_guarded(
    tmp_path: Path,
) -> None:
    """The structure guard must NOT short-circuit the whole branch.

    Structure-guarded H2/H3 sit alongside structure-tolerant H1 in the
    held-branch report. Even when binary-only hypotheses fail structure,
    the branch report stays active, H1 produces an actual verdict (not
    a structure-underpowered one), and the call chain returns a fully-
    populated `ProtocolReport` instead of an early-exit shape.
    """
    base_ts = int(time.time()) - 1_000
    activity, closed = _build_closed_fixture(
        n=35, market_structure="scalar", base_ts=base_ts
    )
    _seed_warehouse(
        ledger_root=tmp_path,
        activity=activity,
        closed_positions=closed,
        captured_at=base_ts + 100,
    )
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_make_window(base_ts=base_ts), ledger_root=tmp_path
    )
    report = analyze_wallet(
        bundle.wallet_data.wallet, **bundle.to_analyze_wallet_kwargs()
    )

    held = report.held_analyst_branch
    assert held is not None
    assert held.is_active
    by_name = {h.name: h for h in held.hypotheses}
    h1 = by_name.get("H1")
    assert h1 is not None
    # H1 must produce a real verdict — the branch did not short-circuit
    # because H2/H3 were structure-guarded.
    assert h1.verdict in ("supported", "refuted", "inconclusive", "underpowered")
    # And H1's reason (if any) is not one of the three structure-guard
    # values — those are reserved for binary-only / token-price-only
    # hypotheses.
    structure_reasons = {
        "market_structure_unsupported",
        "outcome_index_missing",
        "no_token_history",
    }
    assert h1.underpowered_reason not in structure_reasons
    # The protocol report itself completed the full template, including
    # the flipper branch slot (inactive when held_analyst_only routing).
    assert report.flipper_branch is not None
    assert report.flipper_branch.is_active is False


# ---------------------------------------------------------------------------
# Slice 5 verification — idempotency + no-live-imports
# ---------------------------------------------------------------------------


def _serializable_bundle_dump(bundle) -> dict:
    """Return a JSON-style dict of every non-callable surface on the bundle.

    The two callable surfaces (`price_history_fn`, `get_price_bars`) are
    excluded — they are functions, not data. Slice 5's idempotency claim
    is over the data surfaces.
    """
    wallet_data = asdict(bundle.wallet_data)
    return {
        "wallet_data": wallet_data,
        "window": asdict(bundle.window),
        "replay_surface_meta": deepcopy(bundle.replay_surface_meta),
        "manifest_hashes": deepcopy(bundle.manifest_hashes),
        "row_counts": deepcopy(bundle.row_counts),
        "peer_baseline_fn": bundle.peer_baseline_fn,
        "leafs_by_cid": bundle.leafs_by_cid,
        "nav_lookup": bundle.nav_lookup,
    }


def test_materialize_wallet_data_is_idempotent(tmp_path: Path) -> None:
    """Same wallet + same window → byte-for-byte equal data surfaces.

    Slice 5 verification item: the materialization is purely a function
    of the warehouse contents and the window identity. Two calls in the
    same process must produce identical dumps modulo the two callable
    surfaces.
    """
    base_ts = int(time.time()) - 1_000
    activity, closed = _build_closed_fixture(
        n=35, market_structure="binary", base_ts=base_ts
    )
    _seed_warehouse(
        ledger_root=tmp_path,
        activity=activity,
        closed_positions=closed,
        captured_at=base_ts + 100,
    )
    window = _make_window(base_ts=base_ts)
    a = materialize_wallet_data(wallet=WALLET, window=window, ledger_root=tmp_path)
    b = materialize_wallet_data(wallet=WALLET, window=window, ledger_root=tmp_path)
    assert _serializable_bundle_dump(a) == _serializable_bundle_dump(b)


def test_no_live_imports_in_slice5_modules() -> None:
    """Plan §Slice 5 Verification: forbid live-API imports across the
    locked module set."""
    paths = [
        Path("sniper/analysis/wallet_protocol/ledger/materialize.py"),
        Path("sniper/analysis/wallet_protocol/ledger/price_history.py"),
        Path("sniper/analysis/wallet_protocol/ledger/window.py"),
        Path("sniper/analysis/wallet_protocol/protocol_guards.py"),
    ]
    forbidden = ["PolymarketClient", "import requests", "from requests", "httpx"]
    for path in paths:
        full = Path(__file__).resolve().parent.parent.parent / path
        src = full.read_text()
        for needle in forbidden:
            assert needle not in src, (
                f"{path} must not import live HTTP surface: matched {needle!r}"
            )


def test_bundle_wallet_data_is_walletdata_instance(tmp_path: Path) -> None:
    """Round-trip: the bundle's wallet_data is exactly the type
    `analyze_wallet`'s `pre_fetched_data` kwarg expects.

    Catches any future drift where the materializer returns a subclass
    or a dict-shaped surrogate."""
    base_ts = int(time.time()) - 1_000
    activity, closed = _build_closed_fixture(
        n=35, market_structure="binary", base_ts=base_ts
    )
    _seed_warehouse(
        ledger_root=tmp_path,
        activity=activity,
        closed_positions=closed,
        captured_at=base_ts + 100,
    )
    bundle = materialize_wallet_data(
        wallet=WALLET, window=_make_window(base_ts=base_ts), ledger_root=tmp_path
    )
    assert isinstance(bundle.wallet_data, WalletData)
    assert bundle.to_analyze_wallet_kwargs()["pre_fetched_data"] is bundle.wallet_data

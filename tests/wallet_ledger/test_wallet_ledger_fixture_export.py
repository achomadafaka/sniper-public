"""Slice 6 Task 7 — fixture v2 exporter/loader tests.

Round-trip surface: starting bundle → export → load → analyze_wallet
produces an equal `ProtocolReport`. Comparison via `dataclasses.asdict`
because callable surfaces (`price_history_fn`) won't trip Python's
default `__eq__`.

The exporter and loader call no live APIs — module grep guards that.
"""
from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.ledger.fixture_export import (
    export_calibration_fixture,
    load_calibration_fixture,
)
from sniper.analysis.wallet_protocol.ledger.materialize import (
    MaterializedWalletData,
)
from sniper.analysis.wallet_protocol.ledger.price_history import (
    PriceBar,
    PriceBarsResult,
)
from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow
from sniper.analysis.wallet_protocol.types import WalletData


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_EXPORT_FILE = (
    REPO_ROOT
    / "sniper"
    / "analysis"
    / "wallet_protocol"
    / "ledger"
    / "fixture_export.py"
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _window(start_ts: int = 1_700_000_000, end_ts: int = 1_700_086_400) -> AnalysisWindow:
    return AnalysisWindow(
        window_id="w1",
        capture_run_ids=("run-a",),
        start_ts=start_ts,
        end_ts=end_ts,
        source_versions=(("price_v", "v1"),),
    )


def _wallet_data(
    *,
    price_history_lookups: dict[str, float | None] | None = None,
) -> WalletData:
    return WalletData(
        wallet="0xa",
        closed_positions=[],  # empty -> data_insufficient
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        execution_profile=None,
        fetched_at=1_700_086_000,
        capital_ledger=None,
        portfolio_value_usdc=None,
        fetch_quality_flags=[],
        subgraph_fallback_used=False,
        price_history_lookups=price_history_lookups or {},
        leafs_by_cid=None,
        nav_lookup=None,
        peer_baselines=None,
        replay_surface_meta={"events": {"row_count": 0}},
    )


def _bundle(
    *,
    price_bars: dict[str, list[dict]] | None = None,
) -> MaterializedWalletData:
    """Build a synthetic bundle with an in-memory price closure built
    over `price_bars` plus a matching `get_price_bars` callable so the
    exporter can capture exact (ts, price) pairs.
    """
    bars_input = price_bars or {}
    forward_window = max(60 * 60, 600)  # mirror price_history default

    bars_by_token = {
        token_id: [
            PriceBar(
                ts=int(bar["ts"]),
                price=float(bar["price"]),
                source_version=str(bar.get("source_version", "v1")),
            )
            for bar in token_bars
        ]
        for token_id, token_bars in bars_input.items()
    }

    def _phf(token_id: str, ts: int) -> float | None:
        token_bars = bars_by_token.get(token_id, [])
        target = int(ts)
        upper = target + forward_window
        in_window = [b for b in token_bars if target <= b.ts <= upper]
        if not in_window:
            return None
        in_window.sort(key=lambda b: b.ts)
        return float(in_window[0].price)

    def _gpb(token_id: str) -> PriceBarsResult:
        return PriceBarsResult(
            token_id=token_id,
            bars=list(bars_by_token.get(token_id, [])),
            coverage_status="covered",
        )

    # Latest bar per token (mirrors the real materializer surface).
    lookups: dict[str, float | None] = {}
    for token_id, bars in bars_by_token.items():
        if not bars:
            continue
        latest = max(bars, key=lambda b: b.ts)
        lookups[token_id] = latest.price

    return MaterializedWalletData(
        wallet_data=_wallet_data(price_history_lookups=lookups),
        price_history_fn=_phf,
        peer_baseline_fn=None,
        leafs_by_cid=None,
        nav_lookup=None,
        window=_window(),
        replay_surface_meta={"events": {"row_count": 0}},
        manifest_hashes={"manifest:1": "abc123"},
        row_counts={"events": 0, "positions": 0},
        get_price_bars=_gpb,
    )


# ---------------------------------------------------------------------------
# Exporter — JSON envelope shape
# ---------------------------------------------------------------------------


def test_exporter_writes_required_envelope_keys(tmp_path: Path) -> None:
    out = tmp_path / "0xa_2026-04-30.json"
    bundle = _bundle()
    written = export_calibration_fixture(bundle, out)
    assert written == out
    payload = json.loads(out.read_text())
    required = {
        "wallet_data",
        "window",
        "replay_surface_meta",
        "manifest_hashes",
        "row_counts",
        "price_bars",
    }
    assert required <= payload.keys()


def test_exporter_serialises_window_and_wallet_data(tmp_path: Path) -> None:
    out = tmp_path / "0xa.json"
    bundle = _bundle()
    export_calibration_fixture(bundle, out)
    payload = json.loads(out.read_text())
    assert payload["window"]["window_id"] == "w1"
    assert payload["window"]["start_ts"] == bundle.window.start_ts
    assert payload["window"]["end_ts"] == bundle.window.end_ts
    assert payload["wallet_data"]["wallet"] == "0xa"
    # tuple → list round-trip on capture_run_ids / source_versions.
    assert payload["window"]["capture_run_ids"] == list(
        bundle.window.capture_run_ids
    )


def test_exporter_creates_parent_directories(tmp_path: Path) -> None:
    out = tmp_path / "nested" / "subdir" / "0xa.json"
    export_calibration_fixture(_bundle(), out)
    assert out.exists()


def test_exporter_is_idempotent_on_identical_content(
    tmp_path: Path,
) -> None:
    """Identical content -> the file is not rewritten (mtime preserved).
    Different content -> the file is rewritten."""
    out = tmp_path / "0xa.json"
    export_calibration_fixture(_bundle(), out)
    first_mtime = out.stat().st_mtime_ns

    # Sleep nothing — re-export with the same content.
    export_calibration_fixture(_bundle(), out)
    second_mtime = out.stat().st_mtime_ns
    assert first_mtime == second_mtime

    # Different content -> rewrite (different sha256).
    bundle2 = _bundle(
        price_bars={"tokA": [{"ts": 1700000600, "price": 0.42, "source_version": "v1"}]}
    )
    export_calibration_fixture(bundle2, out)
    third_mtime = out.stat().st_mtime_ns
    assert third_mtime != second_mtime


# ---------------------------------------------------------------------------
# Loader — bundle shape + price closure
# ---------------------------------------------------------------------------


def test_loader_returns_materialized_wallet_data(tmp_path: Path) -> None:
    out = tmp_path / "0xa.json"
    export_calibration_fixture(_bundle(), out)
    bundle = load_calibration_fixture(out)
    assert isinstance(bundle, MaterializedWalletData)
    assert isinstance(bundle.wallet_data, WalletData)
    assert isinstance(bundle.window, AnalysisWindow)


def test_loader_rebuilds_to_analyze_wallet_kwargs(tmp_path: Path) -> None:
    """Bundle's `to_analyze_wallet_kwargs()` returns the canonical five
    keys; peer/leaf/nav stay None per Slice 5."""
    out = tmp_path / "0xa.json"
    export_calibration_fixture(_bundle(), out)
    bundle = load_calibration_fixture(out)
    kwargs = bundle.to_analyze_wallet_kwargs()
    assert set(kwargs.keys()) == {
        "pre_fetched_data",
        "price_history_fn",
        "peer_baseline_fn",
        "leafs_by_cid",
        "nav_lookup",
    }
    assert kwargs["peer_baseline_fn"] is None
    assert kwargs["leafs_by_cid"] is None
    assert kwargs["nav_lookup"] is None


def test_loader_price_history_fn_resolves_persisted_bars(
    tmp_path: Path,
) -> None:
    """price_history_fn after re-load returns the same float | None as
    the pre-export closure for every (token_id, ts) the bar surface
    covers, plus None for an out-of-window probe."""
    bars = {
        "tokA": [
            {"ts": 1_700_000_600, "price": 0.42, "source_version": "v1"},
            {"ts": 1_700_010_000, "price": 0.55, "source_version": "v1"},
        ],
    }
    bundle = _bundle(price_bars=bars)
    out = tmp_path / "0xa.json"
    export_calibration_fixture(bundle, out)
    reloaded = load_calibration_fixture(out)

    # Same in-window probe — first bar with ts >= target.
    assert reloaded.price_history_fn("tokA", 1_700_000_500) == 0.42
    # Probe near the second bar.
    assert reloaded.price_history_fn("tokA", 1_700_009_999) == 0.55
    # Out-of-window probe — past the forward pad of the last bar.
    assert reloaded.price_history_fn("tokA", 1_710_000_000) is None
    # Unknown token.
    assert reloaded.price_history_fn("nope", 1_700_000_600) is None


# ---------------------------------------------------------------------------
# Round-trip ProtocolReport equality
# ---------------------------------------------------------------------------


def test_round_trip_protocol_report_is_byte_equal(tmp_path: Path) -> None:
    """The whole point of the exporter: bundle → export → load →
    analyze_wallet matches running analyze_wallet directly on the
    starting bundle. Compare via `dataclasses.asdict` so the
    `price_history_fn` callable identity does not trip equality."""
    bundle = _bundle()
    out = tmp_path / "0xa.json"
    export_calibration_fixture(bundle, out)
    reloaded = load_calibration_fixture(out)

    report_direct = analyze_wallet(
        bundle.wallet_data.wallet,
        n_iter=50,
        seed=1,
        **bundle.to_analyze_wallet_kwargs(),
    )
    report_reloaded = analyze_wallet(
        reloaded.wallet_data.wallet,
        n_iter=50,
        seed=1,
        **reloaded.to_analyze_wallet_kwargs(),
    )

    direct = dataclasses.asdict(report_direct)
    rel = dataclasses.asdict(report_reloaded)
    # `analyzed_at` is `int(time.time())` — non-deterministic. Pin
    # it to the same value for the comparison.
    direct["analyzed_at"] = 0
    rel["analyzed_at"] = 0
    assert direct == rel


# ---------------------------------------------------------------------------
# Module guard — no live API imports
# ---------------------------------------------------------------------------


def test_fixture_export_module_does_not_import_live_apis() -> None:
    """AST-only guard. Substrings would false-positive against the
    docstring (which legitimately names the forbidden surfaces in
    prose to explain why they are forbidden)."""
    tree = ast.parse(FIXTURE_EXPORT_FILE.read_text())
    forbidden_roots = {"bot", "requests", "httpx"}
    forbidden_modules = {"urllib.request"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in forbidden_roots, (
                    f"fixture_export imported forbidden root: {alias.name}"
                )
                assert alias.name not in forbidden_modules, (
                    f"fixture_export imported forbidden module: {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".", 1)[0]
            assert root not in forbidden_roots, (
                f"fixture_export imported forbidden root: {node.module}"
            )
            assert node.module not in forbidden_modules, (
                f"fixture_export imported forbidden module: {node.module}"
            )

"""Slice 5 Task 6 — ledger price-history closures.

Two surfaces (D3a):

1. `price_history_fn(token_id, ts) -> float | None` — mirrors
   `build_price_history_fn`. First bar with `t >= target_ts` inside
   `[target_ts, target_ts + max(fidelity_minutes * 60, 600)]`, falling
   back to the last bar in that tight forward window only if no bar
   reaches the target. Never uses a predecessor bar.
2. `get_price_bars(token_id) -> PriceBarsResult` — diagnostics.
   Returns the bar list inside the window plus a `coverage_status`
   enum (`covered`, `gap`, `not_captured`) read from
   `market_price_coverage_ranges`.

Both surfaces are produced by `make_ledger_price_history_fn(market_conn,
window, fidelity_minutes)` so they share the same fidelity assumption.
"""
from __future__ import annotations

import inspect
from pathlib import Path

import duckdb
import pytest

from sniper.analysis.wallet_protocol.ledger.coverage import record_coverage_range
from sniper.analysis.wallet_protocol.ledger.price_history import (
    PriceBarsResult,
    make_ledger_price_history_fn,
)
from sniper.analysis.wallet_protocol.ledger.prices import (
    MarketPricePoint,
    upsert_price_points,
)
from sniper.analysis.wallet_protocol.ledger.schema import (
    ensure_index_schema,
    ensure_market_schema,
)
from sniper.analysis.wallet_protocol.ledger.window import AnalysisWindow


SOURCE_VERSION = "2026-05-02"
FIDELITY_MINUTES = 1
BASE = 1_700_000_000
WINDOW = AnalysisWindow(
    window_id="2026-05-03_ph_v1",
    capture_run_ids=("run-A",),
    start_ts=BASE,
    end_ts=BASE + 10_000,
    source_versions=(("polymarket_clob_v2", SOURCE_VERSION),),
)


def _market_conn(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    """Open a fresh market_data.duckdb + initialize index/market schema."""
    market_path = tmp_path / "market_data.duckdb"
    index_path = tmp_path / "index.duckdb"
    conn = duckdb.connect(str(market_path))
    ensure_market_schema(conn)
    with duckdb.connect(str(index_path)) as index_conn:
        ensure_index_schema(index_conn)
    return conn


def _seed_points(
    conn: duckdb.DuckDBPyConnection, token_id: str, points: list[tuple[int, float]]
) -> None:
    upsert_price_points(
        conn,
        [
            MarketPricePoint(token_id, FIDELITY_MINUTES, ts, price)
            for ts, price in points
        ],
        source_version=SOURCE_VERSION,
        capture_run_id="run-A",
        captured_at=BASE,
    )


# ---------------------------------------------------------------------------
# Factory contract
# ---------------------------------------------------------------------------


def test_factory_returns_two_callables(tmp_path: Path) -> None:
    with _market_conn(tmp_path) as conn:
        ph_fn, get_bars = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
    assert callable(ph_fn)
    assert callable(get_bars)


def test_price_history_fn_signature_matches_protocol(tmp_path: Path) -> None:
    """Protocol contract: PriceHistoryFn = Callable[[str, int], float | None].

    Verified by `inspect.signature` so we catch any drift toward 3-arg or
    keyword-only callers that the protocol can't supply.
    """
    with _market_conn(tmp_path) as conn:
        ph_fn, _ = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
    sig = inspect.signature(ph_fn)
    params = list(sig.parameters.values())
    assert len(params) == 2
    for p in params:
        assert p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )


# ---------------------------------------------------------------------------
# price_history_fn — first-bar-at-or-after-target rule
# ---------------------------------------------------------------------------


def test_first_bar_at_or_after_target_returns_forward_bar(tmp_path: Path) -> None:
    with _market_conn(tmp_path) as conn:
        _seed_points(
            conn,
            "asset_yes",
            [(BASE, 0.40), (BASE + 60, 0.42), (BASE + 120, 0.45)],
        )
        ph_fn, _ = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        # ts between bars at BASE and BASE+60 → forward bar at BASE+60 (0.42).
        assert ph_fn("asset_yes", BASE + 30) == pytest.approx(0.42)


def test_predecessor_bar_must_not_be_returned(tmp_path: Path) -> None:
    """Regression for D3a: nearest/predecessor lookup is forbidden.

    Bar at ``BASE`` is closer to ``target = BASE + 100`` (lag 100 s) than
    the forward bar at ``BASE + 500`` (lead 400 s); a nearest-bar lookup
    would pick the predecessor. The closure must return the forward bar
    regardless of distance.
    """
    with _market_conn(tmp_path) as conn:
        _seed_points(
            conn,
            "asset_yes",
            [(BASE, 0.10), (BASE + 500, 0.99)],
        )
        ph_fn, _ = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        assert ph_fn("asset_yes", BASE + 100) == pytest.approx(0.99)


def test_returns_exact_bar_when_target_hits_it(tmp_path: Path) -> None:
    with _market_conn(tmp_path) as conn:
        _seed_points(conn, "asset_yes", [(BASE, 0.40), (BASE + 60, 0.42)])
        ph_fn, _ = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        assert ph_fn("asset_yes", BASE + 60) == pytest.approx(0.42)


def test_no_forward_bar_returns_none(tmp_path: Path) -> None:
    with _market_conn(tmp_path) as conn:
        _seed_points(conn, "asset_yes", [(BASE, 0.40)])
        ph_fn, _ = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        # ts past the only bar; window is [ts, ts + max(60*1, 600)] = 600 s.
        # No forward bar → None.
        assert ph_fn("asset_yes", BASE + 1200) is None


def test_unknown_token_returns_none(tmp_path: Path) -> None:
    with _market_conn(tmp_path) as conn:
        _seed_points(conn, "asset_yes", [(BASE, 0.40)])
        ph_fn, _ = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        assert ph_fn("asset_unknown", BASE) is None


def test_window_size_is_max_fidelity_seconds_or_600(tmp_path: Path) -> None:
    """Forward window is `max(fidelity*60, 600)` seconds."""
    with _market_conn(tmp_path) as conn:
        # Bar 599 s ahead of target — inside the 600-s floor.
        _seed_points(
            conn, "asset_yes", [(BASE + 599, 0.55)]
        )
        ph_fn, _ = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        assert ph_fn("asset_yes", BASE) == pytest.approx(0.55)

    # Verify the floor really is 600 s — a bar at BASE + 601 is excluded.
    other = tmp_path / "second"
    other.mkdir()
    with _market_conn(other) as conn:
        _seed_points(conn, "asset_yes", [(BASE + 601, 0.55)])
        ph_fn, _ = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        assert ph_fn("asset_yes", BASE) is None


# ---------------------------------------------------------------------------
# get_price_bars — diagnostics surface
# ---------------------------------------------------------------------------


def test_get_price_bars_returns_pricebarsresult(tmp_path: Path) -> None:
    with _market_conn(tmp_path) as conn:
        _seed_points(
            conn, "asset_yes", [(BASE, 0.40), (BASE + 60, 0.42), (BASE + 120, 0.45)]
        )
        record_coverage_range(
            conn,
            token_id="asset_yes",
            fidelity_minutes=FIDELITY_MINUTES,
            source_version=SOURCE_VERSION,
            range_start_ts=WINDOW.start_ts,
            range_end_ts=WINDOW.end_ts,
            coverage_status="fully_captured",
            source="polymarket_clob_v2",
            api_error_msg=None,
            content_hash="hash-yes",
            first_capture_run_id="run-A",
            captured_at=BASE,
        )
        _, get_bars = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        result = get_bars("asset_yes")

    assert isinstance(result, PriceBarsResult)
    assert result.token_id == "asset_yes"
    assert [(bar.ts, bar.price) for bar in result.bars] == [
        (BASE, 0.40),
        (BASE + 60, 0.42),
        (BASE + 120, 0.45),
    ]
    assert result.coverage_status == "covered"


def test_get_price_bars_status_gap_when_partial_range(tmp_path: Path) -> None:
    with _market_conn(tmp_path) as conn:
        _seed_points(conn, "asset_yes", [(BASE, 0.40)])
        record_coverage_range(
            conn,
            token_id="asset_yes",
            fidelity_minutes=FIDELITY_MINUTES,
            source_version=SOURCE_VERSION,
            range_start_ts=WINDOW.start_ts,
            range_end_ts=WINDOW.end_ts,
            coverage_status="partial",
            source="polymarket_clob_v2",
            api_error_msg=None,
            content_hash="hash-partial",
            first_capture_run_id="run-A",
            captured_at=BASE,
        )
        _, get_bars = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        result = get_bars("asset_yes")
    assert result.coverage_status == "gap"


def test_get_price_bars_status_not_captured_when_no_range(tmp_path: Path) -> None:
    with _market_conn(tmp_path) as conn:
        _seed_points(conn, "asset_yes", [(BASE, 0.40)])
        # No coverage_range row recorded.
        _, get_bars = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        result = get_bars("asset_yes")
    assert result.coverage_status == "not_captured"


def test_get_price_bars_status_not_captured_for_unknown_token(tmp_path: Path) -> None:
    with _market_conn(tmp_path) as conn:
        _, get_bars = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        result = get_bars("asset_unknown")
    assert result.token_id == "asset_unknown"
    assert result.bars == []
    assert result.coverage_status == "not_captured"


def test_get_price_bars_filtered_to_window(tmp_path: Path) -> None:
    """Bars outside the analysis window are excluded from the diagnostics
    surface; only bars inside `[start_ts, end_ts]` are returned."""
    with _market_conn(tmp_path) as conn:
        _seed_points(
            conn,
            "asset_yes",
            [
                (BASE - 1000, 0.10),  # before window
                (BASE, 0.40),
                (BASE + 60, 0.42),
                (BASE + 11_000, 0.99),  # after window
            ],
        )
        record_coverage_range(
            conn,
            token_id="asset_yes",
            fidelity_minutes=FIDELITY_MINUTES,
            source_version=SOURCE_VERSION,
            range_start_ts=WINDOW.start_ts,
            range_end_ts=WINDOW.end_ts,
            coverage_status="fully_captured",
            source="polymarket_clob_v2",
            api_error_msg=None,
            content_hash="hash-window",
            first_capture_run_id="run-A",
            captured_at=BASE,
        )
        _, get_bars = make_ledger_price_history_fn(
            conn, window=WINDOW, fidelity_minutes=FIDELITY_MINUTES
        )
        result = get_bars("asset_yes")

    timestamps = [bar.ts for bar in result.bars]
    assert timestamps == [BASE, BASE + 60]


# ---------------------------------------------------------------------------
# Module hygiene
# ---------------------------------------------------------------------------


def test_no_live_imports_in_price_history_module() -> None:
    import sniper.analysis.wallet_protocol.ledger.price_history as mod

    src = Path(mod.__file__).read_text()
    forbidden = ["PolymarketClient", "import requests", "from requests", "httpx"]
    for needle in forbidden:
        assert needle not in src, f"price_history.py must not reference {needle!r}"

"""Tests for sniper.analysis.backtest_harness."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from sniper.analysis.backtest_harness import (  # noqa: E402
    HARNESS_VERSION,
    _position_matches_filter,
    _terminal_price,
    backtest_rule_on_wallet,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_position_matches_filter_category():
    pos = {"market_category": "sports", "avgPrice": 0.6}
    assert _position_matches_filter(pos, {"categories": ["sports"]})
    assert not _position_matches_filter(pos, {"categories": ["elections"]})


def test_position_matches_filter_price_range():
    pos = {"market_category": "sports", "avgPrice": 0.6}
    assert _position_matches_filter(pos, {"entry_price_range": [0.5, 0.75]})
    assert not _position_matches_filter(pos, {"entry_price_range": [0.0, 0.5]})
    # Exclusive upper bound
    assert not _position_matches_filter({"avgPrice": 0.75}, {"entry_price_range": [0.5, 0.75]})


def test_position_matches_filter_empty_passes():
    assert _position_matches_filter({"avgPrice": 0.6}, {})


def test_position_matches_filter_uses_leafs_by_cid_when_no_explicit_category():
    """When the position lacks `market_category`, the category should be
    resolved via `leafs_by_cid[conditionId]` tag list."""
    pos = {"conditionId": "0xabc", "avgPrice": 0.6}
    leafs = {"0xabc": ["games", "nfl", "sports"]}
    assert _position_matches_filter(pos, {"categories": ["sports"]}, leafs_by_cid=leafs)
    # Same position, but the leaf tags don't include "elections"
    assert not _position_matches_filter(pos, {"categories": ["elections"]}, leafs_by_cid=leafs)
    # Empty leafs map → must fail any non-empty category filter
    assert not _position_matches_filter(pos, {"categories": ["sports"]}, leafs_by_cid={})


def test_position_matches_filter_explicit_category_wins_over_leafs():
    """Explicit market_category on the position takes precedence."""
    pos = {"conditionId": "0xabc", "avgPrice": 0.6, "market_category": "elections"}
    leafs = {"0xabc": ["games", "sports"]}  # contradicts explicit tag
    # The explicit category is "elections" — must NOT match "sports" filter
    assert not _position_matches_filter(pos, {"categories": ["sports"]}, leafs_by_cid=leafs)
    # And must match "elections" filter
    assert _position_matches_filter(pos, {"categories": ["elections"]}, leafs_by_cid=leafs)


def test_position_matches_filter_codex_probe_market_types_rejects_multi():
    """Codex review probe: a multi_outcome / binary_no row should NOT match
    a rule asking for binary / binary_named_outcome.

    Without all_closed_positions threaded, market_type can't be derived;
    with it, the harness sees 3 distinct asset_ids under one condition
    and classifies it multi_outcome → rejected.
    """
    pos = {"conditionId": "0xa", "asset": "1", "outcome": "No", "avgPrice": 0.60}
    closed = [
        pos,
        {"conditionId": "0xa", "asset": "2", "outcome": "Yes"},
        {"conditionId": "0xa", "asset": "3", "outcome": "Other"},
    ]
    rule_filter = {
        "market_types": ["binary"],
        "outcome_roles": ["binary_named_outcome"],
    }
    assert not _position_matches_filter(
        pos, rule_filter,
        all_closed_positions=closed, all_activity_rows=[],
    )


def test_position_matches_filter_codex_probe_outcome_role_rejects_binary_no():
    """Codex review probe: a binary `No` outcome should NOT match a rule
    that asks for `binary_named_outcome`."""
    pos = {"conditionId": "0xa", "asset": "1", "outcome": "No", "avgPrice": 0.60}
    closed = [
        pos,
        {"conditionId": "0xa", "asset": "2", "outcome": "Yes"},  # binary
    ]
    rule_filter = {"outcome_roles": ["binary_named_outcome"]}
    assert not _position_matches_filter(
        pos, rule_filter,
        all_closed_positions=closed, all_activity_rows=[],
    )


def test_position_matches_filter_outcome_role_accepts_named_binary():
    """A genuine binary_named_outcome (e.g. team name on a binary market)
    must pass through the outcome_role filter."""
    pos = {"conditionId": "0xa", "asset": "1", "outcome": "Lakers", "avgPrice": 0.60}
    closed = [
        pos,
        {"conditionId": "0xa", "asset": "2", "outcome": "Celtics"},  # 2 assets → binary
    ]
    rule_filter = {
        "market_types": ["binary"],
        "outcome_roles": ["binary_named_outcome"],
    }
    assert _position_matches_filter(
        pos, rule_filter,
        all_closed_positions=closed, all_activity_rows=[],
    )


def test_position_matches_filter_codex_round2_favorite_status_rejects_mismatch():
    """Codex round 2 finding: extractor emits market_filter.favorite_status,
    but matcher ignored it. A 'longshot' entry (avgPrice=0.10) on a binary
    must NOT match a rule asking for favorite_status='favorite'."""
    pos = {
        "conditionId": "0xa", "asset": "1", "outcome": "Lakers",
        "avgPrice": 0.10,  # longshot
    }
    closed = [
        pos,
        {"conditionId": "0xa", "asset": "2", "outcome": "Celtics"},
    ]
    rule_filter = {"favorite_status": "favorite"}
    assert not _position_matches_filter(
        pos, rule_filter,
        all_closed_positions=closed, all_activity_rows=[],
    )


def test_position_matches_filter_codex_round2_favorite_status_accepts_match():
    """avgPrice >= 0.50 binary → favorite_status='favorite' → matches a
    'favorite' rule."""
    pos = {
        "conditionId": "0xa", "asset": "1", "outcome": "Lakers",
        "avgPrice": 0.65,
    }
    closed = [
        pos,
        {"conditionId": "0xa", "asset": "2", "outcome": "Celtics"},
    ]
    rule_filter = {"favorite_status": "favorite"}
    assert _position_matches_filter(
        pos, rule_filter,
        all_closed_positions=closed, all_activity_rows=[],
    )


def test_position_matches_filter_codex_round2_favorite_status_none_means_no_constraint():
    """When the rule's favorite_status is None (e.g. v1 grain didn't derive it),
    the filter must not block matches on that dimension."""
    pos = {
        "conditionId": "0xa", "asset": "1", "outcome": "Lakers",
        "avgPrice": 0.10,  # would NOT be favorite, but rule doesn't care
    }
    closed = [
        pos,
        {"conditionId": "0xa", "asset": "2", "outcome": "Celtics"},
    ]
    rule_filter = {"favorite_status": None}
    assert _position_matches_filter(
        pos, rule_filter,
        all_closed_positions=closed, all_activity_rows=[],
    )


def test_position_matches_filter_codex_round2_event_family_rejects_mismatch():
    """When rule specifies a non-None event_family, the position must match.
    Position carries explicit event_family field; mismatch → reject."""
    pos = {
        "conditionId": "0xa", "asset": "1", "outcome": "Yes",
        "avgPrice": 0.60, "event_family": "sports_head_to_head",
    }
    closed = [pos, {"conditionId": "0xa", "asset": "2", "outcome": "No"}]
    rule_filter = {"event_family": "election_resolution"}
    assert not _position_matches_filter(
        pos, rule_filter,
        all_closed_positions=closed, all_activity_rows=[],
    )


def test_position_matches_filter_codex_round2_event_family_none_means_no_constraint():
    """v1 dossiers emit event_family=None/'data_unavailable' (deferred).
    A None/'data_unavailable' rule value must not block matches."""
    pos = {
        "conditionId": "0xa", "asset": "1", "outcome": "Yes",
        "avgPrice": 0.60, "event_family": "sports_head_to_head",
    }
    closed = [pos, {"conditionId": "0xa", "asset": "2", "outcome": "No"}]
    for value in (None, "data_unavailable"):
        rule_filter = {"event_family": value}
        assert _position_matches_filter(
            pos, rule_filter,
            all_closed_positions=closed, all_activity_rows=[],
        ), f"event_family={value!r} should be treated as no-constraint"


def test_terminal_price_uses_curprice():
    assert _terminal_price({"curPrice": 1.0}) == 1.0
    assert _terminal_price({"curPrice": 0.0}) == 0.0
    assert _terminal_price({"curPrice": "0.97"}) == 0.97


def test_terminal_price_falls_back_to_realized_pnl_sign_win():
    # Polymarket /closed-positions: totalBought is gross SHARE count (not USDC).
    # 100 shares × (1.0 - 0.4) payout = $60 PnL → resolution = 1.0
    pos = {"avgPrice": 0.4, "totalBought": 100.0, "realizedPnl": 60.0}
    assert _terminal_price(pos) == 1.0


def test_terminal_price_falls_back_to_realized_pnl_sign_loss():
    # 100 shares × (0.0 - 0.4) = -$40 PnL → resolution = 0.0
    pos = {"avgPrice": 0.4, "totalBought": 100.0, "realizedPnl": -40.0}
    assert _terminal_price(pos) == 0.0


def test_terminal_price_returns_none_when_indeterminate():
    # 100 shares × (X - 0.5) = 25 → X = 0.75; neither 1.0 nor 0.0 → unresolved
    pos = {"avgPrice": 0.5, "totalBought": 100.0, "realizedPnl": 25.0}
    assert _terminal_price(pos) is None


def test_terminal_price_codex_probe_short_position():
    """Codex review probe: avgPrice=.5, totalBought=10 (shares), realizedPnl=5
    → 10 shares × (1.0 - 0.5) = $5 PnL → resolution = 1.0."""
    pos = {"avgPrice": 0.5, "totalBought": 10, "realizedPnl": 5}
    assert _terminal_price(pos) == 1.0


def test_terminal_price_returns_none_for_degenerate_inputs():
    assert _terminal_price({}) is None
    assert _terminal_price({"avgPrice": 0}) is None


# ---------------------------------------------------------------------------
# backtest_rule_on_wallet — full flow
# ---------------------------------------------------------------------------


def _swissmiss_rule(median_usdc: float = 100.0) -> dict:
    return {
        "extractor_version": "wallet_rule_v1",
        "wallet": "0xdbade4c82fb72780a0db9a38f821d8671aba9c95",
        "source_schema_version": "v5_behavioral_descriptors",
        "source_grain": {"grain_id": "g1", "grain_quality": "interpretable"},
        "market_filter": {
            "categories": ["sports"],
            "entry_price_range": [0.50, 0.75],
        },
        "sizing": {"median_usdc": median_usdc, "fingerprint": "variable"},
        "exit_rule": {"mode": "held", "fingerprint": "hold_to_resolution"},
        "evidence": {"thesis_supported": True},
    }


def _wallet_data_with_positions(positions: list[dict]) -> dict:
    return {
        "wallet": "0xdbade4c8" + "0" * 32,
        "fetched_at": 2_000_000_000,
        "schema_version": 2,
        "closed_positions": positions,
        "open_positions": [],
        "activity_rows": [],
    }


def test_backtest_returns_v1_envelope():
    rule = _swissmiss_rule()
    data = _wallet_data_with_positions([])
    result = backtest_rule_on_wallet(rule, data)
    assert result["harness_version"] == HARNESS_VERSION
    assert result["rule_wallet"] == "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"


def test_backtest_empty_positions_emits_caveat():
    result = backtest_rule_on_wallet(_swissmiss_rule(), _wallet_data_with_positions([]))
    assert "no_closed_positions" in result["caveats"]
    assert result["n_positions_total"] == 0


def test_backtest_unsupported_exit_mode_aborts():
    rule = _swissmiss_rule()
    rule["exit_rule"]["mode"] = "pre_resolution"
    result = backtest_rule_on_wallet(rule, _wallet_data_with_positions([{"avgPrice": 0.6}]))
    assert any("unsupported_exit_mode" in c for c in result["caveats"])


def test_backtest_filters_oos_positions_by_market_filter():
    positions = [
        {"conditionId": "0xa", "asset": "1", "avgPrice": 0.3, "totalBought": 30.0,
         "realizedPnl": 21.0, "curPrice": 1.0, "market_category": "sports",
         "timestamp": 1_000_000_000},
        {"conditionId": "0xb", "asset": "2", "avgPrice": 0.60, "totalBought": 60.0,
         "realizedPnl": 40.0, "curPrice": 1.0, "market_category": "sports",
         "timestamp": 1_500_000_000},
        {"conditionId": "0xc", "asset": "3", "avgPrice": 0.60, "totalBought": 60.0,
         "realizedPnl": 40.0, "curPrice": 1.0, "market_category": "elections",
         "timestamp": 1_900_000_000},
        {"conditionId": "0xd", "asset": "4", "avgPrice": 0.60, "totalBought": 60.0,
         "realizedPnl": 40.0, "curPrice": 1.0, "market_category": "sports",
         "timestamp": 1_950_000_000},
    ]
    rule = _swissmiss_rule()
    result = backtest_rule_on_wallet(rule, _wallet_data_with_positions(positions),
                                     oos_split_ratio=0.5, slippage_pct=0.0)
    assert result["n_positions_total"] == 4
    assert result["n_in_sample"] == 2
    assert result["n_out_of_sample"] == 2
    assert result["oos_match_count"] == 1
    assert result["oos_simulated_wins"] == 1
    assert result["oos_simulated_losses"] == 0
    assert result["oos_simulated_win_rate"] == 1.0


def test_backtest_slippage_reduces_simulated_pnl():
    positions = [
        {"conditionId": "0xb", "asset": "2", "avgPrice": 0.60, "totalBought": 60.0,
         "realizedPnl": 40.0, "curPrice": 1.0, "market_category": "sports",
         "timestamp": 1_000_000_000},
        {"conditionId": "0xc", "asset": "3", "avgPrice": 0.60, "totalBought": 60.0,
         "realizedPnl": 40.0, "curPrice": 1.0, "market_category": "sports",
         "timestamp": 1_500_000_000},
    ]
    rule = _swissmiss_rule()
    no_slip = backtest_rule_on_wallet(rule, _wallet_data_with_positions(positions),
                                      oos_split_ratio=0.5, slippage_pct=0.0)
    with_slip = backtest_rule_on_wallet(rule, _wallet_data_with_positions(positions),
                                        oos_split_ratio=0.5, slippage_pct=0.10)
    assert with_slip["oos_simulated_pnl_usdc"] < no_slip["oos_simulated_pnl_usdc"]


def test_backtest_unresolved_position_is_skipped():
    positions = [{
        "conditionId": "0xb", "asset": "2", "avgPrice": 0.60, "totalBought": 60.0,
        "realizedPnl": 5.0, "market_category": "sports",
        "timestamp": 1_000_000_000,
    }]
    rule = _swissmiss_rule()
    result = backtest_rule_on_wallet(rule, _wallet_data_with_positions(positions),
                                     oos_split_ratio=1.0, slippage_pct=0.0)
    assert result["oos_match_count"] == 1
    assert result["oos_skipped_unresolved"] == 1
    assert any("skipped_unresolved" in c for c in result["caveats"])


def test_backtest_pnl_vs_wallet_delta_computed():
    """Codex-correct math under share-based totalBought.

    Wallet bought 1000 shares @ 0.60 → curPrice 1.0 → realized PnL $400
    (= 1000 × (1.0 - 0.60)). Our rule sizes at $60 USDC, no slippage,
    so we buy 100 shares (60 / 0.60) → sim PnL = 100 × 0.40 = $40.
    Wallet PnL scaled to our share count: 400 × (100 / 1000) = $40.
    Delta = 0.
    """
    positions = [{
        "conditionId": "0xb", "asset": "2", "avgPrice": 0.60, "totalBought": 1000.0,
        "realizedPnl": 400.0, "curPrice": 1.0, "market_category": "sports",
        "timestamp": 1_000_000_000,
    }]
    rule = _swissmiss_rule(median_usdc=60.0)
    result = backtest_rule_on_wallet(rule, _wallet_data_with_positions(positions),
                                     oos_split_ratio=1.0, slippage_pct=0.0)
    assert abs(result["oos_simulated_pnl_usdc"] - 40.0) < 0.5
    assert abs(result["oos_actual_pnl_usdc_on_same_positions"] - 40.0) < 0.5
    assert abs(result["pnl_vs_wallet_delta_usdc"]) < 0.5


def test_backtest_no_oos_matches_emits_caveat():
    positions = [{
        "conditionId": "0xb", "asset": "2", "avgPrice": 0.30, "totalBought": 30.0,
        "realizedPnl": 21.0, "curPrice": 1.0, "market_category": "elections",
        "timestamp": 1_000_000_000,
    }]
    result = backtest_rule_on_wallet(_swissmiss_rule(),
                                     _wallet_data_with_positions(positions),
                                     oos_split_ratio=1.0)
    assert "no_oos_matches_for_rule" in result["caveats"]
    assert result["oos_match_count"] == 0


def test_backtest_include_per_position_populates_rows():
    positions = [{
        "conditionId": "0xb", "asset": "2", "avgPrice": 0.60, "totalBought": 60.0,
        "realizedPnl": 40.0, "curPrice": 1.0, "market_category": "sports",
        "timestamp": 1_000_000_000,
    }]
    result = backtest_rule_on_wallet(_swissmiss_rule(),
                                     _wallet_data_with_positions(positions),
                                     oos_split_ratio=1.0,
                                     include_per_position=True)
    assert len(result["per_position_results"]) == 1
    row = result["per_position_results"][0]
    assert "condition_id" in row
    assert "sim_pnl_usdc" in row
    assert row["final_price"] == 1.0


def test_backtest_default_per_position_off():
    positions = [{
        "conditionId": "0xb", "asset": "2", "avgPrice": 0.60, "totalBought": 60.0,
        "realizedPnl": 40.0, "curPrice": 1.0, "market_category": "sports",
        "timestamp": 1_000_000_000,
    }]
    result = backtest_rule_on_wallet(_swissmiss_rule(),
                                     _wallet_data_with_positions(positions),
                                     oos_split_ratio=1.0)
    assert result["per_position_results"] == []


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------


def test_cli_smoke(tmp_path: Path):
    from scripts.backtest_wallet_rule import main as cli_main

    rule_path = tmp_path / "rule.json"
    rule_path.write_text(json.dumps(_swissmiss_rule()))

    wallet_data_path = tmp_path / "wallet.json"
    wallet_data = _wallet_data_with_positions([{
        "conditionId": "0xb", "asset": "2", "avgPrice": 0.60, "totalBought": 60.0,
        "realizedPnl": 40.0, "curPrice": 1.0, "market_category": "sports",
        "timestamp": 1_000_000_000,
    }])
    wallet_data_path.write_text(json.dumps(wallet_data))

    out_path = tmp_path / "backtest.json"
    rc = cli_main([
        "--rule", str(rule_path),
        "--wallet-data", str(wallet_data_path),
        "--oos-ratio", "1.0",
        "--out", str(out_path),
    ])
    assert rc == 2  # 1.0 rejected

    rc = cli_main([
        "--rule", str(rule_path),
        "--wallet-data", str(wallet_data_path),
        "--oos-ratio", "0.99",
        "--out", str(out_path),
    ])
    assert rc == 0
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    assert payload["harness_version"] == HARNESS_VERSION


def test_cli_rejects_missing_rule(tmp_path: Path):
    from scripts.backtest_wallet_rule import main as cli_main
    rc = cli_main([
        "--rule", str(tmp_path / "missing.json"),
        "--wallet-data", str(tmp_path / "also-missing.json"),
        "--out", str(tmp_path / "out.json"),
    ])
    assert rc == 2

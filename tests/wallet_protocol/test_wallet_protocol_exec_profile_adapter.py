"""Acceptance tests for the wallet-protocol exec_profile adapter.

Closes wallet-protocol-bulletproofing gap C4. The adapter wraps
`scripts.execution_profile.compute_execution_profile` so the protocol's
CLI can inject an `execution_profile` from already-fetched data.

C4 amendment 2026-04-26 (A3 H4 force=True investigation):
- `force` defaults to `False` (was hard-coded `True`). The legacy gate
  runs naturally and skips terminal-archetype + insufficient-activity
  wallets with `status="not_run"`.
- `archetype` and `closed_positions` are now first-class arguments so
  the gate has the data it needs to fire.
- An explicit `force=True` override is still available for calibration.
"""

from __future__ import annotations

from unittest.mock import patch, MagicMock


def test_build_execution_profile_default_force_false_passes_inputs():
    from sniper.analysis.wallet_protocol.exec_profile_adapter import (
        build_execution_profile,
    )

    fake_client = object()
    activity = [
        {"type": "TRADE", "side": "BUY", "conditionId": "0xc1"},
        {"type": "TRADE", "side": "SELL", "conditionId": "0xc2"},
        {"type": "REDEEM", "side": "BUY", "conditionId": "0xc3"},
    ]
    closed = [{"conditionId": "0xc4"}, {"conditionId": "0xc5"}]
    with patch(
        "scripts.execution_profile.compute_execution_profile",
        return_value={"status": "populated", "sample_tier": "high"},
    ) as mock:
        result = build_execution_profile(
            "0xabc", activity, closed, archetype="directional_discretionary",
            client=fake_client,
        )
    assert result == {"status": "populated", "sample_tier": "high"}
    kwargs = mock.call_args.kwargs
    assert kwargs["client"] is fake_client
    # Default force=False — gate runs naturally.
    assert kwargs["force"] is False
    assert kwargs["max_trades"] == 500
    # classification carries the archetype.
    assert kwargs["classification"] == {
        "archetype": "directional_discretionary"
    }
    # counts.trade_records counts only TRADE rows (REDEEM excluded).
    assert kwargs["counts"]["trade_records"] == 2
    # returns.resolved_position_count = len(closed_positions).
    assert kwargs["returns"]["resolved_position_count"] == 2


def test_build_execution_profile_archetype_none_omits_classification():
    from sniper.analysis.wallet_protocol.exec_profile_adapter import (
        build_execution_profile,
    )

    with patch(
        "scripts.execution_profile.compute_execution_profile",
        return_value={"status": "ok"},
    ) as mock:
        build_execution_profile("0xabc", [], client=object())
    # archetype unset -> classification=None so the gate's terminal check
    # becomes a no-op rather than a false-positive on archetype="".
    assert mock.call_args.kwargs["classification"] is None


def test_build_execution_profile_closed_positions_default_empty():
    from sniper.analysis.wallet_protocol.exec_profile_adapter import (
        build_execution_profile,
    )

    with patch(
        "scripts.execution_profile.compute_execution_profile",
        return_value={"status": "ok"},
    ) as mock:
        build_execution_profile("0xabc", [], client=object())
    assert mock.call_args.kwargs["returns"]["resolved_position_count"] == 0


def test_build_execution_profile_force_true_override():
    """Calibration / QA workflows must still be able to bypass the gate."""
    from sniper.analysis.wallet_protocol.exec_profile_adapter import (
        build_execution_profile,
    )

    with patch(
        "scripts.execution_profile.compute_execution_profile",
        return_value={"status": "populated"},
    ) as mock:
        build_execution_profile(
            "0xabc", [], client=object(),
            archetype="two_sided_inventory_trader", force=True,
        )
    assert mock.call_args.kwargs["force"] is True


def test_build_execution_profile_overrides_max_trades():
    from sniper.analysis.wallet_protocol.exec_profile_adapter import (
        build_execution_profile,
    )

    with patch(
        "scripts.execution_profile.compute_execution_profile",
        return_value={"status": "ok"},
    ) as mock:
        build_execution_profile(
            "0xabc", [], client=object(), max_trades=50,
        )
    assert mock.call_args.kwargs["max_trades"] == 50


def test_build_execution_profile_lazy_client():
    """When client=None, the adapter constructs PolymarketClient itself."""
    from sniper.analysis.wallet_protocol.exec_profile_adapter import (
        build_execution_profile,
    )

    fake_client = MagicMock(name="PolymarketClient")
    with patch(
        "data.polymarket_client.PolymarketClient",
        return_value=fake_client,
    ) as mock_client_ctor, patch(
        "scripts.execution_profile.compute_execution_profile",
        return_value={"status": "ok"},
    ) as mock_compute:
        build_execution_profile("0xabc", [{"type": "TRADE", "side": "BUY"}])
    assert mock_client_ctor.called
    assert mock_compute.call_args.kwargs["client"] is fake_client


def test_build_execution_profile_returns_dict_shape():
    """Result is always a dict with a 'status' key — never None, never raise."""
    from sniper.analysis.wallet_protocol.exec_profile_adapter import (
        build_execution_profile,
    )

    with patch(
        "scripts.execution_profile.compute_execution_profile",
        return_value={"status": "not_run", "reason": "layer1_terminal"},
    ):
        result = build_execution_profile(
            "0xabc", [], client=object(),
            archetype="two_sided_inventory_trader",
        )
    assert isinstance(result, dict)
    assert "status" in result


def test_build_execution_profile_eagerizes_iterator():
    """activity iterables must be materialized so compute_execution_profile
    can index into them (it calls len() and re-iterates)."""
    from sniper.analysis.wallet_protocol.exec_profile_adapter import (
        build_execution_profile,
    )

    def gen():
        yield {"type": "TRADE", "side": "BUY", "conditionId": "0xa"}
        yield {"type": "TRADE", "side": "BUY", "conditionId": "0xb"}

    with patch(
        "scripts.execution_profile.compute_execution_profile",
        return_value={"status": "ok"},
    ) as mock:
        build_execution_profile("0xabc", gen(), client=object())
    passed_activity = mock.call_args.args[1]
    assert isinstance(passed_activity, list)
    assert len(passed_activity) == 2
    # Counts derived after eagerization.
    assert mock.call_args.kwargs["counts"]["trade_records"] == 2

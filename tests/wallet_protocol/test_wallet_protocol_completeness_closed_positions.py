"""G2 — Acceptance tests for `closed_positions_capture` on `data_completeness`.

Spec: docs/superpowers/specs/2026-05-17-g2-closed-positions-cap-on-completeness.md §3
Origin: docs/audits/2026-05-10-wallet-analyzer-live-sampling.md G2

Surfaces the existing `fetch_termination["closed_positions"]` enum +
`n_closed_positions` count onto `ProtocolReport.data_completeness` so
downstream consumers can read closed-position cap state from the same place
they read activity coverage.

12 tests covering:
  §3.1  Field presence + types                      — 2 tests
  §3.2  Fetched count                               — 2 tests
  §3.3  Cap-hit semantics across all enum values    — 5 tests
  §3.4  Caveat propagation                          — 2 tests
  §3.5  Existing keys unchanged                     — 1 test

All tests must be RED until `analyze_wallet` adds the `closed_positions_capture`
sub-dict and the `closed_positions_truncated` caveat.
"""
from __future__ import annotations

import time

from sniper.analysis.wallet_protocol.analyze_wallet import analyze_wallet
from sniper.analysis.wallet_protocol.types import WalletData


def _now() -> int:
    return int(time.time())


def _pos(
    cid: str = "cid_a",
    outcome: str = "Yes",
    curPrice: float = 1.0,
    avgPrice: float = 0.50,
    size: int = 100,
    realizedPnl: float = 50.0,
    ts: int | None = None,
) -> dict:
    if ts is None:
        ts = _now()
    elif ts < 1_000_000_000:
        ts = _now() - 86400 + ts
    return {
        "conditionId": cid,
        "outcome": outcome,
        "curPrice": curPrice,
        "avgPrice": avgPrice,
        "size": size,
        "realizedPnl": realizedPnl,
        "timestamp": ts,
        "eventSlug": f"slug-{cid}",
    }


def _wallet_data(
    *,
    closed_positions: list,
    fetch_termination: dict | None = None,
) -> WalletData:
    return WalletData(
        wallet="0xabc",
        closed_positions=list(closed_positions),
        open_positions=[],
        activity_rows=[],
        activity_cap_hit=False,
        archetype_layer1=None,
        archetype_layer2=None,
        execution_profile=None,
        fetched_at=_now(),
        subgraph_fallback_used=False,
        walk_full_history=False,
        n_unmapped_fills=0,
        fetch_termination=fetch_termination,
    )


def _run(*, closed_positions, fetch_termination=None):
    data = _wallet_data(
        closed_positions=closed_positions, fetch_termination=fetch_termination,
    )
    return analyze_wallet("0xabc", pre_fetched_data=data, n_iter=50, seed=1)


# ===========================================================================
# §3.1  Field presence + types
# ===========================================================================

def test_closed_positions_capture_block_always_present():
    report = _run(closed_positions=[_pos()] * 5)
    dc = report.data_completeness
    assert isinstance(dc, dict)
    assert "closed_positions_capture" in dc, (
        f"closed_positions_capture missing. Keys: {sorted(dc.keys())}"
    )
    block = dc["closed_positions_capture"]
    assert set(block.keys()) == {"closed_positions_fetched", "closed_positions_cap_hit"}, (
        f"Block keys must be exactly {{fetched, cap_hit}}. Got: {sorted(block.keys())}"
    )
    assert isinstance(block["closed_positions_fetched"], int)
    assert block["closed_positions_fetched"] >= 0
    assert isinstance(block["closed_positions_cap_hit"], bool)


def test_closed_positions_capture_block_present_when_zero_closed():
    """Wallets with no closed history still get the block."""
    report = _run(closed_positions=[])
    dc = report.data_completeness
    assert "closed_positions_capture" in dc
    block = dc["closed_positions_capture"]
    assert block["closed_positions_fetched"] == 0
    assert block["closed_positions_cap_hit"] is False


# ===========================================================================
# §3.2  Fetched count
# ===========================================================================

def test_closed_positions_fetched_matches_n_closed_positions():
    report = _run(closed_positions=[_pos()] * 5)
    block = report.data_completeness["closed_positions_capture"]
    assert block["closed_positions_fetched"] == report.n_closed_positions == 5


def test_closed_positions_fetched_is_int_not_none():
    """Zero must be the integer 0, NOT None. JSON consumers depend on the type."""
    report = _run(closed_positions=[])
    block = report.data_completeness["closed_positions_capture"]
    assert block["closed_positions_fetched"] is not None
    assert block["closed_positions_fetched"] == 0
    assert isinstance(block["closed_positions_fetched"], int)


# ===========================================================================
# §3.3  Cap-hit semantics across fetch_termination values
# ===========================================================================

def test_cap_hit_true_when_fetch_termination_api_refused():
    report = _run(
        closed_positions=[_pos()] * 3,
        fetch_termination={"closed_positions": "api_refused"},
    )
    block = report.data_completeness["closed_positions_capture"]
    assert block["closed_positions_cap_hit"] is True


def test_cap_hit_false_when_fetch_termination_empty_page():
    report = _run(
        closed_positions=[_pos()] * 3,
        fetch_termination={"closed_positions": "empty_page"},
    )
    block = report.data_completeness["closed_positions_capture"]
    assert block["closed_positions_cap_hit"] is False


def test_cap_hit_false_when_fetch_termination_partial_page():
    report = _run(
        closed_positions=[_pos()] * 3,
        fetch_termination={"closed_positions": "partial_page"},
    )
    block = report.data_completeness["closed_positions_capture"]
    assert block["closed_positions_cap_hit"] is False


def test_cap_hit_false_when_fetch_termination_missing_closed_positions_key():
    """Absence-of-signal must NOT be read as cap. Conservative default."""
    report = _run(
        closed_positions=[_pos()] * 3,
        fetch_termination={"activity": "empty_page"},  # no closed_positions key
    )
    block = report.data_completeness["closed_positions_capture"]
    assert block["closed_positions_cap_hit"] is False


def test_cap_hit_false_when_fetch_termination_is_none():
    report = _run(
        closed_positions=[_pos()] * 3,
        fetch_termination=None,
    )
    block = report.data_completeness["closed_positions_capture"]
    assert block["closed_positions_cap_hit"] is False


# ===========================================================================
# §3.4  Caveat propagation
# ===========================================================================

def test_caveat_closed_positions_truncated_fires_when_cap_hit():
    report = _run(
        closed_positions=[_pos()] * 3,
        fetch_termination={"closed_positions": "api_refused"},
    )
    caveats = report.data_completeness["caveats"]
    assert "closed_positions_truncated" in caveats, (
        f"closed_positions_truncated key missing from caveats: {sorted(caveats.keys())}"
    )
    assert caveats["closed_positions_truncated"] is True


def test_caveat_closed_positions_truncated_false_when_not_cap_hit():
    report = _run(
        closed_positions=[_pos()] * 3,
        fetch_termination={"closed_positions": "empty_page"},
    )
    caveats = report.data_completeness["caveats"]
    assert caveats["closed_positions_truncated"] is False


# ===========================================================================
# §3.5  Existing keys unchanged
# ===========================================================================

def test_existing_data_completeness_keys_still_present():
    """Defensive pin: no key removal/rename in this slice."""
    report = _run(closed_positions=[_pos()] * 3)
    dc = report.data_completeness
    expected = {
        "activity_capture",
        "attribution_coverage",
        "caveats",
        "closed_positions_capture",
        "tag_lookup_state",  # G4 — tag-lookup availability
    }
    assert set(dc.keys()) == expected, (
        f"data_completeness key set drift. Expected {expected}, got {sorted(dc.keys())}"
    )


# ===========================================================================
# Downstream consumer — strategy_pattern_audit
# ===========================================================================
# strategy_pattern_audit/core.py has a hard-coded known_caveats set used by
# _data_completeness_quality_label. Adding a caveat without updating that set
# causes the function to return "unknown_caveat", which downstream marks
# n_evaluable=0 and emits UNKNOWN_SURFACE_QUALITY. Pinning the consumer is
# part of this slice — G2 ships the surface AND prevents the consumer
# regression. Spec §6 ("rule consumption is downstream and out of scope")
# referred to using the new field for new rules, not preventing audit
# breakage from the new caveat key.


def test_closed_positions_truncated_caveat_does_not_trigger_unknown_quality():
    """Regression: closed_positions_truncated must be in known_caveats so
    _data_completeness_quality_label does not return 'unknown_caveat'."""
    from sniper.analysis.strategy_pattern_audit.core import (
        _data_completeness_quality_label,
    )

    report = _run(
        closed_positions=[_pos()] * 3,
        fetch_termination={"closed_positions": "api_refused"},
    )
    label = _data_completeness_quality_label(report)
    assert label != "unknown_caveat", (
        f"closed_positions_truncated triggered unknown_caveat — "
        f"known_caveats set in strategy_pattern_audit.core needs the new key. "
        f"Got: {label}"
    )


def test_closed_positions_truncated_downgrades_quality_to_poor():
    """Truncated closed-position history maps to the same poor band as
    activity_truncated. Both mean PnL/hypothesis denominators may be
    incomplete; the audit must downgrade the wallet's data quality
    accordingly."""
    from sniper.analysis.strategy_pattern_audit.core import (
        _data_completeness_quality_label,
    )

    report = _run(
        closed_positions=[_pos()] * 3,
        fetch_termination={"closed_positions": "api_refused"},
    )
    label = _data_completeness_quality_label(report)
    assert label == "poor", (
        f"closed_positions_truncated should map to 'poor' (same band as "
        f"activity_truncated). Got: {label!r}"
    )

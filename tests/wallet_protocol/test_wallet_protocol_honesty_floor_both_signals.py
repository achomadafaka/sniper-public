"""Honesty floor uses BOTH realized and combined signals — failing acceptance suite.

Spec: docs/superpowers/specs/2026-05-12-fetch-rebuild-design.md §A.5
Acceptance criteria: #18, #19

New behavior being locked down:
  - pnl_floor_satisfied = (report.realized_pnl > 0) OR (report.combined_pnl > 0)
  - When realized <= 0 but combined > 0 → emit EDGE_DEPENDS_ON_OPEN_POSITIONS flag;
    do NOT downgrade overall_verdict.
  - When realized <= 0 AND combined <= 0 AND no coverage → downgrade +
    NEGATIVE_PNL_EDGE_UNEXPLAINED (existing behavior, now guarded by combined too).
  - New module-level constant: EDGE_DEPENDS_ON_OPEN_POSITIONS in analyze_wallet.

Tests must all fail on `23a3083` because:
  1. ProtocolReport has no `realized_pnl` or `combined_pnl` fields yet.
  2. The constant EDGE_DEPENDS_ON_OPEN_POSITIONS does not exist in analyze_wallet.
  3. _apply_negative_pnl_honesty_floor still uses report.net_pnl > 0 (old logic).

Do NOT modify this file's import or fixture structure after implementation —
the existing 34 honesty-floor tests (test_wallet_protocol_honesty_floor.py and
test_wallet_protocol_tag_grain_rescue_floor.py) remain authoritative for the
prior behavior.
"""
from __future__ import annotations

import pytest

from sniper.analysis.wallet_protocol import analyze_wallet as aw_mod
from sniper.analysis.wallet_protocol.types import ProtocolReport


# ---------------------------------------------------------------------------
# Symbol helpers — single-point fail with clear message when not landed.
# ---------------------------------------------------------------------------


def _require(name: str):
    obj = getattr(aw_mod, name, None)
    if obj is None:
        pytest.fail(
            f"expected `sniper.analysis.wallet_protocol.analyze_wallet.{name}` "
            f"to exist; implementation has not landed yet"
        )
    return obj


def _floor():
    return _require("_apply_negative_pnl_honesty_floor")


def _const_negative():
    return _require("NEGATIVE_PNL_EDGE_UNEXPLAINED")


def _const_depends():
    return _require("EDGE_DEPENDS_ON_OPEN_POSITIONS")


# ---------------------------------------------------------------------------
# Fixture builders — reuse the same shapes as the existing floor tests
# (test_wallet_protocol_honesty_floor.py) so fixture construction style is
# consistent. The key difference: we now populate `realized_pnl` and
# `combined_pnl` (new A.4 fields) in addition to `net_pnl`.
# ---------------------------------------------------------------------------


def _empty_data_completeness() -> dict:
    return {
        "activity_capture": {
            "activity_rows_fetched": 0,
            "activity_cap_hit": False,
            "walk_full_history": False,
            "subgraph_fallback_used": False,
            "n_unmapped_fills": 0,
        },
        "attribution_coverage": {
            "n_total": 5000,
            "n_outside_window": 0,
            "n_insufficient_data": 5000,
            "n_attributed": 0,
            "n_with_resolved_attribution": 0,
            "history_assumed_full": True,
        },
        "caveats": {
            "activity_truncated": False,
            "subgraph_unmapped_fills": False,
            "attribution_coverage_low": True,
            "limitation_present": True,
        },
    }


def _make_report(
    *,
    overall_verdict: str = "edge_detected",
    net_pnl: float = -100_000.0,
    realized_pnl: float | None = None,
    combined_pnl: float | None = None,
    unrealized_pnl: float | None = None,
    data_completeness: dict | None = None,
    timing_surface: dict | None = None,
    position_building_surface: dict | None = None,
    data_quality_flags: list[str] | None = None,
) -> ProtocolReport:
    """Build a ProtocolReport with floor-relevant fields.

    realized_pnl defaults to net_pnl when not explicitly specified (consistent
    with the A.4 backward-compat invariant: realized_pnl == net_pnl).
    combined_pnl defaults to realized_pnl + (unrealized_pnl or 0).
    """
    _realized = net_pnl if realized_pnl is None else realized_pnl
    _unrealized = 0.0 if unrealized_pnl is None else unrealized_pnl
    _combined = (_realized + _unrealized) if combined_pnl is None else combined_pnl

    kwargs: dict = {
        "wallet": "0xtest",
        "overall_verdict": overall_verdict,  # type: ignore[arg-type]
        "net_pnl": net_pnl,
        "realized_pnl": _realized,
        "unrealized_pnl": _unrealized,
        "combined_pnl": _combined,
        "data_completeness": data_completeness,
        "timing_surface": timing_surface,
        "position_building_surface": position_building_surface,
        "data_quality_flags": list(data_quality_flags or []),
    }
    return ProtocolReport(**kwargs)


# ---------------------------------------------------------------------------
# Part 1 — New constant exists at module level (criterion #18 precondition)
# ---------------------------------------------------------------------------


def test_edge_depends_on_open_positions_constant_exists():
    """EDGE_DEPENDS_ON_OPEN_POSITIONS must be a module-level str constant."""
    const = _const_depends()
    assert isinstance(const, str), "EDGE_DEPENDS_ON_OPEN_POSITIONS must be a str"


def test_edge_depends_on_open_positions_constant_value():
    """Constant value must be exactly 'EDGE_DEPENDS_ON_OPEN_POSITIONS'."""
    const = _const_depends()
    assert const == "EDGE_DEPENDS_ON_OPEN_POSITIONS", (
        f"expected 'EDGE_DEPENDS_ON_OPEN_POSITIONS', got {const!r}"
    )


# ---------------------------------------------------------------------------
# Part 2 — Floor downgrades when BOTH realized AND combined are non-positive
# (existing behavior, now using the new fields as the gate)
# ---------------------------------------------------------------------------


def test_floor_fires_when_realized_and_combined_both_nonpositive():
    """Criterion #18: realized<=0 AND combined<=0 AND no coverage → downgrade."""
    floor = _floor()
    neg_const = _const_negative()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        realized_pnl=-100_000.0,
        unrealized_pnl=0.0,          # combined = -100_000 + 0 = -100_000 <= 0
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    assert report.overall_verdict == "underpowered"
    assert neg_const in report.data_quality_flags


def test_floor_fires_when_combined_also_negative():
    """realized < 0 AND unrealized < 0 → combined < 0 → downgrade."""
    floor = _floor()
    neg_const = _const_negative()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-50_000.0,
        realized_pnl=-50_000.0,
        unrealized_pnl=-20_000.0,    # combined = -70_000 <= 0
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    assert report.overall_verdict == "underpowered"
    assert neg_const in report.data_quality_flags


def test_floor_fires_when_realized_zero_and_combined_zero():
    """realized == 0 AND combined == 0 → both non-positive → downgrade."""
    floor = _floor()
    neg_const = _const_negative()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=0.0,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    assert report.overall_verdict == "underpowered"
    assert neg_const in report.data_quality_flags


# ---------------------------------------------------------------------------
# Part 3 — Floor does NOT downgrade when realized > 0 (regardless of combined)
# ---------------------------------------------------------------------------


def test_floor_does_not_fire_when_realized_positive():
    """Criterion #18: realized > 0 → pnl_floor_satisfied → no downgrade."""
    floor = _floor()
    neg_const = _const_negative()
    depends_const = _const_depends()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=100_000.0,
        realized_pnl=100_000.0,
        unrealized_pnl=-200_000.0,   # combined < 0 but realized > 0 → passes
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    assert report.overall_verdict == "edge_detected"
    assert neg_const not in report.data_quality_flags
    assert depends_const not in report.data_quality_flags


def test_floor_does_not_fire_when_combined_positive_and_realized_positive():
    """Both realized > 0 AND combined > 0 → passes trivially."""
    floor = _floor()
    neg_const = _const_negative()
    depends_const = _const_depends()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=50_000.0,
        realized_pnl=50_000.0,
        unrealized_pnl=30_000.0,
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    assert report.overall_verdict == "edge_detected"
    assert neg_const not in report.data_quality_flags
    assert depends_const not in report.data_quality_flags


# ---------------------------------------------------------------------------
# Part 4 — Floor does NOT downgrade when realized <= 0 BUT combined > 0
#          Instead emits EDGE_DEPENDS_ON_OPEN_POSITIONS (criterion #18)
# ---------------------------------------------------------------------------


def test_floor_does_not_downgrade_when_realized_negative_but_combined_positive():
    """Criterion #18: realized < 0 AND combined > 0 → no downgrade; emits EDGE_DEPENDS."""
    floor = _floor()
    neg_const = _const_negative()
    depends_const = _const_depends()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        realized_pnl=-100_000.0,
        unrealized_pnl=150_000.0,    # combined = +50_000 > 0
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    # Verdict must NOT be downgraded.
    assert report.overall_verdict == "edge_detected"
    # Downgrade flag must NOT be present.
    assert neg_const not in report.data_quality_flags
    # EDGE_DEPENDS flag MUST be present.
    assert any(f.startswith(f"{depends_const}:") for f in report.data_quality_flags), (
        f"Expected a flag starting with '{depends_const}:' in {report.data_quality_flags}"
    )


def test_floor_does_not_downgrade_when_realized_zero_but_combined_positive():
    """realized == 0 but combined > 0 → no downgrade; emits EDGE_DEPENDS."""
    floor = _floor()
    neg_const = _const_negative()
    depends_const = _const_depends()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=0.0,
        realized_pnl=0.0,
        unrealized_pnl=80_000.0,     # combined = +80_000 > 0
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    assert report.overall_verdict == "edge_detected"
    assert neg_const not in report.data_quality_flags
    assert any(f.startswith(f"{depends_const}:") for f in report.data_quality_flags)


# ---------------------------------------------------------------------------
# Part 5 — EDGE_DEPENDS_ON_OPEN_POSITIONS flag format (criterion #18)
# ---------------------------------------------------------------------------


def test_edge_depends_flag_contains_realized_and_unrealized_values():
    """The EDGE_DEPENDS flag must embed realized= and unrealized= values."""
    floor = _floor()
    depends_const = _const_depends()
    realized = -727_729.0
    unrealized = 900_000.0
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=realized,
        realized_pnl=realized,
        unrealized_pnl=unrealized,   # combined = +172_271 > 0
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    # Find the flag.
    depends_flags = [f for f in report.data_quality_flags if f.startswith(f"{depends_const}:")]
    assert depends_flags, f"No EDGE_DEPENDS flag found in {report.data_quality_flags}"
    flag_str = depends_flags[0]
    # Must contain 'realized=' and 'unrealized=' per spec format.
    assert "realized=" in flag_str, f"Flag missing 'realized=': {flag_str!r}"
    assert "unrealized=" in flag_str, f"Flag missing 'unrealized=': {flag_str!r}"


def test_edge_depends_flag_prefix_is_exact_constant():
    """The flag string must start with exactly 'EDGE_DEPENDS_ON_OPEN_POSITIONS:'."""
    floor = _floor()
    depends_const = _const_depends()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-10_000.0,
        realized_pnl=-10_000.0,
        unrealized_pnl=50_000.0,
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    depends_flags = [f for f in report.data_quality_flags if "EDGE_DEPENDS" in f]
    assert depends_flags
    # The prefix before ':' must equal the constant exactly.
    prefix = depends_flags[0].split(":")[0]
    assert prefix == depends_const, f"Flag prefix {prefix!r} != constant {depends_const!r}"


# ---------------------------------------------------------------------------
# Part 6 — Mutual exclusion: NEGATIVE_PNL_EDGE_UNEXPLAINED and
#          EDGE_DEPENDS_ON_OPEN_POSITIONS never fire from the SAME floor call
# ---------------------------------------------------------------------------


def test_negative_pnl_and_edge_depends_mutually_exclusive_on_downgrade():
    """When floor downgrades → NEGATIVE_PNL_EDGE_UNEXPLAINED; EDGE_DEPENDS not added."""
    floor = _floor()
    neg_const = _const_negative()
    depends_const = _const_depends()
    # combined <= 0 → triggers downgrade path.
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        realized_pnl=-100_000.0,
        unrealized_pnl=0.0,
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    assert neg_const in report.data_quality_flags
    assert not any(f.startswith(f"{depends_const}:") for f in report.data_quality_flags), (
        f"EDGE_DEPENDS must not fire when downgrade fires: {report.data_quality_flags}"
    )


def test_edge_depends_path_does_not_add_negative_pnl_flag():
    """When EDGE_DEPENDS fires → NEGATIVE_PNL_EDGE_UNEXPLAINED must NOT be added."""
    floor = _floor()
    neg_const = _const_negative()
    depends_const = _const_depends()
    # realized < 0, combined > 0 → EDGE_DEPENDS path.
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        realized_pnl=-100_000.0,
        unrealized_pnl=200_000.0,
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    assert neg_const not in report.data_quality_flags, (
        f"NEGATIVE_PNL_EDGE_UNEXPLAINED must not fire when EDGE_DEPENDS fires"
    )
    assert any(f.startswith(f"{depends_const}:") for f in report.data_quality_flags)


# ---------------------------------------------------------------------------
# Part 7 — overall_verdict stays edge_detected when EDGE_DEPENDS fires
# ---------------------------------------------------------------------------


def test_overall_verdict_not_mutated_when_edge_depends_fires():
    """Criterion #18: when EDGE_DEPENDS fires, overall_verdict stays 'edge_detected'."""
    floor = _floor()
    depends_const = _const_depends()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-50_000.0,
        realized_pnl=-50_000.0,
        unrealized_pnl=100_000.0,    # combined = +50_000 > 0
        data_completeness=_empty_data_completeness(),
    )
    floor(report)
    assert report.overall_verdict == "edge_detected", (
        f"overall_verdict must not be mutated when EDGE_DEPENDS fires; "
        f"got {report.overall_verdict!r}"
    )


# ---------------------------------------------------------------------------
# Part 8 — Floor still skips when verdict != edge_detected (same as before)
# ---------------------------------------------------------------------------


def test_floor_skips_entirely_when_verdict_is_underpowered():
    """Floor is a no-op when overall_verdict is already 'underpowered'."""
    floor = _floor()
    neg_const = _const_negative()
    depends_const = _const_depends()
    report = _make_report(
        overall_verdict="underpowered",
        net_pnl=-100_000.0,
        realized_pnl=-100_000.0,
        unrealized_pnl=0.0,
    )
    floor(report)
    assert report.overall_verdict == "underpowered"
    assert neg_const not in report.data_quality_flags
    assert not any(f.startswith(f"{depends_const}:") for f in report.data_quality_flags)


def test_floor_skips_entirely_when_verdict_is_no_edge():
    """Floor is a no-op when overall_verdict is 'no_edge'."""
    floor = _floor()
    neg_const = _const_negative()
    depends_const = _const_depends()
    report = _make_report(
        overall_verdict="no_edge",
        net_pnl=-100_000.0,
        realized_pnl=-100_000.0,
        unrealized_pnl=0.0,
    )
    floor(report)
    assert report.overall_verdict == "no_edge"
    assert neg_const not in report.data_quality_flags
    assert not any(f.startswith(f"{depends_const}:") for f in report.data_quality_flags)


# ---------------------------------------------------------------------------
# Part 9 — Coverage floor still rescues even under the new PnL gate
#          (existing floor contract preserved; criterion #19 compatibility)
# ---------------------------------------------------------------------------


def test_coverage_rescues_when_realized_negative_and_combined_negative():
    """When coverage is non-trivial, floor does not fire even if both PnL signals negative.

    This preserves the existing coverage-rescue behavior (Issue #24 rows #3-#5).
    """
    from sniper.analysis.wallet_protocol.analyze_wallet import (
        attribution_coverage_nontrivial,
    )
    floor = _floor()
    neg_const = _const_negative()
    # Build data_completeness with n_with_resolved_attribution > 0 → coverage non-trivial.
    dc = _empty_data_completeness()
    dc["attribution_coverage"]["n_with_resolved_attribution"] = 5
    dc["attribution_coverage"]["n_attributed"] = 5
    dc["attribution_coverage"]["n_insufficient_data"] = 0

    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        realized_pnl=-100_000.0,
        unrealized_pnl=-20_000.0,    # combined also negative
        data_completeness=dc,
    )
    floor(report)
    # Coverage satisfied → no downgrade.
    assert report.overall_verdict == "edge_detected"
    assert neg_const not in report.data_quality_flags


def test_edge_depends_fires_even_when_coverage_is_trivial():
    """EDGE_DEPENDS emits regardless of coverage state (it's informational only)."""
    floor = _floor()
    depends_const = _const_depends()
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-100_000.0,
        realized_pnl=-100_000.0,
        unrealized_pnl=200_000.0,    # combined > 0 → no downgrade anyway
        data_completeness=_empty_data_completeness(),   # no coverage
    )
    floor(report)
    assert report.overall_verdict == "edge_detected"
    assert any(f.startswith(f"{depends_const}:") for f in report.data_quality_flags)


# ---------------------------------------------------------------------------
# Part 10 — EDGE_DEPENDS does not clobber pre-existing flags
# ---------------------------------------------------------------------------


def test_edge_depends_appends_without_removing_existing_flags():
    """EDGE_DEPENDS flag is appended; pre-existing data_quality_flags are kept."""
    floor = _floor()
    depends_const = _const_depends()
    existing_flags = ["SOME_OTHER_FLAG", "ANOTHER_FLAG"]
    report = _make_report(
        overall_verdict="edge_detected",
        net_pnl=-50_000.0,
        realized_pnl=-50_000.0,
        unrealized_pnl=100_000.0,
        data_completeness=_empty_data_completeness(),
        data_quality_flags=list(existing_flags),
    )
    floor(report)
    for f in existing_flags:
        assert f in report.data_quality_flags, f"Pre-existing flag {f!r} was dropped"
    assert any(f.startswith(f"{depends_const}:") for f in report.data_quality_flags)

"""Wallet parameter extractor — derives a candidate decision rule from a dossier.

This is the Path A "reverse engineering" starting point: given a wallet's
v5+ dossier, produce a structured description of how the wallet picks
markets, sizes positions, times entries, and exits. The output feeds the
later backtest harness (Path A step 2).

This is NOT a thesis. It's a fingerprint of behavior. Validity of any
rule extracted here depends on:
  - The source dossier having at least one interpretable grain with
    a promoted thesis_candidate (otherwise we're describing noise).
  - Out-of-sample backtest validation (separate spec).

The extractor is purely derivative — no edge claims, no live signals.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Mapping, Sequence

EXTRACTOR_VERSION = "wallet_rule_v1"


def extract_wallet_rule(
    tables: Any,
    *,
    target_grain_id: str | None = None,
) -> dict[str, Any]:
    """Derive a candidate decision rule from a dossier.

    Args:
        tables: A DossierTables-like object (or dict view) with v5
            descriptor surfaces and earlier mechanism/grain tables.
        target_grain_id: If given, scope the rule to a specific grain's
            evidence (e.g. the one that produced the cross-wallet thesis).
            If None, picks the strongest interpretable grain automatically.

    Returns:
        A wallet_rule_v1 structured dict — see this module's docstring.
    """
    view = _as_view(tables)
    wallet = _meta(view, "wallet") or "unknown"
    schema_version = _meta(view, "schema_version") or "unknown"

    grain_row, grain_key = _select_target_grain(view, target_grain_id)
    if grain_row is None:
        return _empty_rule(wallet, schema_version, reason="no_interpretable_grain")

    category = grain_key.get("category", "data_unavailable")
    market_filter = _build_market_filter(grain_key)
    sizing = _build_sizing(view, category)
    timing = _build_timing(view, category)
    exit_rule = _build_exit_rule(view, grain_key.get("exit_mode"))
    fragmentation = _build_fragmentation(view, category)
    evidence = _build_evidence(view, grain_row, grain_key)

    return {
        "extractor_version": EXTRACTOR_VERSION,
        "wallet": wallet,
        "source_schema_version": schema_version,
        "source_grain": {
            "grain_id": grain_row.get("grain_id"),
            "grain_key": grain_key,
            "n_positions": int(grain_row.get("n_positions") or 0),
            "n_episodes": int(grain_row.get("n_episodes") or 0),
            "grain_quality": grain_row.get("grain_quality"),
        },
        "market_filter": market_filter,
        "sizing": sizing,
        "timing": timing,
        "exit_rule": exit_rule,
        "fragmentation": fragmentation,
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_view(tables: Any) -> Mapping[str, Any]:
    """Accept DossierTables dataclass or a dict of table-name → row list."""
    if isinstance(tables, Mapping):
        return tables
    return {
        "dossier_meta": getattr(tables, "dossier_meta", []),
        "result_semantics": getattr(tables, "result_semantics", []),
        "mechanism_evidence": getattr(tables, "mechanism_evidence", []),
        "thesis_candidates": getattr(tables, "thesis_candidates", []),
        "data_gaps": getattr(tables, "data_gaps", []),
        "descriptor_hour_distribution": getattr(tables, "descriptor_hour_distribution", []),
        "descriptor_cadence": getattr(tables, "descriptor_cadence", []),
        "descriptor_sizing": getattr(tables, "descriptor_sizing", []),
        "descriptor_time_in_position": getattr(tables, "descriptor_time_in_position", []),
        "descriptor_fills_per_position": getattr(tables, "descriptor_fills_per_position", []),
    }


def _meta(view: Mapping[str, Any], key: str) -> str | None:
    for row in view.get("dossier_meta") or []:
        if row.get("key") == key:
            return row.get("value")
    return None


# Grain qualities the dossier treats as interpretable/promotable — mirrors the
# wallet_dossier/grain.py promotion gates and report_markdown. The extractor
# previously matched only "interpretable", silently ignoring promotable
# "interpretable_evidence_only" grains (category-unavailable but otherwise
# passing), making it stricter than the system's own interpretability definition.
_INTERPRETABLE_QUALITIES = ("interpretable", "interpretable_evidence_only")


def _select_target_grain(
    view: Mapping[str, Any],
    target_grain_id: str | None,
) -> tuple[dict | None, dict]:
    """Pick a grain to anchor the rule on.

    Priority:
      1. target_grain_id if provided AND present in result_semantics
      2. The interpretable grain with a thesis_candidate
      3. The interpretable grain with the most episodes
      4. None
    """
    grains = list(view.get("result_semantics") or [])

    if target_grain_id:
        for g in grains:
            if str(g.get("grain_id")) == target_grain_id:
                return g, _load_json_object(g.get("grain_key"))
        return None, {}

    thesis_grain_ids = {
        str(r.get("grain_id"))
        for r in (view.get("thesis_candidates") or [])
    }
    interpretable = [
        g for g in grains
        if str(g.get("grain_quality") or "") in _INTERPRETABLE_QUALITIES
    ]

    def _grain_rank(g: Mapping[str, Any]) -> tuple[int, int]:
        # Prefer fully-interpretable over evidence-only, then by episode count.
        q = str(g.get("grain_quality") or "")
        return (1 if q == "interpretable" else 0, int(g.get("n_episodes") or 0))

    with_thesis = [g for g in interpretable if str(g.get("grain_id")) in thesis_grain_ids]
    if with_thesis:
        chosen = max(with_thesis, key=_grain_rank)
        return chosen, _load_json_object(chosen.get("grain_key"))
    if interpretable:
        chosen = max(interpretable, key=_grain_rank)
        return chosen, _load_json_object(chosen.get("grain_key"))
    return None, {}


def _build_market_filter(grain_key: Mapping[str, Any]) -> dict[str, Any]:
    """Mirror the grain_key dimensions an executor would filter against.

    A dimension whose value is unknown (`data_unavailable`/None) is emitted as
    an empty list / None — an empty filter means "no constraint", so an
    evidence-only grain (category unavailable) still backtests on its KNOWN
    dimensions instead of matching zero positions on a `data_unavailable` filter.
    """
    def _dim(name: str) -> list[str]:
        v = grain_key.get(name)
        return [] if (not v or str(v) == "data_unavailable") else [v]

    def _scalar(name: str):
        v = grain_key.get(name)
        return None if (not v or str(v) == "data_unavailable") else v

    bucket = grain_key.get("entry_price_bucket")
    return {
        "categories": _dim("category"),
        "market_types": _dim("market_type"),
        "outcome_roles": _dim("outcome_role"),
        "favorite_status": _scalar("favorite_status"),
        "event_family": _scalar("event_family"),
        "entry_price_bucket": bucket,
        "entry_price_range": _price_range_from_bucket(bucket),
    }


def _price_range_from_bucket(bucket: str | None) -> list[float] | None:
    """Map an entry_price_bucket label to the actual [lo, hi) range."""
    if bucket is None:
        return None
    mapping = {
        "p_lt_05": [0.00, 0.05],
        "p_05_25": [0.05, 0.25],
        "p_25_50": [0.25, 0.50],
        "p_50_75": [0.50, 0.75],
        "p_gte_75": [0.75, 1.00],
    }
    return mapping.get(bucket)


def _build_sizing(
    view: Mapping[str, Any], category: str
) -> dict[str, Any]:
    """Pull descriptor_sizing rows for BUY side in the target category."""
    rows = [
        r for r in (view.get("descriptor_sizing") or [])
        if str(r.get("side") or "").upper() == "BUY"
        and str(r.get("market_category") or "") == category
    ]
    if not rows:
        return {
            "median_usdc": None,
            "p10_usdc": None,
            "p90_usdc": None,
            "max_usdc": None,
            "coeff_of_variation": None,
            "fingerprint": "unknown",
            "n_fills_sourced": 0,
        }
    row = rows[0]  # one row per (side, category)
    cv = row.get("coeff_of_variation")
    return {
        "median_usdc": float(row.get("median_size_usdc") or 0.0),
        "p10_usdc": float(row.get("p10_size_usdc") or 0.0),
        "p90_usdc": float(row.get("p90_size_usdc") or 0.0),
        "max_usdc": float(row.get("max_size_usdc") or 0.0),
        "coeff_of_variation": float(cv) if cv is not None else None,
        "fingerprint": _sizing_fingerprint(cv),
        "n_fills_sourced": int(row.get("n_fills") or 0),
    }


def _sizing_fingerprint(cv: Any) -> str:
    if cv is None:
        return "unknown"
    cv_f = float(cv)
    if cv_f < 0.1:
        return "uniform"  # algorithmic-looking
    if cv_f < 0.5:
        return "consistent"
    if cv_f < 1.0:
        return "variable"
    return "highly_variable"


def _build_timing(
    view: Mapping[str, Any], category: str
) -> dict[str, Any]:
    """Aggregate descriptor_hour_distribution for BUY in target category."""
    hour_rows = [
        r for r in (view.get("descriptor_hour_distribution") or [])
        if str(r.get("side") or "").upper() == "BUY"
        and str(r.get("market_category") or "") == category
    ]
    by_hour: Counter[int] = Counter()
    for r in hour_rows:
        hour = int(r.get("hour_utc") or 0)
        n = int(r.get("n_trades") or 0)
        by_hour[hour] += n
    total = sum(by_hour.values())

    # Top 5 most-active hours
    top_hours = [h for h, _ in by_hour.most_common(5)]

    # Cadence for the same scope
    cadence_rows = [
        r for r in (view.get("descriptor_cadence") or [])
        if str(r.get("side") or "").upper() == "BUY"
        and str(r.get("market_category") or "") == category
    ]
    burst_share = None
    median_seconds = None
    if cadence_rows:
        row = cadence_rows[0]
        burst_share = row.get("burst_share")
        median_seconds = row.get("median_seconds_between_trades")

    return {
        "preferred_hours_utc": sorted(top_hours),
        "n_trades_sourced": total,
        "median_seconds_between_trades": median_seconds,
        "burst_share": burst_share,
        "cadence_fingerprint": _cadence_fingerprint(burst_share, median_seconds),
    }


def _cadence_fingerprint(burst_share: Any, median_s: Any) -> str:
    if burst_share is None:
        return "unknown"
    bs = float(burst_share)
    if bs > 0.3:
        return "bot_like_bursts"
    if median_s is not None and float(median_s) < 60:
        return "fast_reactive"
    if median_s is not None and float(median_s) > 86400:
        return "slow_deliberate"
    return "deliberate"


def _build_exit_rule(
    view: Mapping[str, Any], expected_mode: str | None
) -> dict[str, Any]:
    """Pull descriptor_time_in_position for the grain's expected exit_mode."""
    rows = list(view.get("descriptor_time_in_position") or [])
    if expected_mode:
        filtered = [r for r in rows if str(r.get("exit_mode") or "") == expected_mode]
    else:
        filtered = rows
    if not filtered:
        # Fall back to whichever mode has the most positions
        if rows:
            filtered = [max(rows, key=lambda r: int(r.get("n_positions") or 0))]
        else:
            return {
                "mode": expected_mode or "unknown",
                "median_seconds_in_position": None,
                "p10_seconds_in_position": None,
                "p90_seconds_in_position": None,
                "fingerprint": "unknown",
                "n_positions_sourced": 0,
            }
    row = filtered[0]
    mode = str(row.get("exit_mode") or "unknown")
    return {
        "mode": mode,
        "median_seconds_in_position": row.get("median_seconds_in_position"),
        "p10_seconds_in_position": row.get("p10_seconds_in_position"),
        "p90_seconds_in_position": row.get("p90_seconds_in_position"),
        "fingerprint": _exit_fingerprint(mode),
        "n_positions_sourced": int(row.get("n_positions") or 0),
    }


def _exit_fingerprint(mode: str) -> str:
    return {
        "held": "hold_to_resolution",
        "pre_resolution": "sell_pre_resolution",
        "still_open": "still_holding",
        "unresolved": "unknown_resolution",
    }.get(mode, "unknown")


def _build_fragmentation(
    view: Mapping[str, Any], category: str
) -> dict[str, Any]:
    rows = [
        r for r in (view.get("descriptor_fills_per_position") or [])
        if str(r.get("market_category") or "") == category
    ]
    if not rows:
        return {
            "median_fills_per_position": None,
            "p10_fills": None,
            "p90_fills": None,
            "single_fill_share": None,
            "fingerprint": "unknown",
            "n_positions_sourced": 0,
        }
    row = rows[0]
    median = row.get("median_fills")
    single = row.get("single_fill_share")
    return {
        "median_fills_per_position": median,
        "p10_fills": row.get("p10_fills"),
        "p90_fills": row.get("p90_fills"),
        "single_fill_share": single,
        "fingerprint": _fragmentation_fingerprint(median, single),
        "n_positions_sourced": int(row.get("n_positions") or 0),
    }


def _fragmentation_fingerprint(median: Any, single: Any) -> str:
    if median is None:
        return "unknown"
    m = int(median)
    if m == 1:
        return "one_shot"
    if m <= 3:
        return "lightly_scaled"
    return "heavily_scaled"


def _build_evidence(
    view: Mapping[str, Any],
    grain_row: Mapping[str, Any],
    grain_key: Mapping[str, Any],
) -> dict[str, Any]:
    grain_id = str(grain_row.get("grain_id") or "")
    thesis_for_grain = [
        r for r in (view.get("thesis_candidates") or [])
        if str(r.get("grain_id") or "") == grain_id
    ]
    return {
        "thesis_supported": bool(thesis_for_grain),
        "n_thesis_candidates_for_grain": len(thesis_for_grain),
        "data_gaps_high_severity": [
            g.get("gap_name")
            for g in (view.get("data_gaps") or [])
            if str(g.get("severity") or "low") == "high"
        ],
    }


def _empty_rule(wallet: str, schema_version: str, *, reason: str) -> dict[str, Any]:
    return {
        "extractor_version": EXTRACTOR_VERSION,
        "wallet": wallet,
        "source_schema_version": schema_version,
        "source_grain": None,
        "market_filter": None,
        "sizing": None,
        "timing": None,
        "exit_rule": None,
        "fragmentation": None,
        "evidence": {
            "thesis_supported": False,
            "n_thesis_candidates_for_grain": 0,
            "data_gaps_high_severity": [],
        },
        "rejection_reason": reason,
    }


def _load_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not value:
        return {}
    parsed = json.loads(str(value))
    return dict(parsed) if isinstance(parsed, Mapping) else {}


__all__ = ["extract_wallet_rule", "EXTRACTOR_VERSION"]

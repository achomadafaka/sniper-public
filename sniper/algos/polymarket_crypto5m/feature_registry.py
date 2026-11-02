"""Feature registry + availability accounting (Phase 1B #8).

The registry catalogs every feature ``build_feature_snapshot`` emits, with its family, required sources,
minimum history, time scope, null-reason vocabulary, and a ``decision_safe`` flag. Feature names are
derived from a CANONICAL snapshot so the registry can never silently drift from the real features. No
post-resolution field may be ``known_at_decision``; the legacy categorical is ``diagnostic_only``.

``feature_availability`` reports per-feature and per-checkpoint coverage + null counts over captured
``crypto5m_feature_snapshots`` — so a feature family is approved for modeling on EVIDENCE of coverage, not
merely because the code exists.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from . import features as FE

_GEOMETRY = {"chainlink_price_now", "price_to_beat", "distance_to_beat", "absolute_distance_to_beat",
             "seconds_remaining", "fraction_of_window_elapsed", "normalized_distance_to_beat",
             "vol_per_s"}
_VOLUME_NAMES = {"binance_buy_volume_share", "binance_signed_volume", "binance_trade_count",
                 "binance_reversion_distance"}
_BASIS_INTEGRITY = {"binance_source_age_ms", "chainlink_source_age_ms", "basis_observation_skew_ms"}


def _classify(name):
    """Return ``(family, required_sources, time_scope, decision_safe)``. Order matters: most specific
    first."""
    if name == "legacy_spot_signal_diagnostic":
        return "C_diagnostic", ["binance_price"], "diagnostic_only", False
    if (name.startswith("basis_") or name.startswith("binance_chainlink_basis")
            or name in _BASIS_INTEGRITY):
        return "D_basis", ["binance_price", "chainlink_rtds"], "known_at_decision", True
    if (name.startswith("binance_volume") or name.startswith("binance_vwap")
            or name.startswith("binance_distance_to_vwap") or name in _VOLUME_NAMES):
        return "C_binance", ["binance_volume"], "known_at_decision", True
    if name.startswith("chainlink_realized_vol"):
        return "C_binance", ["chainlink_rtds"], "known_at_decision", True
    if name.startswith("binance_"):
        return "C_binance", ["binance_price"], "known_at_decision", True
    if "_fee_inclusive_cost_" in name:
        return "executable", ["polymarket_book", "fee"], "known_at_decision", True
    if ("_executable_price_" in name or "_slippage_vs_best_ask_" in name
            or "_fillability_" in name or "_levels_consumed_" in name):
        return "executable", ["polymarket_book"], "known_at_decision", True
    if name in _GEOMETRY:
        return "A_geometry", ["chainlink_rtds"], "known_at_decision", True
    if name.startswith("chainlink_"):
        return "B_chainlink_path", ["chainlink_rtds"], "known_at_decision", True
    if name.startswith("up_") or name.startswith("down_"):
        fam = "F_market" if "midpoint_probability" in name else "E_book"
        return fam, ["polymarket_book"], "known_at_decision", True
    return "other", [], "known_at_decision", True


def _min_history(name) -> str:
    m = re.search(r"_(\d+)s$", name)               # trailing "_15s" etc.; "_5_usdc" deliberately excluded
    return f"{m.group(1)}s" if m else "0"


def _null_vocab(name, family, sources) -> list:
    v = ["source_unavailable", "insufficient_history", "known_at_time_excluded"]
    if "binance_volume" in sources:
        v.append("no_volume_source")
    if family == "D_basis":
        v.append("stale_or_misaligned")
    if "fee" in sources:
        v.append("fee_unresolved")
    if family == "executable":
        v.append("insufficient_depth")
    return v


def describe_feature(name, *, feature_version=None) -> dict:
    family, sources, time_scope, decision_safe = _classify(name)
    return {
        "feature_name": name,
        "feature_family": family,
        "feature_version": feature_version or FE.FEATURE_VERSION,
        "required_sources": sources,
        "minimum_history": _min_history(name),
        "time_scope": time_scope,
        "null_reason_vocab": _null_vocab(name, family, sources),
        "decision_safe": decision_safe,
    }


def canonical_feature_names() -> list:
    """Enumerate the feature keys the snapshot emits (deterministic regardless of input values)."""
    snap = FE.build_feature_snapshot(
        condition_id="_", market_id="_", window_start_ts=0, window_end_ts=300, checkpoint=60,
        chainlink_ticks=[], up_book={"asks": [[0.5, 100]]}, down_book={"asks": [[0.5, 100]]},
        binance_obs=[], fee={"fee_status": "known", "fee_rate": 0.0})
    return sorted(k for k in snap["features"].keys() if k != "feature_version")


def build_registry(feature_version=None) -> list:
    return [describe_feature(n, feature_version=feature_version) for n in canonical_feature_names()]


def feature_availability(conn) -> dict:
    """Coverage of each registered feature over captured snapshots: fraction non-null overall and per
    checkpoint, plus raw null counts. Reads crypto5m_feature_snapshots."""
    rows = conn.execute("SELECT checkpoint, features_json FROM crypto5m_feature_snapshots").fetchall()
    total = len(rows)
    names = canonical_feature_names()
    non_null = Counter()
    by_cp = defaultdict(lambda: {"snapshots": 0, "non_null": Counter()})
    for r in rows:
        feats = json.loads(r["features_json"] or "{}")
        cp = r["checkpoint"]
        by_cp[cp]["snapshots"] += 1
        for k in names:
            if feats.get(k) is not None:
                non_null[k] += 1
                by_cp[cp]["non_null"][k] += 1
    coverage = {k: (non_null[k] / total if total else 0.0) for k in names}
    null_counts = {k: (total - non_null[k]) for k in names}
    cov_by_cp = {cp: {"snapshots": d["snapshots"],
                      "coverage": {k: (d["non_null"][k] / d["snapshots"] if d["snapshots"] else 0.0)
                                   for k in names}}
                 for cp, d in by_cp.items()}
    return {"snapshots": total, "coverage_by_feature": coverage, "null_counts": null_counts,
            "coverage_by_checkpoint": cov_by_cp}

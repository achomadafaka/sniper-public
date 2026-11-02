"""Wallet dossier builder for deterministic v0 tables.

Consumes a WalletData fetch fixture + ProtocolReport JSON. Produces the
complete v0 dossier tables as in-memory dicts. Either sink (SQLite or
JSON export) consumes the same dict structure.

Spec: docs/superpowers/specs/2026-05-25-wallet-dossier-semantics-v0.md
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sniper.analysis.wallet_dossier.derivers import (
    DIAGNOSTIC_ACCOUNTING_CLASSIFIER_VERSION,
    RESOLVE_BY_CURPRICE_ENABLED,
    ExitEpisode,
    classify_accounting_bucket,
    derive_category,
    derive_dominant_counterfactual_class,
    derive_entry_exit_ts,
    derive_entry_price_bucket,
    derive_exit_mode,
    derive_market_type,
    derive_favorite_status_and_rank,
    derive_outcome_role,
    derive_selected_token_resolution,
    extract_episodes_for_position,
)
from sniper.analysis.wallet_dossier.grain import (
    GrainAggregate,
    MechanismEvidenceRow,
    build_grain_key,
    build_mechanism_evidence,
    evaluate_honesty_gates,
    grain_id,
    grain_key_json,
)
from sniper.analysis.wallet_dossier.lineage import LineageRow, LineageTracker
from sniper.analysis.wallet_dossier.schema import (
    DOSSIER_SCHEMA_VERSION,
    SCHEMA_DDL,
    all_dossier_tables,
)

# Tables explicitly exempt from §6.10 lineage. Only `dossier_meta` qualifies
# — it is build-process metadata (schema version, build inputs), not a
# semantic claim about the wallet. Every other table MUST lineage every
# non-null, non-key cell. Tests enforce this.
SPEC_EXEMPT_TABLES_FROM_LINEAGE: frozenset[str] = frozenset({"dossier_meta"})

# ---------------------------------------------------------------------------
# Data containers (intermediate dicts; serialized via sinks)
# ---------------------------------------------------------------------------


@dataclass
class DossierTables:
    """In-memory representation of all dossier tables (v5_behavioral_descriptors)."""

    positions: list[dict[str, Any]]
    position_exit_episodes: list[dict[str, Any]]
    market_semantics: list[dict[str, Any]]
    diagnostic_accounting: list[dict[str, Any]]
    result_semantics: list[dict[str, Any]]
    mechanism_evidence: list[dict[str, Any]]
    hypothesis_results: list[dict[str, Any]]
    surfaces: list[dict[str, Any]]
    crosstabs: list[dict[str, Any]]
    thesis_candidates: list[dict[str, Any]]
    data_lineage: list[dict[str, Any]]
    data_gaps: list[dict[str, Any]]
    dossier_meta: list[dict[str, Any]]
    # v5 behavioral descriptor tables
    descriptor_hour_distribution: list[dict[str, Any]]
    descriptor_cadence: list[dict[str, Any]]
    descriptor_sizing: list[dict[str, Any]]
    descriptor_time_in_position: list[dict[str, Any]]
    descriptor_fills_per_position: list[dict[str, Any]]


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _position_id(condition_id: str, asset_id: str) -> str:
    raw = f"{condition_id.lower()}|{asset_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _episode_id(position_id: str, episode_index: int) -> str:
    raw = f"{position_id}|{episode_index}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _clamp_price(p: Any) -> float | None:
    if p is None:
        return None
    try:
        v = float(p)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, v))


def _safe_float(x: Any) -> float | None:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _safe_int(x: Any) -> int | None:
    if x is None:
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _exit_attribution_quality_flags(
    closed_position: dict[str, Any],
    *,
    activity_min_ts: int | None,
) -> list[str]:
    """Per-position flags derivable from Exit Attribution coverage.

    Exit Attribution v1 exposes aggregate coverage, not per-position caveat
    rows. `activity_min_ts` is enough to reproduce the outside-window caveat
    for each closed position without guessing from summaries.
    """
    if activity_min_ts is None:
        return []
    pos_ts = _safe_int(closed_position.get("timestamp"))
    if pos_ts is not None and pos_ts > 0 and pos_ts < activity_min_ts:
        return ["EXIT_ATTRIBUTION_POSITION_OUTSIDE_ACTIVITY_WINDOW"]
    return []


def filter_fixture_by_since_days(
    fixture: dict[str, Any],
    since_days: int,
) -> dict[str, Any]:
    """Return a copy of `fixture` restricted to the last `since_days` of activity.

    Window endpoint is `fixture['fetched_at']`. A closed_position is kept iff
    its `timestamp` (entry) >= cutoff. activity_rows and redemptions are kept
    iff their `timestamp` >= cutoff. Open positions are preserved (they are
    the current snapshot regardless of when they were opened). All other
    fixture keys (`leafs_by_cid`, `nav_lookup`, etc.) pass through unchanged.

    Adds `_since_days_filter` metadata so consumers can record the window.
    """
    if since_days <= 0:
        raise ValueError(f"since_days must be positive, got {since_days}")
    fetched_at = _safe_int(fixture.get("fetched_at"))
    if fetched_at is None or fetched_at <= 0:
        raise ValueError("fixture.fetched_at must be a positive unix epoch for --since-days")
    cutoff_ts = fetched_at - since_days * 86400

    def _ts_in_window(ts: Any) -> bool:
        v = _safe_int(ts)
        return v is not None and v >= cutoff_ts

    filtered = dict(fixture)
    filtered["activity_rows"] = [
        r for r in (fixture.get("activity_rows") or []) if _ts_in_window(r.get("timestamp"))
    ]
    filtered["closed_positions"] = [
        p for p in (fixture.get("closed_positions") or []) if _ts_in_window(p.get("timestamp"))
    ]
    if fixture.get("redemptions") is not None:
        filtered["redemptions"] = [
            r for r in (fixture.get("redemptions") or []) if _ts_in_window(r.get("timestamp"))
        ]
    filtered["_since_days_filter"] = {
        "since_days": since_days,
        "cutoff_ts": cutoff_ts,
        "fetched_at": fetched_at,
    }
    return filtered


def build_tables(
    *,
    wallet: str,
    wallet_data: dict[str, Any],
    protocol_report: dict[str, Any],
) -> DossierTables:
    """Build all v0 dossier tables from inputs. Pure function.

    Args:
        wallet: wallet address (lowercased on insert).
        wallet_data: loaded WalletData fixture JSON (dict, NOT WalletData dataclass).
        protocol_report: loaded ProtocolReport JSON (dict).

    Returns:
        DossierTables with complete v0 table rows populated from the fixture
        and ProtocolReport surfaces.
    """
    tracker = LineageTracker()
    wallet_lc = wallet.lower()

    closed_positions: list[dict[str, Any]] = list(wallet_data.get("closed_positions") or [])
    activity_rows: list[dict[str, Any]] = list(wallet_data.get("activity_rows") or [])
    open_positions: list[dict[str, Any]] = list(wallet_data.get("open_positions") or [])
    redemptions: list[dict[str, Any]] | None = wallet_data.get("redemptions")
    leafs_by_cid = wallet_data.get("leafs_by_cid") or {}
    nav_lookup = wallet_data.get("nav_lookup") or {}

    # ---- position_pattern + data_quality_flags lookup from ProtocolReport ----
    # spec §6.1: position_pattern from
    # `position_building_surface.positions[i].accumulation_pattern`
    # (the producer field name; see wallet_protocol/position_building.py:600).
    # data_quality_flags aggregates the same surface's per-position `caveats`
    # list. Key is (condition_id, asset_id) since that's our position_id basis.
    position_pattern_by_key: dict[tuple[str, str], str] = {}
    position_caveats_by_key: dict[tuple[str, str], list[str]] = {}
    pbs = (protocol_report.get("position_building_surface") or {})
    for pbe in pbs.get("positions") or []:
        pk = pbe.get("position_key") or {}
        cid = (pk.get("condition_id") or "").lower()
        aid = str(pk.get("asset") or "")
        pat = pbe.get("accumulation_pattern")
        if cid and aid and pat:
            position_pattern_by_key[(cid, aid)] = pat
        caveats = pbe.get("caveats")
        if cid and aid and isinstance(caveats, list) and caveats:
            # Producer emits already-sorted; keep that ordering for byte
            # determinism on the dossier side.
            position_caveats_by_key[(cid, aid)] = [str(c) for c in caveats]
    exit_attr_coverage = (
        (protocol_report.get("exit_attribution") or {}).get("coverage") or {}
    )
    exit_attr_activity_min_ts = _safe_int(exit_attr_coverage.get("activity_min_ts"))

    # Redemption timestamp lookup per (condition_id, indexSet) for episode
    # construction.
    redemption_ts_by_key: dict[tuple[str, str], int] = {}
    for r in (redemptions or []):
        cid = (r.get("condition") or "").lower()
        ts = int(r.get("timestamp") or 0)
        for s in (r.get("indexSets") or []):
            key = (cid, str(s))
            if key not in redemption_ts_by_key or ts > redemption_ts_by_key[key]:
                redemption_ts_by_key[key] = ts

    # Precompute: which conditions does the wallet hold multiple distinct
    # asset_ids for? Used by §7.5 to disambiguate paired redemptions.
    _assets_by_cid: dict[str, set[str]] = {}
    for _cp in closed_positions:
        _cid = (_cp.get("conditionId") or "").lower()
        _asset = str(_cp.get("asset") or "")
        if _cid and _asset:
            _assets_by_cid.setdefault(_cid, set()).add(_asset)

    positions_rows: list[dict[str, Any]] = []
    market_semantics_rows: list[dict[str, Any]] = []
    diagnostic_accounting_rows: list[dict[str, Any]] = []
    episodes_rows: list[dict[str, Any]] = []

    # Accumulate per-grain stats during the position loop; finalize after.
    grains: dict[str, GrainAggregate] = {}

    for cp in closed_positions:
        condition_id = (cp.get("conditionId") or "").lower()
        asset_id = str(cp.get("asset") or "")
        if not condition_id or not asset_id:
            # Spec hard rule: position_id requires both. Skip degenerate rows.
            continue
        pid = _position_id(condition_id, asset_id)

        # ---- positions row ----
        outcome_label = cp.get("outcome")
        title = cp.get("title")
        entry_price = _clamp_price(cp.get("avgPrice"))
        # v0 closed-position sizing: Polymarket /closed-positions returns
        # size=0 (no current holdings) and totalBought as the gross share
        # count over the position's lifetime. Verified empirically:
        # (curPrice - avgPrice) * totalBought ≈ realizedPnl across fixtures.
        # Spec §6.1 records the v0 source as totalBought.
        size = _safe_float(cp.get("totalBought"))
        cost_basis = entry_price * size if (entry_price is not None and size is not None) else None
        realized_pnl = _safe_float(cp.get("realizedPnl"))
        unrealized_pnl = _safe_float(cp.get("cashPnl")) if cp.get("cashPnl") is not None else None
        # cashPnl is a Polymarket-side field that approximates unrealized PnL on
        # open portions; closed positions usually have it 0/None.

        # market_type (also feeds market_semantics)
        market_type, mt_source, n_outcomes = derive_market_type(
            condition_id,
            closed_positions=closed_positions,
            activity_rows=activity_rows,
        )

        # entry_price_bucket
        entry_bucket: str | None
        if entry_price is not None:
            entry_bucket = derive_entry_price_bucket(entry_price)
        else:
            entry_bucket = None

        # outcome_role
        outcome_role = derive_outcome_role(
            market_type=market_type, outcome_label=outcome_label
        )

        # v3: favorite_status + outcome_role_rank from entry_price (binary only)
        favorite_status, outcome_role_rank, favrank_source = derive_favorite_status_and_rank(
            market_type=market_type, entry_price=entry_price,
        )

        # category (v1: nav-category resolution via §7.4)
        category, category_source, category_unavailable_reason, leaf_tags_sorted = derive_category(
            condition_id=condition_id,
            leafs_by_cid=leafs_by_cid,
            nav_lookup=nav_lookup,
        )
        leaf_tags_json = json.dumps(leaf_tags_sorted) if leaf_tags_sorted is not None else None

        # ---- §7.5 resolution + exit_mode + entry/exit timestamps ----
        oi = _safe_int(cp.get("outcomeIndex"))
        our_index_set = str(1 << oi) if (oi is not None and oi >= 0) else None
        redemption_ts = (
            redemption_ts_by_key.get((condition_id, our_index_set))
            if our_index_set else None
        )
        holds_other_outcome = len(_assets_by_cid.get(condition_id, set())) > 1
        resolution = derive_selected_token_resolution(
            closed_position=cp,
            redemptions=redemptions,
            open_positions=open_positions,
            binary_market_n_outcomes=n_outcomes,
            wallet_holds_other_outcome_for_condition=holds_other_outcome,
            enable_curprice_rule=RESOLVE_BY_CURPRICE_ENABLED,
        )
        exit_mode = derive_exit_mode(
            resolution=resolution,
            activity_rows=activity_rows,
            asset_id=asset_id,
        )
        entry_ts, exit_ts = derive_entry_exit_ts(
            activity_rows=activity_rows,
            asset_id=asset_id,
            condition_id=condition_id,
            redemption_timestamp=redemption_ts,
        )

        # ---- §7.6 episodes ----
        episodes = extract_episodes_for_position(
            asset_id=asset_id,
            avg_entry_price=entry_price,
            activity_rows=activity_rows,
            selected_token_resolution=resolution.value,
            resolution_source=resolution.source,
            redemption_timestamp=redemption_ts,
        )

        # Position-level rollup (§6.3)
        dom_class, _share_by_class = derive_dominant_counterfactual_class(episodes)
        n_episodes_for_position = len(episodes)

        # position_pattern from ProtocolReport (or "unknown" if missing)
        position_pattern = position_pattern_by_key.get(
            (condition_id, asset_id), "unknown"
        )
        # data_quality_flags: per-position caveats from position_building plus
        # Exit Attribution coverage-derived flags. JSON-encoded; NULL when no
        # caveats fired (clean position).
        pos_caveats = list(position_caveats_by_key.get((condition_id, asset_id)) or [])
        pos_caveats.extend(
            _exit_attribution_quality_flags(
                cp,
                activity_min_ts=exit_attr_activity_min_ts,
            )
        )
        data_quality_flags_json = (
            json.dumps(pos_caveats) if pos_caveats else None
        )

        position_row = {
            "position_id": pid,
            "wallet": wallet_lc,
            "condition_id": condition_id,
            "asset_id": asset_id,
            "outcome_index": _safe_int(cp.get("outcomeIndex")),
            "outcome_label": outcome_label,
            "title": title,
            "entry_price": entry_price,
            "size": size,
            "cost_basis_usdc": cost_basis,
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "entry_ts": entry_ts,
            "exit_ts": exit_ts,
            "exit_mode": exit_mode,
            "n_exit_episodes": n_episodes_for_position,
            "dominant_counterfactual_class": dom_class,
            "market_type": market_type,
            "entry_price_bucket": entry_bucket,
            "outcome_role": outcome_role,
            "outcome_role_rank": outcome_role_rank,
            "outcome_role_rank_source": favrank_source,
            "position_pattern": position_pattern,
            "selected_token_resolution": resolution.value,
            "resolution_source": resolution.source,
            "resolution_confidence": resolution.confidence,
            "data_quality_flags": data_quality_flags_json,
        }
        positions_rows.append(position_row)

        # ---- lineage for positions ----
        _record_position_lineage(
            tracker, pid=pid, position_row=position_row,
            entry_bucket_set=(entry_bucket is not None),
            resolution_detail=resolution.derivation_detail,
        )

        # ---- emit episode rows + lineage ----
        for ep in episodes:
            ep_id = _episode_id(pid, ep.episode_index)
            episode_row = {
                "episode_id": ep_id,
                "position_id": pid,
                "episode_index": ep.episode_index,
                "episode_type": ep.episode_type,
                "timestamp": ep.timestamp,
                "size_exited": ep.size_exited,
                "price_exit": ep.price_exit,
                "price_entry_for_episode": ep.price_entry_for_episode,
                "episode_pnl": ep.episode_pnl,
                "trade_result": ep.trade_result,
                "selected_token_resolution_at_episode_close": ep.selected_token_resolution_at_episode_close,
                "counterfactual_class": ep.counterfactual_class,
                "counterfactual_source": ep.counterfactual_source,
                "counterfactual_confidence": ep.counterfactual_confidence,
            }
            episodes_rows.append(episode_row)
            _record_episode_lineage(tracker, episode_row, ep_id)

        # ---- grain aggregation ----
        grain_key = build_grain_key(
            category_source=category_source,
            category=category,
            market_type=market_type,
            outcome_role=outcome_role,
            entry_price_bucket=(entry_bucket or "data_unavailable"),
            exit_mode=(exit_mode or "unknown"),
            position_pattern=position_pattern,
        )
        gid = grain_id(grain_key)
        if gid not in grains:
            grains[gid] = GrainAggregate(grain_key=grain_key, grain_id=gid)
        g = grains[gid]
        g.add_position(position_id=pid, position_pnl=float(realized_pnl or 0.0))
        for ep in episodes:
            # Codex round 2 audit: size_exited is raw share count; the grain
            # rolls it into total_size_usdc. Convert to USDC notional here
            # (size × price_exit) so the column lives up to its name.
            ep_notional = float(ep.size_exited or 0.0) * float(ep.price_exit or 0.0)
            g.add_episode(klass=ep.counterfactual_class, size=ep_notional)

        # ---- market_semantics row ----
        ms_row = {
            "position_id": pid,
            "market_type": market_type,
            "market_type_source": mt_source,
            "n_outcomes_in_market": n_outcomes,
            "category": category,
            "category_source": category_source,
            "category_unavailable_reason": category_unavailable_reason,
            # leaf_tags_json: post-skip, post-ambiguous, alphabetically sorted (spec §2.2)
            "leaf_tags_json": leaf_tags_json,
            "event_family": "data_unavailable",
            "event_family_source": "data_unavailable",
            "outcome_role": outcome_role,
            "favorite_status": favorite_status,
            "favorite_status_source": favrank_source,
            # When entry_price is missing, use the literal "data_unavailable"
            # sentinel (matches the category pattern in §7.4). This makes the
            # grain fail §8 gate 7 (non-category broad-bucket leakage) once
            # honesty gates ship — exactly what we want when the underlying
            # price is unknown.
            "entry_price_bucket": entry_bucket if entry_bucket is not None else "data_unavailable",
        }
        market_semantics_rows.append(ms_row)
        _record_market_semantics_lineage(
            tracker, pid=pid, ms_row=ms_row,
            category_reason=category_unavailable_reason,
            entry_price_was_observed=(entry_bucket is not None),
        )

        # ---- diagnostic_accounting row ----
        bucket = classify_accounting_bucket(outcome_label)
        da_row = {
            "position_id": pid,
            "accounting_bucket": bucket,
            "outcome_label_raw": outcome_label,
            "classifier_version": DIAGNOSTIC_ACCOUNTING_CLASSIFIER_VERSION,
        }
        diagnostic_accounting_rows.append(da_row)
        tracker.record(
            table="diagnostic_accounting", column="accounting_bucket",
            row_key=pid, source="derived", confidence="high",
            source_detail=f"classify_accounting_bucket({outcome_label!r})",
            derivation_rule="§6.4b string-match classifier",
        )
        if outcome_label is not None:
            tracker.record(
                table="diagnostic_accounting", column="outcome_label_raw",
                row_key=pid, source="fixture_field", confidence="high",
                source_detail="WalletData.closed_positions[i].outcome",
            )
        tracker.record(
            table="diagnostic_accounting", column="classifier_version",
            row_key=pid, source="derived", confidence="high",
            source_detail=f"constant {DIAGNOSTIC_ACCOUNTING_CLASSIFIER_VERSION!r}",
        )

    # ---- result_semantics + mechanism_evidence (§7.7, §7.8, §8) ----
    result_rows: list[dict[str, Any]] = []
    mechanism_rows: list[dict[str, Any]] = []
    fetched_at = _safe_int(wallet_data.get("fetched_at"))
    for gid, g in grains.items():
        gate_result = evaluate_honesty_gates(g)
        result_row = {
            "grain_id": gid,
            "grain_key": grain_key_json(g.grain_key),
            "n_positions": g.n_positions,
            "n_episodes": g.n_episodes,
            "episodes_by_class": json.dumps(g.episodes_by_class, sort_keys=True),
            "outcome_win_rate_held": g.outcome_win_rate_held,
            "trade_win_rate_preres": g.trade_win_rate_preres,
            "sold_winners_early_share": g.sold_winners_early_share,
            "sold_winner_at_loss_share": g.sold_winner_at_loss_share,
            "loss_cut_correctness_rate": g.loss_cut_correctness_rate,
            "pnl_total": g.pnl_total,
            "pnl_concentration_top5_share": g.pnl_concentration_top5_share,
            "total_size_usdc": g.total_size,
            "attribution_coverage": g.attribution_coverage,
            "grain_quality": gate_result.quality,
            "grain_quality_reasons": json.dumps(gate_result.reasons),
        }
        result_rows.append(result_row)
        _record_result_semantics_lineage(tracker, result_row, gid)

        for me in build_mechanism_evidence(g=g, quality=gate_result.quality):
            me_row = {
                "grain_id": me.grain_id,
                "mechanism": me.mechanism,
                "evidence_strength": me.evidence_strength,
                "supporting_metrics": json.dumps(me.supporting_metrics, sort_keys=True, default=str),
                "sample_size": me.sample_size,
                "caveats": json.dumps(me.caveats),
                "missing_data": json.dumps(me.missing_data),
                "candidate_thesis_evidence": me.candidate_thesis_evidence,
                "not_promoted_reason": me.not_promoted_reason,
                "derived_from_fixture_fetched_at": fetched_at,
            }
            mechanism_rows.append(me_row)
            _record_mechanism_evidence_lineage(tracker, me_row, me)

    # ---- §6.7 hypothesis_results (pure mirror) ----
    hypothesis_results_rows = _build_hypothesis_results(protocol_report)
    _record_hypothesis_results_lineage(tracker, hypothesis_results_rows)

    # ---- §6.8 surfaces (pure mirror) ----
    surfaces_rows = _build_surfaces(protocol_report)
    _record_surfaces_lineage(tracker, surfaces_rows)

    # ---- §6.9 crosstabs ----
    crosstabs_rows = _build_crosstabs(
        positions_rows=positions_rows,
        episodes_rows=episodes_rows,
        diagnostic_accounting_rows=diagnostic_accounting_rows,
    )
    _record_crosstabs_lineage(tracker, crosstabs_rows)

    # ---- data_gaps (v0 core gaps; v5 descriptor gaps appended below) ----
    data_gaps_rows = _build_data_gaps(
        positions_rows,
        market_semantics_rows,
        mechanism_rows,
    )

    # ---- v4 thesis_candidates ----
    # Promote only `mechanism_evidence.candidate_thesis_evidence != NULL`
    # rows into the durable lifecycle table. Status='candidate' in v4.
    thesis_candidate_rows = _build_thesis_candidates(mechanism_rows)
    _record_thesis_candidates_lineage(tracker, thesis_candidate_rows)

    # ---- v5 behavioral descriptors ----
    # Build lookup structures shared across all 5 descriptor derivers.
    # market_semantics_by_pid: position_id -> market_semantics row
    market_semantics_by_pid: dict[str, dict[str, Any]] = {
        r["position_id"]: r for r in market_semantics_rows
    }
    # position_id_by_asset: asset_id -> position_id (from closed_positions loop)
    position_id_by_asset: dict[str, str] = {}
    for pos in positions_rows:
        asset_id = str(pos.get("asset_id") or "")
        if asset_id:
            position_id_by_asset[asset_id] = pos["position_id"]

    desc_hour_rows, desc_hour_gaps = _build_descriptor_hour_distribution(
        activity_rows, market_semantics_by_pid, position_id_by_asset,
    )
    desc_cadence_rows, desc_cadence_gaps = _build_descriptor_cadence(
        activity_rows, market_semantics_by_pid, position_id_by_asset,
    )
    desc_sizing_rows, desc_sizing_gaps = _build_descriptor_sizing(
        activity_rows, market_semantics_by_pid, position_id_by_asset,
    )
    desc_tip_rows, desc_tip_gaps = _build_descriptor_time_in_position(
        closed_positions, activity_rows, open_positions, fetched_at, positions_rows,
    )
    desc_fpp_rows, desc_fpp_gaps = _build_descriptor_fills_per_position(
        activity_rows, market_semantics_by_pid, position_id_by_asset, positions_rows,
    )

    # Append v5 descriptor sparse gaps to data_gaps, then record lineage for all.
    for gap_name in (
        desc_hour_gaps + desc_cadence_gaps + desc_sizing_gaps
        + desc_tip_gaps + desc_fpp_gaps
    ):
        data_gaps_rows.append({
            "gap_name": gap_name,
            "affected_columns": json.dumps([gap_name]),
            "affected_grain_count": None,
            "unblocking_spec": None,
            "severity": "low",
        })
    # Record lineage for all gaps (v0 + v5) in one pass after full list is built.
    _record_data_gaps_lineage(tracker, data_gaps_rows)

    # Lineage for v5 descriptor tables
    _record_descriptor_lineage(
        tracker, "descriptor_hour_distribution", desc_hour_rows,
        ("n_trades", "total_size_usdc"),
        "hour_utc", "v5 §1 descriptor_hour_distribution",
    )
    # For multi-column PK tables, build a composite row_key string
    for row in desc_cadence_rows:
        rk = f"{row['side']}|{row['market_category']}"
        for col in ("n_intervals", "median_seconds_between_trades",
                    "p10_seconds_between_trades", "p90_seconds_between_trades",
                    "burst_share"):
            tracker.record(
                table="descriptor_cadence", column=col, row_key=rk,
                source="derived", confidence="high",
                source_detail="derived from activity_rows inter-trade timestamps",
                derivation_rule="v5 §2 descriptor_cadence",
            )
    for row in desc_sizing_rows:
        rk = f"{row['side']}|{row['market_category']}"
        for col in ("n_fills", "median_size_usdc", "p10_size_usdc",
                    "p90_size_usdc", "max_size_usdc", "coeff_of_variation"):
            tracker.record(
                table="descriptor_sizing", column=col, row_key=rk,
                source="derived", confidence="high",
                source_detail="derived from activity_rows fill sizes",
                derivation_rule="v5 §3 descriptor_sizing",
            )
    _record_descriptor_lineage(
        tracker, "descriptor_time_in_position", desc_tip_rows,
        ("n_positions", "median_seconds_in_position",
         "p10_seconds_in_position", "p90_seconds_in_position"),
        "exit_mode", "v5 §4 descriptor_time_in_position",
    )
    _record_descriptor_lineage(
        tracker, "descriptor_fills_per_position", desc_fpp_rows,
        ("n_positions", "median_fills", "p10_fills", "p90_fills", "single_fill_share"),
        "market_category", "v5 §5 descriptor_fills_per_position",
    )

    # ---- dossier_meta ----
    # Metadata table — explicitly exempt from §6.10 lineage by design.
    # See SPEC_EXEMPT_TABLES_FROM_LINEAGE.
    meta_rows = [
        {"key": "schema_version", "value": DOSSIER_SCHEMA_VERSION},
        {"key": "wallet", "value": wallet_lc},
        {"key": "fixture_fetched_at", "value": str(fetched_at) if fetched_at else "unknown"},
        {"key": "protocol_report_version", "value": str(protocol_report.get("protocol_version") or "unknown")},
        {"key": "n_positions", "value": str(len(positions_rows))},
        {"key": "n_episodes", "value": str(len(episodes_rows))},
        {"key": "n_grains", "value": str(len(result_rows))},
        {"key": "n_hypotheses", "value": str(len(hypothesis_results_rows))},
        {"key": "n_surfaces", "value": str(len(surfaces_rows))},
        {"key": "n_crosstab_rows", "value": str(len(crosstabs_rows))},
        {"key": "n_thesis_candidates", "value": str(len(thesis_candidate_rows))},
        # v5 descriptor counts
        {"key": "n_descriptor_hour_rows", "value": str(len(desc_hour_rows))},
        {"key": "n_descriptor_cadence_rows", "value": str(len(desc_cadence_rows))},
        {"key": "n_descriptor_sizing_rows", "value": str(len(desc_sizing_rows))},
        {"key": "n_descriptor_time_in_position_rows", "value": str(len(desc_tip_rows))},
        {"key": "n_descriptor_fills_per_position_rows", "value": str(len(desc_fpp_rows))},
    ]
    since_filter = wallet_data.get("_since_days_filter")
    if isinstance(since_filter, dict):
        meta_rows.append({
            "key": "since_days_filter",
            "value": (
                f"since_days={since_filter.get('since_days')}"
                f";cutoff_ts={since_filter.get('cutoff_ts')}"
                f";fetched_at={since_filter.get('fetched_at')}"
            ),
        })

    return DossierTables(
        positions=positions_rows,
        position_exit_episodes=episodes_rows,
        market_semantics=market_semantics_rows,
        diagnostic_accounting=diagnostic_accounting_rows,
        result_semantics=result_rows,
        mechanism_evidence=mechanism_rows,
        hypothesis_results=hypothesis_results_rows,
        surfaces=surfaces_rows,
        crosstabs=crosstabs_rows,
        thesis_candidates=thesis_candidate_rows,
        data_lineage=[_lineage_row_to_dict(r) for r in tracker.rows()],
        data_gaps=data_gaps_rows,
        dossier_meta=meta_rows,
        descriptor_hour_distribution=desc_hour_rows,
        descriptor_cadence=desc_cadence_rows,
        descriptor_sizing=desc_sizing_rows,
        descriptor_time_in_position=desc_tip_rows,
        descriptor_fills_per_position=desc_fpp_rows,
    )


def _record_position_lineage(
    tracker: LineageTracker,
    *,
    pid: str,
    position_row: dict[str, Any],
    entry_bucket_set: bool,
    resolution_detail: str,
) -> None:
    """Emit lineage rows for every non-null, non-key cell in positions."""
    fixture_cols = {
        "wallet": ("input parameter", "high"),
        "condition_id": ("WalletData.closed_positions[i].conditionId", "high"),
        "asset_id": ("WalletData.closed_positions[i].asset", "high"),
        "outcome_label": ("WalletData.closed_positions[i].outcome", "high"),
        "title": ("WalletData.closed_positions[i].title", "high"),
        "entry_price": ("WalletData.closed_positions[i].avgPrice", "high"),
        "size": ("WalletData.closed_positions[i].totalBought", "high"),
        "realized_pnl": ("WalletData.closed_positions[i].realizedPnl", "high"),
        "unrealized_pnl": ("WalletData.closed_positions[i].cashPnl", "medium"),
        "outcome_index": ("WalletData.closed_positions[i].outcomeIndex", "high"),
    }
    for col, (source_detail, confidence) in fixture_cols.items():
        if position_row.get(col) is not None:
            tracker.record(
                table="positions", column=col, row_key=pid,
                source="fixture_field", confidence=confidence,
                source_detail=source_detail,
            )

    if position_row["cost_basis_usdc"] is not None:
        tracker.record(
            table="positions", column="cost_basis_usdc", row_key=pid,
            source="derived", confidence="high",
            source_detail="avgPrice * totalBought",
            derivation_rule="§6.1 v0 closed-position sizing",
        )
    if position_row["data_quality_flags"] is not None:
        tracker.record(
            table="positions", column="data_quality_flags", row_key=pid,
            source="protocol_report_surface", confidence="high",
            source_detail=(
                "ProtocolReport.position_building_surface.positions[i].caveats; "
                "ProtocolReport.exit_attribution.coverage"
            ),
            derivation_rule="§6.1 (per-position quality flag mirror)",
        )
    tracker.record(
        table="positions", column="market_type", row_key=pid,
        source="derived", confidence="high",
        source_detail="distinct asset_id count for conditionId",
        derivation_rule="§7.1",
    )
    if entry_bucket_set:
        tracker.record(
            table="positions", column="entry_price_bucket", row_key=pid,
            source="derived", confidence="high",
            source_detail="bin(entry_price)",
            derivation_rule="§7.2",
        )
    tracker.record(
        table="positions", column="outcome_role", row_key=pid,
        source="derived", confidence="high",
        source_detail="market_type + outcome_label string match",
        derivation_rule="§7.3",
    )
    # v3: outcome_role_rank lineage switches on actual derivation state.
    _rank_source = position_row["outcome_role_rank_source"]
    if _rank_source == "binary_entry_price_complement":
        if position_row["outcome_role_rank"] is not None:
            tracker.record(
                table="positions", column="outcome_role_rank", row_key=pid,
                source="derived", confidence="high",
                source_detail="binary_entry_price_complement (entry_price-derived favorite rank)",
                derivation_rule="§7.3 v3",
            )
        tracker.record(
            table="positions", column="outcome_role_rank_source", row_key=pid,
            source="derived", confidence="high",
            source_detail="binary_entry_price_complement",
            derivation_rule="§7.3 v3",
        )
    else:
        # v3 reasons: entry_price_missing OR sibling_token_prices_at_entry_not_fetched.
        tracker.record(
            table="positions", column="outcome_role_rank_source", row_key=pid,
            source="data_unavailable", confidence="high",
            unavailable_reason=_rank_source,
            derivation_rule="§7.3 v3 (binary-only; multi-outcome deferred)",
        )

    # §7.5 — resolution columns. Always non-null (derive returns a value).
    tracker.record(
        table="positions", column="selected_token_resolution", row_key=pid,
        source="derived", confidence=position_row["resolution_confidence"],  # type: ignore[arg-type]
        source_detail=resolution_detail,
        derivation_rule="§7.5",
    )
    tracker.record(
        table="positions", column="resolution_source", row_key=pid,
        source="derived", confidence="high",
        source_detail=f"resolution source label: {position_row['resolution_source']!r}",
        derivation_rule="§7.5",
    )
    tracker.record(
        table="positions", column="resolution_confidence", row_key=pid,
        source="derived", confidence="high",
        source_detail=f"confidence band for §7.5 rule that fired",
        derivation_rule="§7.5",
    )
    # exit_mode follows from resolution
    if position_row.get("exit_mode") is not None:
        tracker.record(
            table="positions", column="exit_mode", row_key=pid,
            source="derived", confidence="medium",
            source_detail="rule: held if redemption matched, pre_resolution if SELL fills, else still_open/unresolved/unknown",
            derivation_rule="§7.5 (post-resolution classification)",
        )
    # position_pattern — derived from ProtocolReport.position_building_surface
    if position_row.get("position_pattern") is not None:
        tracker.record(
            table="positions", column="position_pattern", row_key=pid,
            source="protocol_report_surface", confidence="high",
            source_detail="ProtocolReport.position_building_surface.positions[].pattern",
            derivation_rule="§6.1 position_pattern column",
        )
    # n_exit_episodes + dominant_counterfactual_class (per-position rollups)
    if position_row.get("n_exit_episodes") is not None:
        tracker.record(
            table="positions", column="n_exit_episodes", row_key=pid,
            source="derived", confidence="high",
            source_detail="count of position_exit_episodes rows for this position_id",
            derivation_rule="§6.1 n_exit_episodes column",
        )
    if position_row.get("dominant_counterfactual_class") is not None:
        tracker.record(
            table="positions", column="dominant_counterfactual_class", row_key=pid,
            source="derived", confidence="medium",
            source_detail="dominant class by size; 'mixed_episodes' when no class >50%",
            derivation_rule="§6.3 dominant counterfactual rollup",
        )
    # entry_ts from activity rows; exit_ts from the latest SELL fill or redemption.
    if position_row.get("entry_ts") is not None:
        tracker.record(
            table="positions", column="entry_ts", row_key=pid,
            source="derived", confidence="high",
            source_detail="min(timestamp) over BUY fills for this asset",
            derivation_rule="§6.1 entry_ts column",
        )
    if position_row.get("exit_ts") is not None:
        tracker.record(
            table="positions", column="exit_ts", row_key=pid,
            source="derived", confidence="high",
            source_detail="max(timestamp) over SELL fills or redemption event for this asset",
            derivation_rule="§6.1 exit_ts column",
        )


def _record_market_semantics_lineage(
    tracker: LineageTracker,
    *,
    pid: str,
    ms_row: dict[str, Any],
    category_reason: str | None,
    entry_price_was_observed: bool,
) -> None:
    tracker.record(
        table="market_semantics", column="market_type", row_key=pid,
        source="derived", confidence="high",
        source_detail="distinct asset_id count for conditionId; closed_positions.oppositeAsset repairs one-sided binary rows",
        derivation_rule="§7.1 v2 market-structure repair",
    )
    tracker.record(
        table="market_semantics", column="market_type_source", row_key=pid,
        source="derived", confidence="high",
        source_detail="constant",
        derivation_rule="§7.1 v2 market-structure repair",
    )
    if ms_row["n_outcomes_in_market"] is not None:
        tracker.record(
            table="market_semantics", column="n_outcomes_in_market", row_key=pid,
            source="derived", confidence="high",
            source_detail="len(set(asset_ids plus closed_positions.oppositeAsset when present))",
            derivation_rule="§7.1 v2 market-structure repair",
        )
    if ms_row["category"] != "data_unavailable":
        # Resolved nav category
        tracker.record(
            table="market_semantics", column="category", row_key=pid,
            source="derived", confidence="high",
            source_detail="WalletData.leafs_by_cid[<cid>] + WalletData.nav_lookup",
            derivation_rule="§7.4 v1 nav-category resolution",
        )
        tracker.record(
            table="market_semantics", column="category_source", row_key=pid,
            source="derived", confidence="high",
            source_detail="tag_nav_category resolution",
            derivation_rule="§7.4 v1",
        )
    else:
        # Data unavailable — use the actual unavailable reason for BOTH columns
        tracker.record(
            table="market_semantics", column="category", row_key=pid,
            source="data_unavailable", confidence="high",
            unavailable_reason=category_reason,
            derivation_rule="§7.4 v1",
        )
        tracker.record(
            table="market_semantics", column="category_source", row_key=pid,
            source="data_unavailable", confidence="high",
            unavailable_reason=category_reason,
            derivation_rule="§7.4 v1",
        )
    if ms_row["category_unavailable_reason"] is not None:
        tracker.record(
            table="market_semantics", column="category_unavailable_reason", row_key=pid,
            source="derived", confidence="high",
            source_detail="leafs_by_cid + nav_lookup lookup",
            derivation_rule="§7.4 v1",
        )
    if ms_row.get("leaf_tags_json") is not None:
        tracker.record(
            table="market_semantics", column="leaf_tags_json", row_key=pid,
            source="fixture_field", confidence="high",
            source_detail="WalletData.leafs_by_cid[<cid>] (post-skip, post-ambiguous, alphabetical)",
            derivation_rule="§7.4 v1 leaf preservation",
        )
    tracker.record(
        table="market_semantics", column="event_family", row_key=pid,
        source="data_unavailable", confidence="high",
        unavailable_reason="event_family_not_derived_in_v1_market_semantics",
        derivation_rule="§7.4 v1 (event_family deferred)",
    )
    tracker.record(
        table="market_semantics", column="event_family_source", row_key=pid,
        source="data_unavailable", confidence="high",
        unavailable_reason="event_family_not_derived_in_v1_market_semantics",
        derivation_rule="§7.4 v1 (event_family deferred)",
    )
    tracker.record(
        table="market_semantics", column="outcome_role", row_key=pid,
        source="derived", confidence="high",
        source_detail="market_type + outcome_label string match",
        derivation_rule="§7.3",
    )
    # v3: favorite_status lineage switches on actual derivation state.
    _fav_source = ms_row["favorite_status_source"]
    if _fav_source == "binary_entry_price_complement":
        tracker.record(
            table="market_semantics", column="favorite_status", row_key=pid,
            source="derived", confidence="high",
            source_detail="binary_entry_price_complement",
            derivation_rule="§7.3 v3",
        )
        tracker.record(
            table="market_semantics", column="favorite_status_source", row_key=pid,
            source="derived", confidence="high",
            source_detail="binary_entry_price_complement",
            derivation_rule="§7.3 v3",
        )
    else:
        tracker.record(
            table="market_semantics", column="favorite_status", row_key=pid,
            source="data_unavailable", confidence="high",
            unavailable_reason=_fav_source,
            derivation_rule="§7.3 v3 (binary-only; multi-outcome deferred)",
        )
        tracker.record(
            table="market_semantics", column="favorite_status_source", row_key=pid,
            source="data_unavailable", confidence="high",
            unavailable_reason=_fav_source,
            derivation_rule="§7.3 v3 (binary-only; multi-outcome deferred)",
        )
    if entry_price_was_observed:
        tracker.record(
            table="market_semantics", column="entry_price_bucket", row_key=pid,
            source="derived", confidence="high",
            source_detail="bin(entry_price)",
            derivation_rule="§7.2",
        )
    else:
        # Honest provenance: the value in the row is the literal
        # "data_unavailable" sentinel, not a derived bucket.
        tracker.record(
            table="market_semantics", column="entry_price_bucket", row_key=pid,
            source="data_unavailable", confidence="high",
            unavailable_reason="entry_price_missing",
            derivation_rule="§7.2 (no bin computed)",
        )


def _record_episode_lineage(
    tracker: LineageTracker,
    episode_row: dict[str, Any],
    ep_id: str,
) -> None:
    """Spec §6.2 columns; row_key is the episode_id (PK)."""
    NON_KEY = (
        "position_id", "episode_index", "episode_type", "timestamp",
        "size_exited", "price_exit", "price_entry_for_episode", "episode_pnl",
        "trade_result", "selected_token_resolution_at_episode_close",
        "counterfactual_class", "counterfactual_source", "counterfactual_confidence",
    )
    PROVENANCE = {
        "position_id": ("derived", "hash(condition_id|asset_id)", "high", "§6.1 position_id"),
        "episode_index": ("derived", "sequence within position", "high", "§6.2"),
        "episode_type": ("derived", "partial_sell|full_sell|held_to_resolution|redemption", "high", "§6.2"),
        "timestamp": ("derived", "activity_rows[i].timestamp or redemption.timestamp", "high", "§6.2"),
        "size_exited": ("derived", "activity_rows[i].size or remaining shares", "high", "§6.2"),
        "price_exit": ("derived", "activity_rows[i].price or {0,1} for resolution", "high", "§6.2"),
        "price_entry_for_episode": ("derived", "position avgPrice as v0 proxy (no FIFO)", "medium", "§6.2"),
        "episode_pnl": ("derived", "(price_exit - price_entry_for_episode) * size_exited", "high", "§6.2"),
        "trade_result": ("derived", "sign(episode_pnl)", "high", "§6.2"),
        "selected_token_resolution_at_episode_close": ("derived", "position-level §7.5 resolution", "high", "§6.2"),
        "counterfactual_class": ("derived", "spec §7.6 classification table", "high", "§7.6"),
        "counterfactual_source": ("derived", "episode_is_resolution|eventual_resolution_observed|eventual_resolution_unknown", "high", "§6.2"),
        "counterfactual_confidence": ("derived", "follows resolution + episode_type", "high", "§6.2"),
    }
    for col in NON_KEY:
        if episode_row.get(col) is None:
            continue
        source, detail, conf, rule = PROVENANCE[col]
        tracker.record(
            table="position_exit_episodes", column=col, row_key=ep_id,
            source=source, confidence=conf,
            source_detail=detail, derivation_rule=rule,
        )


def _record_result_semantics_lineage(
    tracker: LineageTracker,
    result_row: dict[str, Any],
    gid: str,
) -> None:
    NON_KEY = (
        "grain_key", "n_positions", "n_episodes", "episodes_by_class",
        "outcome_win_rate_held", "trade_win_rate_preres",
        "sold_winners_early_share", "sold_winner_at_loss_share",
        "loss_cut_correctness_rate", "pnl_total",
        "pnl_concentration_top5_share", "total_size_usdc",
        "attribution_coverage", "grain_quality", "grain_quality_reasons",
    )
    for col in NON_KEY:
        if result_row.get(col) is None:
            continue
        tracker.record(
            table="result_semantics", column=col, row_key=gid,
            source="derived", confidence="high",
            source_detail=f"aggregated from positions + position_exit_episodes for grain={gid}",
            derivation_rule="§7.7 grain aggregation",
        )


def _record_mechanism_evidence_lineage(
    tracker: LineageTracker,
    me_row: dict[str, Any],
    me: MechanismEvidenceRow,
) -> None:
    # Composite key: (grain_id, mechanism) — encode as row_key for lineage.
    row_key = f"{me.grain_id}|{me.mechanism}"
    NON_KEY = (
        "evidence_strength", "supporting_metrics", "sample_size",
        "caveats", "missing_data", "candidate_thesis_evidence",
        "not_promoted_reason", "derived_from_fixture_fetched_at",
    )
    for col in NON_KEY:
        if me_row.get(col) is None:
            continue
        tracker.record(
            table="mechanism_evidence", column=col, row_key=row_key,
            source="derived", confidence="high",
            source_detail=f"spec §7.8 {me.mechanism} rule",
            derivation_rule="§7.8 + §8 (gates) + §8.3 (promotion)",
        )


def _record_data_gaps_lineage(
    tracker: LineageTracker,
    data_gaps_rows: list[dict[str, Any]],
) -> None:
    """Emit lineage for every non-key, non-null cell in data_gaps.

    All v0 data_gaps rows are statically authored in this module
    (not derived from fixture data), so source is `derived` with a
    pointer back to the spec section that motivated each gap.
    """
    # row_key for data_gaps is the gap_name (it's the PK).
    NON_KEY_COLUMNS = (
        "affected_columns",
        "affected_grain_count",
        "unblocking_spec",
        "severity",
    )
    for gap in data_gaps_rows:
        gap_name = gap["gap_name"]
        for col in NON_KEY_COLUMNS:
            if gap.get(col) is None:
                continue
            tracker.record(
                table="data_gaps", column=col, row_key=gap_name,
                source="derived", confidence="high",
                source_detail=f"static authored in _build_data_gaps for gap={gap_name!r}",
                derivation_rule="§6.11 (v0 known-gaps list)",
            )


# ---------------------------------------------------------------------------
# v4 thesis_candidates lifecycle (spec docs/superpowers/specs/2026-05-27-
# wallet-dossier-thesis-lifecycle-v4.md)
# ---------------------------------------------------------------------------

# Required-test-data map by mechanism — conservative, named in the spec.
# Names what would be needed to test a thesis later; does NOT claim deployability.
_THESIS_REQUIRED_DATA: dict[str, list[str]] = {
    "prediction_driven": ["resolution_outcomes", "market_metadata", "price_history"],
    "trading_driven": ["fill_sequence", "counterfactual_resolution", "market_timestamps"],
    "risk_management_driven": ["fill_sequence", "counterfactual_resolution", "risk_cut_policy"],
    "mixed": ["mechanism_specific_dataset", "counterfactual_resolution"],
}

# v4 simulation_plan stub — deterministic JSON written per spec §"Simulation Plan".
_V4_SIMULATION_PLAN: dict[str, Any] = {
    "status": "simulation_ready_after_dataset_build",
    "replay_constraints": [
        "no_live_signal",
        "use_only_timestamp-valid data",
        "model fees/slippage before paper validation",
    ],
}


def _thesis_id(grain_id: str, mechanism: str) -> str:
    """Deterministic hash of (grain_id, mechanism, schema_version)."""
    raw = f"{grain_id}|{mechanism}|{DOSSIER_SCHEMA_VERSION}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _build_thesis_candidates(
    mechanism_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """v4 §"Promotion Rule": emit one thesis_candidate per mechanism_evidence
    row with non-null `candidate_thesis_evidence`. Direct mirror of grain_id,
    mechanism, thesis_text, supporting_metrics from the source row."""
    out: list[dict[str, Any]] = []
    for row in mechanism_rows:
        thesis_text = row.get("candidate_thesis_evidence")
        if not thesis_text:
            continue
        mechanism = str(row["mechanism"])
        required = _THESIS_REQUIRED_DATA.get(
            mechanism, ["mechanism_specific_dataset"],
        )
        out.append({
            "thesis_id": _thesis_id(str(row["grain_id"]), mechanism),
            "grain_id": str(row["grain_id"]),
            "mechanism": mechanism,
            "status": "candidate",
            "thesis_text": thesis_text,
            "supporting_metrics": row["supporting_metrics"],
            "required_test_data": json.dumps(required),
            "simulation_plan": json.dumps(_V4_SIMULATION_PLAN, sort_keys=True),
            "source_schema_version": DOSSIER_SCHEMA_VERSION,
        })
    return out


def _record_thesis_candidates_lineage(
    tracker: LineageTracker,
    thesis_rows: list[dict[str, Any]],
) -> None:
    """Every non-key cell traces to its source. thesis_id is the PK."""
    for row in thesis_rows:
        rk = row["thesis_id"]
        # Direct mirrors from mechanism_evidence row
        for col, src in (
            ("grain_id", "mechanism_evidence.grain_id"),
            ("mechanism", "mechanism_evidence.mechanism"),
            ("thesis_text", "mechanism_evidence.candidate_thesis_evidence"),
            ("supporting_metrics", "mechanism_evidence.supporting_metrics"),
        ):
            tracker.record(
                table="thesis_candidates", column=col, row_key=rk,
                source="derived", confidence="high",
                source_detail=f"direct mirror of {src} for promoted grain",
                derivation_rule="v4 §Promotion Rule",
            )
        # Deterministic v4 values
        tracker.record(
            table="thesis_candidates", column="status", row_key=rk,
            source="derived", confidence="high",
            source_detail="v4 emits only 'candidate'",
            derivation_rule="v4 §Lifecycle States",
        )
        tracker.record(
            table="thesis_candidates", column="required_test_data", row_key=rk,
            source="derived", confidence="high",
            source_detail=f"_THESIS_REQUIRED_DATA[mechanism={row['mechanism']!r}]",
            derivation_rule="v4 §Required Test Data",
        )
        tracker.record(
            table="thesis_candidates", column="simulation_plan", row_key=rk,
            source="derived", confidence="high",
            source_detail="_V4_SIMULATION_PLAN deterministic stub",
            derivation_rule="v4 §Simulation Plan",
        )
        tracker.record(
            table="thesis_candidates", column="source_schema_version", row_key=rk,
            source="derived", confidence="high",
            source_detail=f"DOSSIER_SCHEMA_VERSION = {DOSSIER_SCHEMA_VERSION!r}",
            derivation_rule="v4",
        )


# ---------------------------------------------------------------------------
# v5 behavioral descriptor derivers (pure functions from activity_rows +
# closed_positions; no protocol-report dependency).
# Spec: docs/superpowers/specs/2026-05-27-wallet-dossier-descriptor-surfaces-v5.md
# ---------------------------------------------------------------------------

_DESCRIPTOR_HOUR_DISTRIBUTION_MIN_ROWS = 50
_DESCRIPTOR_CADENCE_MIN_INTERVALS = 10
_DESCRIPTOR_SIZING_MIN_FILLS = 10
_DESCRIPTOR_TIME_IN_POSITION_MIN_POSITIONS = 5
_DESCRIPTOR_FILLS_PER_POSITION_MIN_POSITIONS = 5


def _percentile_int(sorted_vals: list[float], p: float) -> int:
    """Return the p-th percentile (0..100) of a sorted list as int.

    Uses nearest-rank method. sorted_vals must be sorted ascending and non-empty.
    """
    n = len(sorted_vals)
    idx = max(0, min(n - 1, int(p / 100 * n)))
    return int(sorted_vals[idx])


def _percentile_float(sorted_vals: list[float], p: float) -> float:
    """Same but returns float."""
    n = len(sorted_vals)
    idx = max(0, min(n - 1, int(p / 100 * n)))
    return float(sorted_vals[idx])


def _get_market_category(
    condition_id: str,
    market_semantics_by_pid: dict[str, dict[str, Any]],
    position_id_by_asset: dict[str, str],
    asset_id: str,
) -> str:
    """Resolve market_category for an activity row."""
    pid = position_id_by_asset.get(asset_id)
    if pid:
        ms = market_semantics_by_pid.get(pid)
        if ms:
            return str(ms.get("category") or "data_unavailable")
    return "data_unavailable"


def _build_descriptor_hour_distribution(
    activity_rows: list[dict[str, Any]],
    market_semantics_by_pid: dict[str, dict[str, Any]],
    position_id_by_asset: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """§1 descriptor_hour_distribution.

    Returns (rows, gap_names). If len(activity_rows) < 50, returns ([], [gap]).
    """
    if len(activity_rows) < _DESCRIPTOR_HOUR_DISTRIBUTION_MIN_ROWS:
        return [], ["wallet_too_sparse_for_hour_distribution"]

    # Bucket: (hour_utc, side, market_category) -> {n_trades, total_size}
    buckets: dict[tuple[int, str, str], dict[str, Any]] = {}
    for row in activity_rows:
        ts = _safe_int(row.get("timestamp"))
        side = str(row.get("side") or "BUY").upper()
        size = _safe_float(row.get("size")) or 0.0
        price = _safe_float(row.get("price")) or 0.0
        notional = size * price
        asset_id = str(row.get("asset") or "")
        if ts is None:
            continue
        hour_utc = (ts // 3600) % 24
        category = _get_market_category(
            str(row.get("conditionId") or ""),
            market_semantics_by_pid,
            position_id_by_asset,
            asset_id,
        )
        key = (hour_utc, side, category)
        if key not in buckets:
            buckets[key] = {"n_trades": 0, "total_size_usdc": 0.0}
        buckets[key]["n_trades"] += 1
        buckets[key]["total_size_usdc"] += notional

    rows = sorted(
        [
            {
                "hour_utc": k[0],
                "side": k[1],
                "market_category": k[2],
                "n_trades": v["n_trades"],
                "total_size_usdc": float(v["total_size_usdc"]),
            }
            for k, v in buckets.items()
        ],
        key=lambda r: (r["hour_utc"], r["side"], r["market_category"]),
    )
    return rows, []


def _build_descriptor_cadence(
    activity_rows: list[dict[str, Any]],
    market_semantics_by_pid: dict[str, dict[str, Any]],
    position_id_by_asset: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """§2 descriptor_cadence: inter-trade intervals per (side, category)."""
    # Collect sorted timestamps per bucket
    bucket_ts: dict[tuple[str, str], list[int]] = defaultdict(list)
    for row in activity_rows:
        ts = _safe_int(row.get("timestamp"))
        if ts is None:
            continue
        side = str(row.get("side") or "BUY").upper()
        asset_id = str(row.get("asset") or "")
        category = _get_market_category(
            str(row.get("conditionId") or ""),
            market_semantics_by_pid,
            position_id_by_asset,
            asset_id,
        )
        bucket_ts[(side, category)].append(ts)

    rows = []
    gap_names = []
    for (side, category), timestamps in sorted(bucket_ts.items()):
        timestamps.sort()
        intervals = [
            timestamps[i + 1] - timestamps[i]
            for i in range(len(timestamps) - 1)
        ]
        n_intervals = len(intervals)
        if n_intervals < _DESCRIPTOR_CADENCE_MIN_INTERVALS:
            gap_names.append(f"wallet_too_sparse_for_cadence_{side}_{category}")
            continue
        intervals_sorted = sorted(intervals)
        burst_count = sum(1 for v in intervals if v < 60)
        rows.append({
            "side": side,
            "market_category": category,
            "n_intervals": n_intervals,
            "median_seconds_between_trades": _percentile_int(intervals_sorted, 50),
            "p10_seconds_between_trades": _percentile_int(intervals_sorted, 10),
            "p90_seconds_between_trades": _percentile_int(intervals_sorted, 90),
            "burst_share": float(burst_count) / n_intervals,
        })
    return rows, gap_names


def _build_descriptor_sizing(
    activity_rows: list[dict[str, Any]],
    market_semantics_by_pid: dict[str, dict[str, Any]],
    position_id_by_asset: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """§3 descriptor_sizing: fill-size distribution per (side, category).

    Spec §3 requires the size columns to be the fill *notional* (size * price),
    not raw shares. Codex review 2026-05-27 caught this in v5 — same class of
    bug as the hour_distribution.total_size_usdc fix (commit 2c41ef1).
    """
    bucket_sizes: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in activity_rows:
        size = _safe_float(row.get("size"))
        if size is None:
            continue
        price = _safe_float(row.get("price")) or 0.0
        notional = size * price
        side = str(row.get("side") or "BUY").upper()
        asset_id = str(row.get("asset") or "")
        category = _get_market_category(
            str(row.get("conditionId") or ""),
            market_semantics_by_pid,
            position_id_by_asset,
            asset_id,
        )
        bucket_sizes[(side, category)].append(notional)

    rows = []
    gap_names = []
    for (side, category), sizes in sorted(bucket_sizes.items()):
        n_fills = len(sizes)
        if n_fills < _DESCRIPTOR_SIZING_MIN_FILLS:
            gap_names.append(f"wallet_too_sparse_for_sizing_{side}_{category}")
            continue
        sizes_sorted = sorted(sizes)
        mean_size = statistics.mean(sizes)
        stdev_size = statistics.pstdev(sizes) if n_fills > 1 else 0.0
        cv = stdev_size / mean_size if mean_size > 0 else 0.0
        rows.append({
            "side": side,
            "market_category": category,
            "n_fills": n_fills,
            "median_size_usdc": _percentile_float(sizes_sorted, 50),
            "p10_size_usdc": _percentile_float(sizes_sorted, 10),
            "p90_size_usdc": _percentile_float(sizes_sorted, 90),
            "max_size_usdc": float(max(sizes)),
            "coeff_of_variation": float(cv),
        })
    return rows, gap_names


def _build_descriptor_time_in_position(
    closed_positions: list[dict[str, Any]],
    activity_rows: list[dict[str, Any]],
    open_positions: list[dict[str, Any]],
    fetched_at: int | None,
    positions_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """§4 descriptor_time_in_position: hold duration by exit_mode.

    duration = exit_ts - entry_ts; for still_open/unresolved: fetched_at - entry_ts.
    Uses already-derived positions rows (which carry entry_ts, exit_ts, exit_mode).
    Fallback: when entry_ts is None, use the closed_position.timestamp for that position.
    """

    # Build fallback entry_ts from closed_positions: position_id -> cp.timestamp
    cp_ts_by_pid: dict[str, int] = {}
    for cp in closed_positions:
        cid = (cp.get("conditionId") or "").lower()
        asset_id = str(cp.get("asset") or "")
        if cid and asset_id:
            pid = _position_id(cid, asset_id)
            ts = _safe_int(cp.get("timestamp"))
            if ts is not None:
                cp_ts_by_pid[pid] = ts

    bucket_durations: dict[str, list[int]] = defaultdict(list)

    for pos in positions_rows:
        exit_mode = str(pos.get("exit_mode") or "unresolved")
        entry_ts = pos.get("entry_ts")
        exit_ts = pos.get("exit_ts")
        pid = pos.get("position_id", "")

        if entry_ts is None:
            # Fall back to closed_position.timestamp (the position entry timestamp
            # from the Polymarket /closed-positions API) when activity-derived
            # entry_ts is unavailable.
            entry_ts = cp_ts_by_pid.get(pid)

        if entry_ts is None:
            continue

        if exit_mode in ("still_open", "unresolved"):
            ref_ts = fetched_at if fetched_at else entry_ts
            duration = ref_ts - entry_ts
        elif exit_ts is not None:
            duration = exit_ts - entry_ts
        else:
            # exit_mode is held/pre_resolution but no exit_ts — use fetched_at
            ref_ts = fetched_at if fetched_at else entry_ts
            duration = ref_ts - entry_ts

        if duration <= 0:
            continue
        bucket_durations[exit_mode].append(duration)

    rows = []
    gap_names = []
    for exit_mode, durations in sorted(bucket_durations.items()):
        n_pos = len(durations)
        if n_pos < _DESCRIPTOR_TIME_IN_POSITION_MIN_POSITIONS:
            gap_names.append(f"wallet_too_sparse_for_time_in_position_{exit_mode}")
            continue
        durations_sorted = sorted(durations)
        rows.append({
            "exit_mode": exit_mode,
            "n_positions": n_pos,
            "median_seconds_in_position": _percentile_int(durations_sorted, 50),
            "p10_seconds_in_position": _percentile_int(durations_sorted, 10),
            "p90_seconds_in_position": _percentile_int(durations_sorted, 90),
        })
    return rows, gap_names


def _build_descriptor_fills_per_position(
    activity_rows: list[dict[str, Any]],
    market_semantics_by_pid: dict[str, dict[str, Any]],
    position_id_by_asset: dict[str, str],
    positions_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """§5 descriptor_fills_per_position: fills per position by category.

    A "position" = one entry in positions_rows. Fills = activity_rows
    matching that position's asset_id.
    """

    # Count fills per position_id
    fills_by_pid: dict[str, int] = defaultdict(int)
    for row in activity_rows:
        asset_id = str(row.get("asset") or "")
        pid = position_id_by_asset.get(asset_id)
        if pid:
            fills_by_pid[pid] += 1

    # Bucket positions by category
    bucket_fills: dict[str, list[int]] = defaultdict(list)
    for pos in positions_rows:
        pid = pos["position_id"]
        ms = market_semantics_by_pid.get(pid)
        category = str(ms.get("category") if ms else "data_unavailable") if ms else "data_unavailable"
        n_fills = fills_by_pid.get(pid, 0)
        bucket_fills[category].append(n_fills)

    rows = []
    gap_names = []
    for category, fills_list in sorted(bucket_fills.items()):
        n_pos = len(fills_list)
        if n_pos < _DESCRIPTOR_FILLS_PER_POSITION_MIN_POSITIONS:
            gap_names.append(f"wallet_too_sparse_for_fills_per_position_{category}")
            continue
        fills_sorted = sorted(fills_list)
        single_count = sum(1 for f in fills_list if f == 1)
        rows.append({
            "market_category": category,
            "n_positions": n_pos,
            "median_fills": _percentile_int(fills_sorted, 50),
            "p10_fills": _percentile_int(fills_sorted, 10),
            "p90_fills": _percentile_int(fills_sorted, 90),
            "single_fill_share": float(single_count) / n_pos,
        })
    return rows, gap_names


def _record_descriptor_lineage(
    tracker: LineageTracker,
    table: str,
    rows: list[dict[str, Any]],
    non_key_cols: tuple[str, ...],
    row_key_col: str,
    derivation_rule: str,
) -> None:
    """Generic lineage recorder for v5 descriptor tables."""
    for row in rows:
        rk = str(row.get(row_key_col, ""))
        for col in non_key_cols:
            val = row.get(col)
            if val is None:
                continue
            tracker.record(
                table=table, column=col, row_key=rk,
                source="derived", confidence="high",
                source_detail="derived from activity_rows and/or closed_positions",
                derivation_rule=derivation_rule,
            )


def _build_data_gaps(
    positions_rows: list[dict[str, Any]],
    market_semantics_rows: list[dict[str, Any]],
    mechanism_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Known gaps that remain after v1_market_semantics table population."""
    n_positions = len(positions_rows)
    gaps: list[dict[str, Any]] = []

    # market_category_unavailable: count-based emission per spec §2.3
    # Severity tiers (spec §2.3): high ≥50%, medium 10–50%, low <10%
    n_unavailable = sum(
        1 for r in market_semantics_rows if r["category"] == "data_unavailable"
    )
    if n_unavailable > 0:
        total = len(market_semantics_rows)
        affected_share = n_unavailable / total if total > 0 else 0.0
        if affected_share >= 0.5:
            severity = "high"
        elif affected_share >= 0.1:
            severity = "medium"
        else:
            severity = "low"
        gaps.append({
            "gap_name": "market_category_unavailable",
            "affected_columns": json.dumps([
                "market_semantics.category",
                "market_semantics.category_source",
                "market_semantics.category_unavailable_reason",
            ]),
            "affected_grain_count": n_unavailable,
            "unblocking_spec": "docs/superpowers/specs/2026-05-26-wallet-dossier-market-semantics-v1.md",
            "severity": severity,
        })

    # v3 §3: sibling_token_prices_at_entry_not_fetched is now count-based.
    # Emit only when at least one position has outcome_role_rank=NULL (i.e.
    # binary-derivation didn't fire OR market is non-binary). For binary
    # markets v3 derives rank from entry_price; multi-outcome stays NULL
    # until sibling-price fetch lands.
    n_no_rank = sum(1 for r in positions_rows if r["outcome_role_rank"] is None)
    if n_no_rank > 0:
        gaps.append({
            "gap_name": "sibling_token_prices_at_entry_not_fetched",
            "affected_columns": json.dumps([
                "positions.outcome_role_rank",
                "market_semantics.favorite_status",
            ]),
            "affected_grain_count": n_no_rank,
            "unblocking_spec": "docs/superpowers/specs/2026-05-26-wallet-dossier-binary-outcome-rank-v3.md#5",
            "severity": (
                "high" if n_no_rank / n_positions >= 0.5 else
                "medium" if n_no_rank / n_positions >= 0.1 else
                "low"
            ),
        })
    gaps += [
        {
            # §7.5 rule 2 stays OFF until §11.5 empirical verification.
            # Until then, positions whose resolution can only be inferred
            # from curPrice will fall through to "unknown" via rule 4.
            "gap_name": "resolution_rule_2_curprice_disabled_pending_verification",
            "affected_columns": json.dumps([
                "positions.selected_token_resolution",
                "positions.resolution_source",
                "positions.resolution_confidence",
                "positions.exit_mode",
            ]),
            "affected_grain_count": sum(
                1 for r in positions_rows
                if r["resolution_source"] == "no_resolution_signal"
            ),
            "unblocking_spec": "docs/superpowers/specs/2026-05-25-wallet-dossier-semantics-v0.md#7.5",
            "severity": "medium",
        },
    ]
    n_market_type_unknown = sum(1 for r in positions_rows if r["market_type"] == "unknown")
    if n_market_type_unknown:
        gaps.append({
            # §7.1 caveat: when the fixture has neither enough observed
            # asset IDs nor closed_positions.oppositeAsset, the derivation
            # rule still cannot distinguish binary from multi-outcome.
            "gap_name": "multi_outcome_undercounted_when_wallet_one_sided",
            "affected_columns": json.dumps([
                "positions.market_type",
                "market_semantics.market_type",
                "market_semantics.n_outcomes_in_market",
            ]),
            "affected_grain_count": n_market_type_unknown,
            "unblocking_spec": "docs/superpowers/specs/2026-05-26-wallet-dossier-market-structure-v2.md",
            "severity": "medium",
        })
    n_market_selection_deferred = sum(
        1 for r in mechanism_rows
        if r["mechanism"] == "market_selection_driven"
        and r["not_promoted_reason"] == "market_selection_driven_deferred"
    )
    if n_market_selection_deferred:
        gaps.append({
            # Market-selection skill requires the market universe the wallet
            # could have selected from, not only the markets it traded.
            "gap_name": "market_selection_universe_baseline_missing",
            "affected_columns": json.dumps([
                "mechanism_evidence.market_selection_driven",
            ]),
            "affected_grain_count": n_market_selection_deferred,
            "unblocking_spec": "docs/superpowers/specs/2026-05-10-strategy-pattern-audit-design.md",
            "severity": "high",
        })
    return gaps


# ---------------------------------------------------------------------------
# §6.7 hypothesis_results — flat mirror of branch.hypotheses
# ---------------------------------------------------------------------------


# Branches the dossier mirrors. Other ProtocolReport branches added later
# (e.g. a third meta-branch) would extend this tuple.
_HYPOTHESIS_BRANCHES: tuple[str, ...] = ("held_analyst_branch", "flipper_branch")

# Effect-size keys we probe in HypothesisResult.details. Different
# hypotheses report effect under different field names; this list is
# best-effort. NULL when nothing matches — the spec field is "best-effort
# mirror", not a derivation.
_EFFECT_SIZE_KEYS: tuple[str, ...] = (
    "effect_size",
    "effect_size_excess_pp",
    "excess_pp",
    "test_statistic",
    "wr_excess_pp",
    "roi_excess_pp",
)


def _build_hypothesis_results(
    protocol_report: dict[str, Any],
) -> list[dict[str, Any]]:
    """§6.7 — flatten held + flipper branch hypotheses into one row per H.

    Pure mirror: no derivation, no mutation. When a branch is absent or
    inactive (`hypotheses == []`), it contributes zero rows.
    """
    rows: list[dict[str, Any]] = []
    for branch_field in _HYPOTHESIS_BRANCHES:
        branch = protocol_report.get(branch_field) or {}
        if not isinstance(branch, dict):
            continue
        # `branch.branch` is the canonical label; fall back to the field
        # stripped of "_branch" if absent for legacy serializations.
        branch_label = branch.get("branch") or branch_field.replace("_branch", "")
        hypotheses = branch.get("hypotheses") or []
        for h in hypotheses:
            if not isinstance(h, dict):
                continue
            details = h.get("details") or {}
            effect_size: float | None = None
            if isinstance(details, dict):
                for k in _EFFECT_SIZE_KEYS:
                    if details.get(k) is not None:
                        try:
                            effect_size = float(details[k])
                            break
                        except (TypeError, ValueError):
                            continue
            if effect_size is None and h.get("test_statistic") is not None:
                try:
                    effect_size = float(h["test_statistic"])
                except (TypeError, ValueError):
                    effect_size = None
            notes = h.get("notes") or []
            note_text = "; ".join(str(n) for n in notes) if notes else None
            p_value = h.get("p_value_bh_adjusted")
            if p_value is None:
                p_value = h.get("p_value_raw")
            rows.append({
                "branch": str(branch_label),
                "name": str(h.get("name") or ""),
                "verdict": str(h.get("verdict") or ""),
                "n": _safe_int(h.get("n")) or 0,
                "p_value": _safe_float(p_value),
                "effect_size": effect_size,
                "note": note_text,
            })
    return rows


def _record_hypothesis_results_lineage(
    tracker: LineageTracker,
    hypothesis_rows: list[dict[str, Any]],
) -> None:
    """Every mirrored cell points back to its ProtocolReport source path."""
    for row in hypothesis_rows:
        rk = f"{row['branch']}|{row['name']}"
        # branch + name are key fields per §6.7 PK; skip those.
        for col in ("verdict", "n", "p_value", "effect_size", "note"):
            val = row.get(col)
            if val is None:
                continue
            tracker.record(
                table="hypothesis_results", column=col, row_key=rk,
                source="protocol_report_surface", confidence="high",
                source_detail=(
                    f"ProtocolReport.{row['branch']}_branch.hypotheses[{row['name']}].{col}"
                ),
                derivation_rule="§6.7 (read-only mirror)",
            )


# ---------------------------------------------------------------------------
# §6.8 surfaces — read-only mirror of ProtocolReport surface dicts
# ---------------------------------------------------------------------------


# Top-level ProtocolReport fields that are "surfaces" per §6.8. Keeping
# this list explicit (not dynamic) so adding/removing a surface is an
# intentional dossier change rather than silently inherited.
_SURFACE_FIELDS: tuple[str, ...] = (
    "capital_ledger",
    "execution_quality_summary",
    "outcome_side_breakdown",
    "extractor_features_summary",
    "exit_attribution",
    "timing_surface",
    "position_building_surface",
    "liquidity_surface",
    "capital_risk_surface",
    "sizing_outcome_surface",
    "null_baseline_surface",
    "survivorship_surface",
    "copy_latency_surface",
    "position_reconciliation_surface",
    "data_completeness",
    "time_coverage",
)


def _build_surfaces(
    protocol_report: dict[str, Any],
) -> list[dict[str, Any]]:
    """§6.8 — one row per populated surface field on ProtocolReport.

    `data_state` and `caveats` are split out into named columns; everything
    else lands in `key_metrics` as a deterministic JSON object. Surfaces
    that are None or empty dicts contribute zero rows. Pure mirror.
    """
    rows: list[dict[str, Any]] = []
    for surface_name in _SURFACE_FIELDS:
        body = protocol_report.get(surface_name)
        if body is None:
            continue
        if isinstance(body, dict) and not body:
            continue
        data_state: str | None = None
        caveats_json: str | None = None
        key_metrics_obj: Any
        if isinstance(body, dict):
            data_state = body.get("data_state")
            cav = body.get("caveats")
            if isinstance(cav, list):
                caveats_json = json.dumps(cav, sort_keys=True, default=str)
            elif cav is not None:
                # Caveats sometimes ship as a dict per-surface; preserve
                # shape under the same column so downstream JSON parses it.
                caveats_json = json.dumps(cav, sort_keys=True, default=str)
            # Build key_metrics deterministically — exclude the split fields.
            key_metrics_obj = {
                k: v for k, v in body.items()
                if k not in ("data_state", "caveats")
            }
        else:
            # Non-dict surface (rare; the legacy display-only fields are
            # plain dicts). Mirror it as the whole body.
            key_metrics_obj = body
        rows.append({
            "surface_name": surface_name,
            "data_state": data_state,
            "key_metrics": json.dumps(key_metrics_obj, sort_keys=True, default=str),
            "caveats": caveats_json,
        })
    return rows


def _record_surfaces_lineage(
    tracker: LineageTracker, surfaces_rows: list[dict[str, Any]],
) -> None:
    for row in surfaces_rows:
        rk = row["surface_name"]
        for col in ("data_state", "key_metrics", "caveats"):
            if row.get(col) is None:
                continue
            tracker.record(
                table="surfaces", column=col, row_key=rk,
                source="protocol_report_surface", confidence="high",
                source_detail=f"ProtocolReport.{rk}.{col if col != 'key_metrics' else '*'}",
                derivation_rule="§6.8 (read-only mirror)",
            )


# ---------------------------------------------------------------------------
# §6.9 crosstabs — pre-computed grain aggregates
# ---------------------------------------------------------------------------


def _crosstab_id(crosstab_name: str, grain_key: dict[str, Any]) -> str:
    """Stable hash for crosstab PK. Same (name, grain_key) → same id."""
    raw = (crosstab_name + "|" + json.dumps(grain_key, sort_keys=True)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _sql_json_object(*args: Any) -> str:
    if len(args) % 2:
        raise ValueError("dossier_json_object expects key/value pairs")
    obj = {str(args[i]): args[i + 1] for i in range(0, len(args), 2)}
    return json.dumps(obj, sort_keys=True)


def _sql_crosstab_id(crosstab_name: str, grain_key_json: str) -> str:
    raw = (str(crosstab_name) + "|" + str(grain_key_json)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _build_crosstabs(
    *,
    positions_rows: list[dict[str, Any]],
    episodes_rows: list[dict[str, Any]],
    diagnostic_accounting_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """§6.9 — materialize the four standard crosstabs from SQL views."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.create_function("dossier_json_object", -1, _sql_json_object)
    conn.create_function("dossier_crosstab_id", 2, _sql_crosstab_id)
    try:
        for ddl in SCHEMA_DDL:
            conn.execute(ddl)
        _insert_dict_rows(conn, "positions", positions_rows)
        _insert_dict_rows(conn, "position_exit_episodes", episodes_rows)
        _insert_dict_rows(conn, "diagnostic_accounting", diagnostic_accounting_rows)
        conn.executescript(
            """
            CREATE TEMP VIEW crosstab_episode_counts AS
            SELECT
              position_id,
              SUM(CASE WHEN counterfactual_class = 'held_won' THEN 1 ELSE 0 END) AS held_won,
              SUM(CASE WHEN counterfactual_class = 'held_lost' THEN 1 ELSE 0 END) AS held_lost,
              SUM(CASE WHEN counterfactual_class IN (
                'profitable_exit_later_won', 'profitable_exit_later_lost'
              ) THEN 1 ELSE 0 END) AS preres_win,
              SUM(CASE WHEN counterfactual_class IN (
                'loss_exit_later_won', 'loss_exit_later_lost'
              ) THEN 1 ELSE 0 END) AS preres_loss
            FROM position_exit_episodes
            GROUP BY position_id;

            CREATE TEMP VIEW crosstab_source AS
            SELECT
              1 AS sort_order,
              'market_type_x_entry_bucket_x_exit_mode' AS crosstab_name,
              dossier_json_object(
                'market_type', COALESCE(p.market_type, 'unknown'),
                'entry_price_bucket', COALESCE(p.entry_price_bucket, 'data_unavailable'),
                'exit_mode', COALESCE(p.exit_mode, 'unknown')
              ) AS grain_key,
              COUNT(*) AS n,
              COALESCE(SUM(COALESCE(p.realized_pnl, 0.0)), 0.0) AS pnl,
              CASE
                WHEN SUM(COALESCE(e.held_won, 0) + COALESCE(e.held_lost, 0)) > 0
                THEN CAST(SUM(COALESCE(e.held_won, 0)) AS REAL)
                  / SUM(COALESCE(e.held_won, 0) + COALESCE(e.held_lost, 0))
                ELSE NULL
              END AS outcome_win_rate_held,
              CASE
                WHEN SUM(COALESCE(e.preres_win, 0) + COALESCE(e.preres_loss, 0)) > 0
                THEN CAST(SUM(COALESCE(e.preres_win, 0)) AS REAL)
                  / SUM(COALESCE(e.preres_win, 0) + COALESCE(e.preres_loss, 0))
                ELSE NULL
              END AS trade_win_rate_preres,
              0 AS is_diagnostic_only
            FROM positions p
            LEFT JOIN crosstab_episode_counts e ON e.position_id = p.position_id
            GROUP BY
              COALESCE(p.market_type, 'unknown'),
              COALESCE(p.entry_price_bucket, 'data_unavailable'),
              COALESCE(p.exit_mode, 'unknown')

            UNION ALL

            SELECT
              2 AS sort_order,
              'market_type_x_accounting_bucket' AS crosstab_name,
              dossier_json_object(
                'market_type', COALESCE(p.market_type, 'unknown'),
                'accounting_bucket', COALESCE(da.accounting_bucket, 'OTHER')
              ) AS grain_key,
              COUNT(*) AS n,
              COALESCE(SUM(COALESCE(p.realized_pnl, 0.0)), 0.0) AS pnl,
              NULL AS outcome_win_rate_held,
              NULL AS trade_win_rate_preres,
              1 AS is_diagnostic_only
            FROM positions p
            LEFT JOIN diagnostic_accounting da ON da.position_id = p.position_id
            GROUP BY
              COALESCE(p.market_type, 'unknown'),
              COALESCE(da.accounting_bucket, 'OTHER')

            UNION ALL

            SELECT
              3 AS sort_order,
              'entry_bucket_x_counterfactual_class' AS crosstab_name,
              dossier_json_object(
                'entry_price_bucket', COALESCE(p.entry_price_bucket, 'data_unavailable'),
                'counterfactual_class', COALESCE(ep.counterfactual_class, 'insufficient_data')
              ) AS grain_key,
              COUNT(*) AS n,
              COALESCE(SUM(COALESCE(ep.episode_pnl, 0.0)), 0.0) AS pnl,
              NULL AS outcome_win_rate_held,
              NULL AS trade_win_rate_preres,
              0 AS is_diagnostic_only
            FROM position_exit_episodes ep
            JOIN positions p ON p.position_id = ep.position_id
            GROUP BY
              COALESCE(p.entry_price_bucket, 'data_unavailable'),
              COALESCE(ep.counterfactual_class, 'insufficient_data')

            UNION ALL

            SELECT
              4 AS sort_order,
              'position_pattern_x_exit_mode' AS crosstab_name,
              dossier_json_object(
                'position_pattern', COALESCE(p.position_pattern, 'unknown'),
                'exit_mode', COALESCE(p.exit_mode, 'unknown')
              ) AS grain_key,
              COUNT(*) AS n,
              COALESCE(SUM(COALESCE(p.realized_pnl, 0.0)), 0.0) AS pnl,
              NULL AS outcome_win_rate_held,
              NULL AS trade_win_rate_preres,
              0 AS is_diagnostic_only
            FROM positions p
            GROUP BY
              COALESCE(p.position_pattern, 'unknown'),
              COALESCE(p.exit_mode, 'unknown');
            """
        )
        rows = conn.execute(
            """
            SELECT
              dossier_crosstab_id(crosstab_name, grain_key) AS crosstab_id,
              crosstab_name,
              grain_key,
              n,
              pnl,
              outcome_win_rate_held,
              trade_win_rate_preres,
              is_diagnostic_only
            FROM crosstab_source
            ORDER BY sort_order, grain_key
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _record_crosstabs_lineage(
    tracker: LineageTracker, crosstabs_rows: list[dict[str, Any]],
) -> None:
    """Every non-key cell gets a lineage row. crosstab_id + crosstab_name +
    grain_key are PK/key fields and are skipped."""
    NON_KEY_COLUMNS = (
        "n", "pnl", "outcome_win_rate_held", "trade_win_rate_preres",
        "is_diagnostic_only",
    )
    for row in crosstabs_rows:
        for col in NON_KEY_COLUMNS:
            if row.get(col) is None:
                continue
            tracker.record(
                table="crosstabs", column=col, row_key=row["crosstab_id"],
                source="derived", confidence="high",
                source_detail=(
                    f"aggregate over positions / episodes for "
                    f"crosstab={row['crosstab_name']!r}"
                ),
                derivation_rule="§6.9 (grain aggregate)",
            )


def _lineage_row_to_dict(r: LineageRow) -> dict[str, Any]:
    return {
        "claim_id": r.claim_id,
        "table": r.table,
        "column": r.column,
        "row_key": r.row_key,
        "source": r.source,
        "source_detail": r.source_detail,
        "confidence": r.confidence,
        "unavailable_reason": r.unavailable_reason,
        "derivation_rule": r.derivation_rule,
    }


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


def write_sqlite(tables: DossierTables, out_db: Path | str) -> Path:
    """Write all tables to a SQLite file. Deterministic — same input,
    same content. The .sqlite binary may differ in metadata (mtime), so
    determinism tests compare row content not raw bytes."""
    out = Path(out_db)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    conn = sqlite3.connect(str(out))
    try:
        for ddl in SCHEMA_DDL:
            conn.execute(ddl)
        _insert_dict_rows(conn, "positions", tables.positions)
        _insert_dict_rows(conn, "position_exit_episodes", tables.position_exit_episodes)
        _insert_dict_rows(conn, "market_semantics", tables.market_semantics)
        _insert_dict_rows(conn, "diagnostic_accounting", tables.diagnostic_accounting)
        _insert_dict_rows(conn, "result_semantics", tables.result_semantics)
        _insert_dict_rows(conn, "mechanism_evidence", tables.mechanism_evidence)
        _insert_dict_rows(conn, "hypothesis_results", tables.hypothesis_results)
        _insert_dict_rows(conn, "surfaces", tables.surfaces)
        _insert_dict_rows(conn, "crosstabs", tables.crosstabs)
        _insert_dict_rows(conn, "thesis_candidates", tables.thesis_candidates)
        _insert_dict_rows(conn, "data_lineage", tables.data_lineage)
        _insert_dict_rows(conn, "data_gaps", tables.data_gaps)
        _insert_dict_rows(conn, "dossier_meta", tables.dossier_meta)
        _insert_dict_rows(conn, "descriptor_hour_distribution", tables.descriptor_hour_distribution)
        _insert_dict_rows(conn, "descriptor_cadence", tables.descriptor_cadence)
        _insert_dict_rows(conn, "descriptor_sizing", tables.descriptor_sizing)
        _insert_dict_rows(conn, "descriptor_time_in_position", tables.descriptor_time_in_position)
        _insert_dict_rows(conn, "descriptor_fills_per_position", tables.descriptor_fills_per_position)
        conn.commit()
    finally:
        conn.close()
    return out


def write_json_export(tables: DossierTables, out_dir: Path | str) -> Path:
    """Write one JSON file per table for deterministic test fixtures."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = {
        "positions": tables.positions,
        "position_exit_episodes": tables.position_exit_episodes,
        "market_semantics": tables.market_semantics,
        "diagnostic_accounting": tables.diagnostic_accounting,
        "result_semantics": tables.result_semantics,
        "mechanism_evidence": tables.mechanism_evidence,
        "hypothesis_results": tables.hypothesis_results,
        "surfaces": tables.surfaces,
        "crosstabs": tables.crosstabs,
        "thesis_candidates": tables.thesis_candidates,
        "data_lineage": tables.data_lineage,
        "data_gaps": tables.data_gaps,
        "dossier_meta": tables.dossier_meta,
        "descriptor_hour_distribution": tables.descriptor_hour_distribution,
        "descriptor_cadence": tables.descriptor_cadence,
        "descriptor_sizing": tables.descriptor_sizing,
        "descriptor_time_in_position": tables.descriptor_time_in_position,
        "descriptor_fills_per_position": tables.descriptor_fills_per_position,
    }
    for name, rows in payload.items():
        (out / f"{name}.json").write_text(
            json.dumps(rows, indent=2, sort_keys=True, default=str)
        )
    return out


def _insert_dict_rows(
    conn: sqlite3.Connection, table: str, rows: list[dict[str, Any]]
) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    # Quote column names that are reserved SQLite keywords (e.g. "table", "column").
    quoted_cols = [f'"{c}"' for c in cols]
    placeholders = ",".join("?" * len(cols))
    sql = f'INSERT INTO {table} ({",".join(quoted_cols)}) VALUES ({placeholders})'
    conn.executemany(sql, [tuple(r.get(c) for c in cols) for r in rows])


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def build_dossier(
    *,
    wallet: str,
    fixture_path: Path | str,
    protocol_report_path: Path | str,
    out_db: Path | str | None = None,
    out_json_dir: Path | str | None = None,
    out_md: Path | str | None = None,
    include_llm_summary: bool = False,
    since_days: int | None = None,
) -> DossierTables:
    """Build the dossier and (optionally) sink to SQLite, JSON, and/or markdown.

    Returns the in-memory tables regardless of sink, so tests can assert
    directly on the dict structures.

    When `out_md` is set, also emits the §10 markdown report.
    `include_llm_summary` adds the §10.K operator slot-filling section.
    `since_days` restricts the dossier to positions whose entry timestamp
    falls within the last N days of `fetched_at`; pre-window positions and
    pre-window activity are dropped before table construction.
    """
    fixture = json.loads(Path(fixture_path).read_text())
    report = json.loads(Path(protocol_report_path).read_text())

    if since_days is not None:
        fixture = filter_fixture_by_since_days(fixture, since_days)

    tables = build_tables(
        wallet=wallet, wallet_data=fixture, protocol_report=report
    )

    if out_db is not None:
        write_sqlite(tables, out_db)
    if out_json_dir is not None:
        write_json_export(tables, out_json_dir)
    if out_md is not None:
        # Imported lazily so the markdown renderer's import doesn't bloat
        # tests that only exercise table construction.
        from sniper.analysis.wallet_dossier.report_markdown import build_markdown
        md = build_markdown(
            tables=tables,
            protocol_report=report,
            wallet=wallet,
            include_llm_summary=include_llm_summary,
        )
        out_md_path = Path(out_md)
        out_md_path.parent.mkdir(parents=True, exist_ok=True)
        out_md_path.write_text(md)

    return tables


__all__ = [
    "DossierTables",
    "build_dossier",
    "build_tables",
    "filter_fixture_by_since_days",
    "write_sqlite",
    "write_json_export",
]

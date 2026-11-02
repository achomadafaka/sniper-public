import datetime as dt
import json
import logging
import sqlite3
import time
from typing import Any, Dict, List

from flask import Blueprint, jsonify, request
from db import ensure_paper_trades_duplicate_of_column, ensure_user_analysis_probabilistic_columns, ensure_wallet_quality_columns

from sniper.routes.contexts import ApiCoreContext
from sniper.web.serializers import serialize_position_signal_row, serialize_wallet_row

logger = logging.getLogger(__name__)
_INTEL_CACHE_TTL_S = 300.0


def _updated_at() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _open_positioned_condition_ids(conn: sqlite3.Connection) -> set:
    """Return condition_ids we already hold open positions on (paper + strategy).

    Used to suppress signals in the UI for markets we're already in — no point
    showing a signal as actionable when you're already tracking that market.
    """
    ensure_paper_trades_duplicate_of_column(conn)
    existing_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('paper_trades','strategy_positions')"
        ).fetchall()
    }
    parts = []
    if "paper_trades" in existing_tables:
        parts.append(
            "SELECT condition_id FROM paper_trades "
            "WHERE status = 'open' AND duplicate_of IS NULL AND condition_id IS NOT NULL"
        )
    if "strategy_positions" in existing_tables:
        parts.append(
            "SELECT condition_id FROM strategy_positions "
            "WHERE state = 'open' AND condition_id IS NOT NULL"
        )
    if not parts:
        return set()
    rows = conn.execute(" UNION ".join(parts)).fetchall()
    return {r[0] for r in rows}


def _format_usd_compact(value: Any) -> str:
    try:
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        amount = 0.0
    abs_amount = abs(amount)
    if abs_amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if abs_amount >= 1_000:
        return f"${amount / 1_000:.1f}k"
    return f"${amount:.0f}"


def create_api_core_blueprint(ctx: ApiCoreContext) -> Blueprint:
    bp = Blueprint("api_core", __name__)
    intel_cache: dict[Any, tuple[float, dict[str, Any]]] = {}

    def cached_intel_payload(key: Any, loader) -> tuple[dict[str, Any], float]:
        now = time.monotonic()
        cached = intel_cache.get(key)
        if cached and now - cached[0] < _INTEL_CACHE_TTL_S:
            return dict(cached[1]), max(0.0, now - cached[0])
        payload = dict(loader())
        if payload.get("available") is True:
            intel_cache[key] = (now, dict(payload))
        return payload, 0.0

    @bp.get("/api/flagged-users")
    def api_flagged_users():
        ignored = ctx["ignored_wallets"](ctx["conn"])
        rows = [row for row in ctx["flagged_users"](ctx["conn"], limit=1000) if ctx["normalize_wallet"](row["wallet"]) not in ignored]
        analysis_by_wallet: Dict[str, Any] = {}
        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)
        if wallets:
            placeholders = ",".join("?" for _ in wallets)
            for analysis_row in ctx["conn"].execute(
                f"""
                SELECT
                    wallet,
                    matches_criteria,
                    max_position_value,
                    top_open_position_value
                FROM user_analysis
                WHERE wallet IN ({placeholders})
                """,
                wallets,
            ).fetchall():
                analysis_by_wallet[str(analysis_row["wallet"])] = analysis_row

        data: List[Dict[str, Any]] = []
        for row in rows:
            wallet = str(row["wallet"] or "")
            analysis = analysis_by_wallet.get(wallet)
            if analysis is not None and not bool(int(analysis["matches_criteria"] or 0)):
                continue
            if ctx["HARD_FILTER_TOP_POSITION"]:
                max_pos = float(row["max_position_value"] or 0.0)
                top_pos = float(row["top_open_position_value"] or 0.0)
                if analysis is not None:
                    max_pos = float(analysis["max_position_value"] or max_pos)
                    top_pos = float(analysis["top_open_position_value"] or top_pos)
                if max(max_pos, top_pos) < ctx["MIN_UI_TOP_POSITION_VALUE"]:
                    continue
            identity = ctx["wallet_identity"](wallet, profiles_by_wallet)
            source_note = str(row["source_note"] or "")
            eligibility_blockers = ctx["eligibility_blockers_from_reason"](source_note)
            data.append(
                serialize_wallet_row(
                    row,
                    identity,
                    extra_fields={
                        "flagged_ts": row["flagged_ts"],
                        "flagged_at": ctx["format_unix"](row["flagged_ts"]),
                        "reason": source_note,
                        "eligibility_blockers": eligibility_blockers,
                    },
                )
            )

        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "items": data,
            }
        )

    @bp.get("/api/high-winrate-wallets")
    def api_high_winrate_wallets():
        ignored = ctx["ignored_wallets"](ctx["conn"])
        rows = ctx["high_winrate_wallets"](
            ctx["conn"],
            limit=1000,
            min_pnl=ctx["HIGH_WINRATE_MIN_PNL"],
            min_max_position_value=ctx["HIGH_WINRATE_MIN_MAX_POSITION_VALUE"],
        )
        rows = [row for row in rows if ctx["normalize_wallet"](row["wallet"]) not in ignored]
        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)
        data: List[Dict[str, Any]] = []
        for row in rows:
            wallet = str(row["wallet"] or "")
            identity = ctx["wallet_identity"](wallet, profiles_by_wallet)
            reason = str(row["reason"] or "")
            data.append(
                serialize_wallet_row(
                    row,
                    identity,
                    extra_fields={
                        "analyzed_ts": row["analyzed_ts"],
                        "analyzed_at": ctx["format_unix"](row["analyzed_ts"]),
                        "reason": reason,
                        "eligibility_blockers": ctx["eligibility_blockers_from_reason"](reason),
                    },
                )
            )
        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "items": data,
            }
        )

    @bp.get("/api/emerging-insiders")
    def api_emerging_insiders():
        ignored = ctx["ignored_wallets"](ctx["conn"])
        rows = ctx["emerging_insider_wallets"](
            ctx["conn"],
            limit=1000,
            min_insider_score=ctx["EMERGING_MIN_INSIDER_SCORE"],
            min_max_position_value=ctx["EMERGING_MIN_MAX_POSITION_VALUE"],
            min_total_open_value=ctx["EMERGING_MIN_TOTAL_OPEN_VALUE"],
        )
        rows = [row for row in rows if ctx["normalize_wallet"](row["wallet"]) not in ignored]
        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)
        data: List[Dict[str, Any]] = []
        for row in rows:
            wallet = str(row["wallet"] or "")
            identity = ctx["wallet_identity"](wallet, profiles_by_wallet)
            reason = str(row["reason"] or "")
            data.append(
                serialize_wallet_row(
                    row,
                    identity,
                    extra_fields={
                        "analyzed_ts": row["analyzed_ts"],
                        "analyzed_at": ctx["format_unix"](row["analyzed_ts"]),
                        "reason": reason,
                        "eligibility_blockers": ctx["eligibility_blockers_from_reason"](reason),
                    },
                )
            )
        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "items": data,
            }
        )

    @bp.get("/api/speculator-flow-movers")
    def api_speculator_flow_movers():
        ignored = ctx["ignored_wallets"](ctx["conn"])
        rows = ctx["speculator_flow_wallets"](
            ctx["conn"],
            limit=1000,
            min_speculator_score=ctx["FLOW_MOVER_MIN_SPECULATOR_SCORE"],
            min_position_speculator_score=ctx["FLOW_MOVER_MIN_POSITION_SPECULATOR_SCORE"],
            min_max_position_value=ctx["FLOW_MOVER_MIN_MAX_POSITION_VALUE"],
            min_total_open_value=ctx["FLOW_MOVER_MIN_TOTAL_OPEN_VALUE"],
            min_abs_pnl=ctx["FLOW_MOVER_MIN_ABS_PNL"],
        )
        rows = [row for row in rows if ctx["normalize_wallet"](row["wallet"]) not in ignored]
        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)
        data: List[Dict[str, Any]] = []
        for row in rows:
            wallet = str(row["wallet"] or "")
            identity = ctx["wallet_identity"](wallet, profiles_by_wallet)
            reason = str(row["reason"] or "")
            data.append(
                serialize_wallet_row(
                    row,
                    identity,
                    extra_fields={
                        "analyzed_ts": row["analyzed_ts"],
                        "analyzed_at": ctx["format_unix"](row["analyzed_ts"]),
                        "reason": reason,
                        "eligibility_blockers": ctx["eligibility_blockers_from_reason"](reason),
                    },
                )
            )
        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "items": data,
            }
        )

    @bp.get("/api/open-position-signals")
    def api_open_position_signals():
        ensure_user_analysis_probabilistic_columns(ctx["conn"])
        ensure_wallet_quality_columns(ctx["conn"])
        wallet = ctx["normalize_wallet"](request.args.get("wallet"))
        if wallet and not ctx["wallet_valid"](wallet):
            return jsonify({"error": "wallet must be a valid 0x address"}), 400
        ignored = ctx["ignored_wallets"](ctx["conn"])
        if wallet and wallet in ignored:
            return jsonify(
                {
                    "count": 0,
                    "updated_at": _updated_at(),
                    "items": [],
                }
            )

        classification = str(request.args.get("classification") or "").strip()
        if not classification:
            classification = None
        min_insider_score = ctx["optional_float"](request.args.get("min_insider_score"))
        focused_param = request.args.get("focused")
        if focused_param is None:
            focused_only = ctx["OPEN_SIGNALS_FOCUSED_DEFAULT"]
        else:
            focused_only = str(focused_param).strip().lower() in {"1", "true", "yes"}
        active_non_holding_only = str(request.args.get("active_non_holding") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        hide_low_signal = str(request.args.get("hide_low_signal") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
        limit = ctx["safe_int"](request.args.get("limit"), 1000)
        if limit <= 0:
            limit = 1000
        limit = min(limit, 5000)
        group_by_wallet = str(request.args.get("group_by_wallet") or "").strip().lower() in {
            "1",
            "true",
            "yes",
        }

        rows = ctx["position_signals"](
            ctx["conn"],
            wallet=wallet or None,
            classification=classification,
            min_insider_score=min_insider_score,
            limit=limit,
            open_only=True,
        )
        rows = [row for row in rows if ctx["normalize_wallet"](row["wallet"]) not in ignored]

        # Filter out markets we already hold a position on (paper or strategy),
        # unless caller explicitly passes ?show_positioned=1
        show_positioned = str(request.args.get("show_positioned") or "").strip().lower() in {"1", "true", "yes"}
        if not show_positioned:
            positioned = _open_positioned_condition_ids(ctx["conn"])
            rows = [row for row in rows if row["condition_id"] not in positioned]

        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)
        followed_position_map: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        followed_rows = ctx["followed_positions"](
            ctx["conn"],
            wallet=wallet or None,
            include_auto=True,
            limit=ctx["MAX_FOLLOWED_POSITION_UI_ROWS"] * 4,
        )
        for followed_row in followed_rows:
            key = ctx["followed_position_key"](
                str(followed_row["wallet"] or ""),
                str(followed_row["condition_id"] or ""),
                str(followed_row["outcome"] or ""),
            )
            followed_position_map[key] = dict(followed_row)
        analysis_by_wallet: Dict[str, Any] = {}
        if wallets:
            placeholders = ",".join("?" for _ in wallets)
            for analysis_row in ctx["conn"].execute(
                f"""
                SELECT
                    wallet,
                    win_rate,
                    wins,
                    losses,
                    closed_sample_size,
                    total_closed_count,
                    resolution_rate,
                    roi_percent,
                    qualifying_roi,
                    qualifying_pnl,
                    qualifying_capital,
                    effective_resolved_wins,
                    effective_resolved_losses,
                    account_age_days,
                    is_fresh_account,
                    tier,
                    classification,
                    position_count,
                    strategy_group,
                    strategy_confidence,
                    COALESCE(wallet_confidence_score, 0.0) AS wallet_confidence_score,
                    COALESCE(wallet_tier, 0) AS wallet_tier,
                    COALESCE(effective_copy_fraction, 1.0) AS effective_copy_fraction,
                    COALESCE(insider_score_trend, 0.0) AS insider_score_trend,
                    wallet_quality_score,
                    wallet_quality_eligible,
                    wallet_quality_fail_reason,
                    COALESCE(market_maker_detected, 0) AS market_maker_detected
                FROM user_analysis
                WHERE wallet IN ({placeholders})
                """,
                wallets,
            ).fetchall():
                analysis_by_wallet[str(analysis_row["wallet"] or "")] = analysis_row

        phase2_by_wallet: Dict[str, Any] = {}
        if wallets and "wallet_resolved_outcome_stats" in ctx:
            phase2_by_wallet = ctx["wallet_resolved_outcome_stats"](
                ctx["conn"],
                wallets,
                max_entry_price=0.80,
            )

        now_ts = int(dt.datetime.now(dt.UTC).timestamp())
        data: List[Dict[str, Any]] = []
        for row in rows:
            wallet_key = str(row["wallet"] or "")
            identity = ctx["wallet_identity"](wallet_key, profiles_by_wallet)
            analysis = analysis_by_wallet.get(wallet_key)
            profile = profiles_by_wallet.get(wallet_key)
            followed_row = followed_position_map.get(
                ctx["followed_position_key"](
                    wallet_key,
                    str(row["condition_id"] or ""),
                    str(row["outcome"] or ""),
                )
            )
            item = serialize_position_signal_row(
                row,
                identity,
                analysis,
                followed_row=followed_row,
            )
            profile_prediction_count = int(profile["profile_prediction_count"] or 0) if profile else 0
            profile_joined_ts = int(profile["profile_joined_ts"] or 0) if profile else 0
            item["profile_prediction_count"] = profile_prediction_count
            item["profile_joined_ts"] = profile_joined_ts
            item["profile_joined_at"] = ctx["format_unix"](profile_joined_ts) if profile_joined_ts > 0 else ""
            item["profile_account_age_days"] = (
                max(0.0, (now_ts - profile_joined_ts) / 86400.0) if profile_joined_ts > 0 else None
            )

            phase2 = phase2_by_wallet.get(wallet_key)
            if phase2:
                item["effective_resolved_wins"] = int(phase2.get("wins") or 0)
                item["effective_resolved_losses"] = int(phase2.get("losses") or 0)
                item["total_closed_count"] = int(phase2.get("closed_count") or 0)
                item["wins"] = int(phase2.get("wins") or 0)
                item["losses"] = int(phase2.get("losses") or 0)
                item["closed_sample_size"] = int(phase2.get("closed_count") or 0)
                phase2_wr = phase2.get("win_rate")
                item["win_rate"] = float(phase2_wr) if phase2_wr is not None else item.get("win_rate")
                item["win_rate_source"] = str(phase2.get("source") or "market_outcomes_phase2")
            else:
                item["win_rate_source"] = "user_analysis"

            if hide_low_signal and str(item.get("position_classification") or "") == "position_low_signal":
                continue
            if focused_only and not ctx["open_signal_is_focused"](item):
                continue
            if active_non_holding_only and not ctx["open_signal_is_active_non_holding"](item):
                continue
            data.append(item)

        raw_count = len(data)
        if group_by_wallet:
            grouped: Dict[str, Dict[str, Any]] = {}
            for item in data:
                wallet_key = str(item.get("wallet") or "")
                if not wallet_key:
                    continue
                current = grouped.get(wallet_key)
                if current is None:
                    merged = dict(item)
                    merged["position_group_count"] = 1
                    grouped[wallet_key] = merged
                    continue
                current["position_group_count"] = int(current.get("position_group_count") or 1) + 1
                current_score = float(current.get("position_insider_score") or 0.0)
                candidate_score = float(item.get("position_insider_score") or 0.0)
                current_value = float(current.get("position_value") or 0.0)
                candidate_value = float(item.get("position_value") or 0.0)
                current_ts = int(current.get("analyzed_ts") or 0)
                candidate_ts = int(item.get("analyzed_ts") or 0)
                if (candidate_score, candidate_value, candidate_ts) > (current_score, current_value, current_ts):
                    merged = dict(item)
                    merged["position_group_count"] = int(current.get("position_group_count") or 1)
                    grouped[wallet_key] = merged
            data = sorted(
                grouped.values(),
                key=lambda r: (
                    float(r.get("position_insider_score") or 0.0),
                    float(r.get("position_value") or 0.0),
                    int(r.get("analyzed_ts") or 0),
                ),
                reverse=True,
            )

        return jsonify(
            {
                "count": len(data),
                "raw_count": raw_count,
                "updated_at": _updated_at(),
                "focused_only": focused_only,
                "active_non_holding_only": active_non_holding_only,
                "hide_low_signal": hide_low_signal,
                "group_by_wallet": group_by_wallet,
                "items": data,
            }
        )

    @bp.get("/api/thesis-signals")
    def api_thesis_signals():
        limit = ctx["safe_int"](request.args.get("limit"), 200)
        if limit <= 0:
            limit = 200
        limit = min(limit, 1000)

        window_hours = ctx["safe_int"](
            request.args.get("window_hours"),
            ctx["THESIS_SIGNAL_WINDOW_HOURS"],
        )
        if window_hours <= 0:
            window_hours = ctx["THESIS_SIGNAL_WINDOW_HOURS"]

        max_entry_price = ctx["optional_float"](request.args.get("max_entry_price"))
        if max_entry_price is None:
            max_entry_price = ctx["THESIS_SIGNAL_MAX_ENTRY_PRICE"]

        min_position_value = ctx["optional_float"](request.args.get("min_position_value"))
        if min_position_value is None:
            min_position_value = ctx["THESIS_SIGNAL_MIN_POSITION_VALUE"]
        high_conviction_min_position_value = ctx["optional_float"](
            request.args.get("high_conviction_min_position_value")
        )
        if high_conviction_min_position_value is None:
            high_conviction_min_position_value = ctx["THESIS_SIGNAL_HIGH_CONVICTION_MIN_POSITION_VALUE"]

        max_account_age_days = ctx["optional_float"](request.args.get("max_account_age_days"))
        if max_account_age_days is None:
            max_account_age_days = ctx["THESIS_SIGNAL_MAX_ACCOUNT_AGE_DAYS"]

        max_lifetime_positions = ctx["safe_int"](
            request.args.get("max_lifetime_positions"),
            ctx["THESIS_SIGNAL_MAX_LIFETIME_POSITIONS"],
        )
        if max_lifetime_positions <= 0:
            max_lifetime_positions = ctx["THESIS_SIGNAL_MAX_LIFETIME_POSITIONS"]

        categories_param = str(request.args.get("categories") or "").strip()
        if categories_param:
            allowed_categories = [
                c.strip().lower()
                for c in categories_param.split(",")
                if c.strip()
            ]
        else:
            allowed_categories = list(ctx["THESIS_SIGNAL_ALLOWED_CATEGORIES"])

        exclude_speculator_bots = str(
            request.args.get("exclude_speculator_bots") or ""
        ).strip().lower() in {"1", "true", "yes"} if request.args.get("exclude_speculator_bots") is not None else bool(
            ctx["THESIS_SIGNAL_EXCLUDE_SPECULATOR_BOTS"]
        )
        single_market_only = str(
            request.args.get("single_market_only") or ""
        ).strip().lower() in {"1", "true", "yes"} if request.args.get("single_market_only") is not None else bool(
            ctx["THESIS_SIGNAL_SINGLE_MARKET_ONLY"]
        )

        rows = ctx["thesis_signal_rows"](
            ctx["conn"],
            window_hours=window_hours,
            max_entry_price=max_entry_price,
            min_position_value=min_position_value,
            max_account_age_days=max_account_age_days,
            max_lifetime_positions=max_lifetime_positions,
            allowed_categories=allowed_categories,
            exclude_speculator_bots=exclude_speculator_bots,
            single_market_only=single_market_only,
            limit=limit,
        )

        # Filter out markets we already hold a position on (paper or strategy)
        show_positioned = str(request.args.get("show_positioned") or "").strip().lower() in {"1", "true", "yes"}
        if not show_positioned:
            positioned = _open_positioned_condition_ids(ctx["conn"])
            rows = [row for row in rows if row["condition_id"] not in positioned]

        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)
        analysis_by_wallet: Dict[str, Any] = {}
        if wallets:
            placeholders = ",".join("?" for _ in wallets)
            for analysis_row in ctx["conn"].execute(
                f"""
                SELECT
                    wallet,
                    win_rate,
                    wins,
                    losses,
                    total_closed_count,
                    resolution_rate,
                    roi_percent,
                    qualifying_roi,
                    qualifying_pnl,
                    qualifying_capital,
                    closed_sample_size,
                    account_age_days,
                    is_fresh_account,
                    tier,
                    classification,
                    position_count,
                    strategy_group,
                    strategy_confidence,
                    wallet_quality_score,
                    wallet_quality_eligible,
                    wallet_quality_fail_reason,
                    COALESCE(market_maker_detected, 0) AS market_maker_detected
                FROM user_analysis
                WHERE wallet IN ({placeholders})
                """,
                wallets,
            ).fetchall():
                analysis_by_wallet[str(analysis_row["wallet"] or "")] = analysis_row

        data: List[Dict[str, Any]] = []
        for row in rows:
            wallet_key = str(row["wallet"] or "")
            identity = ctx["wallet_identity"](wallet_key, profiles_by_wallet)
            analysis = analysis_by_wallet.get(wallet_key)
            item = serialize_position_signal_row(row, identity, analysis, followed_row=None)
            item["market_category"] = str(row["market_category"] or "")
            item["market_category_source"] = str(row["market_category_source"] or "")
            item["open_positions_for_wallet"] = int(row["open_positions_for_wallet"] or 0)
            item["lifetime_position_count"] = int(row["lifetime_position_count"] or 0)
            item["lifetime_trade_count"] = int(row["lifetime_trade_count"] or 0)
            item["hours_to_end_at_entry"] = float(row["hours_to_end_at_entry"]) if row["hours_to_end_at_entry"] is not None else None
            item["thesis_gate_match"] = True
            item["high_conviction"] = float(row["position_value"] or 0.0) >= float(high_conviction_min_position_value)
            item["thesis_tier"] = "high_conviction" if item["high_conviction"] else "watch"
            data.append(item)
        high_conviction_count = sum(1 for item in data if bool(item.get("high_conviction")))

        return jsonify(
            {
                "count": len(data),
                "high_conviction_count": high_conviction_count,
                "updated_at": _updated_at(),
                "window_hours": window_hours,
                "max_entry_price": max_entry_price,
                "min_position_value": min_position_value,
                "high_conviction_min_position_value": high_conviction_min_position_value,
                "max_account_age_days": max_account_age_days,
                "max_lifetime_positions": max_lifetime_positions,
                "allowed_categories": allowed_categories,
                "exclude_speculator_bots": exclude_speculator_bots,
                "single_market_only": single_market_only,
                "items": data,
            }
        )

    @bp.get("/api/thesis-candidates")
    def api_thesis_candidates():
        limit = ctx["safe_int"](request.args.get("limit"), 50)
        if limit <= 0:
            limit = 50
        limit = min(limit, 50)

        window_hours = ctx["safe_int"](
            request.args.get("window_hours"),
            ctx["THESIS_SIGNAL_WINDOW_HOURS"],
        )
        if window_hours <= 0:
            window_hours = ctx["THESIS_SIGNAL_WINDOW_HOURS"]

        max_entry_price = ctx["optional_float"](request.args.get("max_entry_price"))
        if max_entry_price is None:
            max_entry_price = ctx["THESIS_SIGNAL_MAX_ENTRY_PRICE"]

        strict_min_position_value = ctx["optional_float"](
            request.args.get("strict_min_position_value")
        )
        if strict_min_position_value is None:
            strict_min_position_value = ctx["THESIS_SIGNAL_MIN_POSITION_VALUE"]

        candidate_min_position_value = ctx["optional_float"](
            request.args.get("candidate_min_position_value")
        )
        if candidate_min_position_value is None:
            candidate_min_position_value = 3000.0

        max_account_age_days = ctx["optional_float"](request.args.get("max_account_age_days"))
        if max_account_age_days is None:
            max_account_age_days = ctx["THESIS_SIGNAL_MAX_ACCOUNT_AGE_DAYS"]

        max_lifetime_positions = ctx["safe_int"](
            request.args.get("max_lifetime_positions"),
            ctx["THESIS_SIGNAL_MAX_LIFETIME_POSITIONS"],
        )
        if max_lifetime_positions <= 0:
            max_lifetime_positions = ctx["THESIS_SIGNAL_MAX_LIFETIME_POSITIONS"]

        categories_param = str(request.args.get("categories") or "").strip()
        if categories_param:
            allowed_categories = [
                c.strip().lower()
                for c in categories_param.split(",")
                if c.strip()
            ]
        else:
            allowed_categories = list(ctx["THESIS_SIGNAL_ALLOWED_CATEGORIES"])

        exclude_speculator_bots = str(
            request.args.get("exclude_speculator_bots") or ""
        ).strip().lower() in {"1", "true", "yes"} if request.args.get("exclude_speculator_bots") is not None else bool(
            ctx["THESIS_SIGNAL_EXCLUDE_SPECULATOR_BOTS"]
        )
        single_market_only = str(
            request.args.get("single_market_only") or ""
        ).strip().lower() in {"1", "true", "yes"} if request.args.get("single_market_only") is not None else bool(
            ctx["THESIS_SIGNAL_SINGLE_MARKET_ONLY"]
        )

        rows = ctx["thesis_candidate_rows"](
            ctx["conn"],
            window_hours=window_hours,
            max_entry_price=max_entry_price,
            candidate_min_position_value=candidate_min_position_value,
            strict_min_position_value=strict_min_position_value,
            max_account_age_days=max_account_age_days,
            max_lifetime_positions=max_lifetime_positions,
            allowed_categories=allowed_categories,
            exclude_speculator_bots=exclude_speculator_bots,
            single_market_only=single_market_only,
            limit=limit,
        )

        # Filter out markets we already hold a position on (paper or strategy)
        show_positioned = str(request.args.get("show_positioned") or "").strip().lower() in {"1", "true", "yes"}
        if not show_positioned:
            positioned = _open_positioned_condition_ids(ctx["conn"])
            rows = [row for row in rows if row["condition_id"] not in positioned]

        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)
        analysis_by_wallet: Dict[str, Any] = {}
        if wallets:
            placeholders = ",".join("?" for _ in wallets)
            for analysis_row in ctx["conn"].execute(
                f"""
                SELECT
                    wallet,
                    win_rate,
                    wins,
                    losses,
                    total_closed_count,
                    resolution_rate,
                    roi_percent,
                    qualifying_roi,
                    qualifying_pnl,
                    qualifying_capital,
                    closed_sample_size,
                    account_age_days,
                    is_fresh_account,
                    tier,
                    classification,
                    position_count,
                    strategy_group,
                    strategy_confidence,
                    wallet_quality_score,
                    wallet_quality_eligible,
                    wallet_quality_fail_reason,
                    COALESCE(market_maker_detected, 0) AS market_maker_detected
                FROM user_analysis
                WHERE wallet IN ({placeholders})
                """,
                wallets,
            ).fetchall():
                analysis_by_wallet[str(analysis_row["wallet"] or "")] = analysis_row

        data: List[Dict[str, Any]] = []
        for row in rows:
            wallet_key = str(row["wallet"] or "")
            identity = ctx["wallet_identity"](wallet_key, profiles_by_wallet)
            analysis = analysis_by_wallet.get(wallet_key)
            item = serialize_position_signal_row(row, identity, analysis, followed_row=None)
            item["market_category"] = str(row["market_category"] or "")
            item["market_category_source"] = str(row["market_category_source"] or "")
            item["open_positions_for_wallet"] = int(row["open_positions_for_wallet"] or 0)
            item["lifetime_position_count"] = int(row["lifetime_position_count"] or 0)
            item["lifetime_trade_count"] = int(row["lifetime_trade_count"] or 0)
            item["hours_to_end_at_entry"] = (
                float(row["hours_to_end_at_entry"]) if row["hours_to_end_at_entry"] is not None else None
            )
            item["thesis_gate_match"] = False
            item["high_conviction"] = False
            item["thesis_tier"] = "candidate"
            item["blocking_gate"] = "position_value"
            item["blocking_detail"] = (
                f"{_format_usd_compact(row['position_value'])} < "
                f"{_format_usd_compact(strict_min_position_value)}"
            )
            data.append(item)

        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "window_hours": window_hours,
                "max_entry_price": max_entry_price,
                "candidate_min_position_value": candidate_min_position_value,
                "strict_min_position_value": strict_min_position_value,
                "max_account_age_days": max_account_age_days,
                "max_lifetime_positions": max_lifetime_positions,
                "allowed_categories": allowed_categories,
                "exclude_speculator_bots": exclude_speculator_bots,
                "single_market_only": single_market_only,
                "items": data,
            }
        )

    @bp.get("/api/recent-suspicious-trades")
    def api_recent_suspicious_trades():
        ignored = set(ctx["ignored_wallets"](ctx["conn"]))

        now_ts = int(dt.datetime.now(dt.UTC).timestamp())
        data: List[Dict[str, Any]] = []
        min_notional = max(ctx["MIN_ORDER_NOTIONAL"], ctx["MIN_UI_ORDER_NOTIONAL"])
        rows = ctx["recent_low_buy_trades"](ctx["conn"], limit=ctx["MAX_UI_TRADES"] * 3)
        dismissed_hashes = _normalize_hash_set(
            row["transaction_hash"]
            for row in ctx["conn"].execute("SELECT transaction_hash FROM dismissed_suspicious_trades").fetchall()
        )
        deleted_hashes = _normalize_hash_set(
            row["transaction_hash"]
            for row in ctx["conn"].execute("SELECT transaction_hash FROM deleted_trades").fetchall()
        )
        prepared_rows: List[Dict[str, Any]] = []
        for row in rows:
            wallet = _normalize_wallet(row["proxy_wallet"])
            tx_hash = _normalize_hash(row["transaction_hash"])
            if not wallet or wallet in ignored:
                continue
            if tx_hash and tx_hash in dismissed_hashes:
                continue
            if tx_hash and tx_hash in deleted_hashes:
                continue

            market_volume_24h = float(row["market_volume_24h"] or 0.0)
            notional = float(row["notional"] or 0.0)
            if market_volume_24h <= 0 or notional < min_notional:
                continue
            impact_ratio = notional / market_volume_24h
            if impact_ratio < ctx["MIN_UI_IMPACT_RATIO"]:
                continue

            prepared_rows.append(
                {
                    "row": row,
                    "market_volume_24h": market_volume_24h,
                    "notional": notional,
                    "impact_ratio": impact_ratio,
                }
            )
            if len(prepared_rows) >= ctx["MAX_UI_TRADES"]:
                break

        wallets = sorted(
            {str(item["row"]["proxy_wallet"] or "") for item in prepared_rows if item["row"]["proxy_wallet"]}
        )
        analysis_by_wallet: Dict[str, Any] = {}
        if wallets:
            placeholders = ",".join("?" for _ in wallets)
            for analysis_row in ctx["conn"].execute(
                f"""
                SELECT
                    wallet,
                    suspicion_score,
                    insider_score,
                    speculator_score,
                    classification,
                    tier,
                    low_buy_hits_14d,
                    matches_criteria,
                    max_position_value,
                    total_open_value,
                    strategy_group,
                    strategy_confidence,
                    strategy_note,
                    top_open_position_title,
                    top_open_position_value,
                    wins,
                    losses,
                    closed_sample_size,
                    win_rate,
                    high_winrate_qualified,
                    account_age_days,
                    is_fresh_account,
                    position_signal_count,
                    position_insider_candidate_count,
                    position_speculator_count,
                    max_position_insider_score,
                    max_position_speculator_score,
                    reason
                FROM user_analysis
                WHERE wallet IN ({placeholders})
                """,
                wallets,
            ).fetchall():
                analysis_by_wallet[str(analysis_row["wallet"])] = analysis_row
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)

        followed_tx_hashes = {
            _normalize_hash(db_row["transaction_hash"])
            for db_row in ctx["conn"].execute("SELECT transaction_hash FROM followed_trades").fetchall()
            if db_row["transaction_hash"]
        }

        eligible_rows: List[Dict[str, Any]] = []
        for item in prepared_rows:
            row = item["row"]
            wallet = str(row["proxy_wallet"] or "")
            analysis = analysis_by_wallet.get(wallet)
            if ctx["HARD_FILTER_TOP_POSITION"]:
                if analysis is None:
                    continue
                analysis_max_position = float(analysis["max_position_value"] or 0.0)
                analysis_top_position = float(analysis["top_open_position_value"] or 0.0)
                if max(analysis_max_position, analysis_top_position) < ctx["MIN_UI_TOP_POSITION_VALUE"]:
                    continue
            item["analysis"] = analysis
            eligible_rows.append(item)

        eligible_wallets = sorted(
            {str(item["row"]["proxy_wallet"] or "") for item in eligible_rows if item["row"]["proxy_wallet"]}
        )
        wallet_latest_ts: Dict[str, int] = {}
        for item in eligible_rows:
            row = item["row"]
            wallet = str(row["proxy_wallet"] or "")
            if not wallet:
                continue
            ts = ctx["safe_int"](row["timestamp"])
            prev_ts = wallet_latest_ts.get(wallet, 0)
            if ts > prev_ts:
                wallet_latest_ts[wallet] = ts
        wallets_for_sync = sorted(eligible_wallets, key=lambda w: wallet_latest_ts.get(w, 0), reverse=True)

        wallet_position_maps: Dict[str, Dict[str, Any]] = {}
        fetch_budget = max(0, ctx["SUSPICIOUS_STATUS_MAX_WALLET_FETCHES_PER_REQUEST"])
        for wallet in wallets_for_sync:
            if not wallet:
                continue
            cached = ctx["get_suspicious_status_cache"](wallet)
            if cached and now_ts - int(cached.get("synced_ts") or 0) < ctx["SUSPICIOUS_STATUS_SYNC_MIN_INTERVAL_SECONDS"]:
                wallet_position_maps[wallet] = cached
                continue

            if fetch_budget <= 0:
                if cached:
                    wallet_position_maps[wallet] = cached
                continue

            try:
                open_by_key, closed_by_key = ctx["fetch_wallet_position_maps"](wallet)
                payload = {
                    "open_by_key": open_by_key,
                    "closed_by_key": closed_by_key,
                    "synced_ts": now_ts,
                }
                ctx["set_suspicious_status_cache"](
                    wallet,
                    open_by_key=open_by_key,
                    closed_by_key=closed_by_key,
                    synced_ts=now_ts,
                )
                wallet_position_maps[wallet] = payload
                fetch_budget -= 1
            except Exception:
                if cached:
                    wallet_position_maps[wallet] = cached

        wallet_pattern_maps: Dict[str, Dict[str, Any]] = {}
        pattern_fetch_budget = max(0, ctx["SUSPICIOUS_PATTERN_MAX_WALLET_FETCHES_PER_REQUEST"])
        for wallet in wallets_for_sync:
            if not wallet:
                continue
            cached = ctx["get_suspicious_pattern_cache"](wallet)
            if cached and now_ts - int(cached.get("synced_ts") or 0) < ctx["SUSPICIOUS_PATTERN_SYNC_MIN_INTERVAL_SECONDS"]:
                wallet_pattern_maps[wallet] = cached
                continue

            if pattern_fetch_budget <= 0:
                if cached:
                    wallet_pattern_maps[wallet] = cached
                continue

            try:
                patterns_by_trade_id = ctx["fetch_wallet_order_pattern_map"](
                    wallet, limit=ctx["SUSPICIOUS_PATTERN_TRADE_FETCH_LIMIT"]
                )
                payload = {
                    "patterns_by_trade_id": patterns_by_trade_id,
                    "synced_ts": now_ts,
                }
                ctx["set_suspicious_pattern_cache"](
                    wallet,
                    patterns_by_trade_id=patterns_by_trade_id,
                    synced_ts=now_ts,
                )
                wallet_pattern_maps[wallet] = payload
                pattern_fetch_budget -= 1
            except Exception:
                if cached:
                    wallet_pattern_maps[wallet] = cached

        position_signal_by_key: Dict[tuple[str, str, str], Dict[str, Any]] = {}
        if eligible_wallets:
            placeholders = ",".join("?" for _ in eligible_wallets)
            for signal_row in ctx["conn"].execute(
                f"""
                SELECT
                    wallet,
                    condition_id,
                    outcome,
                    position_classification,
                    position_insider_score,
                    position_speculator_score,
                    signal_reason,
                    analyzed_ts
                FROM position_signals
                WHERE is_open = 1
                  AND wallet IN ({placeholders})
                ORDER BY analyzed_ts DESC
                """,
                eligible_wallets,
            ).fetchall():
                key = (
                    _normalize_wallet(signal_row["wallet"]),
                    str(signal_row["condition_id"] or "").strip().lower(),
                    str(signal_row["outcome"] or "").strip().lower(),
                )
                if key in position_signal_by_key:
                    continue
                position_signal_by_key[key] = {
                    "position_classification": str(signal_row["position_classification"] or "position_low_signal"),
                    "position_insider_score": float(signal_row["position_insider_score"] or 0.0),
                    "position_speculator_score": float(signal_row["position_speculator_score"] or 0.0),
                    "position_signal_reason": str(signal_row["signal_reason"] or ""),
                    "position_signal_analyzed_ts": int(signal_row["analyzed_ts"] or 0),
                }

        for item in eligible_rows:
            row = item["row"]
            market_volume_24h = item["market_volume_24h"]
            notional = item["notional"]
            impact_ratio = item["impact_ratio"]
            wallet = str(row["proxy_wallet"] or "")
            identity = ctx["wallet_identity"](wallet, profiles_by_wallet)
            analysis = item.get("analysis")
            tx_hash = _normalize_hash(row["transaction_hash"])
            is_followed_trade = bool(tx_hash and tx_hash in followed_tx_hashes)

            position_snapshot = wallet_position_maps.get(wallet)
            status = ctx["resolve_followed_status"](open_position=None, closed_position=None)
            status_synced_ts = 0
            if position_snapshot:
                key = ctx["position_key"](row["condition_id"], row["outcome"])
                status = ctx["resolve_followed_status"](
                    open_position=position_snapshot.get("open_by_key", {}).get(key),
                    closed_position=position_snapshot.get("closed_by_key", {}).get(key),
                )
                status_synced_ts = int(position_snapshot.get("synced_ts") or 0)
            else:
                status["status_reason"] = "Status sync pending for this wallet"

            if bool(status["is_closed"]) and not is_followed_trade:
                continue

            position_signal = position_signal_by_key.get(
                (
                    _normalize_wallet(wallet),
                    str(row["condition_id"] or "").strip().lower(),
                    str(row["outcome"] or "").strip().lower(),
                )
            )
            analysis_reason = str(analysis["reason"] or "") if analysis else ""
            eligibility_blockers = ctx["eligibility_blockers_from_reason"](analysis_reason)

            data.append(
                {
                    "transaction_hash": row["transaction_hash"],
                    "condition_id": row["condition_id"],
                    "event_slug": row["event_slug"],
                    "timestamp": row["timestamp"],
                    "traded_at": ctx["format_unix"](row["timestamp"]),
                    "age_seconds": max(0, now_ts - int(row["timestamp"] or 0)),
                    "wallet": wallet,
                    "username": identity["username"],
                    "pseudonym": identity["pseudonym"],
                    "profile_image": identity["profile_image"],
                    "side": row["side"],
                    "price": row["price"],
                    "size": row["size"],
                    "notional": notional,
                    "impact_ratio": impact_ratio,
                    "market_volume_24h": market_volume_24h,
                    "title": row["title"],
                    "outcome": row["outcome"],
                    "bet_direction": ctx["bet_direction"](row["outcome"]),
                    "source": row["source"],
                    "trigger_type": row["trigger_type"] or "low_price_entry",
                    "trigger_reason": row["trigger_reason"] or "",
                    "suspicion_score": float(analysis["suspicion_score"] or 0.0) if analysis else 0.0,
                    "insider_score": float(analysis["insider_score"] or 0.0) if analysis else 0.0,
                    "speculator_score": float(analysis["speculator_score"] or 0.0) if analysis else 0.0,
                    "classification": str(analysis["classification"] or "low_signal") if analysis else "low_signal",
                    "tier": str(analysis["tier"] or "") if analysis else "",
                    "strategy_group": str(analysis["strategy_group"] or "") if analysis else "",
                    "strategy_confidence": float(analysis["strategy_confidence"] or 0.0) if analysis else 0.0,
                    "strategy_note": str(analysis["strategy_note"] or "") if analysis else "",
                    "reason": analysis_reason,
                    "eligibility_blockers": eligibility_blockers,
                    "low_buy_hits_14d": int(analysis["low_buy_hits_14d"] or 0) if analysis else 0,
                    "wins": int(analysis["wins"] or 0) if analysis else 0,
                    "losses": int(analysis["losses"] or 0) if analysis else 0,
                    "closed_sample_size": int(analysis["closed_sample_size"] or 0) if analysis else 0,
                    "win_rate": float(analysis["win_rate"] or 0.0) if analysis else 0.0,
                    "high_winrate_qualified": bool(int(analysis["high_winrate_qualified"] or 0)) if analysis else False,
                    "account_age_days": float(analysis["account_age_days"] or 99999.0) if analysis else 99999.0,
                    "is_fresh_account": bool(int(analysis["is_fresh_account"] or 0)) if analysis else False,
                    "position_signal_count": int(analysis["position_signal_count"] or 0) if analysis else 0,
                    "position_insider_candidate_count": int(analysis["position_insider_candidate_count"] or 0)
                    if analysis
                    else 0,
                    "position_speculator_count": int(analysis["position_speculator_count"] or 0) if analysis else 0,
                    "max_position_insider_score": float(analysis["max_position_insider_score"] or 0.0) if analysis else 0.0,
                    "max_position_speculator_score": float(analysis["max_position_speculator_score"] or 0.0)
                    if analysis
                    else 0.0,
                    "is_flagged": bool(int(analysis["matches_criteria"] or 0)) if analysis else False,
                    "max_position_value": float(analysis["max_position_value"] or 0.0) if analysis else 0.0,
                    "total_open_value": float(analysis["total_open_value"] or 0.0) if analysis else 0.0,
                    "top_open_position_title": str(analysis["top_open_position_title"] or "") if analysis else "",
                    "top_open_position_value": float(analysis["top_open_position_value"] or 0.0) if analysis else 0.0,
                    "position_classification": str(position_signal["position_classification"])
                    if position_signal
                    else "position_low_signal",
                    "position_insider_score": float(position_signal["position_insider_score"]) if position_signal else 0.0,
                    "position_speculator_score": float(position_signal["position_speculator_score"])
                    if position_signal
                    else 0.0,
                    "position_signal_reason": str(position_signal["position_signal_reason"]) if position_signal else "",
                    "position_signal_analyzed_ts": int(position_signal["position_signal_analyzed_ts"])
                    if position_signal
                    else 0,
                    "position_signal_analyzed_at": ctx["format_unix"](position_signal["position_signal_analyzed_ts"])
                    if position_signal
                    else "",
                    "live_status": str(status["status"]),
                    "live_status_label": str(status["status_label"]),
                    "live_status_reason": str(status["status_reason"]),
                    "live_status_rank": int(status["status_rank"]),
                    "live_is_closed": bool(status["is_closed"]),
                    "live_is_win": status["is_win"],
                    "live_exited_early": status["exited_early"],
                    "live_price": status["live_price"],
                    "live_exit_price": status["exit_price"],
                    "live_display_price": status["live_price"] if status["live_price"] is not None else status["exit_price"],
                    "live_current_value": float(status["current_value"] or 0.0),
                    "live_realized_pnl": float(status["realized_pnl"] or 0.0),
                    "live_close_ts": status["close_ts"],
                    "live_close_at": ctx["format_unix"](status["close_ts"]),
                    "live_event_end_ts": status["event_end_ts"],
                    "live_event_end_at": ctx["format_unix"](status["event_end_ts"]),
                    "live_status_synced_ts": status_synced_ts,
                    "live_status_synced_at": ctx["format_unix"](status_synced_ts),
                    "is_followed_trade": is_followed_trade,
                    "order_pattern_source": "suspicious_subset",
                    "order_pattern_synced_ts": 0,
                    "order_pattern_synced_at": "",
                }
            )
        ctx["annotate_order_patterns"](data)
        for trade in data:
            wallet = _normalize_wallet(trade.get("wallet"))
            snapshot = wallet_pattern_maps.get(wallet)
            if not snapshot:
                continue
            pattern_row = snapshot.get("patterns_by_trade_id", {}).get(ctx["trade_row_id"](trade))
            if not pattern_row:
                continue
            trade.update(pattern_row)
            synced_ts = int(snapshot.get("synced_ts") or 0)
            trade["order_pattern_source"] = "wallet_raw_stream"
            trade["order_pattern_synced_ts"] = synced_ts
            trade["order_pattern_synced_at"] = ctx["format_unix"](synced_ts)

        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "items": data,
            }
        )

    @bp.get("/api/wallet-leaderboard")
    def api_wallet_leaderboard():
        window = str(request.args.get("window") or "30d").strip().lower()
        if window not in {"30d", "90d", "all"}:
            return jsonify({"error": "window must be one of: 30d, 90d, all"}), 400
        sort_by = str(request.args.get("sort") or "qualifying_roi").strip().lower()
        allowed_sorts = {"qualifying_roi", "roi", "pnl", "dollars_per_trade", "win_rate"}
        if sort_by not in allowed_sorts:
            return jsonify(
                {
                    "error": (
                        "sort must be one of: "
                        "qualifying_roi, roi, pnl, dollars_per_trade, win_rate"
                    )
                }
            ), 400
        limit = ctx["safe_int"](request.args.get("limit"), 500)
        if limit <= 0:
            limit = 500
        limit = min(limit, 5000)
        quality_only_param = request.args.get("quality_only")
        if quality_only_param is None:
            quality_only = True
        else:
            quality_only = str(quality_only_param).strip().lower() in {"1", "true", "yes"}

        ignored = ctx["ignored_wallets"](ctx["conn"])
        rows = ctx["wallet_leaderboard_rows"](
            ctx["conn"],
            window=window,
            sort_by=sort_by,
            quality_only=quality_only,
            limit=limit,
        )
        metrics_source = "user_analysis_phase1"
        window_mode = "activity_filter_only"
        if rows:
            first = rows[0]
            if "metrics_source" in first.keys():
                metrics_source = str(first["metrics_source"] or metrics_source)
            if metrics_source == "market_outcomes_phase2":
                window_mode = "resolved_entry_window"
        rows = [row for row in rows if ctx["normalize_wallet"](row["wallet"]) not in ignored]
        wallets = [str(row["wallet"] or "") for row in rows if row["wallet"]]
        profiles_by_wallet = ctx["wallet_profiles_map"](ctx["conn"], wallets)
        data: List[Dict[str, Any]] = []
        for row in rows:
            wallet = str(row["wallet"] or "")
            identity = ctx["wallet_identity"](wallet, profiles_by_wallet)
            reason = str(row["reason"] or "")
            data.append(
                serialize_wallet_row(
                    row,
                    identity,
                    extra_fields={
                        "analyzed_ts": row["analyzed_ts"],
                        "analyzed_at": ctx["format_unix"](row["analyzed_ts"]),
                        "reason": reason,
                        "eligibility_blockers": ctx["eligibility_blockers_from_reason"](reason),
                        "raw_trade_count": int(row["raw_trade_count"] or row["trade_count"] or 0),
                        "qualifying_trade_count": int(row["qualifying_trade_count"] or row["closed_sample_size"] or 0),
                        "dollars_per_trade": row["dollars_per_trade"],
                        "trades_per_position": row["avg_trades_per_position"],
                        "raw_trade_count_window": int(row["raw_trade_count_window"] or 0),
                        "last_trade_ts_window": int(row["last_trade_ts_window"] or 0),
                        "last_trade_at_window": ctx["format_unix"](row["last_trade_ts_window"]),
                    },
                )
            )
        return jsonify(
            {
                "count": len(data),
                "updated_at": _updated_at(),
                "window": window,
                "sort": sort_by,
                "quality_only": quality_only,
                "metrics_source": metrics_source,
                "window_mode": window_mode,
                "items": data,
            }
        )

    # ------------------------------------------------------------------
    # Leaderboard V2 — official discovery + internal proof
    # ------------------------------------------------------------------

    _V2_CATEGORIES = {"OVERALL", "POLITICS", "SPORTS", "CRYPTO", "CULTURE", "ECONOMICS", "TECH", "FINANCE"}
    _V2_TIME_PERIODS = {"ALL", "MONTH", "WEEK", "DAY"}
    _V2_SORTS = {"official_pnl", "official_volume", "fit_roi", "win_rate"}

    @bp.get("/api/wallet-leaderboard-v2")
    def api_wallet_leaderboard_v2():
        category = str(request.args.get("category") or "OVERALL").strip().upper()
        if category not in _V2_CATEGORIES:
            return jsonify({"error": f"category must be one of: {', '.join(sorted(_V2_CATEGORIES))}"}), 400
        time_period = str(request.args.get("time_period") or "ALL").strip().upper()
        if time_period not in _V2_TIME_PERIODS:
            return jsonify({"error": f"time_period must be one of: {', '.join(sorted(_V2_TIME_PERIODS))}"}), 400
        sort_by = str(request.args.get("sort") or "official_pnl").strip().lower()
        if sort_by not in _V2_SORTS:
            return jsonify({"error": f"sort must be one of: {', '.join(sorted(_V2_SORTS))}"}), 400
        limit = max(1, min(500, ctx["safe_int"](request.args.get("limit"), 100)))
        include_unqualified = str(request.args.get("include_unqualified") or "true").strip().lower() != "false"

        qualified, unqualified, meta = ctx["wallet_leaderboard_v2_rows"](
            ctx["conn"],
            category=category,
            time_period=time_period,
            sort_by=sort_by,
            limit=limit,
            include_unqualified=include_unqualified,
        )

        # If data is stale or empty, enqueue a background refresh
        if meta.get("official_data_stale"):
            ctx["enqueue_background_sync"](
                "external_leaderboard",
                ctx["background_sync_external_leaderboard"],
            )

        return jsonify({
            "items": qualified,
            "unqualified_items": unqualified,
            "meta": meta,
        })

    # ------------------------------------------------------------------
    # Followable leaderboard (V3)
    # ------------------------------------------------------------------

    @bp.get("/api/wallet-followable-leaderboard")
    def api_wallet_followable_leaderboard():
        limit = max(1, min(500, ctx["safe_int"](request.args.get("limit"), 100)))
        include_filtered = str(request.args.get("include_filtered") or "true").strip().lower() != "false"
        filtered_limit = max(0, min(500, ctx["safe_int"](request.args.get("filtered_limit"), 200)))
        min_sample = max(1, ctx["safe_int"](request.args.get("min_sample"), 20))

        raw_min_cap = ctx["optional_float"](request.args.get("min_capital"))
        min_capital = max(0.0, raw_min_cap) if raw_min_cap is not None else 5000.0
        raw_max_cap = ctx["optional_float"](request.args.get("max_capital"))
        max_capital = max(min_capital, raw_max_cap) if raw_max_cap is not None else 250000.0
        raw_min_wr = ctx["optional_float"](request.args.get("min_win_rate"))
        min_win_rate = max(0.0, min(1.0, raw_min_wr)) if raw_min_wr is not None else 0.55
        activity_window_days = max(1, ctx["safe_int"](request.args.get("activity_window_days"), 30))

        items, filtered_items, meta = ctx["wallet_followable_leaderboard_rows"](
            ctx["conn"],
            limit=limit,
            include_filtered=include_filtered,
            filtered_limit=filtered_limit,
            min_sample=min_sample,
            min_capital=min_capital,
            max_capital=max_capital,
            min_win_rate=min_win_rate,
            activity_window_days=activity_window_days,
        )
        return jsonify({
            "items": items,
            "filtered_items": filtered_items,
            "meta": meta,
        })

    @bp.get("/api/convergence-signals")
    def api_convergence_signals():
        from db import market_convergence_signals, market_convergence_detail, convergence_from_history

        min_insider = ctx["optional_float"](request.args.get("min_insider_score")) or 55.0
        min_wallets = int(request.args.get("min_wallets") or 2)
        limit = min(ctx["safe_int"](request.args.get("limit"), 50), 200)
        detail_cid = request.args.get("condition_id")
        detail_outcome = request.args.get("outcome")
        source = request.args.get("source", "live")  # "live" | "history"

        if detail_cid:
            rows = market_convergence_detail(
                ctx["conn"], detail_cid,
                outcome=detail_outcome,
                min_insider_score=min_insider,
            )
            for row in rows:
                row["entry_anchor_at"] = ctx["format_unix"](row.get("entry_anchor_ts"))
                row["analyzed_at"] = ctx["format_unix"](row.get("analyzed_ts"))
                display = (row.get("pseudonym") or "").strip()
                if not display:
                    display = (row.get("username") or "").strip()
                if not display or display.startswith("0x"):
                    w = row.get("wallet") or ""
                    display = w[:6] + "…" + w[-4:] if len(w) > 10 else w
                row["display_name"] = display
            return jsonify({"items": rows, "updated_at": _updated_at()})

        # History source: uses signal_history with immutable entry_anchor_ts
        if source == "history":
            max_window = ctx["optional_float"](request.args.get("max_window_hours")) or 168.0
            result = convergence_from_history(
                ctx["conn"],
                min_insider_score=min_insider,
                min_wallets=min_wallets,
                max_window_hours=max_window,
                limit=limit,
            )
            profiles_cache = {}
            for m in result["items"]:
                wallet_list = [w.strip() for w in (m.get("wallet_list") or "").split(",") if w.strip()]
                if wallet_list and not profiles_cache:
                    all_wallets = set()
                    for mm in result["items"]:
                        for w in (mm.get("wallet_list") or "").split(","):
                            w = w.strip()
                            if w:
                                all_wallets.add(w)
                    profiles_cache = ctx["wallet_profiles_map"](ctx["conn"], list(all_wallets))
                names = []
                for w in wallet_list:
                    p = profiles_cache.get(w, {})
                    name = (dict(p).get("pseudonym") or "").strip() if p else ""
                    if not name:
                        name = (dict(p).get("username") or "").strip() if p else ""
                    if not name or name.startswith("0x"):
                        name = w[:6] + "…" + w[-4:] if len(w) > 10 else w
                    names.append(name)
                m["wallet_names"] = names
                m["earliest_anchor_at"] = ctx["format_unix"](m.get("earliest_anchor_ts"))
                m["latest_anchor_at"] = ctx["format_unix"](m.get("latest_anchor_ts"))
            return jsonify({
                "count": len(result["items"]),
                "items": result["items"],
                "coverage": result["coverage"],
                "source": "history",
                "updated_at": _updated_at(),
            })

        # Default: live source using position_signals
        markets = market_convergence_signals(
            ctx["conn"],
            min_insider_score=min_insider,
            min_wallets=min_wallets,
            limit=limit,
        )
        profiles_cache = {}
        for m in markets:
            wallet_list = [w.strip() for w in (m.get("wallet_list") or "").split(",") if w.strip()]
            if wallet_list and not profiles_cache:
                all_wallets = set()
                for mm in markets:
                    for w in (mm.get("wallet_list") or "").split(","):
                        w = w.strip()
                        if w:
                            all_wallets.add(w)
                profiles_cache = ctx["wallet_profiles_map"](ctx["conn"], list(all_wallets))
            names = []
            for w in wallet_list:
                p = profiles_cache.get(w, {})
                name = (dict(p).get("pseudonym") or "").strip() if p else ""
                if not name:
                    name = (dict(p).get("username") or "").strip() if p else ""
                if not name or name.startswith("0x"):
                    name = w[:6] + "…" + w[-4:] if len(w) > 10 else w
                names.append(name)
            m["wallet_names"] = names
            m["latest_analyzed_at"] = ctx["format_unix"](m.get("latest_analyzed_ts"))
        return jsonify({
            "count": len(markets),
            "items": markets,
            "source": "live",
            "updated_at": _updated_at(),
        })

    @bp.get("/api/market-intelligence")
    def api_market_intelligence():
        from db import market_intelligence_top_markets, market_intelligence_domain_summary, _classify_title_domain

        min_insider = ctx["optional_float"](request.args.get("min_insider_score")) or 0.0
        open_only = str(request.args.get("open_only") or "").strip().lower() in {"1", "true", "yes"}
        limit = min(ctx["safe_int"](request.args.get("limit"), 30), 100)

        top_markets = market_intelligence_top_markets(
            ctx["conn"],
            min_insider_score=min_insider,
            open_only=open_only,
            limit=limit,
        )
        for m in top_markets:
            m["domain"] = _classify_title_domain(m.get("title") or "")
            m["latest_analyzed_at"] = ctx["format_unix"](m.get("latest_analyzed_ts"))

        domain_summary = market_intelligence_domain_summary(
            ctx["conn"],
            min_insider_score=min_insider,
            open_only=open_only,
        )

        return jsonify({
            "top_markets": top_markets,
            "domain_summary": domain_summary,
            "updated_at": _updated_at(),
        })

    @bp.get("/api/system-stats")
    @bp.get("/api/proof-bar")   # keep old URL as alias
    def api_system_stats():
        from db import connection_scope
        import json
        from datetime import datetime, timezone

        conn = ctx["conn"]
        ensure_paper_trades_duplicate_of_column(conn)
        now = datetime.now(timezone.utc)

        # --- paper_trades closed ---
        pt_rows = conn.execute("""
            SELECT pnl_usdc, roi_percent, size_usdc, entry_price, status,
                   signal_insider_score, closed_at
            FROM paper_trades
            WHERE status IN ('resolution_win','resolution_loss','early_exit','manual')
              AND duplicate_of IS NULL AND entry_price > 0
        """).fetchall()

        # --- strategy_positions closed ---
        sp_rows = conn.execute("""
            SELECT sp.realized_pnl, sp.avg_entry_price, sp.entry_shares,
                   sp.state, sp.closed_ts,
                   s.strategy_type, s.label
            FROM strategy_positions sp
            JOIN strategies s ON CAST(sp.strategy_id AS TEXT) = CAST(s.id AS TEXT)
            WHERE sp.state = 'closed'
        """).fetchall()

        # --- open counts ---
        open_paper = conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE status='open'"
        ).fetchone()[0]
        open_strat = conn.execute(
            "SELECT COUNT(*) FROM strategy_positions WHERE state='open'"
        ).fetchone()[0]
        open_total = open_paper + open_strat

        # --- active strategies ---
        active_db_strats = conn.execute(
            "SELECT COUNT(*) FROM strategies WHERE status='active'"
        ).fetchone()[0]
        # The strategies UI always prepends a synthetic paper-trader card in the
        # unfiltered active view, so system_stats should report the same count the
        # operator dashboard sees instead of the raw table-only strategy count.
        active_strats = int(active_db_strats or 0) + 1

        # Browser-safe shell count. The detailed /api/agent/alerts feed remains API-key protected;
        # the authenticated dashboard only needs this aggregate for its global status chip.
        has_alert_outbox = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alerts_outbox'"
        ).fetchone()
        pending_alert_count = int(conn.execute(
            "SELECT COUNT(*) FROM alerts_outbox WHERE status IN ('new', 'sent')"
        ).fetchone()[0]) if has_alert_outbox else 0

        # Combine into unified trade list for stats
        all_pnl = []
        resolved_wins = 0
        resolved_losses = 0

        SLIP_PCT = 0.025
        slip_pnl = 0.0

        for r in pt_rows:
            pnl = r["pnl_usdc"] or 0
            roi = r["roi_percent"]
            size = r["size_usdc"] or 0
            ep = r["entry_price"] or 0
            status = r["status"]
            all_pnl.append(pnl)
            # Resolved-only win rate: only resolution_win / resolution_loss
            if status == "resolution_win":
                resolved_wins += 1
            elif status == "resolution_loss":
                resolved_losses += 1
            if roi is not None:
                slip_pnl += (roi / 100) * size - size * SLIP_PCT

        for r in sp_rows:
            pnl = r["realized_pnl"] or 0
            ep = r["avg_entry_price"] or 0
            shares = r["entry_shares"] or 0
            cost = ep * shares
            all_pnl.append(pnl)
            # Strategy positions count as resolved (win/loss by PnL sign)
            if pnl > 0:
                resolved_wins += 1
            elif pnl < 0:
                resolved_losses += 1
            roi_pct = (pnl / cost * 100) if cost > 0 else None
            if roi_pct is not None:
                slip_pnl += (roi_pct / 100) * cost - cost * SLIP_PCT

        closed_trades = len(all_pnl)
        resolved_denom = resolved_wins + resolved_losses
        win_rate = round(resolved_wins / resolved_denom * 100, 1) if resolved_denom > 0 else 0.0

        total_pnl = round(sum(all_pnl), 2)
        slip_pnl = round(slip_pnl, 2)

        # Monthly breakdown — paper_trades + strategy_positions
        month_buckets: dict = {}
        for r in pt_rows:
            closed_at = r["closed_at"]
            if not closed_at:
                continue
            mk = str(closed_at)[:7]
            month_buckets.setdefault(mk, {"pnl": 0, "wins": 0, "total": 0})
            month_buckets[mk]["pnl"] += r["pnl_usdc"] or 0
            month_buckets[mk]["total"] += 1
            if (r["pnl_usdc"] or 0) > 0:
                month_buckets[mk]["wins"] += 1

        # Add strategy_positions to monthly buckets using closed_ts (Unix epoch)
        for r in sp_rows:
            closed_ts = r["closed_ts"]
            if not closed_ts:
                continue
            try:
                mk = datetime.fromtimestamp(int(closed_ts), tz=timezone.utc).strftime("%Y-%m")
            except Exception:
                continue
            month_buckets.setdefault(mk, {"pnl": 0, "wins": 0, "total": 0})
            pnl = r["realized_pnl"] or 0
            month_buckets[mk]["pnl"] += pnl
            month_buckets[mk]["total"] += 1
            if pnl > 0:
                month_buckets[mk]["wins"] += 1

        monthly = []
        for mk in sorted(month_buckets):
            b = month_buckets[mk]
            try:
                y, m = int(mk[:4]), int(mk[5:7])
                first_next = datetime(y + 1, 1, 1, tzinfo=timezone.utc) if m == 12 else datetime(y, m + 1, 1, tzinfo=timezone.utc)
                complete = now >= first_next
            except Exception:
                complete = False
            monthly.append({
                "month": mk,
                "trades": b["total"],
                "wins": b["wins"],
                "losses": b["total"] - b["wins"],
                "win_rate": round(b["wins"] / b["total"] * 100, 1) if b["total"] else 0,
                "pnl": round(b["pnl"], 2),
                "complete": complete,
                "profitable": b["pnl"] > 0,
            })

        # Health — _populate_health expects a criteria stub (legacy shape)
        from sniper.mcp_tools.read_tools import _populate_health
        health_result = {"criteria": {"health": {}}}
        _populate_health(health_result)
        health_details = health_result.get("health_details", {})
        proof_health_ok = all(
            v.get("status") == "ok"
            for v in health_details.values()
            if v.get("proof_required")
        )
        # Match /api/ops/health semantics: any warning or failure means the overall
        # system health indicator is not fully healthy.
        health_ok = all(v.get("status") == "ok" for v in health_details.values())
        from sniper.settings import memecoin as memecoin_cfg
        memecoin_telegram = {
            "enabled": bool(memecoin_cfg.MEMECOIN_TG_ALERTS),
            "configured": bool(
                memecoin_cfg.MEMECOIN_TG_BOT_TOKEN
                and memecoin_cfg.MEMECOIN_TG_CHAT_ID
            ),
        }

        return jsonify({
            "closed_trades": closed_trades,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "pnl": total_pnl,  # legacy alias
            "slip_pnl": slip_pnl,
            "open_positions": open_total,
            "active_strategies": active_strats,
            "pending_alert_count": pending_alert_count,
            "monthly": monthly,
            "health_details": health_details,
            "health_ok": health_ok,
            "proof_health_ok": proof_health_ok,
            "memecoin_telegram": memecoin_telegram,
            "stale": False,          # live SQLite read — never stale
            "provenance": "sqlite",
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            # Scope breakdown — explains why these numbers differ from /api/paper-trades
            # (which reports paper_trades only) and /api/ops/health (paper_trades only)
            "scope": "paper_trades + strategy_positions",
            "scope_breakdown": {
                "paper_trades_closed": len(pt_rows),
                "strategy_positions_closed": len(sp_rows),
                "paper_trades_pnl": round(sum(r["pnl_usdc"] or 0 for r in pt_rows), 2),
                "strategy_positions_pnl": round(sum(r["realized_pnl"] or 0 for r in sp_rows), 2),
                "paper_trades_open": open_paper,
                "strategy_positions_open": open_strat,
            },
        })

    @bp.get("/api/classification-diagnostics")
    def api_classification_diagnostics():
        from db import classification_diagnostics

        min_insider = ctx["optional_float"](request.args.get("min_insider_score")) or 55.0
        limit = max(1, min(ctx["safe_int"](request.args.get("limit"), 20), 100))

        result = classification_diagnostics(
            ctx["conn"],
            min_insider_score=min_insider,
            limit=limit,
        )
        return jsonify({
            **result,
            "updated_at": _updated_at(),
        })

    @bp.get("/api/world-context")
    def api_world_context():
        """Return current world events knowledge graph from Supabase."""
        try:
            from sniper.analysis.world_context import get_world_events_status

            payload, cache_age_s = cached_intel_payload(
                "world-context",
                get_world_events_status,
            )

            # Derive actual data timestamp from the freshest event rather than
            # using wall time, so callers can detect staleness correctly.
            events = payload.get("events") or []
            ts_candidates = [
                e.get("_memory_updated_at") or e.get("last_updated") or ""
                for e in events
                if e.get("_memory_updated_at") or e.get("last_updated")
            ]
            data_updated_at = max(ts_candidates) if ts_candidates else None

            # Stale if freshest event is >6h old or no events available
            stale = True
            if data_updated_at:
                try:
                    freshest_dt = dt.datetime.fromisoformat(
                        data_updated_at.replace("Z", "+00:00")
                    )
                    age_h = (
                        dt.datetime.now(dt.timezone.utc) - freshest_dt
                    ).total_seconds() / 3600
                    stale = age_h > 6
                except Exception:
                    pass

            payload["data_updated_at"] = data_updated_at
            payload["stale"] = stale
            payload["provenance"] = payload.get("source", "supabase")
            payload["api_ts"] = _updated_at()
            payload["cache_age_s"] = round(cache_age_s, 3)
            # Keep legacy updated_at = wall time for any consumers still relying on it
            payload["updated_at"] = payload["api_ts"]
            return jsonify(payload)
        except Exception as e:
            logger.warning("api_world_context failed: %s", e)
            return jsonify({
                "events": [],
                "count": 0,
                "available": False,
                "degraded": True,
                "stale": True,
                "provenance": "error",
                "code": "world_context_unexpected_error",
                "message": "World context unavailable — unexpected server error.",
                "error": str(e),
                "updated_at": _updated_at(),
            })

    @bp.get("/api/trade-ideas")
    def api_trade_ideas():
        """Return open trade ideas from SQLite trade_ideas table.

        Query params:
          source_agent — filter by agent (e.g. 'finance_analyst', 'world_analyst')
          limit        — max rows, capped at 100 (default 30)
        """
        source_agent = request.args.get("source_agent")
        limit = min(ctx["safe_int"](request.args.get("limit"), 30), 100)
        try:
            now_str = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
            if source_agent:
                rows = ctx["conn"].execute(
                    """SELECT * FROM trade_ideas
                       WHERE status='open'
                         AND source_agent=?
                         AND (expires_at IS NULL OR datetime(expires_at) > datetime(?))
                       ORDER BY created_at DESC LIMIT ?""",
                    (source_agent, now_str, limit),
                ).fetchall()
            else:
                rows = ctx["conn"].execute(
                    """SELECT * FROM trade_ideas
                       WHERE status='open'
                         AND (expires_at IS NULL OR datetime(expires_at) > datetime(?))
                       ORDER BY created_at DESC LIMIT ?""",
                    (now_str, limit),
                ).fetchall()
            ideas = [dict(r) for r in rows]
            # Freshness: stale if the newest idea is >24h old (or no ideas exist)
            newest_created = max(
                (i.get("created_at") or "" for i in ideas), default=""
            )
            trade_ideas_stale = True
            if newest_created:
                try:
                    newest_dt = dt.datetime.fromisoformat(
                        newest_created.replace("Z", "+00:00")
                    )
                    if newest_dt.tzinfo is None:
                        newest_dt = newest_dt.replace(tzinfo=dt.timezone.utc)
                    trade_ideas_stale = (
                        dt.datetime.now(dt.timezone.utc) - newest_dt
                    ).total_seconds() > 86400
                except Exception:
                    pass
            return jsonify({
                "items": ideas,
                "count": len(ideas),
                "available": True,
                "stale": trade_ideas_stale,
                "provenance": "sqlite",
                "updated_at": _updated_at(),
            })
        except Exception as e:
            return jsonify({"items": [], "available": False, "error": str(e)})

    @bp.get("/api/market-opportunities")
    def api_market_opportunities():
        """Research trade ideas from the Supabase market_opportunities table (world/finance analyst
        output). Read-only, display-only — these are hypotheses for the Intel tab, NOT advice or
        executed trades; many have a NULL polymarket_condition_id and must be market-verified first."""
        limit = min(ctx["safe_int"](request.args.get("limit"), 30), 100)
        try:
            from sniper.analysis.world_context import get_market_opportunities
            payload, cache_age_s = cached_intel_payload(
                ("market-opportunities", limit),
                lambda: get_market_opportunities(limit=limit),
            )
            payload["updated_at"] = _updated_at()
            payload["cache_age_s"] = round(cache_age_s, 3)
            payload["disclaimer"] = ("Research hypotheses from the intel analyst — NOT advice, "
                                     "expected PnL, or sizing. Verify the market before acting.")
            return jsonify(payload)
        except Exception as e:  # noqa: BLE001
            logger.warning("api_market_opportunities failed: %s", e)
            return jsonify({"items": [], "count": 0, "available": False, "error": str(e),
                            "updated_at": _updated_at()})

    @bp.get("/api/finance-context")
    def api_finance_context():
        """Return the finance analyst's market intelligence report + boosts from Supabase brain."""
        try:
            from brain import Brain
            b = Brain()
            if not b.sb_available:
                return jsonify({
                    "report": None,
                    "boosts": [],
                    "available": False,
                    "report_available": False,
                    "message": "Finance context unavailable — Supabase brain not configured.",
                    "updated_at": None,
                })

            report_entry = b.read_entry("markets/state")
            boosts_entry = b.read_entry("market_boosts_current")
            report = report_entry.get("content") if report_entry else None
            boosts: list = []
            raw = boosts_entry.get("content") if boosts_entry else None
            if raw:
                try:
                    boosts = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    pass  # malformed content — return empty boosts rather than crash
            report_updated_at = report_entry.get("updated_at") if report_entry else None
            boosts_updated_at = boosts_entry.get("updated_at") if boosts_entry else None
            latest_updated_at = max(
                [ts for ts in (report_updated_at, boosts_updated_at) if ts],
                default=None,
            )
            return jsonify({
                "report": report,
                "boosts": boosts,
                "available": True,
                "report_available": bool(report),
                "message": None if report else "No market intelligence report yet.",
                "report_updated_at": report_updated_at,
                "boosts_updated_at": boosts_updated_at,
                "updated_at": latest_updated_at,
            })
        except Exception as e:
            return jsonify({
                "report": None,
                "boosts": [],
                "available": False,
                "report_available": False,
                "message": "Finance context unavailable — unexpected server error.",
                "error": str(e),
                "updated_at": None,
            })

    @bp.get("/api/news-feed")
    def api_news_feed():
        """Return recent news items from the local SQLite news_items table.

        Reports available=False when the table is empty and stale=True when the
        newest item is older than 24h. Brain.available is hardcoded True ("SQLite
        always available"), so it cannot distinguish a healthy feed from a dead
        producer — the 3-month outage reported as available:true, count:0.
        """
        limit = min(ctx["safe_int"](request.args.get("limit"), 30), 100)
        try:
            from brain import Brain
            b = Brain()
            news = b.get_recent_news(limit=limit)
            newest = max((n.get("fetched_at") or "" for n in news), default="")
            stale = True
            if newest:
                try:
                    ts = dt.datetime.fromisoformat(str(newest).replace("Z", "+00:00"))
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=dt.timezone.utc)
                    stale = (dt.datetime.now(dt.timezone.utc) - ts).total_seconds() > 86400
                except Exception:
                    pass  # unparseable timestamp — stay stale, never claim fresh
            return jsonify({
                "items": news,
                "count": len(news),
                "available": bool(news),
                "stale": stale,
                "message": None if news else "News feed empty — the news_intel producer has not run.",
                "updated_at": _updated_at(),
            })
        except Exception as e:
            logger.exception("api_news_feed failed")
            return jsonify({"items": [], "count": 0, "available": False, "stale": True,
                            "message": "News feed unavailable — unexpected server error.",
                            "error": str(e), "updated_at": _updated_at()})

    @bp.post("/api/trade-ideas/<int:idea_id>/act")
    def api_act_on_idea(idea_id: int):
        """Mark a trade idea as acted on."""
        try:
            from brain import Brain
            b = Brain()
            if not b.available:
                return jsonify({"ok": False, "error": "Supabase not available"})
            payload = request.get_json(silent=True) or {}
            b.mark_idea_acted(idea_id, notes=payload.get("notes"))
            return jsonify({"ok": True, "idea_id": idea_id})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.post("/api/trade-ideas/<int:idea_id>/reject")
    def api_reject_trade_idea(idea_id: int):
        """Reject/dismiss a trade idea — marks it rejected so it won't resurface."""
        try:
            from brain import Brain
            b = Brain()
            if not b.available:
                return jsonify({"ok": False, "error": "Supabase not available"})
            payload = request.get_json(silent=True) or {}
            b.reject_idea(idea_id, notes=payload.get("notes", "dismissed via dashboard"))
            return jsonify({"ok": True, "idea_id": idea_id})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @bp.post("/api/trade-ideas/<int:idea_id>/execute")
    def api_execute_trade_idea(idea_id: int):
        """Execute a trade idea as a paper strategy position."""
        # Mutating endpoint — always require API key
        from sniper.web.server import SNIPER_API_KEY, _is_trusted_gateway_request, _request_api_key
        if SNIPER_API_KEY and not _is_trusted_gateway_request() and _request_api_key() != SNIPER_API_KEY:
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        try:
            payload = request.get_json(silent=True) or {}
            raw_size = float(payload.get("size_usdc", 10.0))
            size_usdc = max(1.0, min(raw_size, 500.0))  # clamp to sane range
            result = execute_trade_idea(ctx["conn"], idea_id, size_usdc)
            if result.get("ok"):
                return jsonify(result), 200
            else:
                return jsonify(result), 400
        except Exception as e:
            logger.exception("api_execute_trade_idea failed: idea_id=%s", idea_id)
            return jsonify({"ok": False, "error": str(e)}), 500

    return bp


def execute_trade_idea(conn: sqlite3.Connection, idea_id: int, size_usdc: float = 10.0) -> Dict[str, Any]:
    """Execute a trade idea by creating a strategy_position under news_intel_v1.

    This is a module-level function so it can be called from tests without Flask.
    """
    from brain import get_trade_idea_review_gate

    now_ts = int(time.time())
    now_dt = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # 1. Load the trade idea
    row = conn.execute("SELECT * FROM trade_ideas WHERE id = ?", (idea_id,)).fetchone()
    if row is None:
        return {"ok": False, "error": f"Trade idea {idea_id} not found"}
    idea = dict(row)

    # 2. Validate status is open
    if idea.get("status") != "open":
        return {"ok": False, "error": f"Idea status is '{idea.get('status')}', must be 'open'"}

    # 2b. Manual execution still respects the adversarial gate.
    approved, gate_reason = get_trade_idea_review_gate(conn, idea_id)
    if not approved:
        return {"ok": False, "error": gate_reason}

    # 3. Find active news_intel_v1 strategy
    strat_row = conn.execute(
        "SELECT id FROM strategies WHERE strategy_type='news_intel_v1' AND status='active' LIMIT 1"
    ).fetchone()
    if strat_row is None:
        return {"ok": False, "error": "No active news_intel_v1 strategy found"}
    strategy_id = strat_row["id"]

    condition_id = idea["condition_id"]
    outcome = idea.get("outcome", "YES")

    # 4. Fetch live CLOB price for the correct outcome token
    #    Always fetch YES price from CLOB, then derive NO price = 1 - YES
    yes_price_fallback = float(idea.get("current_price") or 0.50)
    yes_price = yes_price_fallback
    try:
        import urllib.request
        import json as _json
        url = f"https://clob.polymarket.com/markets/{condition_id}"
        req = urllib.request.Request(url, headers={"User-Agent": "sniper/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _json.loads(resp.read().decode())
            tokens = data.get("tokens", [])
            for tok in tokens:
                if tok.get("outcome", "").upper() == "YES":
                    live_price = float(tok.get("price", 0))
                    if 0 < live_price <= 1.0:
                        yes_price = live_price
                    break
    except Exception as e:
        logger.debug("CLOB price fetch failed for %s, using idea price: %s", condition_id, e)

    # Refresh stored price with live CLOB data
    if yes_price != yes_price_fallback:
        conn.execute(
            "UPDATE trade_ideas SET current_price=? WHERE id=?",
            (yes_price if outcome.upper() == "YES" else 1.0 - yes_price, idea_id)
        )

    # Use the correct side's price — YES pays off at $1 if YES, NO pays off at $1 if NO
    if outcome.upper() == "NO":
        entry_price = max(0.01, min(0.99, 1.0 - yes_price))
    else:
        entry_price = max(0.01, min(0.99, yes_price))
    shares = size_usdc / entry_price

    # 5. Insert strategy_position — guard against duplicates on (strategy_id, condition_id, outcome, state='open')
    cur = conn.execute(
        """INSERT INTO strategy_positions (
            wallet, condition_id, outcome, strategy_id, state,
            entry_price, avg_entry_price, entry_shares, filled_entry_shares,
            opened_ts, version, updated_ts, size_usdc
        )
        SELECT ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, 1, ?, ?
        WHERE NOT EXISTS (
            SELECT 1 FROM strategy_positions
            WHERE strategy_id = ? AND condition_id = ? AND outcome = ? AND state = 'open'
        )""",
        (
            "news_intel_manual",  # synthetic wallet for manual intel trades
            condition_id,
            outcome,
            str(strategy_id),
            entry_price,
            entry_price,
            shares,
            shares,
            now_ts,
            now_ts,
            size_usdc,
            str(strategy_id),
            condition_id,
            outcome,
        ),
    )
    if cur.rowcount == 0:
        return {
            "ok": False,
            "error": f"Duplicate: an open position for {condition_id}/{outcome} already exists under strategy {strategy_id}",
        }
    position_id = cur.lastrowid

    # 6. Insert strategy_execution
    conn.execute(
        """INSERT INTO strategy_executions (
            strategy_id, wallet, condition_id, outcome, title,
            execution_type, size_usdc, entry_price, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'paper', ?, ?, 'open', ?, ?)""",
        (
            strategy_id,
            "news_intel_manual",
            condition_id,
            outcome,
            idea.get("market_title", ""),
            size_usdc,
            entry_price,
            now_dt,
            now_dt,
        ),
    )

    # 7. Increment trades_triggered on strategy
    conn.execute(
        "UPDATE strategies SET trades_triggered = trades_triggered + 1, updated_at = ? WHERE id = ?",
        (now_dt, strategy_id),
    )

    # 8. Mark idea as acted
    conn.execute(
        "UPDATE trade_ideas SET status='acted', acted_at=? WHERE id=?",
        (now_dt, idea_id),
    )

    conn.commit()

    return {
        "ok": True,
        "idea_id": idea_id,
        "position_id": position_id,
        "entry_price": entry_price,
        "outcome": outcome,
        "size_usdc": size_usdc,
    }


def _normalize_hash(tx_hash: Any) -> str:
    normalized = str(tx_hash or "").strip().lower()
    if not normalized:
        return ""
    if not normalized.startswith("0x"):
        normalized = f"0x{normalized}"
    return normalized


def _normalize_hash_set(values) -> set[str]:
    hashes: set[str] = set()
    for value in values:
        normalized = _normalize_hash(value)
        if normalized:
            hashes.add(normalized)
    return hashes


def _normalize_wallet(wallet: Any) -> str:
    normalized = str(wallet or "").strip().lower()
    if not normalized:
        return ""
    if not normalized.startswith("0x"):
        normalized = f"0x{normalized}"
    return normalized

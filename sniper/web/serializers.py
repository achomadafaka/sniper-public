from typing import Any, Dict, Mapping

from sniper.utils import _format_unix, _safe_float


def _as_dict(row: Mapping[str, Any] | None) -> Dict[str, Any]:
    if row is None:
        return {}
    return dict(row)


def serialize_wallet_row(
    row: Mapping[str, Any],
    identity: Mapping[str, str],
    extra_fields: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    row_dict = _as_dict(row)
    item: Dict[str, Any] = {
        "wallet": str(row_dict.get("wallet") or ""),
        "username": str(identity.get("username") or ""),
        "pseudonym": str(identity.get("pseudonym") or ""),
        "profile_image": str(identity.get("profile_image") or ""),
        "position_count": row_dict.get("position_count"),
        "pnl": row_dict.get("pnl"),
        "max_position_value": row_dict.get("max_position_value"),
        "top_open_position_title": row_dict.get("top_open_position_title"),
        "top_open_position_value": row_dict.get("top_open_position_value"),
        "total_open_value": row_dict.get("total_open_value"),
        "trade_count": row_dict.get("trade_count"),
        # avg_trades_per_position: how many CLOB order fills per market position.
        # AMM wallets have trade_count=0 (they swap in one click, not via order book).
        # For AMM wallets we return 1 — they make one clean entry per position.
        # This is actually the EASIEST pattern to copy (single entry, no laddering).
        "avg_trades_per_position": (
            lambda pos_count, trade_count: (
                max(1.0, float(trade_count) / pos_count) if pos_count and pos_count > 0
                else (1.0 if trade_count == 0 else None)
            )
        )(row_dict.get("position_count"), row_dict.get("trade_count") or 0),
        "event_slug": row_dict.get("event_slug"),
        "low_buy_hits": row_dict.get("low_buy_hits"),
        "low_buy_hits_14d": row_dict.get("low_buy_hits_14d"),
        "suspicion_score": row_dict.get("suspicion_score"),
        "insider_score": row_dict.get("insider_score"),
        "speculator_score": row_dict.get("speculator_score"),
        "classification": row_dict.get("classification"),
        "win_rate": row_dict.get("win_rate"),
        "wins": row_dict.get("wins"),
        "losses": row_dict.get("losses"),
        "closed_sample_size": row_dict.get("closed_sample_size"),
        "total_closed_count": row_dict.get("total_closed_count"),
        "resolution_rate": row_dict.get("resolution_rate"),
        "effective_resolved_wins": row_dict.get("effective_resolved_wins"),
        "effective_resolved_losses": row_dict.get("effective_resolved_losses"),
        "roi_percent": row_dict.get("roi_percent"),
        "qualifying_roi": row_dict.get("qualifying_roi"),
        "qualifying_pnl": row_dict.get("qualifying_pnl"),
        "qualifying_capital": row_dict.get("qualifying_capital"),
        "high_winrate_qualified": bool(int(row_dict.get("high_winrate_qualified") or 0)),
        "account_age_days": row_dict.get("account_age_days"),
        "is_fresh_account": bool(int(row_dict.get("is_fresh_account") or 0)),
        "position_signal_count": int(row_dict.get("position_signal_count") or 0),
        "position_insider_candidate_count": int(row_dict.get("position_insider_candidate_count") or 0),
        "position_speculator_count": int(row_dict.get("position_speculator_count") or 0),
        "max_position_insider_score": float(row_dict.get("max_position_insider_score") or 0.0),
        "max_position_speculator_score": float(row_dict.get("max_position_speculator_score") or 0.0),
        "tier": row_dict.get("tier"),
        "strategy_group": row_dict.get("strategy_group"),
        "strategy_confidence": row_dict.get("strategy_confidence"),
        "strategy_note": row_dict.get("strategy_note"),
        "wallet_edge_tier": row_dict.get("wallet_edge_tier") or "",
        "followability_cached": row_dict.get("followability_cached"),
        "last_title": row_dict.get("last_title"),
        "wallet_quality_score": row_dict.get("wallet_quality_score"),
        "wallet_quality_eligible": row_dict.get("wallet_quality_eligible"),
        "wallet_quality_fail_reason": row_dict.get("wallet_quality_fail_reason"),
        "market_maker_detected": bool(int(row_dict.get("market_maker_detected") or 0)),
    }
    if extra_fields:
        item.update(dict(extra_fields))
    return item


def _compute_position_action(
    entry_price: float,
    current_price: float,
    buys_since_entry: int,
    sell_notional_since_entry: float,
    hold_hours: float | None,
    max_trades_60s: int,
) -> str:
    """Derive a position-action label from existing signal fields.

    Labels:
    - twap_accumulation_down: algorithmic buying while price is >5% below entry
    - twap_new_build: algorithmic buying at or above entry
    - position_building: >10 manual-pace buys, not algorithmic
    - adding: moderate accumulation (4-10 buys)
    - new_position: fresh entry (<=3 buys)
    """
    if entry_price <= 0:
        return "new_position"

    price_chg = (current_price - entry_price) / entry_price
    hh = max(hold_hours or 0.01, 0.01)
    buys_per_hour = buys_since_entry / hh
    is_twap = max_trades_60s >= 3 or buys_per_hour > 10
    is_avg_down = price_chg < -0.05
    no_exits = sell_notional_since_entry == 0

    if is_twap and is_avg_down and no_exits:
        return "twap_accumulation_down"
    elif is_twap and not is_avg_down:
        return "twap_new_build"
    elif buys_since_entry > 10 and not is_twap:
        return "position_building"
    elif buys_since_entry > 3:
        return "adding"
    else:
        return "new_position"


def serialize_position_signal_row(
    row: Mapping[str, Any],
    identity: Mapping[str, str],
    analysis: Mapping[str, Any] | None,
    followed_row: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    row_dict = _as_dict(row)
    analysis_dict = _as_dict(analysis)
    followed_dict = _as_dict(followed_row)
    has_followed = bool(followed_dict)
    resolved_title = str(row_dict.get("market_title") or row_dict.get("title") or "")
    resolved_event_slug = str(row_dict.get("market_event_slug") or row_dict.get("event_slug") or "")
    resolved_end_date_iso = row_dict.get("market_end_date_iso") or row_dict.get("end_date_iso") or None

    return {
        "wallet": str(row_dict.get("wallet") or ""),
        "username": str(identity.get("username") or ""),
        "pseudonym": str(identity.get("pseudonym") or ""),
        "profile_image": str(identity.get("profile_image") or ""),
        "condition_id": str(row_dict.get("condition_id") or ""),
        "outcome": str(row_dict.get("outcome") or ""),
        "title": resolved_title,
        "event_slug": resolved_event_slug,
        "market_title": resolved_title,
        "market_event_slug": resolved_event_slug,
        "market_end_date_iso": resolved_end_date_iso,
        "market_volume_24h": float(row_dict.get("market_volume_24h") or 0.0),
        "market_is_open": bool(int(row_dict.get("market_is_open") or 1)),
        "is_open": bool(int(row_dict.get("is_open") or 0)),
        "position_value": float(row_dict.get("position_value") or 0.0),
        "position_share": float(row_dict.get("position_share") or 0.0),
        "position_count_wallet": int(row_dict.get("position_count_wallet") or 0),
        "entry_anchor_ts": int(row_dict.get("entry_anchor_ts") or 0),
        "entry_anchor_at": _format_unix(row_dict.get("entry_anchor_ts")),
        "entry_price": float(row_dict.get("entry_price") or 0.0),
        "entry_size": float(row_dict.get("entry_size") or 0.0),
        "entry_notional": float(row_dict.get("entry_notional") or 0.0),
        "current_price": float(row_dict.get("current_price") or 0.0),
        "current_size": float(row_dict.get("current_size") or 0.0),
        "current_amount": float(row_dict.get("position_value") or 0.0),
        "latest_trade_ts": int(row_dict.get("latest_trade_ts") or 0),
        "latest_trade_at": _format_unix(row_dict.get("latest_trade_ts")),
        "buys_5m": int(row_dict.get("buys_5m") or 0),
        "buys_1h": int(row_dict.get("buys_1h") or 0),
        "buys_24h": int(row_dict.get("buys_24h") or 0),
        "trades_5m": int(row_dict.get("trades_5m") or 0),
        "trades_1h": int(row_dict.get("trades_1h") or 0),
        "trades_24h": int(row_dict.get("trades_24h") or 0),
        "sells_5m": int(row_dict.get("sells_5m") or 0),
        "sells_1h": int(row_dict.get("sells_1h") or 0),
        "sells_24h": int(row_dict.get("sells_24h") or 0),
        "notional_5m": float(row_dict.get("notional_5m") or 0.0),
        "notional_1h": float(row_dict.get("notional_1h") or 0.0),
        "notional_24h": float(row_dict.get("notional_24h") or 0.0),
        "sell_notional_5m": float(row_dict.get("sell_notional_5m") or 0.0),
        "sell_notional_1h": float(row_dict.get("sell_notional_1h") or 0.0),
        "sell_notional_24h": float(row_dict.get("sell_notional_24h") or 0.0),
        "buys_since_entry": int(row_dict.get("buys_since_entry") or 0),
        "sells_since_entry": int(row_dict.get("sells_since_entry") or 0),
        "buy_notional_since_entry": float(row_dict.get("buy_notional_since_entry") or 0.0),
        "sell_notional_since_entry": float(row_dict.get("sell_notional_since_entry") or 0.0),
        "net_notional_24h": float(row_dict.get("net_notional_24h") or 0.0),
        "net_notional_since_entry": float(row_dict.get("net_notional_since_entry") or 0.0),
        "last_trade_side": str(row_dict.get("last_trade_side") or ""),
        "activity_state": str(row_dict.get("activity_state") or "holding"),
        "position_action": _compute_position_action(
            entry_price=float(row_dict.get("entry_price") or 0),
            current_price=float(row_dict.get("current_price") or 0),
            buys_since_entry=int(row_dict.get("buys_since_entry") or 0),
            sell_notional_since_entry=float(row_dict.get("sell_notional_since_entry") or 0),
            hold_hours=(lambda h: None if h is None or float(h) < 0 else float(h))(row_dict.get("hold_hours")),
            max_trades_60s=int(row_dict.get("max_trades_60s") or 0),
        ),
        "hold_hours": (lambda h: None if h is None or float(h) < 0 else float(h))(row_dict.get("hold_hours")),
        "unique_sizes_5m": int(row_dict.get("unique_sizes_5m") or 0),
        "max_trades_60s": int(row_dict.get("max_trades_60s") or 0),
        "position_insider_score": float(row_dict.get("position_insider_score") or 0.0),
        "position_speculator_score": float(row_dict.get("position_speculator_score") or 0.0),
        "position_classification": str(row_dict.get("position_classification") or "position_low_signal"),
        "signal_reason": str(row_dict.get("signal_reason") or ""),
        "analyzed_ts": int(row_dict.get("analyzed_ts") or 0),
        "analyzed_at": _format_unix(row_dict.get("analyzed_ts")),
        "is_followed_position": has_followed,
        "followed_position_source": str(followed_dict.get("added_source") or "") if has_followed else "",
        "followed_position_label": str(followed_dict.get("label") or "") if has_followed else "",
        "followed_position_added_ts": int(followed_dict.get("added_ts") or 0) if has_followed else 0,
        "followed_position_added_at": _format_unix(followed_dict.get("added_ts")) if has_followed else "",
        # Aliases for shared score filters/sorters in the UI.
        "insider_score": float(row_dict.get("position_insider_score") or 0.0),
        "speculator_score": float(row_dict.get("position_speculator_score") or 0.0),
        "classification": str(row_dict.get("position_classification") or "position_low_signal"),
        "win_rate": _safe_float(analysis_dict.get("win_rate"), 0.0) if analysis_dict.get("wins") is not None or analysis_dict.get("losses") is not None else None,
        "wins": int(analysis_dict.get("wins") or 0) if analysis_dict else None,
        "losses": int(analysis_dict.get("losses") or 0) if analysis_dict else None,
        "total_closed_count": int(analysis_dict.get("total_closed_count") or 0) if analysis_dict else None,
        "resolution_rate": _safe_float(analysis_dict.get("resolution_rate"), 0.0) if analysis_dict else None,
        "roi_percent": _safe_float(analysis_dict.get("roi_percent"), 0.0) if analysis_dict else None,
        "qualifying_roi": analysis_dict.get("qualifying_roi") if analysis_dict else None,  # None = unreliable
        "qualifying_pnl": _safe_float(analysis_dict.get("qualifying_pnl"), 0.0) if analysis_dict else None,
        "qualifying_capital": _safe_float(analysis_dict.get("qualifying_capital"), 0.0) if analysis_dict else None,
        "closed_sample_size": int(analysis_dict.get("closed_sample_size") or 0),
        "account_age_days": _safe_float(analysis_dict.get("account_age_days"), 99999.0),
        "is_fresh_account": bool(int(analysis_dict.get("is_fresh_account") or 0)),
        "wallet_tier": str(analysis_dict.get("tier") or ""),
        "wallet_classification": str(analysis_dict.get("classification") or ""),
        "wallet_confidence_score": float(analysis_dict.get("wallet_confidence_score") or 0.0),
        "wallet_numeric_tier": int(analysis_dict.get("wallet_tier") or 0),
        "effective_copy_fraction": float(analysis_dict.get("effective_copy_fraction") or 1.0),
        "insider_score_trend": float(analysis_dict.get("insider_score_trend") or 0.0),
        "wallet_position_count": int(analysis_dict.get("position_count") or 0),
        "wallet_strategy_group": str(analysis_dict.get("strategy_group") or ""),
        "wallet_strategy_confidence": _safe_float(analysis_dict.get("strategy_confidence"), 0.0),
        "wallet_quality_score": _safe_float(analysis_dict.get("wallet_quality_score"), None),
        "wallet_quality_eligible": analysis_dict.get("wallet_quality_eligible"),
        "wallet_quality_fail_reason": str(analysis_dict.get("wallet_quality_fail_reason") or "") if analysis_dict.get("wallet_quality_fail_reason") else None,
        "market_maker_detected": bool(int(analysis_dict.get("market_maker_detected") or 0)),
        "tier": str(analysis_dict.get("tier") or ""),
        "strategy_group": str(analysis_dict.get("strategy_group") or ""),
        "end_date_iso": resolved_end_date_iso,
        "paper_trade_outcome": row_dict.get("paper_trade_outcome"),
        "paper_trade_roi": _safe_float(row_dict.get("paper_trade_roi"), None),
        "paper_trade_closed_at": row_dict.get("paper_trade_closed_at"),
    }

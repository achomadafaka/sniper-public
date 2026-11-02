from typing import Any, TypedDict


class ApiAgentContext(TypedDict):
    conn: Any
    SNIPER_API_VERSION: str
    SNIPER_API_KEY: str
    SNIPER_API_KEY_PROTECT_MUTATING: bool
    AGENT_FEED_DEFAULT_LIMIT: int
    AGENT_FEED_MAX_LIMIT: int
    AGENT_ALERT_DEDUPE_SECONDS: int
    safe_int: Any
    normalize_wallet: Any
    wallet_valid: Any
    collect_agent_high_signals: Any
    emit_agent_alerts: Any
    alert_outbox_rows: Any
    wallet_profiles_map: Any
    serialize_alert_row: Any
    mark_alert_outbox_sent: Any
    acknowledge_alert_outbox: Any
    acknowledge_alert_outbox_by_dedupe_key: Any
    agent_mutes: Any
    serialize_agent_mute_row: Any
    add_agent_mute: Any
    delete_agent_mute: Any


def build_api_agent_context(**kwargs: Any) -> ApiAgentContext:
    return ApiAgentContext(**kwargs)


class ApiCoreContext(TypedDict):
    conn: Any
    HARD_FILTER_TOP_POSITION: bool
    MIN_UI_TOP_POSITION_VALUE: float
    HIGH_WINRATE_MIN_PNL: float
    HIGH_WINRATE_MIN_MAX_POSITION_VALUE: float
    EMERGING_MIN_INSIDER_SCORE: float
    EMERGING_MIN_MAX_POSITION_VALUE: float
    EMERGING_MIN_TOTAL_OPEN_VALUE: float
    FLOW_MOVER_MIN_SPECULATOR_SCORE: float
    FLOW_MOVER_MIN_POSITION_SPECULATOR_SCORE: float
    FLOW_MOVER_MIN_MAX_POSITION_VALUE: float
    FLOW_MOVER_MIN_TOTAL_OPEN_VALUE: float
    FLOW_MOVER_MIN_ABS_PNL: float
    OPEN_SIGNALS_FOCUSED_DEFAULT: bool
    THESIS_SIGNAL_WINDOW_HOURS: int
    THESIS_SIGNAL_MAX_ENTRY_PRICE: float
    THESIS_SIGNAL_MIN_POSITION_VALUE: float
    THESIS_SIGNAL_HIGH_CONVICTION_MIN_POSITION_VALUE: float
    THESIS_SIGNAL_MAX_ACCOUNT_AGE_DAYS: float
    THESIS_SIGNAL_MAX_LIFETIME_POSITIONS: int
    THESIS_SIGNAL_ALLOWED_CATEGORIES: tuple[str, ...]
    THESIS_SIGNAL_EXCLUDE_SPECULATOR_BOTS: bool
    THESIS_SIGNAL_SINGLE_MARKET_ONLY: bool
    MAX_FOLLOWED_POSITION_UI_ROWS: int
    MIN_ORDER_NOTIONAL: float
    MIN_UI_ORDER_NOTIONAL: float
    MIN_UI_IMPACT_RATIO: float
    MAX_UI_TRADES: int
    SUSPICIOUS_STATUS_SYNC_MIN_INTERVAL_SECONDS: int
    SUSPICIOUS_STATUS_MAX_WALLET_FETCHES_PER_REQUEST: int
    SUSPICIOUS_PATTERN_SYNC_MIN_INTERVAL_SECONDS: int
    SUSPICIOUS_PATTERN_MAX_WALLET_FETCHES_PER_REQUEST: int
    SUSPICIOUS_PATTERN_TRADE_FETCH_LIMIT: int
    safe_int: Any
    optional_float: Any
    normalize_wallet: Any
    wallet_valid: Any
    ignored_wallets: Any
    flagged_users: Any
    high_winrate_wallets: Any
    wallet_leaderboard_rows: Any
    wallet_leaderboard_v2_rows: Any
    wallet_followable_leaderboard_rows: Any
    wallet_resolved_outcome_stats: Any
    emerging_insider_wallets: Any
    speculator_flow_wallets: Any
    position_signals: Any
    thesis_signal_rows: Any
    thesis_candidate_rows: Any
    followed_positions: Any
    recent_low_buy_trades: Any
    wallet_profiles_map: Any
    wallet_identity: Any
    eligibility_blockers_from_reason: Any
    format_unix: Any
    followed_position_key: Any
    open_signal_is_focused: Any
    open_signal_is_active_non_holding: Any
    get_suspicious_status_cache: Any
    set_suspicious_status_cache: Any
    fetch_wallet_position_maps: Any
    get_suspicious_pattern_cache: Any
    set_suspicious_pattern_cache: Any
    fetch_wallet_order_pattern_map: Any
    resolve_followed_status: Any
    position_key: Any
    bet_direction: Any
    annotate_order_patterns: Any
    trade_row_id: Any
    enqueue_background_sync: Any
    background_sync_external_leaderboard: Any


def build_api_core_context(**kwargs: Any) -> ApiCoreContext:
    return ApiCoreContext(**kwargs)


class ApiWatchlistContext(TypedDict):
    conn: Any
    MAX_WATCHLIST_UI_TRADES: int
    MIN_UI_ORDER_NOTIONAL: float
    normalize_wallet: Any
    wallet_valid: Any
    ignored_wallets: Any
    watchlist_wallets: Any
    upsert_watchlist_wallet: Any
    remove_watchlist_wallet: Any
    sync_watchlist_wallet: Any
    sync_all_watchlist_wallets: Any
    enqueue_background_sync: Any
    background_sync_watchlist: Any
    recent_watchlist_trades: Any
    deleted_trade_hashes: Any
    normalize_tx_hash: Any
    format_unix: Any
    bet_direction: Any
    annotate_order_patterns: Any
    delete_trade_and_tombstone: Any
    position_snapshot_history: Any
    followed_wallet_activity: Any
    research_position_building_clusters: Any


def build_api_watchlist_context(**kwargs: Any) -> ApiWatchlistContext:
    return ApiWatchlistContext(**kwargs)


class ApiFollowedContext(TypedDict):
    conn: Any
    MAX_FOLLOWED_UI_TRADES: int
    MAX_FOLLOWED_POSITION_UI_ROWS: int
    MIN_UI_ORDER_NOTIONAL: float
    safe_float: Any
    safe_int: Any
    normalize_wallet: Any
    normalize_tx_hash: Any
    wallet_valid: Any
    ignored_wallets: Any
    watchlist_wallets: Any
    upsert_watchlist_wallet: Any
    remove_watchlist_wallet: Any
    sync_watchlist_wallet: Any
    followed_trades: Any
    followed_positions: Any
    real_trades_positions_summary: Any
    wallet_profiles_map: Any
    is_trade_deleted: Any
    upsert_followed_trade: Any
    remove_followed_trade: Any
    upsert_followed_position: Any
    remove_followed_position: Any
    deleted_trade_hashes: Any
    followed_position_key: Any
    wallet_identity: Any
    format_unix: Any
    bet_direction: Any
    sync_followed_wallet: Any
    sync_followed_trades: Any
    sync_followed_positions_wallet: Any
    sync_followed_positions: Any
    enqueue_background_sync: Any
    background_sync_followed_trades: Any
    background_sync_followed_positions: Any
    background_sync_external_leaderboard: Any
    delete_trade_and_tombstone: Any


def build_api_followed_context(**kwargs: Any) -> ApiFollowedContext:
    return ApiFollowedContext(**kwargs)


class ApiMiscContext(TypedDict):
    conn: Any
    normalize_wallet: Any
    normalize_tx_hash: Any
    wallet_valid: Any
    wallet_profiles_map: Any
    wallet_identity: Any
    format_unix: Any
    delete_trade_and_tombstone: Any
    undismiss_suspicious_trade: Any
    clear_dismissed_suspicious_trades: Any
    ignored_wallets: Any
    ignore_wallet: Any
    unignore_wallet: Any
    clear_ignored_wallets: Any
    block_wallet: Any
    blocked_wallets_list: Any
    is_wallet_blocked: Any


def build_api_misc_context(**kwargs: Any) -> ApiMiscContext:
    return ApiMiscContext(**kwargs)


class ApiStrategiesContext(TypedDict):
    conn: Any
    normalize_wallet: Any
    wallet_valid: Any
    safe_int: Any
    optional_float: Any


def build_api_strategies_context(**kwargs: Any) -> ApiStrategiesContext:
    return ApiStrategiesContext(**kwargs)

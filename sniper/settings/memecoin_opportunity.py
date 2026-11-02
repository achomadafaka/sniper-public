"""Validated settings for the isolated, default-OFF opportunity capture worker."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from sniper.util.state_paths import state_path


@dataclass(frozen=True)
class MemecoinOpportunitySettings:
    enabled: bool
    db_path: str
    insight_db_path: str
    solana_rpc_url: str
    solana_recovery_rpc_url: str
    solana_recovery_rps: float
    solana_indexed_recovery: bool
    solana_ws_url: str
    robinhood_rpc_url: str
    robinhood_ws_url: str
    robinhood_v3_quoter: str
    queue_max: int
    stale_after_s: float
    recovery_max_pages: int
    robinhood_poll_s: float
    robinhood_warm_start_blocks: int
    robinhood_poll_chunk_blocks: int
    primary_clip_usd_micros: int
    clip_usd_micros: tuple[int, ...]
    capture_window_s: int
    candidate_capture_window_s: int
    hot_retention_s: int
    insight_retention_s: int
    alert_retention_s: int
    cleanup_interval_s: int
    hot_emergency_retention_s: int
    hot_soft_cap_bytes: int
    hot_hard_cap_bytes: int
    insight_soft_cap_bytes: int
    insight_hard_cap_bytes: int
    rpc_concurrency: int
    rpc_initial_rps: float
    rpc_min_rps: float
    rpc_max_rps: float
    robinhood_rpc_rps: float
    solana_orphan_after_s: float
    paper_adapter_enabled: bool
    adaptive_v3_enabled: bool
    paper_adapter_expiry_s: float
    paper_adapter_consumer_heartbeat_path: str
    paper_adapter_consumer_max_age_s: float
    quote_max_queue_age_ms: int
    quote_max_pair_age_ms: int
    quote_max_completion_age_ms: int
    quote_cache_max: int
    quote_completion_max: int


def load_memecoin_opportunity_settings(env=None) -> MemecoinOpportunitySettings:
    """Load opportunity-only configuration without affecting funded imports."""
    env = os.environ if env is None else env
    enabled = str(env.get("MEMECOIN_OPPORTUNITY_CAPTURE", "0")).strip().lower() in {
        "1", "true", "yes", "on"
    }
    db_path = str(env.get("MEMECOIN_OPPORTUNITY_DB", "") or "").strip() or str(
        state_path("data", "memecoin_opportunity.db")
    )
    insight_db_path = str(
        env.get("MEMECOIN_OPPORTUNITY_INSIGHT_DB", "") or ""
    ).strip() or str(state_path("data", "memecoin_opportunity_insights.db"))
    solana_rpc = str(
        env.get("MEMECOIN_OPPORTUNITY_SOLANA_RPC_URL", "")
        or env.get("MEMECOIN_SOLANA_READ_RPC_URL", "")
        or ""
    ).strip()
    solana_recovery_rpc = str(
        env.get("MEMECOIN_OPPORTUNITY_SOLANA_RECOVERY_RPC_URL", "")
        or "https://api.mainnet-beta.solana.com"
    ).strip()
    solana_indexed_recovery = str(
        env.get("MEMECOIN_OPPORTUNITY_SOLANA_INDEXED_RECOVERY", "0")
    ).strip().lower() in {"1", "true", "yes", "on"}
    solana_recovery_rps = float(
        env.get(
            "MEMECOIN_OPPORTUNITY_SOLANA_RECOVERY_RPS",
            "1.0" if solana_indexed_recovery else "0.25",
        )
    )
    solana_ws = str(env.get("MEMECOIN_OPPORTUNITY_SOLANA_WS_URL", "") or "").strip()
    robinhood_rpc = str(
        env.get("MEMECOIN_OPPORTUNITY_4663_RPC_URL", "")
        or env.get("MEMECOIN_EVM_RPC_URL_4663", "")
        or ""
    ).strip()
    robinhood_ws = str(env.get("MEMECOIN_OPPORTUNITY_4663_WS_URL", "") or "").strip()
    robinhood_v3_quoter = str(
        env.get(
            "MEMECOIN_OPPORTUNITY_4663_V3_QUOTER",
            "0x33e885ed0ec9bf04ecfb19341582aadcb4c8a9e7",
        )
    ).strip().lower()
    queue_max = int(env.get("MEMECOIN_OPPORTUNITY_QUEUE_MAX", "4096"))
    stale_after_s = float(env.get("MEMECOIN_OPPORTUNITY_STALE_AFTER_S", "5"))
    recovery_pages = int(env.get("MEMECOIN_OPPORTUNITY_RECOVERY_MAX_PAGES", "30"))
    robinhood_poll_s = float(env.get("MEMECOIN_OPPORTUNITY_4663_POLL_S", "2"))
    robinhood_warm_start_blocks = int(
        env.get("MEMECOIN_OPPORTUNITY_4663_WARM_START_BLOCKS", "50000")
    )
    robinhood_poll_chunk_blocks = int(
        env.get("MEMECOIN_OPPORTUNITY_4663_POLL_CHUNK_BLOCKS", "5000")
    )
    capture_window_s = int(env.get("MEMECOIN_OPPORTUNITY_CAPTURE_WINDOW_S", "120"))
    candidate_capture_window_s = int(
        env.get("MEMECOIN_OPPORTUNITY_CANDIDATE_CAPTURE_WINDOW_S", "300")
    )
    hot_retention_s = int(env.get("MEMECOIN_OPPORTUNITY_HOT_RETENTION_S", "360"))
    insight_retention_s = int(
        env.get("MEMECOIN_OPPORTUNITY_INSIGHT_RETENTION_S", str(7 * 86_400))
    )
    # The exact alert rows the matched-control gate is computed from. They are ~1.6% of the
    # insight file, so a long clock is cheap; a short one silently turns a preregistered
    # cumulative gate into a rolling-window statistic that cannot reproduce its own pairs.
    alert_retention_s = int(
        env.get("MEMECOIN_OPPORTUNITY_ALERT_RETENTION_S", str(90 * 86_400))
    )
    cleanup_interval_s = int(env.get("MEMECOIN_OPPORTUNITY_CLEANUP_INTERVAL_S", "30"))
    hot_emergency_retention_s = int(
        env.get("MEMECOIN_OPPORTUNITY_HOT_EMERGENCY_RETENTION_S", "300")
    )
    mib = 1024 * 1024
    hot_soft_cap_bytes = int(env.get("MEMECOIN_OPPORTUNITY_HOT_SOFT_CAP_MB", "128")) * mib
    hot_hard_cap_bytes = int(env.get("MEMECOIN_OPPORTUNITY_HOT_HARD_CAP_MB", "256")) * mib
    insight_soft_cap_bytes = int(
        env.get("MEMECOIN_OPPORTUNITY_INSIGHT_SOFT_CAP_MB", "1024")
    ) * mib
    insight_hard_cap_bytes = int(
        env.get("MEMECOIN_OPPORTUNITY_INSIGHT_HARD_CAP_MB", "1536")
    ) * mib
    rpc_concurrency = int(env.get("MEMECOIN_OPPORTUNITY_RPC_CONCURRENCY", "1"))
    # WS is the live clock. HTTP is only confirmation/recovery, so leave room
    # for the other laptop services sharing a public endpoint.
    rpc_initial_rps = float(env.get("MEMECOIN_OPPORTUNITY_RPC_INITIAL_RPS", "0.25"))
    rpc_min_rps = float(env.get("MEMECOIN_OPPORTUNITY_RPC_MIN_RPS", "0.25"))
    rpc_max_rps = float(env.get("MEMECOIN_OPPORTUNITY_RPC_MAX_RPS", "0.25"))
    robinhood_rpc_rps = float(
        env.get("MEMECOIN_OPPORTUNITY_4663_RPC_RPS", "1.0")
    )
    solana_orphan_after_s = float(
        env.get("MEMECOIN_OPPORTUNITY_SOLANA_ORPHAN_AFTER_S", "120")
    )
    # Default OFF. The adapter mints paper fills a separate consumer must admit, so it
    # stays dark until both it and its consumer are deliberately turned on.
    paper_adapter_enabled = str(
        env.get("MEMECOIN_OPPORTUNITY_PAPER_ADAPTER", "0")
    ).strip().lower() in {"1", "true", "yes", "on"}
    adaptive_v3_enabled = str(
        env.get("MEMECOIN_RADAR_ADAPTIVE_V3_ENABLED", "0")
    ).strip().lower() in {"1", "true", "yes", "on"}
    paper_adapter_expiry_s = float(
        env.get("MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_EXPIRY_S", "30")
    )
    paper_adapter_consumer_max_age_s = float(
        env.get("MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_CONSUMER_MAX_AGE_S", "120")
    )
    paper_adapter_consumer_heartbeat_path = str(
        env.get(
            "MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_CONSUMER_HEARTBEAT",
            "data/memecoin_v6_paper.heartbeat",
        )
    ).strip()
    quote_max_queue_age_ms = int(
        env.get("MEMECOIN_OPPORTUNITY_QUOTE_MAX_QUEUE_AGE_MS", "2000")
    )
    quote_max_pair_age_ms = int(
        env.get("MEMECOIN_OPPORTUNITY_QUOTE_MAX_PAIR_AGE_MS", "3500")
    )
    quote_max_completion_age_ms = int(
        env.get("MEMECOIN_OPPORTUNITY_QUOTE_MAX_COMPLETION_AGE_MS", "1000")
    )
    quote_cache_max = int(env.get("MEMECOIN_OPPORTUNITY_QUOTE_CACHE_MAX", "256"))
    quote_completion_max = int(
        env.get("MEMECOIN_OPPORTUNITY_QUOTE_COMPLETION_MAX", "256")
    )
    if queue_max <= 0:
        raise ValueError("MEMECOIN_OPPORTUNITY_QUEUE_MAX must be > 0")
    if not math.isfinite(stale_after_s) or stale_after_s <= 0:
        raise ValueError("MEMECOIN_OPPORTUNITY_STALE_AFTER_S must be finite and > 0")
    if recovery_pages <= 0:
        raise ValueError("MEMECOIN_OPPORTUNITY_RECOVERY_MAX_PAGES must be > 0")
    if not math.isfinite(robinhood_poll_s) or robinhood_poll_s <= 0:
        raise ValueError("MEMECOIN_OPPORTUNITY_4663_POLL_S must be finite and > 0")
    if robinhood_warm_start_blocks < 0:
        raise ValueError("MEMECOIN_OPPORTUNITY_4663_WARM_START_BLOCKS must be >= 0")
    positive_ints = {
        "MEMECOIN_OPPORTUNITY_HOT_RETENTION_S": hot_retention_s,
        "MEMECOIN_OPPORTUNITY_CAPTURE_WINDOW_S": capture_window_s,
        "MEMECOIN_OPPORTUNITY_CANDIDATE_CAPTURE_WINDOW_S": candidate_capture_window_s,
        "MEMECOIN_OPPORTUNITY_INSIGHT_RETENTION_S": insight_retention_s,
        "MEMECOIN_OPPORTUNITY_ALERT_RETENTION_S": alert_retention_s,
        "MEMECOIN_OPPORTUNITY_CLEANUP_INTERVAL_S": cleanup_interval_s,
        "MEMECOIN_OPPORTUNITY_HOT_EMERGENCY_RETENTION_S": hot_emergency_retention_s,
        "MEMECOIN_OPPORTUNITY_RPC_CONCURRENCY": rpc_concurrency,
        "MEMECOIN_OPPORTUNITY_4663_POLL_CHUNK_BLOCKS": robinhood_poll_chunk_blocks,
        "MEMECOIN_OPPORTUNITY_QUOTE_MAX_QUEUE_AGE_MS": quote_max_queue_age_ms,
        "MEMECOIN_OPPORTUNITY_QUOTE_MAX_PAIR_AGE_MS": quote_max_pair_age_ms,
        "MEMECOIN_OPPORTUNITY_QUOTE_MAX_COMPLETION_AGE_MS": quote_max_completion_age_ms,
        "MEMECOIN_OPPORTUNITY_QUOTE_CACHE_MAX": quote_cache_max,
        "MEMECOIN_OPPORTUNITY_QUOTE_COMPLETION_MAX": quote_completion_max,
    }
    for key, value in positive_ints.items():
        if value <= 0:
            raise ValueError(f"{key} must be > 0")
    if capture_window_s < 120:
        raise ValueError("MEMECOIN_OPPORTUNITY_CAPTURE_WINDOW_S must be >= 120")
    if candidate_capture_window_s < 300:
        raise ValueError(
            "MEMECOIN_OPPORTUNITY_CANDIDATE_CAPTURE_WINDOW_S must be >= 300"
        )
    if candidate_capture_window_s < capture_window_s:
        raise ValueError("candidate capture must not be shorter than ordinary capture")
    if alert_retention_s < insight_retention_s:
        raise ValueError("alert retention must not be shorter than insight retention")
    if hot_retention_s < candidate_capture_window_s + 30:
        raise ValueError("hot retention must exceed candidate capture by at least 30 seconds")
    if hot_emergency_retention_s > hot_retention_s:
        raise ValueError("hot emergency retention must not exceed normal retention")
    if hot_emergency_retention_s < candidate_capture_window_s:
        raise ValueError("emergency retention must cover candidate capture")
    if hot_soft_cap_bytes <= 0 or hot_hard_cap_bytes <= hot_soft_cap_bytes:
        raise ValueError("hot soft cap must be positive and below hard cap")
    if insight_soft_cap_bytes <= 0 or insight_hard_cap_bytes <= insight_soft_cap_bytes:
        raise ValueError("insight soft cap must be positive and below hard cap")
    if not all(
        math.isfinite(value) and value > 0
        for value in (rpc_initial_rps, rpc_min_rps, rpc_max_rps)
    ) or not rpc_min_rps <= rpc_initial_rps <= rpc_max_rps:
        raise ValueError("RPC rates must be finite, positive, and min <= initial <= max")
    if not math.isfinite(robinhood_rpc_rps) or robinhood_rpc_rps <= 0:
        raise ValueError("MEMECOIN_OPPORTUNITY_4663_RPC_RPS must be finite and > 0")
    if not math.isfinite(solana_recovery_rps) or solana_recovery_rps <= 0:
        raise ValueError("MEMECOIN_OPPORTUNITY_SOLANA_RECOVERY_RPS must be finite and > 0")
    if not math.isfinite(solana_orphan_after_s) or solana_orphan_after_s <= 0:
        raise ValueError("MEMECOIN_OPPORTUNITY_SOLANA_ORPHAN_AFTER_S must be finite and > 0")
    if not math.isfinite(paper_adapter_expiry_s) or paper_adapter_expiry_s <= 0:
        raise ValueError("MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_EXPIRY_S must be finite and > 0")
    if not math.isfinite(paper_adapter_consumer_max_age_s) or paper_adapter_consumer_max_age_s <= 0:
        raise ValueError(
            "MEMECOIN_OPPORTUNITY_PAPER_ADAPTER_CONSUMER_MAX_AGE_S must be finite and > 0"
        )
    if paper_adapter_enabled and not paper_adapter_consumer_heartbeat_path:
        raise ValueError("the paper adapter requires a consumer heartbeat path")
    if adaptive_v3_enabled and not paper_adapter_enabled:
        raise ValueError("adaptive v3 requires the paper adapter")
    if enabled and not any(
        (solana_rpc, solana_ws, robinhood_rpc, robinhood_ws)
    ):
        raise ValueError("enabled opportunity capture requires at least one read-only source URL")
    if enabled and solana_ws and not solana_rpc:
        raise ValueError("Solana WebSocket requires a read-only RPC URL for transaction/block truth")
    if solana_indexed_recovery and not solana_rpc:
        raise ValueError("indexed Solana recovery requires a read-only RPC URL")
    if enabled and robinhood_ws and not robinhood_rpc:
        raise ValueError("Robinhood WebSocket requires a read-only RPC URL for block/receipt truth")
    return MemecoinOpportunitySettings(
        enabled=enabled,
        db_path=db_path,
        insight_db_path=insight_db_path,
        solana_rpc_url=solana_rpc,
        solana_recovery_rpc_url=solana_recovery_rpc,
        solana_recovery_rps=solana_recovery_rps,
        solana_indexed_recovery=solana_indexed_recovery,
        solana_ws_url=solana_ws,
        robinhood_rpc_url=robinhood_rpc,
        robinhood_ws_url=robinhood_ws,
        robinhood_v3_quoter=robinhood_v3_quoter,
        queue_max=queue_max,
        stale_after_s=stale_after_s,
        recovery_max_pages=recovery_pages,
        robinhood_poll_s=robinhood_poll_s,
        robinhood_warm_start_blocks=robinhood_warm_start_blocks,
        robinhood_poll_chunk_blocks=robinhood_poll_chunk_blocks,
        primary_clip_usd_micros=25_000_000,
        clip_usd_micros=(5_000_000, 10_000_000, 25_000_000, 50_000_000),
        capture_window_s=capture_window_s,
        candidate_capture_window_s=candidate_capture_window_s,
        hot_retention_s=hot_retention_s,
        insight_retention_s=insight_retention_s,
        alert_retention_s=alert_retention_s,
        cleanup_interval_s=cleanup_interval_s,
        hot_emergency_retention_s=hot_emergency_retention_s,
        hot_soft_cap_bytes=hot_soft_cap_bytes,
        hot_hard_cap_bytes=hot_hard_cap_bytes,
        insight_soft_cap_bytes=insight_soft_cap_bytes,
        insight_hard_cap_bytes=insight_hard_cap_bytes,
        rpc_concurrency=rpc_concurrency,
        rpc_initial_rps=rpc_initial_rps,
        rpc_min_rps=rpc_min_rps,
        rpc_max_rps=rpc_max_rps,
        robinhood_rpc_rps=robinhood_rpc_rps,
        solana_orphan_after_s=solana_orphan_after_s,
        paper_adapter_enabled=paper_adapter_enabled,
        adaptive_v3_enabled=adaptive_v3_enabled,
        paper_adapter_expiry_s=paper_adapter_expiry_s,
        paper_adapter_consumer_heartbeat_path=paper_adapter_consumer_heartbeat_path,
        paper_adapter_consumer_max_age_s=paper_adapter_consumer_max_age_s,
        quote_max_queue_age_ms=quote_max_queue_age_ms,
        quote_max_pair_age_ms=quote_max_pair_age_ms,
        quote_max_completion_age_ms=quote_max_completion_age_ms,
        quote_cache_max=quote_cache_max,
        quote_completion_max=quote_completion_max,
    )


MEMECOIN_OPPORTUNITY_SETTINGS = load_memecoin_opportunity_settings()

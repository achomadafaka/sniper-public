"""Collector configuration — all knobs in one place."""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class CollectorConfig:
    """Configuration for the WS collector and metadata sync."""

    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_url: str = "https://gamma-api.polymarket.com"
    raw_dir: str = os.path.expanduser("~/sniper/raw")
    snapshot_path: str = os.path.expanduser("~/sniper/state/markets_snapshot.json")
    venue: str = "polymarket"
    max_markets: int = 200
    rotation_seconds: int = 3600
    reconnect_base_delay: float = 5.0
    reconnect_max_delay: float = 300.0
    meta_poll_interval: int = 1800  # 30 minutes
    market_reload_seconds: int = 60
    app_ping_seconds: int = 10
    custom_feature_enabled: bool = True
    min_volume: float = 10_000.0
    max_days_to_end: int = 90

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        raw = os.getenv(name)
        if raw is None:
            return default
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def from_env(cls) -> CollectorConfig:
        return cls(
            ws_url=os.getenv("SIM_WS_URL", cls.ws_url),
            gamma_url=os.getenv("SIM_GAMMA_URL", cls.gamma_url),
            raw_dir=os.getenv("SIM_RAW_DIR", os.path.expanduser("~/sniper/raw")),
            snapshot_path=os.getenv("SIM_MARKETS_SNAPSHOT_PATH", os.path.expanduser("~/sniper/state/markets_snapshot.json")),
            venue=os.getenv("SIM_VENUE", cls.venue),
            max_markets=int(os.getenv("SIM_MAX_MARKETS", str(cls.max_markets))),
            rotation_seconds=int(os.getenv("SIM_ROTATION_SECONDS", str(cls.rotation_seconds))),
            meta_poll_interval=int(os.getenv("SIM_META_POLL_INTERVAL", str(cls.meta_poll_interval))),
            market_reload_seconds=int(os.getenv("SIM_MARKET_RELOAD_SECONDS", str(cls.market_reload_seconds))),
            app_ping_seconds=int(os.getenv("SIM_APP_PING_SECONDS", str(cls.app_ping_seconds))),
            custom_feature_enabled=cls._env_bool("SIM_CUSTOM_FEATURE_ENABLED", cls.custom_feature_enabled),
            min_volume=float(os.getenv("SIM_MIN_VOLUME", str(cls.min_volume))),
            max_days_to_end=int(os.getenv("SIM_MAX_DAYS_TO_END", str(cls.max_days_to_end))),
        )

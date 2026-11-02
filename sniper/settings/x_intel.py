"""Strict default-OFF runtime settings for X Intel capture."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path


def _flag(env, name, default="0") -> bool:
    raw = str(env.get(name, default)).strip()
    if raw not in {"0", "1"}:
        raise ValueError(f"{name} must be 0 or 1")
    return raw == "1"


def _positive(env, name, default) -> int:
    try:
        value = int(env.get(name, default))
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer") from None
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class XIntelSettings:
    enabled: bool = False
    bearer_token: str | None = field(default=None, repr=False)
    db_path: Path = Path("data/memecoin_x.db")
    max_post_reads_per_day: int = 500
    recovery_max_pages: int = 10
    alert_max_age_s: int = 900
    clock_offset_file: Path = Path("data/clock_offset.json")
    telegram_enabled: bool = False
    telegram_bot_token: str | None = field(default=None, repr=False)
    telegram_chat_id: str | None = None

    @classmethod
    def from_env(cls, env=os.environ, *, require_enabled=False):
        enabled = _flag(env, "X_INTEL_API_ENABLED")
        if require_enabled and not enabled:
            raise ValueError("X Intel API capture is disabled")
        token = env.get("X_INTEL_BEARER_TOKEN")
        if enabled and not token:
            raise ValueError("X Intel bearer token is required")
        if token and (any(ch.isspace() for ch in token) or any(ord(ch) < 32 for ch in token)):
            raise ValueError("X Intel bearer token contains invalid whitespace")
        db_path = Path(env.get("X_INTEL_DB_PATH", "data/memecoin_x.db")).expanduser()
        banned = {"sniper_tracker.db", "memecoin_tape.db", "memecoin_sol_tape.db"}
        if enabled and db_path.name in banned:
            raise ValueError("X Intel requires an isolated X database")
        telegram_enabled = _flag(env, "X_INTEL_TELEGRAM_ENABLED")
        telegram_token = env.get("TELEGRAM_BOT_TOKEN")
        telegram_chat = env.get("TELEGRAM_CHAT_ID")
        if telegram_enabled and (not telegram_token or not telegram_chat):
            raise ValueError("X Intel Telegram requires bot token and chat ID")
        return cls(
            enabled=enabled, bearer_token=token, db_path=db_path,
            max_post_reads_per_day=_positive(
                env, "X_INTEL_MAX_POST_READS_PER_DAY", 500),
            recovery_max_pages=_positive(env, "X_INTEL_RECOVERY_MAX_PAGES", 10),
            alert_max_age_s=_positive(env, "X_INTEL_ALERT_MAX_AGE_S", 900),
            clock_offset_file=Path(env.get(
                "SNIPER_CLOCK_OFFSET_FILE", "data/clock_offset.json")).expanduser(),
            telegram_enabled=telegram_enabled, telegram_bot_token=telegram_token,
            telegram_chat_id=telegram_chat,
        )

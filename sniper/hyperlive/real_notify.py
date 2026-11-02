"""Telegram notifier for the HL real leg. Reuses the shared memecoin sender (resolves
MEMECOIN_TG_* then TELEGRAM_* env; fail-closed when unconfigured). Never raises —
alerting must not be able to take down the trading loop."""
from __future__ import annotations

import logging

from sniper.services.memecoin_telegram import send_telegram

logger = logging.getLogger(__name__)


def notify(text: str) -> bool:
    try:
        ok, _ = send_telegram(f"[hl-real] {text}")
        return bool(ok)
    except Exception:
        logger.exception("hl-real notify failed text=%r", text)
        return False

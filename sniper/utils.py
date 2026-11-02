import datetime as dt
from typing import Any


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _normalize_wallet(wallet: str | None) -> str:
    return str(wallet or "").strip().lower()


def _normalize_tx_hash(tx_hash: str | None) -> str:
    return str(tx_hash or "").strip().lower()


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    if raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _format_unix(ts: int | float | None) -> str:
    if not ts:
        return ""
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError, OSError):
        return ""


def _short_wallet(wallet: str | None) -> str:
    text = _normalize_wallet(wallet)
    if not text:
        return ""
    if len(text) <= 14:
        return text
    return f"{text[:8]}...{text[-4:]}"


def _parse_any_ts(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = int(value)
        return ts if ts > 0 else None

    raw = str(value).strip()
    if not raw:
        return None
    if raw.isdigit():
        ts = int(raw)
        return ts if ts > 0 else None

    raw = raw.replace("Z", "+00:00")
    parsed: dt.datetime | None = None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        try:
            parsed = dt.datetime.strptime(raw, "%Y-%m-%d")
            parsed = parsed.replace(tzinfo=dt.UTC)
        except ValueError:
            parsed = None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return int(parsed.timestamp())

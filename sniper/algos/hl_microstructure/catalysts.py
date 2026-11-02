"""Conservative news catalyst matching for Hyperliquid symbols."""
from __future__ import annotations

from dataclasses import dataclass
import re

try:
    from sniper.markets.source_meta import get_source_meta
except Exception:  # pragma: no cover - import fallback for isolated use
    def get_source_meta(source_key: str) -> dict:
        return {"reliability": "medium"}


@dataclass(frozen=True)
class CatalystSignal:
    asset: str
    direction: str | None
    score: float
    headline: str
    source: str
    catalyst_type: str
    reason: str
    received_ts_ms: int


_LONG_HINTS = (
    "surge", "surges", "rally", "rallies", "support", "upgrade", "listing",
    "launch", "approval", "partnership", "integrat", "breakout",
)
_SHORT_HINTS = (
    "hack", "exploit", "lawsuit", "probe", "delist", "halt", "outage",
    "crash", "plunge", "fraud", "sec charges",
)
_TYPE_HINTS = {
    "upgrade": ("upgrade", "mainnet", "fork", "integration", "partnership"),
    "listing": ("listing", "listed"),
    "delisting": ("delist", "delisting"),
    "exploit": ("hack", "exploit", "stolen"),
    "legal": ("lawsuit", "probe", "sec charges", "settlement"),
    "macro": ("fed", "cpi", "inflation", "rates"),
}


def _received_ms(item: dict) -> int | None:
    for key in ("received_ts_ms", "receive_ts_ms", "stored_ts_ms", "published_at_ms"):
        if item.get(key) is not None:
            try:
                return int(item[key])
            except (TypeError, ValueError):
                return None
    return None


def _words(text: str) -> set[str]:
    return set(re.findall(r"[A-Z0-9]{2,12}", text.upper()))


def _direction(text_lower: str) -> str | None:
    if any(h in text_lower for h in _SHORT_HINTS):
        return "short"
    if any(h in text_lower for h in _LONG_HINTS):
        return "long"
    return None


def _catalyst_type(text_lower: str) -> str:
    for kind, hints in _TYPE_HINTS.items():
        if any(h in text_lower for h in hints):
            return kind
    return "news"


def _source_weight(source: str) -> float:
    rel = str(get_source_meta(source).get("reliability") or "medium").lower()
    return {"high": 1.0, "medium": 0.75, "low": 0.4}.get(rel, 0.7)


def match_catalysts(
    news_items: list[dict],
    symbols: set[str],
    *,
    now_ms: int,
    max_age_ms: int = 30 * 60 * 1000,
) -> dict[str, CatalystSignal]:
    """Match recent explicit headlines to tradeable Hyperliquid symbols."""
    symbols_u = {s.upper() for s in symbols}
    out: dict[str, CatalystSignal] = {}
    for item in news_items:
        received = _received_ms(item)
        if received is None or int(now_ms) - received > int(max_age_ms):
            continue
        title = str(item.get("title") or item.get("headline") or "")
        if not title:
            continue
        tokens = _words(title)
        matched = sorted(tokens & symbols_u)
        if not matched:
            continue
        lower = title.lower()
        source = str(item.get("source") or item.get("source_name") or "unknown")
        direction = _direction(lower)
        kind = _catalyst_type(lower)
        for asset in matched:
            score = 0.45 * _source_weight(source)
            if direction:
                score += 0.2
            if kind != "news":
                score += 0.2
            signal = CatalystSignal(
                asset=asset,
                direction=direction,
                score=min(1.0, score),
                headline=title,
                source=source,
                catalyst_type=kind,
                reason="explicit_symbol",
                received_ts_ms=received,
            )
            prev = out.get(asset)
            if prev is None or signal.score > prev.score:
                out[asset] = signal
    return out

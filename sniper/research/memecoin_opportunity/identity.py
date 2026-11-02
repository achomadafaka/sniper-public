"""Deterministic serialization, fingerprints, identities, and UTC timestamps."""

from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _jsonable(value: object) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    """Return stable compact JSON, rejecting non-portable non-finite numbers."""

    return json.dumps(
        _jsonable(value),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_fingerprint(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_identity(network_id: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("identity must be a non-empty string")
    normalized = value.strip()
    network = str(getattr(network_id, "value", network_id))
    return normalized.lower() if network == "4663" else normalized


def require_utc_iso(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("timestamp must be a non-empty UTC ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be valid UTC ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    if parsed.utcoffset().total_seconds() != 0:
        raise ValueError("timestamp must use UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

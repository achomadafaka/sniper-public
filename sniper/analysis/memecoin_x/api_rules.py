"""Deterministic Filtered Stream rules for the active curated X roster."""
from __future__ import annotations

import hashlib
import json
import re


_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_SUPPORTED_ROLES = {"tier1_trader", "tier2_source", "official_context"}


def _normalized_accounts(accounts: list[dict]) -> list[dict]:
    if not accounts:
        raise ValueError("active curated roster is empty")
    normalized = []
    seen_handles = set()
    for account in accounts:
        handle = str(account.get("handle") or account.get("display_handle") or "").strip()
        if not _HANDLE_RE.fullmatch(handle):
            raise ValueError(f"unsafe X handle: {handle!r}")
        role = str(account.get("role") or "")
        if role not in _SUPPORTED_ROLES:
            raise ValueError(f"unsupported roster role: {role!r}")
        user_id = str(account.get("x_user_id") or "").strip()
        if not user_id:
            raise ValueError("active curated roster account is missing x_user_id")
        canonical_handle = handle.lower()
        if canonical_handle in seen_handles:
            continue
        seen_handles.add(canonical_handle)
        normalized.append({
            "x_user_id": user_id,
            "handle": canonical_handle,
            "role": role,
        })
    return sorted(normalized, key=lambda row: (row["handle"], row["x_user_id"]))


def roster_hash(accounts: list[dict]) -> str:
    """Return the stable identity of the exact roster used to build rules."""
    raw = json.dumps(_normalized_accounts(accounts), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _rule_value(terms: list[str]) -> str:
    return f"({' OR '.join(terms)}) has:cashtags"


def compile_stream_rules(accounts: list[dict], *, max_rule_length: int = 1024) -> list[dict]:
    """Compile bounded, whole-handle, cashtag-only Filtered Stream rules."""
    normalized = _normalized_accounts(accounts)
    digest = roster_hash(normalized)[:12]
    groups: list[list[str]] = []
    current: list[str] = []
    for account in normalized:
        term = f"from:{account['handle']}"
        candidate = [*current, term]
        if len(_rule_value(candidate)) <= max_rule_length:
            current = candidate
            continue
        if not current:
            raise ValueError(f"max_rule_length cannot fit handle: {account['handle']}")
        groups.append(current)
        current = [term]
        if len(_rule_value(current)) > max_rule_length:
            raise ValueError(f"max_rule_length cannot fit handle: {account['handle']}")
    if current:
        groups.append(current)
    return [
        {"value": _rule_value(group), "tag": f"sniper-x-intel:{digest}:{index}"}
        for index, group in enumerate(groups)
    ]

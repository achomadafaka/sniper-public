"""Solana endpoint selection and offline allocation checks; no network or entry work."""
from __future__ import annotations

import math
import os
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

from sniper.execution.solana_rpc_pool import key_id

_ROLES = {
    "funded": "MEMECOIN_SOLANA_RPC_POOL_URLS",
    "signal": "ONCHAIN_WATCH_SOLANA_RPC_URL",
    "research": "MEMECOIN_RESEARCH_SOLANA_RPC_URLS",
    "read": "MEMECOIN_SOLANA_READ_RPC_URL",
    "tpu": "MEMECOIN_TPU_RPC_URL",
    "cohort": "MEMECOIN_SOL_COHORT_RPC_URL",
}
# Global HTTP product plus exact EU hosts verified in the authenticated account dashboard.
_SHYFT_RPC_HOSTS = {"rpc.shyft.to", "rpc.ams.shyft.to", "rpc.fra.shyft.to"}
_PUBLIC_RPC = "https://api.mainnet-beta.solana.com"


def infra_provider(env=None) -> str:
    env = os.environ if env is None else env
    profile = (env.get("MEMECOIN_SOLANA_INFRA_PROVIDER") or "legacy").strip().lower()
    if profile not in {"legacy", "shyft"}:
        raise ValueError("provider_unknown")
    return profile


def _listed(env, name):
    return tuple(u.strip() for u in (env.get(name) or "").split(",") if u.strip())


def _one(env, name):
    url = (env.get(name) or "").strip()
    return (url,) if url else ()


def _legacy_urls(role, env):
    if role == "funded":
        from sniper.settings.memecoin import solana_funded_rpc_urls
        return solana_funded_rpc_urls(env)
    if role == "research":
        return _listed(env, "MEMECOIN_RESEARCH_HELIUS_RPC_URLS") or _one(
            env, "MEMECOIN_RESEARCH_HELIUS_RPC_URL")
    if role == "signal":
        return _one(env, "ONCHAIN_WATCH_HELIUS_RPC_URL") or _legacy_urls("research", env)[:1]
    if role == "cohort":
        return _one(env, "MEMECOIN_SOL_COHORT_HELIUS_URL")
    if role == "read":
        return _one(env, "MEMECOIN_SOLANA_READ_RPC_URL") if "MEMECOIN_SOLANA_READ_RPC_URL" in env \
            else (_PUBLIC_RPC,)
    return _one(env, "MEMECOIN_TPU_RPC_URL") or _legacy_urls("funded", env)[-1:]


def _validate_url(url, role):
    try:
        parts = urlsplit(url)
        params = parse_qs(parts.query, keep_blank_values=True)
        keys = params.get("api_key", [])
        aliases = params.get("api-key", [])
        valid = (parts.scheme == "https" and parts.hostname in _SHYFT_RPC_HOSTS
                 and parts.port in {None, 443} and not parts.username and not parts.password
                 and not parts.fragment and len(keys) == 1 and bool(keys[0].strip())
                 and (not aliases or aliases == keys))
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(role + "_url_invalid") from None


def provider_urls(role: str, env=None) -> tuple[str, ...]:
    """Select explicit Shyft role URLs, or retain legacy lookup rules.

    Request only enabled roles at startup. URL/key validation cannot establish account isolation;
    the separate offline allocation record must establish that before activation.
    """
    env = os.environ if env is None else env
    if not isinstance(role, str) or role not in _ROLES:
        raise ValueError("role_unknown")
    if infra_provider(env) == "legacy":
        return _legacy_urls(role, env)
    urls = (_listed if role in {"funded", "research"} else _one)(env, _ROLES[role])
    if role == "funded":
        urls += _one(env, "MEMECOIN_SOLANA_RPC_URL")
    if not urls:
        raise ValueError(role + "_missing")
    for url in urls:
        _validate_url(url, role)
    if len({key_id(url) for url in urls}) != len(urls):
        raise ValueError(role + "_duplicate")
    if role == "research":
        from sniper.services.memecoin_research_rpc import funded_overlap
        funded = _listed(env, "MEMECOIN_SOLANA_RPC_POOL_URLS") + _one(
            env, "MEMECOIN_SOLANA_RPC_URL")
        for url in funded:
            _validate_url(url, "funded")
        if funded_overlap(urls, env):
            raise ValueError("research_funded_overlap")
    return urls


def validate_shyft_capacity(record: dict) -> tuple[str, ...]:
    """Refuse incomplete or shared offline allocation attestations using fixed safe codes.

    `allocations` contains per-role account/scope, capacity, region, verification time and evidence.
    Shared accounts/scopes require each role's `reserved_capacity_evidence`, except the explicit
    `usage=funded_only` record: one funded allocation covers all critical subroles jointly and
    may retain an undocumented filter limit as None. Values are never
    summed: repeat account quotas are one entitlement, not independent capacity. Demand and
    reconnect headroom must be compared with the authenticated entitlement by deployment preflight.
    """
    rows = record.get("allocations") if isinstance(record, dict) else None
    if not isinstance(rows, list) or not rows:
        return ("allocations_missing",)
    reasons = []
    funded_only = record.get("usage") == "funded_only"
    if funded_only and (len(rows) != 1 or not isinstance(rows[0], dict)
                        or rows[0].get("role") != "funded"):
        reasons.append("funded_only_allocation_invalid")
    for row in rows:
        if not isinstance(row, dict):
            reasons.append("allocation_invalid")
            continue
        if not isinstance(row.get("role"), str) or row["role"] not in _ROLES:
            reasons.append("role_invalid")
        for field in ("account_label", "quota_scope_label", "region", "allocation_evidence"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                reasons.append(field + "_missing")
        for field in ("rpc_rps", "send_rps", "grpc_connections", "grpc_filters"):
            value = row.get(field)
            if funded_only and field == "grpc_filters" and value is None:
                continue
            if (type(value) not in {int, float} or (type(value) is float and not math.isfinite(value)) or value < 0
                    or (field.startswith("grpc_") and type(value) is not int)):
                reasons.append(field + "_invalid")
        try:
            verified = datetime.fromisoformat(row.get("verified_at", "").replace("Z", "+00:00"))
            if verified.utcoffset() is None:
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            reasons.append("verified_at_invalid")
    for field, reason in (("quota_scope_label", "quota_scope_shared"),
                          ("account_label", "account_shared")):
        groups = {}
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get(field), str) and row[field].strip():
                groups.setdefault(row[field].strip(), []).append(row)
        for group in groups.values():
            if len({row.get("role") for row in group if isinstance(row.get("role"), str)}) > 1:
                if any(not isinstance(row.get("reserved_capacity_evidence"), str)
                       or not row["reserved_capacity_evidence"].strip() for row in group):
                    reasons.append(reason)
    return tuple(dict.fromkeys(reasons))

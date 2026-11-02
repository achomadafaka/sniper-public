"""Deterministic Office Risk (pilot seat; spec §2 Risk, prereg Am1): veto or the FULL frozen clip, never a size cut.

Inputs are the frozen OfficeBookPolicy, the trusted decision bundle identity and the sheet's safety block
(memecoin_desk_contract.safety_from_evidence: authority_mint/freeze, sell_probe, sells_exist_60s, ...). An unknown
safety fact is a veto, never an allow. Pure: no model, no network, no I/O."""
from __future__ import annotations

from dataclasses import dataclass

from sniper.office.ledger import SUPPORTED_CHAINS, _hash, policy_refusal

RISK_VERSION = "office-risk-pilot-v1"


@dataclass(frozen=True)
class RiskResult:
    decision: str                       # 'allow' | 'veto'
    reason: str | None
    clip_usd: float | None              # the full frozen clip on allow, None on veto
    risk_version: str
    bundle_hash: str | None             # the exact bundle and policy this verdict is for (Zeta): never reusable
    decision_id: str | None
    policy_hash: str | None


def risk_check(policy, *, bundle, safety_blocks) -> RiskResult:
    b = bundle if isinstance(bundle, dict) else {}
    try:
        ph = _hash(policy.frozen())
    except (ValueError, TypeError):
        ph = None
    decision, reason, clip = _verdict(policy, bundle, safety_blocks)
    return RiskResult(decision, reason, clip, RISK_VERSION, b.get("bundle_hash"), b.get("decision_id"), ph)


def _veto(reason):
    return "veto", reason, None


def _verdict(policy, bundle, safety_blocks):
    bad = policy_refusal(policy)
    if bad:
        return _veto("policy:" + bad)
    if not policy.enabled:
        return _veto("office_book_disabled")
    if not isinstance(bundle, dict) or bundle.get("decision") != "ENTER":
        return _veto("not_enter")
    if bundle.get("network_id") not in SUPPORTED_CHAINS or bundle.get("network_id") not in policy.chains:
        return _veto("chain_not_supported")
    if not isinstance(safety_blocks, dict):
        return _veto("safety_blocks_missing")
    s = safety_blocks
    if s.get("authority_mint") != "closed":
        return _veto("authority_mint_not_closed")
    if s.get("authority_freeze") != "closed":
        return _veto("authority_freeze_not_closed")
    if s.get("sell_probe") != "ok":
        return _veto("sell_probe_not_ok")
    if s.get("sells_exist_60s") is False:
        return _veto("no_sells_60s")
    if s.get("sells_exist_60s") is not True and s.get("sells_unknown_allowed_by_probe") is not True:     # partial sheets only (Alpha 2026-10-31): sells unknown, stored sell probe passed; a proven zero vetoed above
        return _veto("sells_60s_unknown")
    return "allow", None, float(policy.clip_usd)

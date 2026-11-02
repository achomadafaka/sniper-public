"""Archetype-led wallet SIGNAL PROFILE — a compact, explainable triage layer over already-computed
analyzer outputs (archetype classification + thesis edge read + cached-activity Q75). This is NOT a
grade or a score: it ranks research attention and filters obviously-bad wallets off the Live Signals
board. Everything here is an IN-SAMPLE read — the analyzer identifies behaviour patterns, it has not
proven live edge. Out-of-sample validation is required before any wallet/trade is acted on. No insider_score.
"""
from __future__ import annotations

# copy_policy tiers from wallet_archetype_extractor: only validated_copy / reverse_engineer_candidate
# describe a DIRECTIONAL pick worth surfacing. exclude/study_only aren't — and `manual_review` is the
# extractor's UNCLASSIFIABLE fallback (basket builders, mixed_unresolved, directional_unknown), which
# is "human hasn't decided", NOT a directional profile.
_DIRECTIONAL = {"validated_copy", "reverse_engineer_candidate"}

# Authoritative thesis-analyzer verdict -> read strength. Only the analyzer (proper shuffle/MC test)
# may assert "profitable_provable"; we never mint it from a bare ratio. Still an IN-SAMPLE read.
_READ_STRENGTH = {"profitable_provable": "high", "profitable_unproven": "medium",
                  "mixed": "low", "net_loser": "low"}


def _in_sample_edge_read(thesis: dict) -> tuple[str, str]:
    """In-sample edge READ + read-strength. PREFER the thesis analyzer's own verdict (proper test).
    As a FALLBACK read the raw shuffle ratio — but a bare ratio, however large, is in-sample, so it
    caps at 'profitable_unproven' (never 'provable'). This is a hypothesis, not a certification."""
    t = thesis or {}
    authoritative = t.get("edge_verdict")
    if isinstance(authoritative, str) and authoritative in _READ_STRENGTH:
        return authoritative, _READ_STRENGTH[authoritative]
    over = (t.get("shuffle_baseline") or {}).get("obs_over_expected")
    if over is None:
        return "profitable_unproven", "low"
    if over >= 2:
        return "profitable_unproven", "high"      # strong in-sample shuffle ratio — still UNPROVEN
    if over >= 1.3:
        return "profitable_unproven", "medium"
    return "mixed", "low"


def _edge_domains(thesis: dict) -> list:
    """Profitable edge-cluster labels (loss centers excluded), PnL-ranked, deduped."""
    ec = [c for c in ((thesis or {}).get("edge_clusters") or []) if not c.get("is_loss_center")]
    ec = sorted(ec, key=lambda c: abs(float(c.get("pnl") or 0)), reverse=True)
    seen: set = set()
    out: list = []
    for c in ec:
        lbl = c.get("label")
        if lbl and lbl not in seen:
            seen.add(lbl)
            out.append(lbl)
    return out[:4]


def build_profile(thesis: dict, archetype_report: dict, *, q75_usdc: float = 0.0) -> dict:
    """Build the explainable signal PROFILE (not a grade/score). directional_profile = the wallet
    makes directional picks (vs arb/basket/MM); in_sample_edge_read = the analyzer's in-sample edge
    hypothesis (validation required). The description leads with the REASONS, never a single label."""
    cls = (archetype_report or {}).get("classification") or {}
    l2 = (archetype_report or {}).get("classification_layer2") or {}
    policy = cls.get("copy_policy") or "study_only"
    archetype = cls.get("archetype") or "unknown"
    intent = l2.get("archetype")
    edge_read, read_strength = _in_sample_edge_read(thesis)
    domains = _edge_domains(thesis)
    primary_tag = ((archetype_report or {}).get("domain_tags") or {}).get("primary_tag")
    directional = policy in _DIRECTIONAL
    arch_h = archetype.replace("_", " ")
    if directional and domains:
        desc = (f"{arch_h} ({intent or '—'}); in-sample edge read in {', '.join(domains[:2])} "
                f"({edge_read.replace('_', ' ')}) — validation required.")
    elif directional:
        desc = (f"{arch_h} ({intent or '—'}); {edge_read.replace('_', ' ')} in-sample, no named "
                f"edge domain yet — validation required.")
    else:
        desc = f"{arch_h} — {policy.replace('_', ' ')} (not a directional profile)."
    return {
        "archetype": archetype,
        "intent": intent,
        "archetype_confidence": cls.get("confidence") or "low",
        "copy_policy": policy,
        "directional_profile": directional,
        "in_sample_edge_read": edge_read,
        "edge_read_strength": read_strength,
        "edge_domains": domains,
        "primary_tag": primary_tag,
        "q75_usdc": float(q75_usdc or 0.0),
        "description": desc,
    }

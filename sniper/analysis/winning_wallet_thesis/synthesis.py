"""Synthesis — fuse the history-rich thesis engine and the fresh-anomaly engine into ONE
coherent read: a single primary_verdict + one operational_route + a preserved evidence
profile. The precedence is the 'no nonsense' guarantee — collisions resolve here, never
in flag-soup.

Design-review rules baked in:
- structural archetype that explains MOST activity is the primary mechanism, but a
  directional residual is RETAINED in the evidence/edge_location (not erased).
- a confirmed fresh anomaly (multi-channel + FDR + no alt-explanation) is checked BEFORE
  'insufficient', so genuinely sharp newcomers are not suppressed.
- 'established_directional_edge' requires enough INDEPENDENT history (set upstream in
  established_edge_supported), so a thin lucky wallet can never collide with it.
- a deep wallet's suspicious recent burst changes the ROUTE (expedited_review), never the
  lifetime verdict.
NEVER emits the word 'insider'.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ThesisView:
    """Adapter over ThesisProfile — only the fields synthesis reads (orchestrator fills it)."""
    assessable: bool = False
    established_edge_supported: bool = False
    emerging_positive: bool = False
    negative_evidence: bool = False
    directional_residual_supported: bool = False
    obs_over_expected: float = 1.0
    p_value: float = 1.0
    e_value: float = 1.0
    history_depth: str = "thin"          # thin | moderate | deep
    n_independent_events: int = 0
    edge_domains: list = field(default_factory=list)
    loss_centers: list = field(default_factory=list)
    entry_shape: dict = field(default_factory=dict)
    recent_anomaly_level: str = "none"
    regime_shift_candidate: bool = False
    realized_pnl: float = 0.0


@dataclass
class ArchetypeView:
    archetype: str = "unknown"
    intent: str = ""
    structural_share_high: bool = False
    structural_share: float = 0.0


@dataclass
class QualityView:
    data_fresh: bool = True
    analyzer_version: int = 0
    rule_version: int = 0
    data_as_of: int = 0
    source_snapshot_hash: str = ""
    fdr_pass: bool = False


@dataclass
class WalletRead:
    primary_verdict: str
    operational_route: str
    headline: str
    reasons: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    edge_location: dict = field(default_factory=dict)
    evidence_profile: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


VERDICTS = (
    "insufficient_evidence", "structural_non_directional", "fresh_high_surprise",
    "fresh_high_conviction", "established_directional_edge", "emerging_directional_evidence",
    "negative_edge_or_antipattern", "no_supported_edge",
)

# Survivorship caveat for fresh_high_conviction — a thin-history big-bet wallet has a HIGH
# false-positive base rate. We surface it as a strategy-review candidate, never proven edge.
_FRESH_CONVICTION_WARNING = (
    "Thin history with a large-conviction shape (big, concentrated bets). This has a HIGH "
    "false-positive base rate — you may be seeing a winner only because it won (survivorship). "
    "WATCH / paper validation only — not proven edge, not advice."
)


def _fresh_high_conviction(thesis: ThesisView, fresh) -> bool:
    """A THIN-history wallet whose ex-ante shape is large-conviction (freshly-funded / concentrated /
    big bets — fresh.ex_ante_level moderate|strong) AND that actually made money. No statistical
    surprise is claimed (that's fresh_high_surprise); this is the 'came from nowhere, bet huge, WON'
    shape we surface for strategy review instead of burying it as no_supported_edge.

    Gates (CODEX P1): require an ASSESSABLE fresh read (no ex-ante shape without evidence behind it),
    no structural/churn exclude, no alternative explanation (a public_truth_stale buyer is an artifact,
    not conviction), and POSITIVE realized PnL (else the 'made money' headline would be false and there
    is no winner to follow). Deep wallets are judged by the shuffle test, so the thin guard keeps them
    out."""
    return bool(getattr(fresh, "assessable", False)
                and getattr(fresh, "history_thin", False)
                and thesis.history_depth == "thin"
                and not getattr(fresh, "excludes", None)
                and not getattr(fresh, "alt_explanation", None)
                and thesis.realized_pnl > 0
                and getattr(fresh, "ex_ante_level", "none") in ("moderate", "strong"))


def _fresh_confirmed(fresh, quality: QualityView) -> bool:
    return bool(getattr(fresh, "assessable", False)
                and getattr(fresh, "history_thin", False)
                and getattr(fresh, "high_surprise", False)
                and getattr(fresh, "channels_strong", 0) >= 2
                and not getattr(fresh, "alt_explanation", None)
                and quality.fdr_pass)


def _pick_verdict(thesis: ThesisView, fresh, archetype: ArchetypeView, quality: QualityView) -> str:
    # Structural if EITHER the archetype extractor says so OR the fresh engine's hard exclude found a
    # hedger/arb (holds both outcomes of >= half its cost). The fresh exclude is a strong structural
    # signal the archetype snapshot sometimes misses (e.g. it returns 'mixed_unresolved') — without
    # this, a clear two-sided trader falls through to 'no_supported_edge' instead of structural.
    if archetype.structural_share_high or getattr(fresh, "excludes", None) == "structural":
        return "structural_non_directional"
    if _fresh_confirmed(fresh, quality):
        return "fresh_high_surprise"
    if thesis.established_edge_supported:
        return "established_directional_edge"
    if thesis.emerging_positive:
        return "emerging_directional_evidence"
    if thesis.negative_evidence:
        return "negative_edge_or_antipattern"
    # Fresh + large-conviction shape: surface as its own verdict for strategy review rather
    # than burying the "came from nowhere, bet huge" wallet as no_supported_edge. Checked AFTER the
    # provable verdicts (established/emerging/negative) so it never overrides a real read.
    if _fresh_high_conviction(thesis, fresh):
        return "fresh_high_conviction"
    # insufficient ONLY when NEITHER engine can render a judgment — a fresh-assessable wallet
    # (even one whose anomaly isn't confirmed) is not "insufficient", it's "nothing supported yet"
    # and may still be worth monitoring (handled in derive_route via the fresh ex-ante signal).
    if not thesis.assessable and not getattr(fresh, "assessable", False):
        return "insufficient_evidence"
    return "no_supported_edge"


def derive_route(verdict: str, *, recent_anomaly_level: str = "none",
                 directional_residual: bool = False, fresh_ex_ante: str = "none",
                 data_fresh: bool = True) -> str:
    if verdict == "fresh_high_surprise":
        return "expedited_review"
    if verdict == "established_directional_edge":
        return "expedited_review" if recent_anomaly_level == "strong" else "strategy_candidate"
    if verdict == "emerging_directional_evidence":
        return "strategy_candidate"
    if verdict == "fresh_high_conviction":
        return "strategy_candidate"
    if verdict == "structural_non_directional":
        return "monitor" if directional_residual else "ignore"
    if verdict in ("insufficient_evidence", "no_supported_edge"):
        # an ex-ante fresh signal (e.g. a freshly-funded, concentrated, large-bet account whose
        # surprise isn't yet confirmed) is worth monitoring; stale data needs a refresh
        if fresh_ex_ante in ("moderate", "strong") or not data_fresh:
            return "monitor"
        return "ignore"
    return "ignore"  # negative_edge_or_antipattern


def _headline(verdict: str, thesis: ThesisView, fresh, archetype: ArchetypeView) -> str:
    domains = ", ".join(thesis.edge_domains) if thesis.edge_domains else "no clear domain"
    losses = ", ".join(thesis.loss_centers) if thesis.loss_centers else ""
    if verdict == "structural_non_directional":
        tail = f"; a directional residual in {domains} is retained for review" if thesis.directional_residual_supported else ""
        return (f"Structural / non-directional ({archetype.archetype}) — PnL is mechanical, "
                f"not directional picks{tail}.")
    if verdict == "fresh_high_surprise":
        return (f"Fresh account with unusually concentrated, high-surprise results in {domains} "
                f"and entry timing that warrants expedited review — validation required.")
    if verdict == "established_directional_edge":
        burst = (" with an unusual recent cluster — routed for expedited review"
                 if thesis.recent_anomaly_level == "strong" else "")
        return f"Established directional edge in {domains}{burst}."
    if verdict == "fresh_high_conviction":
        where = f" (so far in {domains})" if thesis.edge_domains else ""
        return (f"Fresh wallet, big conviction{where} — made money but no provable repeatable rule yet. "
                f"Could be foresight or just survivorship; paper validation before leaning on it.")
    if verdict == "emerging_directional_evidence":
        return f"Emerging directional evidence in {domains} — positive but not yet significant."
    if verdict == "negative_edge_or_antipattern":
        where = f" (bleeds in {losses})" if losses else ""
        return f"Negative expectancy / antipattern{where} — not a follow candidate."
    if verdict == "insufficient_evidence":
        return "Insufficient evidence to judge edge yet — monitoring for more resolved bets."
    return "No supported edge."


def synthesize_read(thesis: ThesisView, fresh, archetype: ArchetypeView,
                    quality: QualityView) -> WalletRead:
    """Collapse the two engines + archetype + quality into one WalletRead. Pure."""
    verdict = _pick_verdict(thesis, fresh, archetype, quality)
    route = derive_route(
        verdict,
        recent_anomaly_level=thesis.recent_anomaly_level,
        directional_residual=thesis.directional_residual_supported,
        fresh_ex_ante=getattr(fresh, "ex_ante_level", "none"),
        data_fresh=quality.data_fresh,
    )

    reasons: list[str] = []
    if thesis.edge_domains:
        reasons.append(f"profitable in {', '.join(thesis.edge_domains)}")
    if thesis.loss_centers:
        reasons.append(f"loses in {', '.join(thesis.loss_centers)}")
    reasons.extend(getattr(fresh, "reasons", []) or [])

    warnings: list[str] = []
    sw = getattr(fresh, "sample_warning", "")
    if verdict == "fresh_high_surprise" and sw:
        warnings.append(sw)
    if verdict == "fresh_high_conviction":
        warnings.append(_FRESH_CONVICTION_WARNING)
    warnings.append("In-sample read — a hypothesis to forward-test, not proven edge, not advice.")

    edge_location = {
        "domains": list(thesis.edge_domains),
        "loss_centers": list(thesis.loss_centers),
        "entry_shape": dict(thesis.entry_shape),
    }

    evidence_profile = {
        "history_depth": thesis.history_depth,
        "n_independent_events": max(thesis.n_independent_events,
                                    getattr(fresh, "n_independent_events", 0)),
        "effective_sample_size": getattr(fresh, "effective_sample_size", 0.0),
        "edge_evidence": {
            "e_value": thesis.e_value,
            "anytime_p": getattr(fresh, "anytime_p", 1.0),
            "obs_over_expected": thesis.obs_over_expected,
            "fixed_p_value": thesis.p_value,
            "fdr_pass": quality.fdr_pass,
        },
        "fresh": {
            "assessable": getattr(fresh, "assessable", False),
            "ex_ante_level": getattr(fresh, "ex_ante_level", "none"),
            "ex_post_level": getattr(fresh, "ex_post_level", "none"),
            "coordination_level": getattr(fresh, "coordination_level", "none"),
            "channels": getattr(fresh, "channels", {}),
            "channels_strong": getattr(fresh, "channels_strong", 0),
            "high_surprise": getattr(fresh, "high_surprise", False),
            "alt_explanation": getattr(fresh, "alt_explanation", None),
            "shrunk_oe": getattr(fresh, "shrunk_oe", 1.0),
        },
        "recent_anomaly_level": thesis.recent_anomaly_level,
        "regime_shift_candidate": thesis.regime_shift_candidate,
        "structural_share": archetype.structural_share,
        "archetype": archetype.archetype,
        "intent": archetype.intent,
    }

    provenance = {
        "analyzer_version": quality.analyzer_version,
        "rule_version": quality.rule_version,
        "data_as_of": quality.data_as_of,
        "source_snapshot_hash": quality.source_snapshot_hash,
    }

    return WalletRead(
        primary_verdict=verdict, operational_route=route,
        headline=_headline(verdict, thesis, fresh, archetype),
        reasons=reasons, warnings=warnings, edge_location=edge_location,
        evidence_profile=evidence_profile, provenance=provenance,
    )

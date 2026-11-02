"""Synthesis — two engines in, ONE coherent read out. Mutually-exclusive primary verdict
+ operational route + preserved evidence. Every precedence collision is a golden case here:
arb-with-huge-PnL never reads as edge; a deep wallet's recent burst changes the route, not
the lifetime verdict; a thin lucky wallet can't satisfy 'established'."""
from __future__ import annotations

from sniper.analysis.winning_wallet_thesis import synthesis as SY
from sniper.analysis.winning_wallet_thesis.synthesis import ThesisView, ArchetypeView, QualityView
from helpers_thesis import fresh_view


def _q(**kw):
    return QualityView(data_fresh=True, analyzer_version=1, rule_version=1, data_as_of=0,
                       source_snapshot_hash="h", fdr_pass=kw.get("fdr_pass", True))


def _thesis(**kw):
    base = dict(assessable=True, established_edge_supported=False, emerging_positive=False,
                negative_evidence=False, directional_residual_supported=False, obs_over_expected=1.0,
                p_value=1.0, e_value=1.0, history_depth="deep", n_independent_events=100,
                edge_domains=[], loss_centers=[], entry_shape={}, recent_anomaly_level="none",
                regime_shift_candidate=False, realized_pnl=0.0)
    base.update(kw)
    return ThesisView(**base)


def test_thin_high_conviction_is_fresh_high_conviction_not_no_edge():
    # REAL-DATA (gardenshed $956K / GRIMDRIP $7.5M): a thin-history wallet with a large-conviction
    # ex-ante shape (freshly-funded / concentrated / big bets) currently falls to 'no_supported_edge'
    # (the card chip reads "No edge"), burying exactly the fresh-whale signal we most want to surface.
    # It must get its OWN verdict -> a strategy-review candidate, always carrying the survivorship caveat.
    r = SY.synthesize_read(
        _thesis(assessable=True, history_depth="thin", n_independent_events=10, realized_pnl=956_000),
        fresh_view(assessable=True, history_thin=True, ex_ante_level="strong", high_surprise=False),
        ArchetypeView("mixed_unresolved", "", structural_share_high=False, structural_share=0.1),
        _q(fdr_pass=False))
    assert r.primary_verdict == "fresh_high_conviction"
    assert r.operational_route == "strategy_candidate"
    assert any("survivorship" in w.lower() for w in r.warnings)   # honest caveat is mandatory


def test_losing_thin_wallet_not_promoted_no_made_money_claim():
    # CODEX P1: a thin, large-conviction wallet that LOST money must NOT become fresh_high_conviction —
    # the 'made money' headline would be factually false. It stays no_supported (monitor via ex-ante).
    r = SY.synthesize_read(
        _thesis(assessable=True, history_depth="thin", n_independent_events=8, realized_pnl=-9000),
        fresh_view(assessable=True, history_thin=True, ex_ante_level="strong"),
        ArchetypeView("mixed_unresolved", "", structural_share_high=False, structural_share=0.1),
        _q(fdr_pass=False))
    assert r.primary_verdict != "fresh_high_conviction"
    assert "made money" not in r.headline.lower()


def test_stale_buyer_thin_not_fresh_high_conviction():
    # CODEX P1: alt_explanation=public_truth_stale (bought AFTER the outcome was public) is an ARTIFACT,
    # not conviction edge — must not be promoted as a high-conviction whale.
    r = SY.synthesize_read(
        _thesis(assessable=True, history_depth="thin", n_independent_events=8, realized_pnl=500_000),
        fresh_view(assessable=True, history_thin=True, ex_ante_level="strong",
                   alt_explanation="public_truth_stale"),
        ArchetypeView("mixed_unresolved", "", structural_share_high=False, structural_share=0.1),
        _q(fdr_pass=False))
    assert r.primary_verdict != "fresh_high_conviction"


def test_nonassessable_exante_not_fresh_high_conviction():
    # CODEX P1 (defense-in-depth): synthesis must not promote an ex-ante shape with NO assessable fresh
    # read behind it — there is no edge evidence to forward-test.
    r = SY.synthesize_read(
        _thesis(assessable=False, history_depth="thin", n_independent_events=0, realized_pnl=0.0),
        fresh_view(assessable=False, history_thin=True, ex_ante_level="strong"),
        ArchetypeView("mixed_unresolved", "", structural_share_high=False, structural_share=0.1),
        _q(fdr_pass=False))
    assert r.primary_verdict != "fresh_high_conviction"


def test_thin_low_conviction_stays_no_supported():
    # GUARD: a thin wallet WITHOUT the large-conviction ex-ante shape is still 'no_supported_edge'.
    # We only promote the high-conviction shape, not every thin/small dabbler.
    r = SY.synthesize_read(
        _thesis(assessable=True, history_depth="thin", n_independent_events=5, realized_pnl=100),
        fresh_view(assessable=True, history_thin=True, ex_ante_level="none"),
        ArchetypeView("mixed_unresolved", "", structural_share_high=False, structural_share=0.1),
        _q(fdr_pass=False))
    assert r.primary_verdict == "no_supported_edge"


def test_deep_wallet_never_fresh_high_conviction():
    # GUARD: fresh_high_conviction is for THIN history only — a deep wallet with a big ex_ante shape
    # is judged by the shuffle test, never promoted as a fresh whale.
    r = SY.synthesize_read(
        _thesis(assessable=True, history_depth="deep", n_independent_events=300, realized_pnl=500_000),
        fresh_view(assessable=True, history_thin=False, ex_ante_level="strong"),
        ArchetypeView("mixed_unresolved", "", structural_share_high=False, structural_share=0.1),
        _q(fdr_pass=False))
    assert r.primary_verdict != "fresh_high_conviction"


def test_arb_huge_pnl_is_structural():                         # case 5
    r = SY.synthesize_read(_thesis(realized_pnl=5_000_000), fresh_view(assessable=False),
                           ArchetypeView("paired_outcome_arb", "structural_bias_exploiter",
                                         structural_share_high=True, structural_share=0.95), _q())
    assert r.primary_verdict == "structural_non_directional"
    assert r.operational_route == "ignore"


def test_arb_with_directional_residual_retains_evidence():     # case 6
    r = SY.synthesize_read(
        _thesis(established_edge_supported=True, directional_residual_supported=True,
                obs_over_expected=2.4, p_value=0.001, e_value=50, edge_domains=["sports"],
                realized_pnl=1_000_000),
        fresh_view(assessable=False),
        ArchetypeView("two_sided_inventory_trader", "structural_bias_exploiter",
                      structural_share_high=True, structural_share=0.8), _q())
    assert r.primary_verdict == "structural_non_directional"
    assert "sports" in r.edge_location["domains"]              # secondary directional evidence retained
    assert r.operational_route == "monitor"                    # residual worth watching, not ignore


def test_established_with_recent_burst_keeps_verdict_changes_route():  # case 7
    r = SY.synthesize_read(
        _thesis(established_edge_supported=True, directional_residual_supported=True,
                obs_over_expected=2.0, p_value=0.004, e_value=40, edge_domains=["geopolitics"],
                recent_anomaly_level="strong", regime_shift_candidate=True, realized_pnl=22_000_000),
        fresh_view(assessable=False),
        ArchetypeView("directional_discretionary", "thesis_specialist",
                      structural_share_high=False, structural_share=0.1), _q())
    assert r.primary_verdict == "established_directional_edge"
    assert r.operational_route == "expedited_review"
    assert r.evidence_profile["recent_anomaly_level"] == "strong"
    assert r.evidence_profile["regime_shift_candidate"] is True


def test_established_no_burst_is_strategy_candidate():
    r = SY.synthesize_read(
        _thesis(established_edge_supported=True, directional_residual_supported=True,
                p_value=0.004, e_value=40, edge_domains=["politics"], realized_pnl=500_000),
        fresh_view(assessable=False),
        ArchetypeView("directional_discretionary", "thesis_specialist", False, 0.1), _q())
    assert r.primary_verdict == "established_directional_edge"
    assert r.operational_route == "strategy_candidate"


def test_high_wr_negative_pnl_is_antipattern():               # case 8
    r = SY.synthesize_read(
        _thesis(negative_evidence=True, obs_over_expected=0.8, e_value=0.5,
                loss_centers=["crypto"], realized_pnl=-40_000),
        fresh_view(assessable=False),
        ArchetypeView("directional_discretionary", "noise", False, 0.1), _q())
    assert r.primary_verdict == "negative_edge_or_antipattern"
    assert r.operational_route == "ignore"


def test_fresh_high_surprise_needs_two_channels_and_fdr():
    r = SY.synthesize_read(
        _thesis(assessable=False, history_depth="thin", n_independent_events=4, edge_domains=["pol"],
                realized_pnl=50_000),
        fresh_view(assessable=True, history_thin=True, channels_strong=2, alt_explanation=None,
                   high_surprise=True, e_value=80, ex_post_level="strong"),
        ArchetypeView("directional_discretionary", "directional_unknown", False, 0.1), _q(fdr_pass=True))
    assert r.primary_verdict == "fresh_high_surprise"
    assert r.operational_route == "expedited_review"
    blob = (r.headline + " " + " ".join(r.warnings)).lower()
    assert "insider" not in blob
    assert r.warnings  # carries the sample_warning


def test_fresh_blocked_when_fdr_fails():
    r = SY.synthesize_read(
        _thesis(assessable=False, history_depth="thin", n_independent_events=2, realized_pnl=1000),
        fresh_view(assessable=True, history_thin=True, channels_strong=2, alt_explanation=None,
                   high_surprise=True, e_value=4, ex_post_level="moderate"),
        ArchetypeView("directional_discretionary", "directional_unknown", False, 0.1), _q(fdr_pass=False))
    assert r.primary_verdict != "fresh_high_surprise"


def test_fresh_blocked_when_alt_explanation():
    r = SY.synthesize_read(
        _thesis(assessable=False, history_depth="thin", n_independent_events=1),
        fresh_view(assessable=True, history_thin=True, channels_strong=2,
                   alt_explanation="public_truth_stale", high_surprise=False, ex_post_level="none"),
        ArchetypeView("directional_discretionary", "directional_unknown", False, 0.1), _q())
    assert r.primary_verdict != "fresh_high_surprise"


def test_both_unassessable_is_insufficient():
    r = SY.synthesize_read(_thesis(assessable=False), fresh_view(assessable=False),
                           ArchetypeView("mixed_unresolved", "directional_unknown", False, 0.1), _q())
    assert r.primary_verdict == "insufficient_evidence"


def test_no_supported_edge_when_thesis_flat():
    r = SY.synthesize_read(_thesis(assessable=True, realized_pnl=10.0),
                           fresh_view(assessable=False),
                           ArchetypeView("directional_discretionary", "noise", False, 0.1), _q())
    assert r.primary_verdict == "no_supported_edge"
    assert r.operational_route == "ignore"


def test_fresh_assessable_unconfirmed_is_no_supported_not_insufficient():
    # M1: thesis blind + fresh assessable-but-unconfirmed -> no_supported_edge (not insufficient),
    # and an ex-ante fresh signal still routes to monitor.
    r = SY.synthesize_read(
        _thesis(assessable=False),
        fresh_view(assessable=True, ex_post_level="weak", high_surprise=False, ex_ante_level="strong"),
        ArchetypeView("directional_discretionary", "directional_unknown", False, 0.1), _q())
    assert r.primary_verdict == "no_supported_edge"
    assert r.operational_route == "monitor"


def test_edge_location_always_populated():
    r = SY.synthesize_read(_thesis(edge_domains=["sports"], loss_centers=["politics"]),
                           fresh_view(assessable=False),
                           ArchetypeView("directional_discretionary", "noise", False, 0.1), _q())
    assert r.edge_location["domains"] == ["sports"]
    assert r.edge_location["loss_centers"] == ["politics"]


def test_fresh_structural_exclude_drives_structural_verdict():
    # REAL-DATA (0x84dbb710, +$1.8M two-sided hedger): archetype extractor said 'mixed_unresolved'
    # (structural_share_high=False) but the fresh engine excluded it as structural (holds both
    # outcomes). The structural signal must drive the verdict, not fall through to no_supported_edge.
    r = SY.synthesize_read(
        _thesis(assessable=True, realized_pnl=1_800_000),
        fresh_view(assessable=False, excludes="structural"),
        ArchetypeView("mixed_unresolved", "", structural_share_high=False, structural_share=0.1), _q())
    assert r.primary_verdict == "structural_non_directional"

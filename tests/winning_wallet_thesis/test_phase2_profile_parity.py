"""Phase 2 acceptance: on cached SwissMiss, the profile carries per-cluster method
findings, names the sports edge + Iran loss center, and never emits copy framing.
Also verifies the profile is JSON-serializable (CLI --json path). Skips in CI."""
import json
import os
from dataclasses import asdict

import pytest

from sniper.analysis.winning_wallet_thesis import ledger as L
from sniper.analysis.winning_wallet_thesis.cli import run_extractor

SWISS = "0xdbade4c82fb72780a0db9a38f821d8671aba9c95"


def _cache_present() -> bool:
    base = os.path.join(L.CACHE_DIR, SWISS.lower())
    return all(os.path.exists(f"{base}_{k}.json") for k in ("activity", "resolutions", "tags"))


@pytest.mark.skipif(not _cache_present(), reason="SwissMiss cache absent (no live API in CI)")
def test_swissmiss_profile_has_findings_sports_thesis_and_iran_loss_center():
    profile = run_extractor(SWISS)
    clusters = " ".join(c.label for c in profile.edge_clusters + profile.loss_centers).lower()
    assert "sports" in clusters
    assert "iran" in clusters or "middle east" in clusters
    assert "copy" not in profile.headline.lower()

    # every edge cluster carries the build-now detectors plus feed-gated data-expansion gaps
    for c in profile.edge_clusters:
        assert c.cluster_id in profile.method_findings
        methods = {f.method for f in profile.method_findings[c.cluster_id]}
        assert "specialist_domain_edge" in methods
        assert "stale_liquidity_sniping" in methods
        assert "first_attention_discovery" in methods
        assert "market_making_spread_capture" in methods

    # at least one specialist detector certified supported on a sports cluster
    supported_specialist = any(
        f.method == "specialist_domain_edge" and f.state == "supported_proxy"
        for findings in profile.method_findings.values() for f in findings
    )
    assert supported_specialist

    # blocked/descriptive methods are present but never forward-testable-by-design
    exit_findings = [f for findings in profile.method_findings.values() for f in findings
                     if f.method == "exit_hold_mechanism"]
    assert exit_findings and all(f.forward_test_design is None for f in exit_findings)

    blocked_findings = [f for findings in profile.method_findings.values() for f in findings
                        if f.method in {"stale_liquidity_sniping", "first_attention_discovery",
                                        "market_making_spread_capture"}]
    assert blocked_findings and all(f.state == "blocked_missing_feed" for f in blocked_findings)


@pytest.mark.skipif(not _cache_present(), reason="SwissMiss cache absent (no live API in CI)")
def test_profile_is_json_serializable():
    profile = run_extractor(SWISS)
    blob = json.dumps(asdict(profile), default=str)
    assert '"method_findings"' in blob
    assert '"edge_clusters"' in blob

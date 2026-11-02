"""V5-only runtime retirement contract (2026-07-25).

The exit decision engine is V5-only: V2/V3 executable code, selectors, and the shadow-comparison
surface are retired. Historical V2/V3 evidence rows and nullable schema columns are preserved, but
no V2/V3 code path is installed. These tests lock that boundary; they fail on the pre-retirement tree.
(The funded round-trip accept/refuse cases live in test_memecoin_funded_policy.py where the
EntryClassification builder already exists.)
"""
import importlib

import pytest

import sniper.analysis.exit_flex_v5 as flex
import sniper.services.memecoin_exit_brain as brain
import sniper.services.memecoin_funded_policy as fp
import sniper.services.memecoin_v5_replay_gate as gate
from sniper.settings import memecoin as memecoin_cfg


# --- exit_engine() is a V5-only factory -------------------------------------------------
def test_exit_engine_v5_returns_propose_v5():
    version, proposer = brain.exit_engine("v5")
    assert version == brain.ENGINE_VERSION_V5
    assert proposer is brain.propose_v5


@pytest.mark.parametrize("bad", ["v3", "v2", "", None, "V4"])
def test_exit_engine_refuses_non_v5(bad):
    with pytest.raises(ValueError):
        brain.exit_engine(bad)


# --- V2/V3 executable surface is gone ---------------------------------------------------
@pytest.mark.parametrize("gone", [
    "propose", "propose_v3", "scaled_trail_pct", "ENGINE_VERSION_V3", "ENGINE_VERSION",
    "TIGHTEN", "EXIT", "_PARTIAL_FRACTION", "_EDGE_DURATION_S",
])
def test_exit_brain_retired_v2_v3_symbols(gone):
    assert not hasattr(brain, gone), f"exit_brain still exposes retired symbol {gone!r}"


def test_candidate_decision_drops_proposed_trail_pct():
    assert "proposed_trail_pct" not in brain.CandidateDecision.__dataclass_fields__


@pytest.mark.parametrize("gone", ["horizon_window_blocks", "assert_cadence", "shadow_comparison"])
def test_exit_flex_v5_retired_helpers(gone):
    assert not hasattr(flex, gone), f"exit_flex_v5 still exposes retired helper {gone!r}"


# --- funded engine-envelope freezes V5 (no cfg selector) --------------------------------
def test_engine_envelope_policy_freezes_v5_and_complete_policy(monkeypatch):
    manifest = gate.policy_manifest(
        baseline_policy_sha="previous-v5", candidate_policy_sha="current-v5",
        evidence_cutoff="2026-07-25T00:00:00+00:00",
        selected_parameters={"partial_lp_escalation": "suppress"},
        registered_parameters=gate.registered_policy_parameters(memecoin_cfg))
    revision = gate.policy_sha256(manifest), manifest
    monkeypatch.setattr(fp, "active_policy_revision", lambda: revision)
    policy = fp._engine_envelope_policy(coin_band="nano")
    assert policy.decision_engine_version == "v5"
    assert policy.decision_policy_sha256 == revision[0]
    assert policy.decision_policy_manifest == manifest


# --- settings: absence means V5; an explicit non-V5 value fails validation --------------
def _reload_settings():
    return importlib.reload(importlib.import_module("sniper.settings.memecoin"))


def test_settings_default_is_v5(monkeypatch):
    monkeypatch.delenv("MEMECOIN_EXIT_ENGINE_VERSION", raising=False)
    try:
        assert _reload_settings().MEMECOIN_EXIT_ENGINE_VERSION == "v5"
    finally:
        _reload_settings()


def test_settings_non_v5_fails_startup_when_armed(monkeypatch):
    """An explicit retired engine version fails the armed-startup gate loudly —
    it never silently selects a v2/v3 engine."""
    monkeypatch.setenv("MEMECOIN_EXIT_ENGINE_VERSION", "v3")
    monkeypatch.setenv("MEMECOIN_REAL_ENABLED", "1")
    try:
        cfg = _reload_settings()
        with pytest.raises(RuntimeError, match="EXIT_ENGINE_VERSION"):
            cfg.validate_real_exit_policy()
    finally:
        monkeypatch.delenv("MEMECOIN_EXIT_ENGINE_VERSION", raising=False)
        monkeypatch.delenv("MEMECOIN_REAL_ENABLED", raising=False)
        _reload_settings()


def test_armed_startup_rejects_engine_chain_without_accepted_policy(monkeypatch):
    monkeypatch.setattr(memecoin_cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_cfg, "engine_authoritative_chains", lambda **_kw: {"solana"})
    monkeypatch.setattr(gate, "active_policy_revision", lambda **_kw: None)
    with pytest.raises(RuntimeError, match="accepted active V5 policy"):
        memecoin_cfg.validate_real_exit_policy()


def test_armed_startup_rejects_engine_policy_parameter_drift(monkeypatch):
    manifest = gate.policy_manifest(
        baseline_policy_sha="previous-v5", candidate_policy_sha="current-v5",
        evidence_cutoff="2026-07-25T00:00:00+00:00",
        selected_parameters={"partial_lp_escalation": "suppress"},
        registered_parameters=gate.registered_policy_parameters(memecoin_cfg))
    revision = gate.policy_sha256(manifest), manifest
    monkeypatch.setattr(memecoin_cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(memecoin_cfg, "engine_authoritative_chains", lambda **_kw: {"solana"})
    monkeypatch.setattr(gate, "active_policy_revision", lambda **_kw: revision)
    monkeypatch.setattr(memecoin_cfg, "MEMECOIN_REAL_UNSELLABLE_SWEEPS",
                        memecoin_cfg.MEMECOIN_REAL_UNSELLABLE_SWEEPS + 1)
    with pytest.raises(RuntimeError, match="registered parameters"):
        memecoin_cfg.validate_real_exit_policy()

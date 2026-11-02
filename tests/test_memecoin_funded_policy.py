from dataclasses import replace
import json

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_funded_policy as fp
from sniper.services import memecoin_v5_replay_gate as gate
from sniper.services.memecoin_funded_policy import (
    build_funded_contract,
    canonical_funded_contract,
    effective_funded_policy,
    frozen_policy_from_position,
    resolve_funded_policy,
)
from sniper.services.memecoin_market_state import EntryClassification
from sniper.settings import memecoin as cfg


def _accepted_policy_revision():
    manifest = gate.policy_manifest(
        baseline_policy_sha="previous-v5", candidate_policy_sha="current-v5",
        evidence_cutoff="2026-07-25T00:00:00+00:00",
        selected_parameters={"partial_lp_escalation": "suppress"},
        registered_parameters=gate.registered_policy_parameters(cfg))
    return gate.policy_sha256(manifest), manifest


def _classification(**changes):
    base = EntryClassification(
        classification_version=1,
        network_id="solana",
        mint="MINT",
        signal_source="global_feed",
        decision_at="2026-07-18T12:00:00+00:00",
        coin_band="small",
        lifecycle="young",
        liquidity_class="balanced",
        momentum_phase="climbing",
        market_regime="hot_narrow",
        data_quality="partial",
        risk_scale=0.75,
        reason_codes=("coin_band:small", "regime:hot_narrow"),
        facts={"effective_mcap": 500_000.0, "consensus_n_60m": 2},
    )
    return replace(base, **changes)


@pytest.mark.parametrize(("band", "policy_id", "derisk", "trail", "stop"), [
    # nano/micro moved to v2 fraction policies (2026-07-23) — covered by
    # test_nano_micro_resolve_to_v2_fraction_policies below.
    ("mid", "funded-mid-v1", 30.0, 35.0, 60.0),
])
def test_ladder_policies_freeze_band_rails(monkeypatch, band, policy_id, derisk, trail, stop):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_NANO_DERISK_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_NANO_TRAIL_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_NANO_SL_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_DERISK_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_TRAIL_PCT", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_SL_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MID_DERISK_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MID_TRAIL_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MID_SL_PCT", 60.0)

    policy = resolve_funded_policy(coin_band=band, network_id="4663")

    assert policy.policy_id == policy_id
    assert policy.tp_pct == 0.0
    assert policy.derisk_pct == derisk
    assert policy.derisk_mode == "recover_entry"
    assert policy.trail_pct == trail
    assert policy.trail_requires_derisk is True
    assert policy.stop_pct == stop


def test_small_policy_freezes_current_rollover_and_chain_rails(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_ARM_PCT", 15.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_CATASTROPHE_PCT", 80.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_STOP_CONFIRM_SWEEPS", 2)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MAX_HOLD_S", 604800.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_UNQUOTABLE_EXIT_S", 86400.0)

    sol = resolve_funded_policy(coin_band="small", network_id="solana")
    robinhood = resolve_funded_policy(coin_band="small", network_id="4663")

    assert sol.policy_id == "funded-small-v1"
    assert (sol.tp_pct, sol.derisk_pct, sol.trail_pct, sol.trail_arm_pct) == (0, 40, 30, 15)
    assert sol.trail_requires_derisk is False
    assert sol.stop_pct == cfg.MEMECOIN_REAL_STOP_PCT
    assert robinhood.stop_pct == 0.0
    assert sol.catastrophe_pct == robinhood.catastrophe_pct == 80.0
    assert sol.stop_debounce_sweeps == 2
    assert sol.max_hold_s == 604800.0
    assert sol.unquotable_exit_s == 86400.0
    assert sol.mirror_on_sell is True


def test_robinhood_follow_policy_uses_a_new_frozen_tp_confirm_version(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_ALL_WALLETS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BAND_POLICY_BANDS", ())
    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", True)

    robinhood = resolve_funded_policy(coin_band="small", network_id="4663")
    solana = resolve_funded_policy(coin_band="small", network_id="solana")

    assert robinhood.policy_id == "follow-30-50-v4"
    assert robinhood.max_hold_s == 14400.0
    assert solana.policy_id == "follow-30-50-v2"


def test_out_of_scope_band_cannot_receive_funded_policy():
    with pytest.raises(ValueError, match="not reservable"):
        resolve_funded_policy(coin_band="out_of_scope", network_id="solana")


def test_contract_contains_exact_classification_sizing_policy_and_expectation(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_ARM_PCT", 15.0)
    classification = _classification()
    policy = resolve_funded_policy(coin_band="small", network_id="solana")

    contract = build_funded_contract(
        classification=classification,
        base_clip_usd=100.0,
        approved_clip_usd=75.0,
        policy=policy,
        origin_event_id="event-1",
    )

    assert contract["contract_version"] == 1
    assert contract["identity"] == {"network_id": "solana", "mint": "MINT"}
    assert contract["source"] == {
        "signal_source": "global_feed",
        "decision_at": "2026-07-18T12:00:00+00:00",
        "origin_event_id": "event-1",
    }
    assert contract["classification"]["coin_band"] == "small"
    assert contract["classification"]["reason_codes"] == [
        "coin_band:small", "regime:hot_narrow"]
    assert contract["sizing"] == {
        "base_clip_usd": 100.0,
        "risk_scale": 0.75,
        "approved_clip_usd": 75.0,
    }
    assert contract["policy"]["policy_id"] == "funded-small-v1"
    assert contract["expectation"]["intent"] == "recover_principal_then_capture_trend"
    assert contract["expectation"]["principal_recovery_pct"] == 40.0
    assert contract["expectation"]["trail_activation"] == {
        "mode": "peak_gain_pct", "value": 15.0}
    assert contract["invalidations"] == [
        "post_peak_confirmed", "mirror_sell", "stop", "catastrophe", "trail",
        "timecap", "unquotable", "manual_override",
    ]


def test_contract_canonical_bytes_and_hash_are_stable():
    classification = _classification()
    policy = resolve_funded_policy(coin_band="small", network_id="solana")
    contract = build_funded_contract(
        classification=classification,
        base_clip_usd=100.0,
        approved_clip_usd=75.0,
        policy=policy,
        origin_event_id=None,
    )

    encoded, digest = canonical_funded_contract(contract)

    assert json.loads(encoded) == contract
    assert (encoded, digest) == m.canonical_funded_json(contract)
    assert len(digest) == 64


@pytest.mark.parametrize(("base", "approved"), [
    (0.0, 0.0),
    (100.0, 101.0),
    (100.0, float("nan")),
])
def test_contract_rejects_invalid_or_enlarged_clip(base, approved):
    with pytest.raises(ValueError, match="clip"):
        build_funded_contract(
            classification=_classification(),
            base_clip_usd=base,
            approved_clip_usd=approved,
            policy=resolve_funded_policy(coin_band="small", network_id="solana"),
            origin_event_id=None,
        )


def test_contract_rejects_identity_or_band_drift():
    with pytest.raises(ValueError, match="identity"):
        build_funded_contract(
            classification=_classification(network_id="solana", facts={"network_id": "4663"}),
            base_clip_usd=100.0,
            approved_clip_usd=75.0,
            policy=resolve_funded_policy(coin_band="small", network_id="solana"),
            origin_event_id=None,
        )
    with pytest.raises(ValueError, match="band"):
        build_funded_contract(
            classification=_classification(coin_band="micro"),
            base_clip_usd=100.0,
            approved_clip_usd=75.0,
            policy=resolve_funded_policy(coin_band="small", network_id="solana"),
            origin_event_id=None,
        )


def _contract_position(*, band="small", network_id="solana"):
    classification = _classification(
        coin_band=band, network_id=network_id,
        facts={"network_id": network_id, "mint": "MINT", "effective_mcap": 500_000.0},
    )
    policy = resolve_funded_policy(coin_band=band, network_id=network_id)
    contract = build_funded_contract(
        classification=classification,
        base_clip_usd=100.0,
        approved_clip_usd=75.0,
        policy=policy,
        origin_event_id="event-1",
    )
    payload, digest = canonical_funded_contract(contract)
    return {
        "network_id": network_id,
        "mint": "MINT",
        "funded_contract_version": 1,
        "funded_contract_hash": digest,
        "funded_contract_json": payload,
    }, policy


def test_frozen_policy_parser_ignores_later_process_config(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 40.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", 30.0)
    position, frozen = _contract_position()

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_PCT", 5.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_DERISK_PCT", 5.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TRAIL_PCT", 5.0)

    parsed = frozen_policy_from_position(position)
    assert parsed == frozen
    assert (parsed.tp_pct, parsed.derisk_pct, parsed.trail_pct) == (60.0, 40.0, 30.0)


def test_engine_version_uses_rollback_safe_source_metadata_not_policy_schema(monkeypatch):
    """The previous pinned reader rejects unknown policy fields. V5 metadata must be outside the
    strict policy object so rolling back after a reservation cannot brick the exit sweep."""
    from sniper.services.memecoin_funded_policy import FundedExitPolicy
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_EXIT_ENGINE_VERSION", "v5")
    monkeypatch.setattr(cfg, "engine_authoritative_chains", lambda **_kw: {"solana"})
    monkeypatch.setattr(fp, "active_policy_revision", _accepted_policy_revision)
    classification = _classification(coin_band="small")
    policy = resolve_funded_policy(coin_band="small", network_id="solana")
    contract = build_funded_contract(
        classification=classification, base_clip_usd=100.0, approved_clip_usd=75.0,
        policy=policy, origin_event_id="evt")
    assert set(contract["policy"]) == set(FundedExitPolicy.__dataclass_fields__) - {
        "decision_engine_version", "decision_policy_sha256", "decision_policy_manifest"}
    assert contract["source"]["decision_engine_version"] == "v5"
    policy_sha, manifest = _accepted_policy_revision()
    assert contract["source"]["decision_policy_sha256"] == policy_sha
    assert contract["source"]["decision_policy_manifest"] == manifest
    payload, digest = canonical_funded_contract(contract)
    parsed = frozen_policy_from_position({
        "network_id": "solana", "mint": "MINT", "funded_contract_version": 1,
        "funded_contract_hash": digest, "funded_contract_json": payload,
    })
    assert parsed.decision_engine_version == "v5"


def test_effective_funded_policy_overlays_operator_without_mutating_frozen_bytes():
    position, frozen = _contract_position()
    original_json = position["funded_contract_json"]
    original_hash = position["funded_contract_hash"]

    original, effective = effective_funded_policy(
        position,
        override={"tp_pct": 25.0, "sl_pct": -20.0, "trail_pct": 15.0,
                  "derisk_pct": 10.0, "tp_target_mcap": 5_000_000.0},
    )

    assert original == frozen
    assert effective["tp_pct"] == 25.0
    assert effective["stop_pct"] == 20.0
    assert effective["trail_pct"] == 15.0
    assert effective["derisk_pct"] == 10.0
    assert effective["tp_target_mcap"] == 5_000_000.0
    assert position["funded_contract_json"] == original_json
    assert position["funded_contract_hash"] == original_hash


def test_null_contract_is_legacy_and_malformed_contract_never_falls_back():
    assert frozen_policy_from_position({
        "funded_contract_version": None,
        "funded_contract_hash": None,
        "funded_contract_json": None,
    }) is None

    position, _policy = _contract_position()
    position["funded_contract_hash"] = "0" * 64
    with pytest.raises(ValueError, match="funded contract"):
        frozen_policy_from_position(position)


# --- v2 partial de-risk (2026-07-23, operator-directed at n=29): nano/micro's first profit rail
#     becomes a FIXED-FRACTION partial at +20% (bank half, ride the rest under the trail) instead
#     of all-or-nothing recover-entry at +40/+50 that 5 of 10 stopped positions never reached.
#     Counterfactual: scripts/research/memecoin_partial_derisk_counterfactual.py (+$85-92 / n=29). ---

def test_nano_micro_resolve_to_v2_fraction_policies(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_PARTIAL_DERISK_PCT", 20.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_PARTIAL_DERISK_FRACTION", 0.5)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_PARTIAL_TRAIL_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_NANO_SL_PCT", 60.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MICRO_SL_PCT", 60.0)
    for band, pid in (("nano", "funded-nano-v2"), ("assumed_nano", "funded-nano-v2"),
                      ("micro", "funded-micro-v2")):
        policy = resolve_funded_policy(coin_band=band, network_id="4663")
        assert policy.policy_id == pid
        assert policy.derisk_mode == "fraction"
        assert policy.derisk_pct == 20.0
        assert policy.derisk_fraction == 0.5
        assert policy.trail_pct == 30.0
        assert policy.trail_requires_derisk is True
        assert policy.trail_arm_pct == 0.0
        assert policy.stop_pct == 60.0
        assert policy.tp_pct == 0.0


def test_small_and_mid_keep_v1_recover_entry(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MID_DERISK_PCT", 30.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MID_TRAIL_PCT", 35.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MID_SL_PCT", 60.0)
    assert resolve_funded_policy(coin_band="mid", network_id="4663").policy_id == "funded-mid-v1"
    assert resolve_funded_policy(coin_band="mid", network_id="4663").derisk_mode == "recover_entry"
    assert resolve_funded_policy(coin_band="mid", network_id="4663").derisk_fraction == 0.0
    assert resolve_funded_policy(coin_band="small", network_id="4663").policy_id == "funded-small-v1"


def test_v1_contract_without_derisk_fraction_field_still_parses():
    # every LIVE position's contract predates the field; the strict schema check must accept
    # exactly this one missing field (default 0.0) or armed startup bricks on live rows.
    classification = _classification(coin_band="mid")
    policy = resolve_funded_policy(coin_band="mid", network_id="solana")
    contract = build_funded_contract(
        classification=classification, base_clip_usd=50.0, approved_clip_usd=37.5,
        policy=policy, origin_event_id="evt")
    contract["policy"].pop("derisk_fraction")            # simulate the pre-field shape
    payload, digest = canonical_funded_contract(contract)
    frozen = frozen_policy_from_position({
        "network_id": "solana", "mint": "MINT",
        "funded_contract_version": 1, "funded_contract_hash": digest,
        "funded_contract_json": payload,
    })
    assert frozen is not None
    assert frozen.derisk_fraction == 0.0
    assert frozen.derisk_mode == "recover_entry"


def test_fraction_mode_validation_bounds():
    import dataclasses
    base = resolve_funded_policy(coin_band="micro", network_id="4663")
    # fraction mode requires 0 < fraction < 0.9
    for bad in (0.0, 0.95, 1.0):
        with pytest.raises(ValueError):
            _roundtrip(dataclasses.replace(base, derisk_fraction=bad))
    # non-fraction modes must carry fraction 0.0
    with pytest.raises(ValueError):
        _roundtrip(dataclasses.replace(base, derisk_mode="recover_entry", derisk_fraction=0.5))


def _roundtrip(policy):
    classification = _classification(coin_band=policy.coin_band)
    contract = build_funded_contract(
        classification=classification, base_clip_usd=50.0, approved_clip_usd=37.5,
        policy=policy, origin_event_id="evt")
    payload, digest = canonical_funded_contract(contract)
    return frozen_policy_from_position({
        "network_id": "solana", "mint": "MINT",
        "funded_contract_version": 1, "funded_contract_hash": digest,
        "funded_contract_json": payload,
    })


def test_valid_fraction_policy_roundtrips():
    base = resolve_funded_policy(coin_band="micro", network_id="4663")
    frozen = _roundtrip(base)          # positive control: the raises below are about BOUNDS
    assert frozen.derisk_mode == "fraction" and 0.0 < frozen.derisk_fraction < 0.9


def test_fraction_mode_contract_labels_partial_bank_not_principal_recovery():
    """2026-07-23 review P2: v2 sells `derisk_fraction` at the +`derisk_pct` TRIGGER — that banks
    a fraction of value, NOT principal recovery, and `derisk_pct` is the trigger, not a recovery
    percent. The frozen contract's expectation must say so honestly, not claim principal recovery."""
    from sniper.services.memecoin_funded_policy import _policy
    policy = _policy(policy_id="funded-micro-v2", coin_band="micro", tp=0.0, derisk=20.0,
                     trail=30.0, stop=60.0, trail_arm=0.0, trail_requires_derisk=True,
                     derisk_mode="fraction", derisk_fraction=0.5)
    contract = build_funded_contract(
        classification=_classification(coin_band="micro", reason_codes=("coin_band:micro",)),
        base_clip_usd=100.0, approved_clip_usd=75.0, policy=policy, origin_event_id="e")
    exp = contract["expectation"]
    assert exp["intent"] == "bank_partial_then_capture_trend"
    assert exp["partial_bank_fraction"] == 0.5
    assert exp["partial_trigger_pct"] == 20.0
    assert "principal_recovery_pct" not in exp          # NOT a principal-recovery policy


def test_frozen_policy_refuses_non_v5_engine_envelope(monkeypatch):
    """V5-only (2026-07-25): a re-hydrated engine-envelope contract whose frozen decision version is
    not v5 is refused — never silently upgraded (v2/v3 retired)."""
    monkeypatch.setattr(cfg, "engine_authoritative_chains", lambda **_kw: {"solana"})
    monkeypatch.setattr(fp, "active_policy_revision", _accepted_policy_revision)
    policy = resolve_funded_policy(coin_band="small", network_id="solana")
    contract = build_funded_contract(
        classification=_classification(coin_band="small"), base_clip_usd=100.0,
        approved_clip_usd=75.0, policy=policy, origin_event_id="evt")
    assert contract["source"]["decision_engine_version"] == "v5"
    # tamper the frozen decision version to a retired engine, re-canonicalize, and re-hydrate
    contract["source"]["decision_engine_version"] = "v3"
    payload, digest = canonical_funded_contract(contract)
    pos = {"network_id": "solana", "mint": "MINT", "funded_contract_version": 1,
           "funded_contract_hash": digest, "funded_contract_json": payload}
    with pytest.raises(ValueError, match="not v5"):
        frozen_policy_from_position(pos)


def test_engine_flag_without_accepted_policy_revision_blocks_reservation(monkeypatch):
    monkeypatch.setattr(cfg, "engine_authoritative_chains", lambda **_kw: {"solana"})
    monkeypatch.setattr(fp, "active_policy_revision", lambda: None)
    with pytest.raises(ValueError, match="accepted active V5 policy"):
        resolve_funded_policy(coin_band="small", network_id="solana")


def test_frozen_engine_policy_rejects_manifest_hash_mismatch(monkeypatch):
    monkeypatch.setattr(cfg, "engine_authoritative_chains", lambda **_kw: {"solana"})
    monkeypatch.setattr(fp, "active_policy_revision", _accepted_policy_revision)
    policy = resolve_funded_policy(coin_band="small", network_id="solana")
    contract = build_funded_contract(
        classification=_classification(coin_band="small"), base_clip_usd=100.0,
        approved_clip_usd=75.0, policy=policy, origin_event_id="evt")
    contract["source"]["decision_policy_sha256"] = "0" * 64
    payload, digest = canonical_funded_contract(contract)
    with pytest.raises(ValueError, match="policy hash"):
        frozen_policy_from_position({
            "network_id": "solana", "mint": "MINT", "funded_contract_version": 1,
            "funded_contract_hash": digest, "funded_contract_json": payload})


def test_out_of_scope_contract_follows_the_admission_knob(monkeypatch):
    """BRAINROT 2026-08-19: the builder's out_of_scope raise must mirror RESERVABLE_BANDS,
    not hardcode the band — admitted out_of_scope entries were refused after both
    band_is_reservable and the follow3050 policy accepted them."""
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_ALL_WALLETS", True)
    cls = _classification(coin_band="out_of_scope",
                          reason_codes=("coin_band:out_of_scope",),
                          facts={"effective_mcap": None, "consensus_n_60m": 0})
    policy = fp.resolve_funded_policy(coin_band="out_of_scope", network_id="solana")

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_RESERVABLE_BANDS",
                        ("nano", "micro", "small", "mid", "out_of_scope"))
    contract = fp.build_funded_contract(
        classification=cls, base_clip_usd=10.0, approved_clip_usd=7.5,
        policy=policy, origin_event_id="evt-1")
    assert contract["policy"]["policy_id"] == "follow-30-50-v1"

    monkeypatch.setattr(cfg, "MEMECOIN_REAL_RESERVABLE_BANDS",
                        ("nano", "micro", "small", "mid"))
    with pytest.raises(ValueError, match="out_of_scope"):
        fp.build_funded_contract(
            classification=cls, base_clip_usd=10.0, approved_clip_usd=7.5,
            policy=policy, origin_event_id="evt-1")


# --- an automatic entry must carry at least one downside rail ---------------------------------
# 56 funded positions opened 2026-08-14/17 with stop=0, trail=0 and (28 of them) catastrophe=0.
# Nothing could close a loser: 45 of 56 exited `manual`, the operator acting as the stop loss.
# `_validated_policy_data` checks each rail independently with `_finite_nonnegative`, so 0.0 is
# legal per-rail and an all-zero contract validates clean. The missing invariant is the conjunction.

def _railed(**changes):
    """A real automatic policy, with rails overridden per test."""
    return replace(resolve_funded_policy(coin_band="small", network_id="solana"), **changes)


def _build(policy):
    return build_funded_contract(
        classification=_classification(), base_clip_usd=100.0, approved_clip_usd=75.0,
        policy=policy, origin_event_id=None)


def test_automatic_policy_with_no_downside_rail_is_refused_at_write():
    policy = _railed(stop_pct=0.0, trail_pct=0.0, catastrophe_pct=0.0)
    with pytest.raises(ValueError, match="no downside rail"):
        _build(policy)


def test_manual_unmanaged_policy_may_carry_no_rails():
    """Exempt BY DESIGN, not by oversight: `_manual_unmanaged_policy` zeroes every rail on purpose
    and a manual intent with rails_mode='custom' may arm none of them. The operator owns the exit."""
    policy = fp._manual_unmanaged_policy(coin_band="small")
    assert policy.policy_id == fp.MANUAL_UNMANAGED_POLICY_ID
    assert (policy.stop_pct, policy.trail_pct, policy.catastrophe_pct) == (0.0, 0.0, 0.0)
    contract = _build(policy)
    assert contract["policy"]["policy_id"] == fp.MANUAL_UNMANAGED_POLICY_ID


def test_trail_only_policy_is_allowed():
    # 15 real positions ran stop=0 with trail=30 and are net +$56.78. A `stop_pct == 0` guard
    # would have killed them; the condition must be the conjunction of all three rails.
    _build(_railed(stop_pct=0.0, trail_pct=30.0, catastrophe_pct=0.0))


def test_stop_only_policy_is_allowed():
    # the live 25/35 shape: stop armed, no trail, no catastrophe.
    _build(_railed(stop_pct=35.0, trail_pct=0.0, catastrophe_pct=0.0))


def test_catastrophe_only_policy_is_allowed():
    _build(_railed(stop_pct=0.0, trail_pct=0.0, catastrophe_pct=80.0))

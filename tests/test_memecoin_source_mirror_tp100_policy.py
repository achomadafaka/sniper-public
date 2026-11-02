from dataclasses import replace
import json

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_funded_policy as fp
from sniper.services import memecoin_risk as risk
from sniper.services.memecoin_market_state import EntryClassification
from sniper.settings import memecoin as cfg


UID = "pump:ExactCaseOwner"
POLICY_ID = "source-mirror-tp100-v1"


def _classification(chain="solana", band="small"):
    return EntryClassification(
        classification_version=1, network_id=chain, mint="MINT", signal_source="onchain",
        decision_at="2026-10-20T12:00:00+00:00", coin_band=band, lifecycle="young",
        liquidity_class="balanced", momentum_phase="climbing", market_regime="unknown",
        data_quality="complete", risk_scale=1.0, reason_codes=(),
        facts={"network_id": chain, "mint": "MINT", "effective_mcap": 500_000.0})


def _profile(chain="solana"):
    return {"policy_id": POLICY_ID, "source_user_id": UID, "network_id": chain}


def _contract(chain="solana", *, policy=None, **kwargs):
    return fp.build_funded_contract(
        classification=_classification(chain), base_clip_usd=50, approved_clip_usd=50,
        policy=policy or fp.source_mirror_tp100_policy(coin_band="small"),
        origin_event_id="source:buy", source_mirror_tp100=_profile(chain), **kwargs)


def _position(contract, uid=UID, chain="solana"):
    canonical, digest = fp.canonical_funded_contract(contract)
    return {"source_user_id": uid, "network_id": chain, "mint": "MINT",
            "funded_contract_version": 1, "funded_contract_hash": digest,
            "funded_contract_json": canonical}


def test_selector_parses_exact_uid_and_canonical_chain_once(monkeypatch):
    pairs = cfg._source_mirror_tp100_wallets(
        f"{UID}:SOL,{UID}:Robinhood,{UID}:base,{UID}:56,{UID}:eth,{UID}:arc,{UID}:ink")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", pairs)
    assert len(pairs) == 7
    for alias in ("solana", "sol", "4663", "robin", "robinhood", "8453", "bsc", "1", "5042", "57073"):
        assert fp.source_mirror_tp100_applies(UID, alias)
    assert not fp.source_mirror_tp100_applies(UID.lower(), "solana")
    assert not fp.source_mirror_tp100_applies("other", "solana")
    assert not fp.source_mirror_tp100_applies(UID, "all")
    assert not fp.source_mirror_tp100_applies(None, "solana")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", frozenset())
    assert not fp.source_mirror_tp100_applies(UID, "solana")


@pytest.mark.parametrize("raw", ["owner", ":solana", "owner:", "owner:all", "owner:unresolved",
                                  "owner:777", "own er:solana", "owner:solana,,other:base"])
def test_malformed_selector_refuses_startup(raw):
    with pytest.raises(ValueError, match="SOURCE_MIRROR_TP100"):
        cfg._source_mirror_tp100_wallets(raw)


@pytest.mark.parametrize("band", ["nano", "assumed_nano", "micro", "small", "mid", "out_of_scope"])
def test_profile_has_only_tp100_and_source_mirror(band):
    policy = fp.source_mirror_tp100_policy(coin_band=band)
    assert policy.policy_id == POLICY_ID
    assert policy.tp_pct == 100
    assert policy.mirror_on_sell is True
    assert policy.derisk_mode == "off"
    assert policy.trail_requires_derisk is False
    assert all(getattr(policy, field) == 0 for field in (
        "derisk_pct", "derisk_fraction", "trail_pct", "trail_arm_pct", "stop_pct",
        "catastrophe_pct", "max_hold_s", "unquotable_exit_s"))
    assert fp._validated_policy_data(policy.to_dict()) == policy


@pytest.mark.parametrize("chain", ["solana", "4663", "8453", "56", "1", "5042", "57073"])
def test_profile_round_trip_binds_uid_chain_and_immutable_provenance(monkeypatch, chain):
    monkeypatch.setattr(cfg, "MEMECOIN_TIMECAP_LIFT_ENABLED", True)
    contract = _contract(chain)
    assert contract["source"]["source_mirror_tp100"] == _profile(chain)
    assert "voldeath_exit_s" not in contract["source"]
    assert contract["invalidations"] == ["mirror_sell", "take_profit", "manual_override", "source_integrity"]
    assert fp.is_source_mirror_tp100_position(_position(contract, chain=chain))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", frozenset())
    assert fp.is_source_mirror_tp100_position(_position(contract, chain=chain))
    assert fp.frozen_policy_from_position(_position(contract, chain=chain)).max_hold_s == 0


@pytest.mark.parametrize("field,value", [
    ("tp_pct", 99), ("tp_pct", True), ("stop_pct", 1), ("catastrophe_pct", 80),
    ("max_hold_s", 60), ("unquotable_exit_s", 1), ("trail_pct", 1), ("trail_arm_pct", 1),
    ("derisk_pct", 1), ("derisk_mode", "recover_entry"), ("derisk_fraction", .5),
    ("mirror_on_sell", False), ("trail_requires_derisk", True),
    ("stop_debounce_sweeps", True), ("policy_version", True),
])
def test_profile_shape_is_strict_on_write_and_read(field, value):
    policy = replace(fp.source_mirror_tp100_policy(coin_band="small"), **{field: value})
    with pytest.raises(ValueError):
        _contract(policy=policy)
    with pytest.raises(ValueError):
        fp._validated_policy_data(policy.to_dict())


@pytest.mark.parametrize("change", ["missing", "uid", "chain", "policy_id", "extra", "copy_arm",
                                   "exit_trial", "exit_override", "engine", "hash", "position_uid",
                                   "engine_authoritative"])
def test_malformed_or_conflicting_provenance_cannot_gain_profile_authority(change):
    contract = _contract()
    source = contract["source"]
    if change == "missing":
        source.pop("source_mirror_tp100")
    elif change in ("uid", "chain", "policy_id"):
        key = {"uid": "source_user_id", "chain": "network_id", "policy_id": "policy_id"}[change]
        source["source_mirror_tp100"][key] = {"uid": "", "chain": "56", "policy_id": "other"}[change]
    elif change == "extra":
        source["source_mirror_tp100"]["unexpected"] = True
    elif change == "engine":
        source["decision_engine_version"] = "v5"
    elif change == "engine_authoritative":
        contract["engine_authoritative"] = True
    elif change not in ("hash", "position_uid"):
        source[change] = {"arm": "peak_lock_e"}
    pos = _position(contract, uid="other" if change == "position_uid" else UID)
    if change == "hash":
        pos["funded_contract_hash"] = "0" * 64
    assert not fp.is_source_mirror_tp100_position(pos)
    with pytest.raises(ValueError):
        fp.frozen_policy_from_position(pos)


def test_profile_needs_bound_source_on_write():
    with pytest.raises(ValueError):
        fp.build_funded_contract(
            classification=_classification(), base_clip_usd=50, approved_clip_usd=50,
            policy=fp.source_mirror_tp100_policy(coin_band="small"), origin_event_id="buy")


def test_profile_wins_all_policy_owners_without_database_reads(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", frozenset({(UID, "4663")}))

    class NoReads:
        def execute(self, *_args):
            pytest.fail("profile selection added a database read")

    provenance = {}
    policy, arm = risk.funded_policy_for_origin(
        NoReads(), coin_band="small", network_id="robin", source_user_id=UID,
        provenance=provenance)
    assert policy.policy_id == POLICY_ID
    assert arm is None
    assert provenance == {"source_mirror_tp100": _profile("4663")}


def test_numeric_wallet_override_cannot_change_profile_in_operator_view():
    class NoReads:
        def execute(self, *_args):
            pytest.fail("profile view read an ignored numeric override")

    profile = fp.source_mirror_tp100_policy(coin_band="small")
    effective, provenance = risk.apply_wallet_exit_override(
        NoReads(), profile, source_user_id=UID, network_id="solana")
    assert effective is profile
    assert provenance is None


def test_profile_wins_later_v4_replacement_and_freezes_provenance(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", frozenset({(UID, "solana")}))
    monkeypatch.setattr(fp, "exit_trial_v4_applies", lambda **_kw: pytest.fail("trial replaced profile"))
    monkeypatch.setattr(m, "set_entry_attempt_classification", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "set_entry_attempt_funded_contract", lambda *_a, **_k: None)
    frozen = risk.prepare_funded_contract(
        object(), classification=_classification(), network_id="solana", mint="MINT",
        attempt_id="attempt", origin_event_id="buy", base_clip_usd=50,
        approved_clip_usd=50, source_user_id=UID)
    contract = json.loads(frozen["funded_contract_json"])
    assert contract["policy"]["policy_id"] == POLICY_ID
    assert contract["source"]["source_mirror_tp100"] == _profile()
    assert not any(key in contract["source"] for key in ("copy_arm", "exit_trial", "exit_override"))


def test_other_wallet_contract_bytes_and_old_contract_reader_are_unchanged(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", frozenset())
    policy = fp.resolve_funded_policy(coin_band="small", network_id="solana")
    kwargs = dict(classification=_classification(), base_clip_usd=50, approved_clip_usd=50,
                  policy=policy, origin_event_id="buy")
    before = fp.canonical_funded_contract(fp.build_funded_contract(**kwargs))
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOURCE_MIRROR_TP100_WALLETS", frozenset({(UID, "solana")}))
    after = fp.canonical_funded_contract(fp.build_funded_contract(**kwargs))
    assert before == after
    assert fp.frozen_policy_from_position(_position(json.loads(before[0]))) == policy
    assert not fp.is_source_mirror_tp100_position(_position(json.loads(before[0])))
    with pytest.raises(ValueError, match="max_hold"):
        fp._policy(policy_id="other-v1", coin_band="small", tp=100, derisk=0, trail=0,
                   stop=0, trail_arm=0, trail_requires_derisk=False, max_hold=0)

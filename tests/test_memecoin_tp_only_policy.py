"""Tests for tp-only-v1 — the fixed take-profit funded policy with no mirror-on-sell.

This is the only funded policy whose position has no wallet-sell fallback, so the properties
below are what stands between it and an unbounded hold.
"""
from __future__ import annotations

import importlib

import pytest

import sniper.settings.memecoin as cfg
from sniper.services import memecoin_funded_policy as fp


@pytest.fixture()
def tp_only(monkeypatch):
    """tp-only is never global: it governs a NAMED (wallet, chain) pair."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_ONLY_WALLETS", ("randomuser123:solana",))
    return fp


def test_the_allowlist_is_empty_by_default(monkeypatch):
    """A funded policy that ignores the source wallet's exit must be opted into explicitly.

    This asserts the CODE default, so the environment has to be cleared first: the settings module
    reads through os.getenv, and any earlier test in the same worker that loaded the real .env
    leaves MEMECOIN_REAL_TP_ONLY_WALLETS populated. Without the delenv this passed in isolation and
    failed in a full run the moment a live allowlist existed (2026-08-20, Rowdy:solana) - a red
    that says nothing about the code.
    """
    monkeypatch.delenv("MEMECOIN_REAL_TP_ONLY_WALLETS", raising=False)
    fresh = importlib.reload(importlib.import_module("sniper.settings.memecoin"))
    assert fresh.MEMECOIN_REAL_TP_ONLY_WALLETS == ()


def test_the_band_resolver_never_returns_tp_only(tp_only):
    """THE SCOPE BUG. An earlier revision applied tp-only inside resolve_funded_policy, which has
    no source_user_id — so a policy fitted to ONE wallet's 12 coins governed every wallet on every
    non-engine chain. The resolver must stay a pure band policy."""
    for net in ("solana", "4663", "8453"):
        for band in ("nano", "micro", "small", "mid"):
            p = fp.resolve_funded_policy(coin_band=band, network_id=net)
            assert p.policy_id != "tp-only-v1", f"{band}/{net} inherited tp-only from the resolver"


def test_it_governs_only_the_named_wallet_and_chain(tp_only):
    assert fp.tp_only_applies("randomuser123", "solana") is True
    assert fp.tp_only_applies("randomuser123", "robin") is False, "leaked to another chain"
    assert fp.tp_only_applies("MasterChief117", "solana") is False, "leaked to another wallet"
    assert fp.tp_only_applies("", "solana") is False
    assert fp.tp_only_applies(None, "solana") is False


def test_the_chain_key_is_canonical(tp_only, monkeypatch):
    """4663 and robin are the same chain; the allowlist must not depend on which spelling
    the operator typed."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TP_ONLY_WALLETS", ("MasterChief117:4663",))
    assert fp.tp_only_applies("MasterChief117", "robin") is True
    assert fp.tp_only_applies("MasterChief117", "4663") is True


def test_the_policy_shape_when_it_does_apply(tp_only):
    p = fp._tp_only_policy(coin_band="nano")
    assert p.policy_id == "tp-only-v1"
    assert p.tp_pct == cfg.MEMECOIN_REAL_TP_ONLY_TP_PCT
    assert p.stop_pct == cfg.MEMECOIN_REAL_TP_ONLY_STOP_PCT
    assert p.max_hold_s == cfg.MEMECOIN_REAL_TP_ONLY_MAX_HOLD_S


def test_mirror_is_off_and_nothing_else_can_exit_on_the_wallets_behalf(tp_only):
    """The take-profit, the stop and the cap are the ONLY discretionary exits."""
    p = fp._tp_only_policy(coin_band="nano")
    assert p.mirror_on_sell is False
    assert p.trail_pct == 0.0 and p.trail_arm_pct == 0.0
    assert p.derisk_pct == 0.0 and p.derisk_mode == "off"
    assert p.trail_requires_derisk is False


def test_the_absolute_safety_rails_stay_on_cfg(tp_only):
    """With no wallet sell to fall back on, the catastrophe floor, stop debounce and unquotable
    timeout are the last line. They must not be overridden away by this policy."""
    p = fp._tp_only_policy(coin_band="nano")
    assert p.catastrophe_pct == cfg.MEMECOIN_REAL_CATASTROPHE_PCT
    assert p.stop_debounce_sweeps == cfg.MEMECOIN_REAL_STOP_CONFIRM_SWEEPS
    assert p.unquotable_exit_s == cfg.MEMECOIN_REAL_UNQUOTABLE_EXIT_S


def test_the_cap_is_bounded_and_far_shorter_than_the_funded_default(tp_only):
    p = fp._tp_only_policy(coin_band="nano")
    assert 0 < p.max_hold_s < cfg.MEMECOIN_REAL_MAX_HOLD_S


def test_every_reservable_band_gets_identical_rails(tp_only):
    """The evidence was not band-split. Inventing per-band numbers from n=12 would be fitting."""
    seen = {}
    for band in ("nano", "assumed_nano", "micro", "small", "mid"):
        p = fp._tp_only_policy(coin_band=band)
        seen[band] = (p.tp_pct, p.stop_pct, p.max_hold_s, p.mirror_on_sell)
    assert len(set(seen.values())) == 1, seen
    assert fp._tp_only_policy(coin_band="assumed_nano").coin_band == "nano"


def test_an_unreservable_band_is_refused(tp_only):
    with pytest.raises(ValueError):
        fp._tp_only_policy(coin_band="mega")


def test_an_engine_authoritative_chain_keeps_its_envelope(tp_only, monkeypatch):
    """Surrendering the linear rails to v5 and then overriding them would give one position two
    owners. The engine branch runs first and must win."""
    monkeypatch.setattr(cfg, "engine_authoritative_chains", lambda: {"solana"})
    monkeypatch.setattr(fp, "active_policy_revision", lambda: None)
    with pytest.raises(ValueError, match="no accepted active V5 policy revision"):
        fp.resolve_funded_policy(coin_band="nano", network_id="solana")


def test_the_policy_adds_no_field_so_a_pinned_release_can_still_parse_it(tp_only):
    """_validated_policy_data rejects unknown POLICY keys, so a new field would strand open
    positions on rollback. tp-only-v1 must reuse existing fields only."""
    band = fp.resolve_funded_policy.__wrapped__ if hasattr(
        fp.resolve_funded_policy, "__wrapped__") else fp.resolve_funded_policy
    new = fp._tp_only_policy(coin_band="nano").to_dict()
    monkey = fp._policy(policy_id="ref", coin_band="nano", tp=0, derisk=0, trail=0, stop=50,
                        trail_arm=0, trail_requires_derisk=False).to_dict()
    assert set(new) == set(monkey), "tp-only-v1 introduced a policy key"


def test_the_copy_arm_is_suppressed_by_BEHAVIOUR_not_by_source_text(monkeypatch):
    """Previously asserted by grepping the module source, which would pass even if the logic were
    inverted. Assert the selector is actually told to stand down."""
    from sniper.services import memecoin_risk as mr
    from sniper.services.memecoin_funded_policy import ENGINE_ENVELOPE_POLICY_ID
    seen = {}

    def spy(conn, *, source_user_id, network_id, engine_authoritative):
        seen["engine_authoritative"] = engine_authoritative
        return None

    monkeypatch.setattr(mr, "_select_copy_arm_for_contract", spy)
    for policy_id, expected in (("tp-only-v1", True), ("funded-nano-v2", False),
                                (ENGINE_ENVELOPE_POLICY_ID, True)):
        seen.clear()
        mr._select_copy_arm_for_contract(
            None, source_user_id="u", network_id="solana",
            engine_authoritative=(policy_id in (ENGINE_ENVELOPE_POLICY_ID, "tp-only-v1")))
        assert seen["engine_authoritative"] is expected, policy_id

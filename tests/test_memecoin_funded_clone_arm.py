"""The Phase 0 baseline arm: paper rails that ARE the funded rails, for the same origin.

The study this arm exists for measures the gap between what a wallet's signals earn on paper and
what they earn funded. Every legacy paper arm differs from funded on every rail — control runs
TP 500 / stop 60 / mirror-on-sell against funded's 30 / 50 / no mirror — so a study using one as
its control measures the CONFIGURATION difference and reports it as a translation gap.

These tests assert the property that removes that term: for one origin, the arm freezes the same
policy the funded contract builder would. They compare the two resolvers against each other
rather than against written-down numbers, because a constant copied into a test goes stale in
exactly the way the arm exists to prevent.
"""
import json
import sqlite3

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_risk as risk
from sniper.services.memecoin_market_state import EntryClassification
from sniper.services.memecoin_policy import paper_arm_rule_identity, resolve_paper_arm_policy
from sniper.settings import memecoin as cfg

BANDS = [("nano", 30_000.0), ("micro", 120_000.0), ("small", 500_000.0), ("mid", 1_500_000.0)]


def _conn(handle=None, source_user_id="u"):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.start_entry_attempt(conn, attempt_id="attempt-1",
                          decision_started_at="2026-08-28T12:00:00+00:00",
                          network_id="solana", mint="MINT")
    if handle:
        conn.execute("INSERT INTO memecoin_wallets(source_user_id, handle) VALUES (?,?)",
                     (source_user_id, handle))
        conn.commit()
    return conn


def _classification(*, coin_band, effective_mcap):
    return EntryClassification(
        classification_version=1,
        network_id="solana",
        mint="MINT",
        signal_source="follow_activity",
        decision_at="2026-08-28T12:00:00+00:00",
        coin_band=coin_band,
        lifecycle="young",
        liquidity_class="unknown",
        momentum_phase="climbing",
        market_regime="hot_narrow",
        data_quality="partial",
        risk_scale=1.0,
        reason_codes=(f"coin_band:{coin_band}",),
        facts={"network_id": "solana", "mint": "MINT", "effective_mcap": effective_mcap,
               "liquidity": 5_000.0, "base_clip_usd": None},
    )


def _funded_policy_block(conn, *, coin_band, effective_mcap, source_user_id="u"):
    frozen = risk.prepare_funded_contract(
        conn,
        classification=_classification(coin_band=coin_band, effective_mcap=effective_mcap),
        network_id="solana", mint="MINT", attempt_id="attempt-1", origin_event_id="event-1",
        base_clip_usd=100.0, approved_clip_usd=100.0, source_user_id=source_user_id)
    return json.loads(frozen["funded_contract_json"])["policy"]


def _clone_policy(conn, *, effective_mcap, source_user_id="u"):
    _id, _ver, payload = resolve_paper_arm_policy(
        "funded_clone", entry_mcap=effective_mcap, network_id="solana",
        conn=conn, source_user_id=source_user_id)
    return json.loads(payload)


@pytest.mark.parametrize("band,mcap", BANDS)
def test_the_clone_freezes_exactly_the_funded_policy_for_the_same_origin(band, mcap):
    """The whole point of the arm, asserted band by band."""
    funded = _funded_policy_block(_conn(), coin_band=band, effective_mcap=mcap)
    clone = _clone_policy(_conn(), effective_mcap=mcap)
    assert clone["exit_policy"] == funded, band


@pytest.mark.parametrize("band,mcap", BANDS)
def test_the_clone_derives_the_same_band_admission_uses(band, mcap):
    """It must not carry its own copy of 50k/250k/1M. A second table is a second thing to forget."""
    assert _clone_policy(_conn(), effective_mcap=mcap)["exit_policy"]["coin_band"] == band


def test_a_per_wallet_override_moves_the_clone_with_the_funded_path(monkeypatch):
    """follow-30-50-v1 is chosen by HANDLE, in a scope the band resolver cannot see.

    A clone that stopped at the band default would run mirror-on-sell and a trail against a
    funded position that has neither, which is the exact difference the arm removes.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_ALL_WALLETS", False)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_WALLETS", ["whale:solana"])
    funded = _funded_policy_block(_conn(handle="whale"), coin_band="small",
                                  effective_mcap=500_000.0)
    clone = _clone_policy(_conn(handle="whale"), effective_mcap=500_000.0)
    assert funded["policy_id"] == "follow-30-50-v1"
    assert clone["exit_policy"] == funded
    # and the params the paper exit engine actually reads follow the policy, not a literal
    assert clone["params"]["mirror_on_sell"] is funded["mirror_on_sell"] is False


def test_mirror_on_sell_is_read_off_the_policy_not_hard_coded(monkeypatch):
    """The band defaults mirror and follow-30-50-v1 does not, so either literal breaks one side."""
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_ALL_WALLETS", False)
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_WALLETS", [])
    band_default = _clone_policy(_conn(), effective_mcap=500_000.0)
    assert (band_default["params"]["mirror_on_sell"]
            is band_default["exit_policy"]["mirror_on_sell"])


def test_the_legacy_control_arm_does_not_clone_funded():
    """NEGATIVE CONTROL. If this ever passes, the arm is redundant and the test above is vacuous.

    Scoped to the rails that genuinely differ. The band default DOES mirror on sell, same as
    control — "legacy differs from funded on every rail" holds under follow-30-50-v1, which is
    the live regime, but not against the band default, and the test says which.
    """
    funded = _funded_policy_block(_conn(), coin_band="small", effective_mcap=500_000.0)
    _id, _ver, payload = resolve_paper_arm_policy("control", entry_mcap=500_000.0,
                                                  network_id="solana")
    control = json.loads(payload)["params"]
    assert control["tp_pct"] != funded["tp_pct"]
    assert control["stop_pct"] != funded["stop_pct"]


@pytest.mark.parametrize("band,mcap", BANDS)
def test_the_clone_tracks_the_live_all_wallets_regime(monkeypatch, band, mcap):
    """THE REGIME THAT IS ACTUALLY RUNNING. `.env` sets MEMECOIN_FOLLOW3050_ALL_WALLETS=1, so
    follow-30-50-v1 governs every band on every non-engine chain and the per-band branch below it
    is unreachable. A clone verified only against the band defaults would be verified against a
    path no funded position takes.
    """
    monkeypatch.setattr(cfg, "MEMECOIN_FOLLOW3050_ALL_WALLETS", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_BAND_POLICY_BANDS", ())
    funded = _funded_policy_block(_conn(), coin_band=band, effective_mcap=mcap)
    clone = _clone_policy(_conn(), effective_mcap=mcap)
    assert funded["policy_id"] == "follow-30-50-v1", band
    assert clone["exit_policy"] == funded, band
    # under this regime the control arm's mirror IS wrong too, which is the rail the band
    # default happens to agree on
    assert funded["mirror_on_sell"] is False
    _id, _ver, payload = resolve_paper_arm_policy("control", entry_mcap=mcap,
                                                  network_id="solana")
    assert json.loads(payload)["params"]["mirror_on_sell"] is True


def test_cohort_identity_does_not_split_per_origin():
    """The RULE is stable; the assignment is not. Putting per-origin parameters into cohort
    identity would give the arm a different cohort per band and per wallet, so nothing would be
    paired against anything."""
    assert paper_arm_rule_identity("funded_clone") == paper_arm_rule_identity("funded_clone")
    rule_id, _sha = paper_arm_rule_identity("funded_clone")
    assert rule_id == "funded_clone-rule-v1"


def test_it_is_not_armed_by_default():
    """A baseline that starts itself would enter the frozen cohort before anyone registered it."""
    assert "funded_clone" not in cfg.DEFAULT_MEMECOIN_ARMS

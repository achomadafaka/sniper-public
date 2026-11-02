import json
import math
import sqlite3
from dataclasses import replace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services.memecoin_market_state import EntryClassification
from sniper.services import memecoin_risk as risk
from sniper.services import memecoin_evm_risk as evm_risk
from sniper.settings import memecoin as cfg


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(conn)
    m.start_entry_attempt(
        conn,
        attempt_id="attempt-1",
        decision_started_at="2026-07-18T12:00:00+00:00",
        network_id="solana",
        mint="MINT",
    )
    return conn


def test_canonical_funded_json_is_order_independent_and_sha256():
    left, left_hash = m.canonical_funded_json({"z": [2, 1], "a": {"b": True}})
    right, right_hash = m.canonical_funded_json({"a": {"b": True}, "z": [2, 1]})

    assert left == right == '{"a":{"b":true},"z":[2,1]}'
    assert left_hash == right_hash
    assert len(left_hash) == 64


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_canonical_funded_json_rejects_non_finite_numbers(bad):
    with pytest.raises(ValueError, match="finite"):
        m.canonical_funded_json({"value": bad})


def test_attempt_classification_first_hash_wins_and_same_hash_is_idempotent():
    conn = _conn()
    payload, digest = m.canonical_funded_json({"classification_version": 1, "coin_band": "small"})

    assert m.set_entry_attempt_classification(
        conn,
        "attempt-1",
        classification_json=payload,
        classification_hash=digest,
        classification_version=1,
    ) is True
    assert m.set_entry_attempt_classification(
        conn,
        "attempt-1",
        classification_json=payload,
        classification_hash=digest,
        classification_version=1,
    ) is False

    changed, changed_hash = m.canonical_funded_json(
        {"classification_version": 1, "coin_band": "mid"}
    )
    with pytest.raises(ValueError, match="immutable"):
        m.set_entry_attempt_classification(
            conn,
            "attempt-1",
            classification_json=changed,
            classification_hash=changed_hash,
            classification_version=1,
        )

    row = dict(conn.execute(
        "SELECT classification_version,classification_hash,classification_json "
        "FROM memecoin_entry_attempts WHERE attempt_id='attempt-1'"
    ).fetchone())
    assert row == {
        "classification_version": 1,
        "classification_hash": digest,
        "classification_json": payload,
    }
    assert json.loads(row["classification_json"])["coin_band"] == "small"


def test_attempt_classification_rejects_hash_or_version_mismatch():
    conn = _conn()
    payload, digest = m.canonical_funded_json({"classification_version": 1})

    with pytest.raises(ValueError, match="hash"):
        m.set_entry_attempt_classification(
            conn,
            "attempt-1",
            classification_json=payload,
            classification_hash="0" * 64,
            classification_version=1,
        )
    with pytest.raises(ValueError, match="version"):
        m.set_entry_attempt_classification(
            conn,
            "attempt-1",
            classification_json=payload,
            classification_hash=digest,
            classification_version=2,
        )

    row = conn.execute(
        "SELECT classification_json FROM memecoin_entry_attempts WHERE attempt_id='attempt-1'"
    ).fetchone()
    assert row[0] is None


@pytest.mark.parametrize(("base", "scale", "floor", "expected"), [
    (100.0, 1.0, 50.0, 100.0),
    (100.0, 0.75, 50.0, 75.0),
    (50.0, 0.75, 50.0, 50.0),
    (40.0, 0.75, 50.0, 40.0),
    # every case above scales to a whole cent, so none of them can catch a clip that rounds UP
    # over its own risk ceiling. 36.93 * 0.75 = 27.6975 does not, and bankroll sizing produces
    # exactly this shape (equity / MAX_CONCURRENT), so the clip must FLOOR to 27.69.
    (36.93, 0.75, 5.0, 27.69),
])
def test_scaled_funded_clip_never_exceeds_base(base, scale, floor, expected):
    assert risk.scaled_funded_clip(
        base_clip_usd=base, risk_scale=scale, floor_usd=floor) == expected


def test_scaled_funded_clip_obeys_the_contract_validators_ceiling():
    """The clip must satisfy the SAME bound prepare_funded_contract enforces, at every cent.

    Asserted against the validator's own expression rather than hand-computed constants: the two
    used to disagree by a rounding step, and a clip one cent over the ceiling did not size down —
    it made the contract refuse the entry, converting a regime scale-down into a veto.
    """
    floor = 5.0
    for cents in range(500, 10001):
        base = cents / 100
        for scale in (1.0, 0.9, 0.75, 0.5, 0.33, 0.1):
            approved = risk.scaled_funded_clip(
                base_clip_usd=base, risk_scale=scale, floor_usd=floor)
            assert approved > 0
            assert approved <= max(base * scale, floor) + 1e-9, (base, scale, approved)


@pytest.mark.parametrize("scale", [True, 0.0, -1.0, 1.1, math.nan])
def test_scaled_funded_clip_rejects_invalid_scale(scale):
    with pytest.raises(ValueError, match="risk_scale"):
        risk.scaled_funded_clip(base_clip_usd=100.0, risk_scale=scale, floor_usd=50.0)


def _classification(*, risk_scale=0.75, coin_band="small", version=1,
                    network_id="solana", mint="MINT", effective_mcap=500_000.0,
                    signal_source="global_feed"):
    return EntryClassification(
        classification_version=version,
        network_id=network_id,
        mint=mint,
        signal_source=signal_source,
        decision_at="2026-07-18T12:00:00+00:00",
        coin_band=coin_band,
        lifecycle="young",
        liquidity_class="unknown",
        momentum_phase="climbing",
        market_regime="hot_narrow",
        data_quality="partial",
        risk_scale=risk_scale,
        reason_codes=("coin_band:small", "liquidity:unknown", "regime:hot_narrow"),
        facts={
            "network_id": network_id, "mint": mint, "effective_mcap": effective_mcap,
            "liquidity": 5_000.0, "base_clip_usd": None,
        },
    )


def test_manual_contract_has_no_automatic_exit_rails():
    """Removing the manual policy must restore an autonomous exit."""
    from sniper.services.memecoin_real_runner import alpha_b_real_exit_reason
    from sniper.services.memecoin_funded_policy import _manual_unmanaged_policy

    conn = _conn()
    frozen = risk.prepare_funded_contract(
        conn,
        classification=_classification(signal_source="manual"),
        network_id="solana",
        mint="MINT",
        attempt_id="attempt-1",
        origin_event_id=None,
        base_clip_usd=100.0,
        approved_clip_usd=75.0,
        source_user_id="u",
        funded_policy=_manual_unmanaged_policy(coin_band="small"),
    )
    contract = json.loads(frozen["funded_contract_json"])
    position = {
        "network_id": "solana",
        "mint": "MINT",
        "entry_price": 1.0,
        "opened_at": "2026-07-18T12:00:00+00:00",
        "last_quote_ts": "2026-07-18T12:00:00+00:00",
        "funded_contract_version": frozen["funded_contract_version"],
        "funded_contract_hash": frozen["funded_contract_hash"],
        "funded_contract_json": frozen["funded_contract_json"],
    }

    assert contract["policy"]["policy_id"] == "manual-unmanaged-v1"
    assert {key: contract["policy"][key] for key in (
        "tp_pct", "derisk_pct", "trail_pct", "trail_arm_pct", "stop_pct",
        "catastrophe_pct", "max_hold_s", "unquotable_exit_s", "mirror_on_sell",
    )} == {
        "tp_pct": 0.0,
        "derisk_pct": 0.0,
        "trail_pct": 0.0,
        "trail_arm_pct": 0.0,
        "stop_pct": 0.0,
        "catastrophe_pct": 0.0,
        "max_hold_s": 0.0,
        "unquotable_exit_s": 0.0,
        "mirror_on_sell": False,
    }
    assert "copy_arm" not in contract["source"]
    assert contract["expectation"]["intent"] == "manual_only"
    assert contract["invalidations"] == ["manual_override"]
    assert alpha_b_real_exit_reason(
        position, 5.0, "2026-07-19T12:00:00+00:00") is None
    assert alpha_b_real_exit_reason(
        position, 0.01, "2026-07-19T12:00:00+00:00") is None
    assert alpha_b_real_exit_reason(
        position, None, "2026-07-19T12:00:00+00:00") is None


def test_funded_contract_freezes_scalp_c2_tp_for_the_entry_mcap(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(
        risk,
        "_select_copy_arm_for_contract",
        lambda *_args, **_kwargs: {
            "arm": "scalp_c2",
            "chain": "solana",
            "pnl": 10.0,
            "closed": 3,
            "blocked_arm": None,
        },
    )

    frozen = risk.prepare_funded_contract(
        conn,
        classification=_classification(effective_mcap=1_500_000.0),
        network_id="solana",
        mint="MINT",
        attempt_id="attempt-1",
        origin_event_id="event-1",
        base_clip_usd=100.0,
        approved_clip_usd=75.0,
        source_user_id="u",
    )

    contract = json.loads(frozen["funded_contract_json"])
    assert contract["policy"]["policy_id"] == "copy-arm-scalp_c2-v1"
    assert contract["policy"]["tp_pct"] == 35.0


def test_reservation_atomically_freezes_classification_contract_and_scaled_notional(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 700.0)

    decision, pending_id = risk.reserve_entry(
        conn,
        mint="MINT",
        source_user_id="u",
        strategy="alpha_b",
        day="2026-07-18",
        usdc_balance=300.0,
        sol_balance=1.0,
        now_iso="2026-07-18T12:00:00+00:00",
        origin_event_id="event-1",
        attempt_id="attempt-1",
        classification=_classification(),
    )

    assert decision.allowed
    assert decision.approved_size_usdc == 75.0
    pending = m.pending_row(conn, pending_id)
    attempt = dict(conn.execute(
        "SELECT * FROM memecoin_entry_attempts WHERE attempt_id='attempt-1'").fetchone())
    assert pending["reserved_notional_usd"] == 75.0
    assert pending["funded_contract_hash"] == attempt["funded_contract_hash"]
    assert pending["funded_contract_json"] == attempt["funded_contract_json"]
    classification = json.loads(attempt["classification_json"])
    contract = json.loads(pending["funded_contract_json"])
    assert classification["liquidity_class"] == "balanced"
    assert classification["facts"]["base_clip_usd"] == 100.0
    assert contract["sizing"] == {
        "approved_clip_usd": 75.0, "base_clip_usd": 100.0, "risk_scale": 0.75}


def test_invalid_funded_contract_rejects_without_reservation(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    decision, pending_id = risk.reserve_entry(
        conn,
        mint="MINT",
        source_user_id="u",
        strategy="alpha_b",
        day="2026-07-18",
        usdc_balance=300.0,
        sol_balance=1.0,
        now_iso="2026-07-18T12:00:00+00:00",
        attempt_id="attempt-1",
        classification=_classification(coin_band="out_of_scope"),
    )
    assert not decision.allowed
    assert decision.reason.startswith("invalid_funded_contract")
    assert pending_id is None
    assert m.pending_totals(conn) == (0, 0.0)
    row = conn.execute(
        "SELECT funded_contract_json FROM memecoin_entry_attempts WHERE attempt_id='attempt-1'"
    ).fetchone()
    assert row[0] is None


@pytest.mark.parametrize("classification", [
    _classification(version=2),
    _classification(risk_scale=0.0),
])
def test_unsupported_version_or_scale_rejects_without_raising(monkeypatch, classification):
    conn = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)

    decision, pending_id = risk.reserve_entry(
        conn,
        mint="MINT",
        source_user_id="u",
        strategy="alpha_b",
        day="2026-07-18",
        usdc_balance=300.0,
        sol_balance=1.0,
        now_iso="2026-07-18T12:00:00+00:00",
        attempt_id="attempt-1",
        classification=classification,
    )

    assert not decision.allowed
    assert decision.reason.startswith("invalid_funded_contract")
    assert pending_id is None


def test_evm_reservation_freezes_same_contract_bytes(monkeypatch):
    conn = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 700.0)
    classification = _classification(network_id="4663", mint="0xMINT")

    decision, pending_id = evm_risk.reserve_entry_evm(
        conn,
        mint="0xMINT",
        source_user_id="u",
        strategy="alpha_b",
        network_id="4663",
        day="2026-07-18",
        usdc_balance=300.0,
        native_balance=1.0,
        now_iso="2026-07-18T12:00:00+00:00",
        attempt_id="attempt-1",
        classification=classification,
    )

    assert decision.allowed and decision.approved_size_usdc == 75.0
    pending = m.pending_row(conn, pending_id)
    attempt = dict(conn.execute(
        "SELECT * FROM memecoin_entry_attempts WHERE attempt_id='attempt-1'").fetchone())
    contract = json.loads(pending["funded_contract_json"])
    assert pending["funded_contract_json"] == attempt["funded_contract_json"]
    assert pending["funded_contract_hash"] == attempt["funded_contract_hash"]
    assert contract["identity"] == {"network_id": "4663", "mint": "0xMINT"}
    assert contract["policy"]["stop_pct"] == 0.0


def test_reservation_carries_liquidity_and_token_age_from_classification_facts(monkeypatch):
    """entry_liquidity/entry_token_age_s were unrecorded on every funded position (2026-09-03
    finding): the classifier already computes both in `facts` and nothing carried them past the
    reservation. This is the plumbing check for reserve_pending -> memecoin_pending_entries."""
    conn = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 700.0)
    classification = _classification()
    classification = replace(
        classification, facts={**classification.facts, "pair_age_s": 42.0})

    decision, pending_id = risk.reserve_entry(
        conn, mint="MINT", source_user_id="u", strategy="alpha_b", day="2026-07-18",
        usdc_balance=300.0, sol_balance=1.0, now_iso="2026-07-18T12:00:00+00:00",
        attempt_id="attempt-1", classification=classification,
    )

    assert decision.allowed
    pending = m.pending_row(conn, pending_id)
    assert pending["source_liquidity"] == 5_000.0
    assert pending["source_token_age_s"] == 42.0


def test_reservation_without_classification_leaves_liquidity_and_age_null(monkeypatch):
    """A reservation with no classifier facts must record NULL, never a fabricated 0 — a written
    0 is indistinguishable from a real zero-liquidity/zero-age reading."""
    conn = _conn()
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)

    decision, pending_id = risk.reserve_entry(
        conn, mint="MINT", source_user_id="u", strategy="control", day="2026-07-18",
        usdc_balance=300.0, sol_balance=1.0, now_iso="2026-07-18T12:00:00+00:00",
    )

    assert decision.allowed
    pending = m.pending_row(conn, pending_id)
    assert pending["source_liquidity"] is None
    assert pending["source_token_age_s"] is None

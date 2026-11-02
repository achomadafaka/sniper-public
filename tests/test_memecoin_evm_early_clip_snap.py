"""The early EVM quote survives a cent of clip drift (Alpha GO 2026-10-17, 9177657c family).

The early quote is sized from the cached balance and the reservation from the fresh one, so the
two clips drift by cents: on 4663 in 7 days 6 of 8 clip_mismatch discards were 1-4 cent drifts
(343df639 early 22.19, reserved 22.21), and each re-ran the ladder serially (9177657c paid a 292 ms
market read plus a 1.1 s Relay re-quote). A reservation whose fresh clip is at most
MEMECOIN_EVM_EARLY_CLIP_SNAP_PCT above the early clip reserves the early clip verbatim, so the
contract, pending notional and spend all equal it and take() matches.

DOWN ONLY: an early clip above the fresh one (9177657c's 82.33 vs 82.32) would break the contract
rule approved <= base x risk_scale, so it stays a mismatch. A real halving (f46ee859, 102.86 vs
51.40) stays a mismatch. The gate runs again on the snapped size; a refusal keeps the fresh one.
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from sniper.persistence import db_memecoin as m
from sniper.services import memecoin_evm_leg as leg
from sniper.services import memecoin_evm_risk as evm_risk
from sniper.services.memecoin_evm_early_quote import EarlyEvmQuote
from sniper.services.memecoin_funded_reconcile import (
    reconcile_severity, validate_funded_contract_reconciliation)
from sniper.services.memecoin_market_state import EntryClassification
from sniper.settings import memecoin as cfg

MINT = "0x" + "7d" * 20
AT = "2026-10-17T07:23:56+00:00"
CUTOVER = "2026-10-17T00:00:00+00:00"
FRESH = 75.0                       # fixed 100 x risk_scale 0.75


def _classification(risk_scale=0.75):
    return EntryClassification(
        classification_version=1, network_id="4663", mint=MINT, signal_source="onchain_watch",
        decision_at=AT, coin_band="small", lifecycle="young", liquidity_class="unknown",
        momentum_phase="climbing", market_regime="hot_narrow", data_quality="partial",
        risk_scale=risk_scale, reason_codes=("coin_band:small",),
        facts={"network_id": "4663", "mint": MINT, "effective_mcap": 500_000.0,
               "liquidity": 5_000.0, "base_clip_usd": None})


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING", "fixed")
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIZING_BY_CHAIN", {})
    monkeypatch.setattr(cfg, "MEMECOIN_TRADE_SIZE_USD", 100.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", 50.0)
    monkeypatch.setattr(cfg, "MEMECOIN_MAX_TOTAL_EXPOSURE_USD", 700.0)
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EARLY_CLIP_SNAP_PCT", 0.1)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    m.start_entry_attempt(c, attempt_id="attempt-1", decision_started_at=AT, network_id="4663",
                          mint=MINT)
    yield c
    c.close()


def _reserve(conn, early, facts=None):
    return evm_risk.reserve_entry_evm(
        conn, mint=MINT, source_user_id="u", strategy="alpha_b", network_id="4663",
        day=AT[:10], usdc_balance=300.0, native_balance=1.0, now_iso=AT, attempt_id="attempt-1",
        classification=_classification(), early_clip_usd=early, snap_facts=facts)


def _early_quote(clip):
    """A real EarlyEvmQuote whose worker sized `clip` and finished its quote."""
    quote = EarlyEvmQuote(mint=MINT, quote_fn=lambda mint, usd: {"chain": "4663", "usd": usd},
                          sizing_fn=lambda: (clip, None, {"balance_source": "cache"}), trace=None)
    quote.worker.join(2)
    assert quote.state == "done" and quote.clip == clip
    return quote


def test_a_cent_drift_below_reserves_the_early_clip_and_the_quote_is_used(conn):
    early, facts = 74.95, {}                       # 0.067% under the fresh 75.00
    decision, pid = _reserve(conn, early, facts)
    assert decision.allowed and decision.approved_size_usdc == early
    assert m.pending_row(conn, pid)["reserved_notional_usd"] == early
    assert facts == {"early_clip_usd": early, "fresh_clip_usd": FRESH, "approved_clip_usd": early,
                     "snapped": True, "reason": None, "snap_pct": 0.1}
    taken = _early_quote(early).take(MINT, decision.approved_size_usdc, max_age_s=5.0)
    assert taken == {"chain": "4663", "usd": early}, "the snapped size is what take() matches"


def test_the_snapped_contract_bytes_match_and_both_reconciles_pass(conn):
    """Gamma 2a: the deploy validator refuses any mismatched cohort row, so the attempt, pending and
    position must carry the same contract, its approved clip the snapped one."""
    decision, pid = _reserve(conn, 74.95)
    pending = m.pending_row(conn, pid)
    attempt = dict(conn.execute("SELECT * FROM memecoin_entry_attempts WHERE attempt_id='attempt-1'")
                   .fetchone())
    contract = json.loads(pending["funded_contract_json"])
    assert contract["sizing"]["approved_clip_usd"] == 74.95
    assert contract["sizing"]["base_clip_usd"] == 100.0
    assert (pending["funded_contract_json"], pending["funded_contract_hash"]) == (
        attempt["funded_contract_json"], attempt["funded_contract_hash"])
    m.open_position(
        conn, source_user_id="u", source_handle="u", mint=MINT, ticker="M", entry_price=1.0,
        entry_notional=74.95, entry_fill_source="chain", entry_lag_s=1.0, their_size_usd=2000.0,
        opened_at="2026-10-17T07:23:58+00:00", strategy="alpha_b", execution_mode="real",
        network_id="4663", attempt_id="attempt-1",
        funded_contract_version=pending["funded_contract_version"],
        funded_contract_hash=pending["funded_contract_hash"],
        funded_contract_json=pending["funded_contract_json"])
    m.confirm_pending(conn, pid)
    assert validate_funded_contract_reconciliation(conn, cutover_at=CUTOVER) == []
    assert reconcile_severity(conn, cutover_at=CUTOVER)[0] == []


@pytest.mark.parametrize("early,reason", [
    (37.5, "outside_tolerance"),          # a real halving (f46ee859 shape)
    (74.90, "outside_tolerance"),         # 0.133%: past the 0.1% band
    (75.01, "early_above_fresh"),         # 9177657c shape: an up-snap breaks base x risk_scale
    (FRESH, "equal"),
])
def test_anything_but_a_small_drift_below_keeps_the_fresh_clip(conn, early, reason):
    facts = {}
    decision, pid = _reserve(conn, early, facts)
    assert decision.allowed and decision.approved_size_usdc == FRESH
    assert m.pending_row(conn, pid)["reserved_notional_usd"] == FRESH
    assert facts["snapped"] is (reason is None) and facts["reason"] == reason


def test_the_real_halving_still_discards_the_early_quote(conn):
    decision, _pid = _reserve(conn, 37.5)
    assert _early_quote(37.5).take(MINT, decision.approved_size_usdc, max_age_s=5.0) is None


@pytest.mark.parametrize("early", [float("nan"), float("inf"), 0.0, -1.0, True, "74.95"])
def test_an_invalid_early_clip_never_snaps(conn, early):
    facts = {}
    decision, _pid = _reserve(conn, early, facts)
    assert decision.approved_size_usdc == FRESH
    assert facts["reason"] == "early_clip_invalid" and facts["early_clip_usd"] is None


def test_no_early_clip_is_today_byte_for_byte(conn):
    facts = {}
    decision, _pid = _reserve(conn, None, facts)
    assert decision.approved_size_usdc == FRESH and facts == {}


def test_the_knob_at_zero_is_off(conn, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_EVM_EARLY_CLIP_SNAP_PCT", 0.0)
    facts = {}
    decision, _pid = _reserve(conn, 74.95, facts)
    assert decision.approved_size_usdc == FRESH and facts["reason"] == "off"


def test_a_snapped_size_the_gate_refuses_keeps_the_fresh_decision(conn, monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_MIN_TRADE_FLOOR_USD", FRESH)   # 74.95 is under the floor
    facts = {}
    decision, _pid = _reserve(conn, 74.95, facts)
    assert decision.allowed and decision.approved_size_usdc == FRESH
    assert facts["snapped"] is False and facts["reason"].startswith("gate: below min-trade floor")


@pytest.mark.parametrize("raw,want", [("0.1", 0.1), ("0", 0.0), ("1", 1.0), ("1.5", 0.0),
                                      ("-0.1", 0.0), ("nan", 0.0), ("x", 0.0), ("", 0.0)])
def test_the_knob_parses_fail_off(raw, want):
    assert cfg._early_clip_snap_pct(raw) == want


# --- the leg offers the early clip only on an automatic first attempt -----------------------------

class _Ev:
    def __init__(self, **kw):
        self.mint, self.source_user_id, self.network_id = MINT, "u1", "4663"
        self.__dict__.update(kw)


class _Trace:
    def __init__(self):
        self.subs = []

    def mark(self, *a, **k):
        pass

    def mark_substage(self, name, **fields):
        self.subs.append((name, fields))


def _leg(monkeypatch, tmp_path, *, ev=None, early=None, attempt_base=1, capacity_clip=None):
    seen = {}

    def reserve(*args, **kwargs):
        seen.update(kwargs)
        if kwargs.get("snap_facts") is not None:
            kwargs["snap_facts"].update(snapped=False, reason="equal")
        return SimpleNamespace(allowed=False, approved_size_usdc=0.0, reason="stop here",
                               clamped=False, duplicate_blocker=None, reentry_lift=None), None
    monkeypatch.setattr(leg, "reserve_entry_evm", reserve)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_ENABLED", True)
    from sniper.services import memecoin_arming
    monkeypatch.setattr(memecoin_arming.cfg, "MEMECOIN_REAL_ENABLED", True)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    m.ensure_memecoin_tables(c)
    trace = _Trace()
    leg.evm_real_entry(
        c, ev or _Ev(), route="evm:4663", day=AT[:10], usdc_balance=300.0, native_balance=1.0,
        now_iso=AT, trade_size=100.0, token_safety_fn=lambda mint, **k: (True, "ok"),
        execute_fn=lambda *a, **k: {"status": "filled"}, kill_path=str(tmp_path / "kill"),
        trace=trace, early_quote=early, trace_attempt_base=attempt_base, capacity_clip=capacity_clip)
    c.close()
    return seen, [f for n, f in trace.subs if n == "evm.clip_snap"]


def _fake_early(clip=74.95, mint=MINT, done_at=None):
    return SimpleNamespace(mint=mint, clip=clip, done_at=done_at, clock=lambda: 100.25)


def test_an_automatic_first_attempt_offers_the_early_clip_and_marks_the_trace(monkeypatch, tmp_path):
    early = _fake_early(done_at=100.0)
    seen, marks = _leg(monkeypatch, tmp_path, early=early)
    assert seen["early_clip_usd"] == 74.95 and seen["snap_facts"] is not None
    assert len(marks) == 1 and marks[0]["reason"] == "equal" and marks[0]["early_done"] is True
    assert marks[0]["early_age_ms"] == pytest.approx(250.0)


@pytest.mark.parametrize("case", ["manual", "override", "capacity", "retry", "other_mint"])
def test_manual_override_capacity_retry_and_another_mint_offer_nothing(monkeypatch, tmp_path, case):
    ev = (_Ev(signal_source="manual") if case == "manual"
          else _Ev(override=True) if case == "override" else _Ev())
    seen, marks = _leg(monkeypatch, tmp_path, ev=ev,
                       early=_fake_early(mint="0x" + "ee" * 20 if case == "other_mint" else MINT),
                       attempt_base=2 if case == "retry" else 1,
                       capacity_clip=50.0 if case == "capacity" else None)
    assert seen["early_clip_usd"] is None and seen["snap_facts"] is None
    assert marks == ([] if case == "retry" else [dict(marks[0], snapped=False, reason="not_eligible")])


def test_an_unsized_early_quote_offers_nothing_and_says_why(monkeypatch, tmp_path):
    seen, marks = _leg(monkeypatch, tmp_path, early=_fake_early(clip=None))
    assert seen["early_clip_usd"] is None
    assert marks[0]["reason"] == "early_clip_unset" and marks[0]["early_done"] is False

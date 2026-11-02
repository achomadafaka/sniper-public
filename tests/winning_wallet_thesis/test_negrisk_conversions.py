"""Step 1 — pure parsing of negRisk CONVERSION receipts into per-sub-market legs.

Drives off the network-free fixture captured in Step 0
(tests/fixtures/negrisk_conversion/fed_december_0x4dac05b0.json): a real
0x5bffcf conversion where NO('50+ bps decrease') was burned and YES minted on
the other three outcomes, $0 collateral (m=1).

Mechanics (NegRiskAdapter, verified): set bits = NO burned (amount each),
unset bits = YES minted (amount each), collateral = (m-1)*amount. The receipt's
NET wallet transfers are the accounting truth (adapter internals ignored).
"""
from __future__ import annotations

import json
import os

import pytest

from sniper.analysis.winning_wallet_thesis import conversions as CV

FIX = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                   "fixtures", "negrisk_conversion", "fed_december_0x4dac05b0.json")


@pytest.fixture
def fed():
    with open(FIX) as f:
        return json.load(f)


def test_token_map_maps_clob_ids_to_submarket_outcomes(fed):
    tm = CV.build_token_map(fed["submarkets"])
    # 4 sub-markets * 2 outcomes = 8 ids
    assert len(tm) == 8
    # every value is (condition_id, outcome_index, label)
    sample = next(iter(tm.values()))
    assert len(sample) == 3


def test_parse_conversion_effect_fed_december(fed):
    tm = CV.build_token_map(fed["submarkets"])
    eff = CV.parse_conversion_effect(fed["receipt_logs"], fed["wallet"], tm)

    # 1 NO burned, 3 YES minted, $0 collateral, amount = activity size
    assert eff.collateral_usdc == pytest.approx(0.0, abs=1e-6)
    assert eff.amount == pytest.approx(fed["amount"], rel=1e-9)
    assert eff.m_no_burned == 1
    assert len(eff.yes_minted) == 3

    burned = {(leg.outcome_label) for leg in eff.no_burned}
    assert burned == {"50+ bps decrease"}
    minted = {leg.outcome_label for leg in eff.yes_minted}
    assert minted == {"25 bps decrease", "No change", "25+ bps increase"}

    # every leg moves exactly `amount` shares; minted are YES, burned are NO
    for leg in eff.yes_minted:
        assert leg.share_delta == pytest.approx(fed["amount"], rel=1e-9)
        assert leg.outcome == "Yes"
    for leg in eff.no_burned:
        assert leg.share_delta == pytest.approx(-fed["amount"], rel=1e-9)
        assert leg.outcome == "No"


def test_collateral_matches_formula(fed):
    """collateral == (m-1)*amount (here m=1 -> 0)."""
    tm = CV.build_token_map(fed["submarkets"])
    eff = CV.parse_conversion_effect(fed["receipt_logs"], fed["wallet"], tm)
    assert eff.collateral_usdc == pytest.approx((eff.m_no_burned - 1) * eff.amount, abs=1e-6)


# --- Step 3: fold conversions into the position book (cost-basis transfer) ---------------
from sniper.analysis.winning_wallet_thesis import position_builder as PB
from sniper.analysis.winning_wallet_thesis.types import (
    EnrichedLedger, LedgerRow, WalletEventLedger,
)


def _episode_by(book, cond, oidx):
    for e in book.episodes:
        if e.condition_id == cond and e.outcome_index == oidx:
            return e
    return None


def test_conversion_transfers_cost_basis_no_to_yes():
    """3-outcome negRisk event A/B/C. Wallet buys NO(A) for $30, converts it (burn NO(A) ->
    mint YES(B)+YES(C), $0 collateral). B wins. Economically the NO(A) bet pays 100 for a
    $30 cost -> +$70. The conversion must transfer the $30 cost to the minted YES legs and
    zero out the burned NO leg, so resolved_with_pnl totals +$70 (not phantom profit)."""
    W = "0xwallet"
    A, B, C = "0xA", "0xB", "0xC"
    led = WalletEventLedger(wallet=W, rows=[
        LedgerRow(kind="buy", wallet=W, condition_id=A, ts=100, seq=0,
                  outcome_index=1, outcome="No", size=100.0, usdc=30.0, event_slug="evt"),
    ])
    eff = CV.ConversionEffect(
        tx="0xc", wallet=W,
        yes_minted=[CV.ConversionLeg(B, 0, "Yes", "B", 100.0),
                    CV.ConversionLeg(C, 0, "Yes", "C", 100.0)],
        no_burned=[CV.ConversionLeg(A, 1, "No", "A", -100.0)],
        collateral_usdc=0.0, amount=100.0, m_no_burned=1, n_outcomes=3)
    enr = EnrichedLedger(ledger=led, conversions=[(200, eff)], resolutions={
        A: {"resolved": True, "winner": 0, "push": False},   # A 'Yes' wins? no — B wins, so A No also...
        B: {"resolved": True, "winner": 0, "push": False},   # B Yes wins
        C: {"resolved": True, "winner": 0, "push": False},   # C Yes loses (winner is its Yes? see below)
    })
    # NOTE: in a negRisk event exactly one sub-market resolves Yes. Model each sub-market's
    # resolution independently: B-Yes wins (winner index 0); A and C resolve No (their Yes loses).
    enr.resolutions = {
        A: {"resolved": True, "winner": 1, "push": False},   # A: 'No' wins (A did not happen)
        B: {"resolved": True, "winner": 0, "push": False},   # B: 'Yes' wins
        C: {"resolved": True, "winner": 1, "push": False},   # C: 'No' wins (C did not happen)
    }
    book = PB.build_position_book(enr)

    a_no = _episode_by(book, A, 1)
    b_yes = _episode_by(book, B, 0)
    c_yes = _episode_by(book, C, 0)
    # burned NO(A): shares + cost released, nets to 0
    assert a_no.realized_pnl == pytest.approx(0.0, abs=1e-6)
    # minted YES legs each carry $15 cost (30 split 2 ways)
    assert b_yes.convert_in_cost == pytest.approx(15.0, abs=1e-6)
    assert c_yes.convert_in_cost == pytest.approx(15.0, abs=1e-6)
    assert b_yes.realized_pnl == pytest.approx(85.0, abs=1e-6)   # won: 100 - 15
    assert c_yes.realized_pnl == pytest.approx(-15.0, abs=1e-6)  # lost: 0 - 15
    total = sum(e.realized_pnl or 0.0 for e in book.resolved_with_pnl())
    assert total == pytest.approx(70.0, abs=1e-6)


def test_conversion_zero_minted_books_collateral():
    """Burn NO on ALL outcomes -> mints ZERO YES, returns (m-1)*amount collateral. The
    collateral is a real cash gain and must NOT be dropped (Codex 2026-06-16 bug). 2-outcome
    event: buy NO(A) $40 + NO(B) $40 (100 sh each); convert burns both NO, collateral $100
    (=(2-1)*100). Holding both NOs is a guaranteed 100 unit (cost $80) -> realized +$20."""
    W = "0xw"; A, B = "0xA", "0xB"
    led = WalletEventLedger(wallet=W, rows=[
        LedgerRow(kind="buy", wallet=W, condition_id=A, ts=100, seq=0, outcome_index=1,
                  outcome="No", size=100.0, usdc=40.0, event_slug="evt"),
        LedgerRow(kind="buy", wallet=W, condition_id=B, ts=101, seq=1, outcome_index=1,
                  outcome="No", size=100.0, usdc=40.0, event_slug="evt"),
    ])
    eff = CV.ConversionEffect(
        tx="0xc", wallet=W, yes_minted=[],
        no_burned=[CV.ConversionLeg(A, 1, "No", "A", -100.0), CV.ConversionLeg(B, 1, "No", "B", -100.0)],
        collateral_usdc=100.0, amount=100.0, m_no_burned=2, n_outcomes=2)
    enr = EnrichedLedger(ledger=led, conversions=[(200, eff)], resolutions={
        A: {"resolved": True, "winner": 0, "push": False},   # A 'Yes' wins -> A 'No' loses
        B: {"resolved": True, "winner": 1, "push": False},   # B 'No' wins
    })
    book = PB.build_position_book(enr)
    total = round(sum(e.realized_pnl or 0.0 for e in book.resolved_with_pnl()), 4)
    assert total == pytest.approx(20.0, abs=1e-6)  # collateral 100 - NO cost 80


def test_load_conversions_offline(fed, tmp_path):
    """load_conversions wires receipt + sub-market fetch into ConversionEffects (network mocked)."""
    raw_activity = [{
        "type": "CONVERSION", "transactionHash": fed["tx"],
        "eventSlug": fed["event_slug"], "timestamp": 1770000000,
        "conditionId": "0xevent", "usdcSize": fed["amount"],
    }]
    effects = CV.load_conversions(
        raw_activity, wallet=fed["wallet"], cache_dir=str(tmp_path),
        receipt_fetch=lambda tx: {"logs": fed["receipt_logs"]},
        submarket_fetch=lambda slug: fed["submarkets"])
    assert len(effects) == 1
    ts, eff = effects[0]
    assert ts == 1770000000
    assert eff.event_slug == fed["event_slug"]
    assert eff.m_no_burned == 1 and len(eff.yes_minted) == 3


def test_load_conversions_no_rows_no_network():
    """Wallets with no CONVERSION activity get [] and make no network calls."""
    def boom(*_a, **_k):
        raise AssertionError("should not fetch")
    assert CV.load_conversions([{"type": "TRADE"}], wallet="0xw",
                               receipt_fetch=boom, submarket_fetch=boom) == []

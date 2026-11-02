"""Tests: CAPITAL_LEDGER_COLLATERAL_MIXED flag emission.

Background:
    Polymarket CLOB V2 (live 2026-04-28) replaced USDC.e collateral with
    pUSD. The capital_ledger resolver in ledger/collateral.py infers
    per-event collateral from timestamp + on-row address. When a wallet's
    capital_ledger spans the cutoff, its events legitimately mix USDC.e
    (pre-cutoff) and pUSD (post-cutoff). Consumers must downweight
    headline `total_deposits_usdc`/`total_withdrawals_usdc` accordingly
    because the 1:1 USD assumption between tokens, while documented as
    enforced onchain, is auditable not free.

    The CAPITAL_LEDGER_COLLATERAL_MIXED flag surfaces this state so
    downstream reviewers (and the calibration review gate) can note the
    caveat in the rationale rather than silently treating mixed-token
    sums as a single homogeneous unit.

Closes part (d) of task #55.
"""
from __future__ import annotations

import pytest

from sniper.analysis.wallet_protocol.analyze_wallet import (
    _compute_capital_ledger_mixed_flag,
)
from sniper.analysis.wallet_protocol.capital_ledger import CapitalLedger


def _ledger(collateral_summary: dict[str, dict[str, float]]) -> CapitalLedger:
    """Synthetic CapitalLedger with caller-supplied collateral_summary.

    Only the fields the flag reads matter; the rest stay at dataclass
    defaults.
    """
    return CapitalLedger(
        wallet="0xtest",
        collateral_summary=collateral_summary,
    )


def test_returns_none_when_capital_ledger_is_none():
    """No flag emitted when capital_ledger surface is unavailable
    (Option B fixtures captured without --capital-ledger)."""
    assert _compute_capital_ledger_mixed_flag(None) is None


def test_returns_none_when_collateral_summary_is_empty():
    """Empty summary (no events captured) → no flag."""
    ledger = _ledger({})
    assert _compute_capital_ledger_mixed_flag(ledger) is None


def test_returns_none_when_only_usdce_present():
    """All pre-cutoff events → single collateral → no MIXED flag."""
    ledger = _ledger({
        "USDC.e": {
            "n_events": 12.0,
            "deposits_usdc": 50_000.0,
            "withdrawals_usdc": 30_000.0,
            "redemption_payout_usdc": 0.0,
        },
    })
    assert _compute_capital_ledger_mixed_flag(ledger) is None


def test_returns_none_when_only_pusd_present():
    """All post-cutoff events → single collateral → no MIXED flag."""
    ledger = _ledger({
        "pUSD": {
            "n_events": 8.0,
            "deposits_usdc": 25_000.0,
            "withdrawals_usdc": 5_000.0,
            "redemption_payout_usdc": 12_000.0,
        },
    })
    assert _compute_capital_ledger_mixed_flag(ledger) is None


def test_fires_when_both_usdce_and_pusd_present():
    """Both tokens active → MIXED flag fires."""
    ledger = _ledger({
        "USDC.e": {
            "n_events": 12.0,
            "deposits_usdc": 50_000.0,
            "withdrawals_usdc": 30_000.0,
            "redemption_payout_usdc": 0.0,
        },
        "pUSD": {
            "n_events": 5.0,
            "deposits_usdc": 10_000.0,
            "withdrawals_usdc": 2_000.0,
            "redemption_payout_usdc": 4_000.0,
        },
    })
    flag = _compute_capital_ledger_mixed_flag(ledger)
    assert flag is not None
    assert flag.startswith("CAPITAL_LEDGER_COLLATERAL_MIXED:")
    # The flag must surface the per-token volumes so reviewers can see
    # how much of the lifetime headline is in each bucket.
    assert "USDC.e" in flag
    assert "pUSD" in flag


def test_does_not_fire_when_one_token_has_zero_activity():
    """Token key with zero net activity (n_events=0) is degenerate; not
    a real mix. Defensive: the resolver should never produce this, but
    fixtures that come from manual edits might."""
    ledger = _ledger({
        "USDC.e": {
            "n_events": 12.0,
            "deposits_usdc": 50_000.0,
            "withdrawals_usdc": 30_000.0,
            "redemption_payout_usdc": 0.0,
        },
        "pUSD": {
            "n_events": 0.0,
            "deposits_usdc": 0.0,
            "withdrawals_usdc": 0.0,
            "redemption_payout_usdc": 0.0,
        },
    })
    assert _compute_capital_ledger_mixed_flag(ledger) is None


def test_unknown_collateral_alone_does_not_fire():
    """The resolver emits `"unknown"` as a sentinel when the row carries
    no usable hint. A wallet with only `unknown` should not fire the
    mix flag — the flag's job is to surface a known multi-token state,
    not to highlight resolver gaps (those have their own coverage caveats).
    """
    ledger = _ledger({
        "unknown": {
            "n_events": 7.0,
            "deposits_usdc": 1_234.0,
            "withdrawals_usdc": 0.0,
            "redemption_payout_usdc": 0.0,
        },
    })
    assert _compute_capital_ledger_mixed_flag(ledger) is None


def test_flag_text_separates_inference_from_peg_verification():
    """Regression: the flag text must NOT contradict the peg-verification
    research note shipped alongside this flag.

    Pre-fix wording said "1:1 USD assumption inferred ... not empirically
    verified" while docs/research/scopes/2026-05-14-pusd-peg-verification.md
    documented an empirical at-peg snapshot from two independent sources
    (±0.15% deviation). The flag must surface BOTH facts separately:
        * Per-event token attribution is INFERRED (via cutoff resolver).
        * pUSD/USDC PEG was snapshot-verified at-peg on 2026-05-14.

    Surfaced 2026-05-14 by Codex audit of commit 024ddf3.
    """
    ledger = _ledger({
        "USDC.e": {
            "n_events": 12.0,
            "deposits_usdc": 50_000.0,
            "withdrawals_usdc": 30_000.0,
            "redemption_payout_usdc": 0.0,
        },
        "pUSD": {
            "n_events": 5.0,
            "deposits_usdc": 10_000.0,
            "withdrawals_usdc": 2_000.0,
            "redemption_payout_usdc": 4_000.0,
        },
    })
    flag = _compute_capital_ledger_mixed_flag(ledger)
    assert flag is not None
    # Inference fact: present
    assert "inferred" in flag.lower()
    assert "cutoff" in flag.lower() or "resolver" in flag.lower()
    # Peg fact: present and dated
    assert "peg" in flag.lower()
    assert "2026-05-14" in flag
    assert "0.15%" in flag
    # Reference to the research note for the next reviewer
    assert "docs/research/scopes/2026-05-14-pusd-peg-verification.md" in flag
    # Forbidden phrasing — the contradiction we are guarding against
    assert "not empirically verified" not in flag.lower()


def test_three_buckets_with_known_pair_fires():
    """USDC.e + pUSD + unknown → flag fires (the known pair is the
    actionable mix). Unknown counts as a third bucket but doesn't
    suppress the warning."""
    ledger = _ledger({
        "USDC.e": {
            "n_events": 10.0,
            "deposits_usdc": 40_000.0,
            "withdrawals_usdc": 25_000.0,
            "redemption_payout_usdc": 0.0,
        },
        "pUSD": {
            "n_events": 3.0,
            "deposits_usdc": 8_000.0,
            "withdrawals_usdc": 1_000.0,
            "redemption_payout_usdc": 0.0,
        },
        "unknown": {
            "n_events": 1.0,
            "deposits_usdc": 200.0,
            "withdrawals_usdc": 0.0,
            "redemption_payout_usdc": 0.0,
        },
    })
    flag = _compute_capital_ledger_mixed_flag(ledger)
    assert flag is not None
    assert "USDC.e" in flag and "pUSD" in flag

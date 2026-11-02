"""scripts/research/wallet_follow_candidates.py: the per-coin return, close and ran rules."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "research"))

import wallet_follow_candidates as W  # noqa: E402


def test_selftest_rules_hold():
    assert W.selftest() == 0


def test_a_wallet_short_of_the_bar_is_excluded_with_its_reasons():
    rows = [{"uid": "u", "chain": "solana", "coins": 4, "closed": 2, "ran": 1, "median_ret": 0.5,
             "win_share": 1.0, "flags": ["bot:hold"]}]
    known = {"u": {"handle": "h", "excluded": True, "armed": {"solana"}, "trials": set()}}
    cands, out = W.screen(rows, known, {}, {}, {("u", "solana"): (5, 0.3, 0.3, 0)}, {})
    assert cands == [] and out[0]["why"] == ["retired", "armed", "bot:hold", "paper<20"]


def test_the_ranking_is_paper_return_and_own_return_never_gates():
    rows = [{"uid": u, "chain": "solana", "coins": 1, "closed": 0, "ran": 0, "median_ret": own,
             "win_share": None, "flags": []} for u, own in (("a", -0.5), ("b", 0.9))]
    paper = {("a", "solana"): (30, 0.20, 0.25, 0), ("b", "solana"): (30, 0.05, 0.04, 2)}
    cands, out = W.screen(rows, {}, {}, {}, paper, {})
    assert [r["uid"] for r in cands] == ["a", "b"] and out == []


def test_a_wallet_funded_already_disproved_gets_no_trial_command():
    assert W.trial_worthy({"paper_ret": 0.23, "funded_n": 32, "funded_ret": -0.001}) is False
    assert W.trial_worthy({"paper_ret": 0.06, "funded_n": 23, "funded_ret": 0.05}) is True
    assert W.trial_worthy({"paper_ret": 0.20, "funded_n": 4, "funded_ret": -0.5}) is True     # too few to say
    assert W.trial_worthy({"paper_ret": -0.01, "funded_n": 0, "funded_ret": None}) is False


def test_a_return_that_prints_as_zero_gets_no_trial_command():
    assert W.trial_worthy({"paper_ret": 0.0005, "funded_n": 1, "funded_ret": -0.4}) is False   # songz
    assert W.trial_worthy({"paper_ret": 0.010, "funded_n": 4, "funded_ret": -0.03}) is True    # frankdegods


# Explicit legacy arithmetic diagnostics. Default publication rejection is tested
# separately by test_wallet_execution_publication and its inventory check.
import pytest as _pytest
from sniper.research.wallet_execution.publication import diagnostic_mode as _diagnostic_mode
@_pytest.fixture(autouse=True)
def _legacy_arithmetic_only():
    with _diagnostic_mode():
        yield

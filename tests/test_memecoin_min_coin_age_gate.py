"""A funded entry may require the coin to already have existed for a while.

MEASURED over 700 funded closes with a known pair age, 30 days to 2026-09-04:
    under 10 min    n=112   -$429.00   median +$0.43   win 52%
    10 min and up   n=588   +$265.58   median +$0.63   win 52%
Per chain the picture differs and the gate is per chain because of it: 4663 under 10 minutes was
n=10 / -$218.55 / median -$21.29 / win 30%, while Solana under 10 minutes was n=102 / -$210.45 but
with a POSITIVE median of +$2.93 and a 54% win rate, its total dragged down by five trades worth
-$350.52. That is a tail an exit rail should own, not proof the entries are bad.
"""
import pytest

from sniper.services.memecoin_risk import coin_age_refusal
from sniper.settings import memecoin as cfg


class _Classification:
    def __init__(self, facts):
        self.facts = facts


def _floor(monkeypatch, **per_chain):
    monkeypatch.setattr(cfg, "min_coin_age_s_for_chain",
                        lambda chain, env=None: float(per_chain.get(str(chain), 0.0)))


def test_no_floor_configured_admits_everything(monkeypatch):
    """The default is off. A gate nobody enabled must never refuse an entry."""
    _floor(monkeypatch)
    assert coin_age_refusal(_Classification({"pair_age_s": 1.0}), "4663") is None


def test_a_coin_younger_than_the_floor_is_refused(monkeypatch):
    _floor(monkeypatch, **{"4663": 600})
    reason = coin_age_refusal(_Classification({"pair_age_s": 61.0}), "4663")
    assert reason is not None
    assert "coin_too_young" in reason
    assert "61s" in reason and "600s" in reason, "the reason must carry both numbers, not just a label"


def test_a_coin_exactly_at_the_floor_is_admitted(monkeypatch):
    """The floor is a minimum, not a strict inequality; a coin that has served its time trades."""
    _floor(monkeypatch, **{"4663": 600})
    assert coin_age_refusal(_Classification({"pair_age_s": 600.0}), "4663") is None
    assert coin_age_refusal(_Classification({"pair_age_s": 600.1}), "4663") is None


def test_the_floor_is_per_chain(monkeypatch):
    """Solana's young bucket has a POSITIVE median, so one chain's floor must not bind another."""
    _floor(monkeypatch, **{"4663": 600, "solana": 0})
    young = _Classification({"pair_age_s": 30.0})
    assert coin_age_refusal(young, "4663") is not None
    assert coin_age_refusal(young, "solana") is None


@pytest.mark.parametrize("facts", [
    {},                                 # no age recorded at all
    {"pair_age_s": None},               # explicitly unknown
    {"pair_age_s": "600"},              # a string is not a reading
    {"pair_age_s": True},               # a bool is not a duration
])
def test_an_unknown_age_admits(monkeypatch, facts):
    """Age is missing on about 9% of funded entries and those carry a POSITIVE median (+$1.02,
    57% win). Refusing on a missing reading would cost more than the floor saves, and treating
    absent as young is the "a number that is absent is not a number that is bad" error."""
    _floor(monkeypatch, **{"4663": 600})
    assert coin_age_refusal(_Classification(facts), "4663") is None


def test_a_missing_classification_admits(monkeypatch):
    _floor(monkeypatch, **{"4663": 600})
    assert coin_age_refusal(None, "4663") is None


def test_the_setting_reads_per_chain_then_global_then_zero():
    env = {"MEMECOIN_REAL_MIN_COIN_AGE_S": "600", "MEMECOIN_REAL_MIN_COIN_AGE_S_solana": "0"}
    assert cfg.min_coin_age_s_for_chain("4663", env) == 600.0
    assert cfg.min_coin_age_s_for_chain("solana", env) == 0.0
    assert cfg.min_coin_age_s_for_chain("4663", {}) == 0.0
    assert cfg.min_coin_age_s_for_chain("4663", {"MEMECOIN_REAL_MIN_COIN_AGE_S": "junk"}) == 0.0
    assert cfg.min_coin_age_s_for_chain("4663", {"MEMECOIN_REAL_MIN_COIN_AGE_S": "-5"}) == 0.0


def test_desk_floor_applies_only_to_entries_without_a_source_wallet():
    """2026-10-26: MEMECOIN_DESK_MIN_COIN_AGE_S sets the floor for desk/operator entries (no source wallet);
    copy entries keep the chain floor; unset keeps today's behaviour."""
    from sniper.settings import memecoin as cfg
    env = {"MEMECOIN_REAL_MIN_COIN_AGE_S": "120", "MEMECOIN_DESK_MIN_COIN_AGE_S": "60"}
    assert cfg.min_coin_age_s_for("solana", None, env=env) == 60.0
    assert cfg.min_coin_age_s_for("solana", "some-wallet-uid", env=env) == 120.0
    assert cfg.min_coin_age_s_for("solana", None, env={"MEMECOIN_REAL_MIN_COIN_AGE_S": "120"}) == 120.0
    assert cfg.min_coin_age_s_for("solana", None, env={**env, "MEMECOIN_DESK_MIN_COIN_AGE_S": "bad"}) == 120.0

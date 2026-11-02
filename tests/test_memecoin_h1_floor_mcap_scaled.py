"""The 4663 hourly dollar floor scales with the coin (operator direction via Alpha, 2026-09-28).

"The 4663 h1 floor is too high for small coins; it must scale with the coin." Gamma's read: 28
under_abs_floor refusals on 4663 since 09-14, nano median organic h1 $1 (dead coins) and 5 near
misses at $1,000 or more. Option (b1): floor = min(MEMECOIN_REAL_MIN_VOLUME_H1_USD, r x effective
mcap) with r = MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663 (default 0 = today's flat $3,000). It only
ever LOOSENS a small coin; an unknown market cap keeps the flat floor; the pair-age pro-rating still
applies on top; the refusal names the floor it used and its basis.

The real card: DopamineFeenFr1's 4663 buy 0x2285b544 at 12:38:25Z. The on-chain lane (99043164)
had mcap $28,360, provider h1 $0 and, since 98f1f641, the tape bound $2,171.33. J7 (28211dc9) had
no market cap at all (their_market_cap 0 on 4663), so it keeps the flat floor.
"""
import pytest

from sniper.services import memecoin_real_runner as R
from sniper.settings import memecoin as cfg

CARD = dict(mcap=28360.0296817611, vol_h1=0.0, bound=2171.332671075248, signal=301.749042078,
            pair_age_s=41123.575331926346)


@pytest.fixture(autouse=True)
def _floors(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_USD", 3_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_TURNOVER_H1", 0.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663", 0.0, raising=False)


def _floor(network_id, mcap, *, band="nano"):
    _a, _b, _c, kwargs = R.volume_gate_args({"volume_h1": CARD["vol_h1"]}, mcap=mcap, network_id=network_id,
                                            coin_band=band, source_user_id="u", signal_usd=CARD["signal"])
    return kwargs["abs_floor"]


def _decide(abs_floor, *, pair_age_s=CARD["pair_age_s"]):
    return R.volume_alive_decision(CARD["vol_h1"], CARD["mcap"], abs_floor=abs_floor, turnover_floor=0.0,
                                   signal_usd=CARD["signal"], h1_floor_lower_bound=CARD["bound"],
                                   pair_age_s=pair_age_s)


def test_the_real_card_is_admitted_at_r_005_and_refused_at_r_0(monkeypatch):
    assert _floor("4663", CARD["mcap"]) == 3_000.0
    assert _decide(3_000.0)[2] == "under_abs_floor"
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663", 0.05, raising=False)
    floor = _floor("4663", CARD["mcap"])
    assert floor == pytest.approx(0.05 * CARD["mcap"])                   # $1,418 < $3,000
    allowed, _need, why = _decide(floor)
    assert allowed and why == "ok"


def test_an_unknown_market_cap_keeps_the_flat_floor(monkeypatch):
    """J7's 28211dc9: the same buy, no market cap on the J7 4663 path."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663", 0.05, raising=False)
    assert _floor("4663", None) == 3_000.0


def test_a_big_coin_keeps_the_flat_floor(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663", 0.05, raising=False)
    assert _floor("4663", 1_000_000.0) == 3_000.0                       # never tightened


@pytest.mark.parametrize("network_id", ["8453", "56", "solana"])
def test_other_chains_are_untouched(monkeypatch, network_id):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663", 0.05, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SOLANA_SMALL_H1_FLOOR_ENABLED", True, raising=False)
    assert _floor(network_id, CARD["mcap"], band="small") == 3_000.0


def test_the_pair_age_pro_rating_still_applies_on_top(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663", 0.05, raising=False)
    floor = _floor("4663", CARD["mcap"])
    _allowed, need, _why = R.volume_alive_decision(0.0, CARD["mcap"], abs_floor=floor, turnover_floor=0.0,
                                                   signal_usd=0.0, pair_age_s=900.0)
    assert need == pytest.approx(0.05 * CARD["mcap"] * 900.0 / 3600.0)


def test_the_floor_note_names_the_basis(monkeypatch):
    assert R.h1_abs_floor_for("4663", CARD["mcap"]) == (3_000.0, None)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663", 0.05, raising=False)
    floor, basis = R.h1_abs_floor_for("4663", CARD["mcap"])
    assert floor == pytest.approx(1418.0, abs=0.01)
    assert basis == "5.00% of mcap $28360"
    assert R.h1_abs_floor_for("4663", 1_000_000.0) == (3_000.0, None)


def test_a_refusal_under_a_scaled_floor_says_so(monkeypatch):
    """Through the real hook: the reason names the scaled floor and its basis."""
    from test_memecoin_h1_floor_young_coin import _hook
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_solana", 0.05, raising=False)
    res, reached = _hook(monkeypatch, age_s=4_000.0, volume=223.0)
    assert res["status"] == "rejected" and not reached
    assert res["reason"] == ("h1 organic volume $1 under floor $282 "
                             "(under_abs_floor, 5.00% of mcap $5635)")


@pytest.mark.parametrize("mcap", [0.0, -5.0, float("nan"), float("inf")])
def test_a_zero_or_invalid_market_cap_keeps_the_flat_floor(monkeypatch, mcap):
    """Zero is not 'a tiny coin': a 0 market cap is a missing read, and must never scale to $0."""
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_MIN_VOLUME_H1_MCAP_RATIO_4663", 0.05, raising=False)
    assert R.h1_abs_floor_for("4663", mcap) == (3_000.0, None)

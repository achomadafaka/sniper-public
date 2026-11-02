"""A Raydium entry quote far above Jupiter's is not trusted (2026-09-27, Alpha GO).

LUCKY ee561e40: Raydium's trade API quoted 1,390,323,206,327 tokens for $131.45, Jupiter
1,049,956,917,310. Raydium "won" the thin-route contest, our min-out came from its number, and the
chain's own route log produced 1,049,802,481,445 (Jupiter's quote within 0.015%) against
minimum_amount_out 1,181,774,725,377: router 0x26. ATqKVV 4b839bbb: Raydium 1.445x Jupiter, refused
at simulation; the same coin 10 s and 33 s later had the two within 0.5%, and the second filled.

The PEECOIN rescue (2026-08-14) is kept: when Jupiter's own impact says its route is dust, a much
larger Raydium answer is the real pool, not a phantom. Exits never pass through the guard: a held
coin's exit value must keep the better venue.
"""
import pytest

from sniper.execution import solana_real_seams as S
from sniper.settings import memecoin as cfg


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    S._jup_host_cooldown.clear()
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK", True)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK_MAX_RATIO", 1.15, raising=False)
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK_DUST_IMPACT", 0.50, raising=False)
    yield
    S._jup_host_cooldown.clear()


def _quotes(monkeypatch, jup, ray):
    monkeypatch.setattr(S, "_jup_quote_status", lambda *a, **k: (jup, 200))
    monkeypatch.setattr(S, "_ray_quote", lambda *a, **k: ray)


def _entry(input_mint=None):
    return S._quote_with_fallback(input_mint or S.USDC_MINT, "MINT", 131_450_000, 1500, None,
                                  tag_evidence=True)


LUCKY = ({"outAmount": "1049956917310", "priceImpactPct": "0.11246754305529104"},
         {"outAmount": "1390323206327", "otherAmountThreshold": "1181774725377", "_venue": "raydium"})
ATQKVV = ({"outAmount": "2627770885302", "priceImpactPct": "0.1311432058961473"},
          {"outAmount": "3796220219857", "_venue": "raydium"})


@pytest.mark.parametrize("pair", [LUCKY, ATQKVV], ids=["LUCKY_ee561e40", "ATqKVV_4b839bbb"])
def test_a_phantom_raydium_win_returns_jupiters_quote(monkeypatch, pair):
    jup, ray = dict(pair[0]), dict(pair[1])
    _quotes(monkeypatch, jup, ray)
    out = _entry()
    assert out is jup, "min-out and every entry gate must read the number the chain matched"
    assert out["_sniper_fallback"]["raydium"] == "distrusted"
    assert out["_sniper_fallback"]["raydium_out_atomic"] == ray["outAmount"]


def test_a_raydium_win_within_the_ratio_still_wins(monkeypatch):
    jup = {"outAmount": "1000000", "priceImpactPct": "0.12"}
    ray = {"outAmount": "1150000", "_venue": "raydium"}          # exactly 1.15x
    _quotes(monkeypatch, jup, ray)
    out = _entry()
    assert out is ray and out["_sniper_fallback"]["raydium"] == "won"


def test_one_atomic_unit_past_the_ratio_is_distrusted(monkeypatch):
    jup = {"outAmount": "1000000", "priceImpactPct": "0.12"}
    ray = {"outAmount": "1150001", "_venue": "raydium"}
    _quotes(monkeypatch, jup, ray)
    assert _entry() is jup


def test_a_peecoin_shaped_dust_route_keeps_the_raydium_rescue(monkeypatch):
    """Jupiter through a $287 dust pool (its own impact > 50%) vs the real Raydium pool."""
    jup = {"outAmount": "100", "priceImpactPct": "0.83"}
    ray = {"outAmount": "999", "_venue": "raydium"}
    _quotes(monkeypatch, jup, ray)
    out = _entry()
    assert out is ray and out["_sniper_fallback"]["raydium"] == "won"


def test_the_exit_side_is_untouched(monkeypatch):
    """A coin -> USDC quote keeps the better venue whatever the ratio: a held coin's exit value
    read from a dust route would look like a crash and fire a stop."""
    jup, ray = dict(LUCKY[0]), dict(LUCKY[1])
    _quotes(monkeypatch, jup, ray)
    out = S._quote_with_fallback("MINT", S.USDC_MINT, 1_000_000, 300, None)
    assert out is ray


def test_a_sol_funded_entry_is_guarded_too(monkeypatch):
    jup, ray = dict(LUCKY[0]), dict(LUCKY[1])
    _quotes(monkeypatch, jup, ray)
    assert _entry(input_mint=S._SOL_MINT) is jup


def test_the_knob_at_zero_restores_todays_rule(monkeypatch):
    monkeypatch.setattr(cfg, "MEMECOIN_SOLANA_VENUE_FALLBACK_MAX_RATIO", 0.0)
    jup, ray = dict(LUCKY[0]), dict(LUCKY[1])
    _quotes(monkeypatch, jup, ray)
    assert _entry() is ray


@pytest.mark.parametrize("jup_out", ["0", None, "abc"])
def test_a_jupiter_size_that_cannot_be_compared_keeps_todays_rule(monkeypatch, jup_out):
    jup = {"priceImpactPct": "0.12"}
    if jup_out is not None:
        jup["outAmount"] = jup_out
    ray = {"outAmount": "999", "_venue": "raydium"}
    _quotes(monkeypatch, jup, ray)
    out = _entry()
    # today's contest: a comparable Jupiter 0 loses to Raydium; an unparseable one keeps Jupiter
    assert out is (ray if jup_out == "0" else jup)

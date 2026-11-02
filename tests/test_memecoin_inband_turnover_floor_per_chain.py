"""The in-band m5 turnover floor is per chain, because the book is per chain.

Measured 2026-09-08 on 635 closed funded positions carrying `volume_m5` and `effective_mcap` in
`classification_json.facts`, turnover = volume_m5 / effective_mcap:

    chain 4663                              chain solana
      <0.01       n= 22  -$191.97             <0.01       n= 25   +$86.11   <- PROFITABLE
      0.01-0.02   n=  8  -$115.71             0.01-0.02   n= 13   -$40.69
      0.02-0.05   n= 16  +$173.94             0.02-0.05   n= 36   -$61.13
      0.05-0.10   n= 20  +$192.21             0.05-0.10   n= 24    +$7.97
      0.10-0.50   n= 51  +$433.10             0.10-0.50   n=168  +$608.86

On 4663 everything below 0.02 loses (-$307.68 over 30) and everything from 0.02 to 0.10 wins
(+$366.15 over 36), so a 0.10 floor refuses profitable trades and a 0.02 floor refuses the losses.
On Solana the same 0.02 floor admits 0.02-0.10, which loses, and its LOWEST bucket is the
profitable one — so on that chain a low floor is the harmful direction. One global number gives one
of the two chains away.

This matters now rather than in principle: the in-band floor only became reachable earlier the same
day (it had been supplied only when `_volume_active`, which is the branch where it cannot be used),
so the global 0.10 was about to start refusing 4663's profitable 0.02-0.10 band for the first time.

Caveats kept deliberately visible: the 4663 cells are n=16 and n=20, and every bucket's PnL mixes
entry quality with the exit overshoot that dominates this book. The floor is supported by two
adjacent positive buckets rather than by one, which is the check that killed the turnover ceiling.
"""
import math

import pytest

from sniper.settings import memecoin as cfg


G = "MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5"
P = G + "_%s"


def f(chain, env):
    return cfg.inband_min_turnover_m5_for_chain(chain, env=env)


# --- the point of the change ---------------------------------------------------------------------

def test_a_chain_key_overrides_the_global():
    env = {G: "0.10", P % "4663": "0.02"}
    assert f("4663", env) == pytest.approx(0.02)
    assert f("solana", env) == pytest.approx(0.10)


def test_the_measured_split_admits_4663s_profitable_band_and_still_refuses_its_losses():
    env = {G: "0.10", P % "4663": "0.02"}
    floor = f("4663", env)
    for winning in (0.021, 0.03, 0.05, 0.099):        # the +$366.15 band
        assert winning >= floor, winning
    for losing in (0.001, 0.009, 0.015, 0.019):       # the -$307.68 band
        assert losing < floor, losing


def test_solana_keeps_the_floor_that_refuses_its_losing_band():
    env = {G: "0.10", P % "4663": "0.02"}
    floor = f("solana", env)
    for losing in (0.012, 0.03, 0.09):                # solana 0.01-0.10 loses
        assert losing < floor, losing


# --- fallbacks, in the order the helper resolves them ---------------------------------------------

def test_an_unlisted_chain_takes_the_global():
    env = {G: "0.10", P % "4663": "0.02"}
    for chain in ("56", "8453", "999"):
        assert f(chain, env) == pytest.approx(0.10)


def test_an_empty_chain_key_falls_through_rather_than_disabling_the_floor():
    """A blank override is a config slip, not an instruction to remove the floor."""
    assert f("4663", {G: "0.10", P % "4663": "   "}) == pytest.approx(0.10)
    assert f("4663", {G: "0.10", P % "4663": ""}) == pytest.approx(0.10)


def test_no_keys_at_all_uses_the_module_default():
    assert f("4663", {}) == pytest.approx(
        float(cfg.MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5))


@pytest.mark.parametrize("bad", ["junk", "nan", "inf", "-inf", "-0.05", "0", None])
def test_a_malformed_or_nonpositive_value_is_no_floor_never_a_guessed_one(bad):
    """NaN is the trap: every `turnover < nan` is False, so a NaN floor disables the gate in
    silence. Returning 0.0 disables it LOUDLY — the caller passes 0 and the decision records it."""
    got = f("4663", {G: "0.10", P % "4663": bad} if bad is not None else {G: "0.10"})
    assert got == (0.0 if bad is not None else pytest.approx(0.10))
    assert math.isfinite(got)


def test_a_malformed_global_does_not_take_a_configured_chain_down_with_it():
    assert f("4663", {G: "junk", P % "4663": "0.02"}) == pytest.approx(0.02)


# --- the runner reads it per chain, not once ------------------------------------------------------

def test_the_funded_gate_passes_the_chain():
    import inspect
    import sniper.services.memecoin_real_runner as R
    src = inspect.getsource(R.real_entry_hook)
    assert "volume_gate_args(" in src and "network_id=identity.network_id" in src
    assert "cfg.inband_min_turnover_m5_for_chain(network_id)" in inspect.getsource(R.volume_gate_args)
    assert 'getattr(cfg, "MEMECOIN_REAL_INBAND_MIN_TURNOVER_M5", 0.0)' not in src, \
        "the global must no longer be read directly at the funded gate"


def test_the_revival_watch_judges_with_the_gate_that_refused(monkeypatch):
    """REVERSED 2026-09-12 (operator "go ahead"). This test used to pin the revival clear as
    "deliberately untouched". Untouched meant it applied NO in-band floor: every in-band refusal
    cleared on its first poll with no data (40 of 40 since 2026-08-31; ITEMS on 4663 read
    `vol_m5 0 >= mcap 0 x 0.000`), re-asked one second later on the same stale snapshot, and never
    filled (0 of 58). The watch now reads the same arguments as the hook, so a stale snapshot stays
    refused and fresh volume clears."""
    import inspect
    import sniper.services.memecoin_real_runner as R
    assert "volume_gate_args(" in inspect.getsource(R.revival_watch_volume_cleared)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_TURNOVER_BAND_MAX_MCAP_USD", 2_000_000.0)
    monkeypatch.setattr(cfg, "MEMECOIN_REAL_SIGNAL_VOLUME_EXCLUDED", True)
    monkeypatch.setattr(cfg, "inband_min_turnover_m5_for_chain",
                        lambda chain_id, env=None: 0.02 if str(chain_id) == "4663" else 0.10)
    monkeypatch.setattr(cfg, "inband_max_turnover_m5_for", lambda chain_id, band, env=None: 0.0)

    class Ev:
        source_user_id = "u"
        their_size_usd = 1986.88
    stale = {"volume_m5": 9.03, "volume_h1": 1975.58, "market_cap": 47_268.0}
    fresh = {"volume_m5": 23_000.0, "volume_h1": 90_000.0, "market_cap": 117_000.0}
    kw = dict(mcap=47_322.0, network_id="4663", coin_band="nano")
    assert not R.revival_watch_volume_cleared(stale, Ev(), **kw)
    assert R.revival_watch_volume_cleared(fresh, Ev(), **kw)

"""The in-band m5 turnover ceiling is per chain AND per coin band. Solana nano only is widened.

Operator-approved 2026-09-08. Measured on closed funded positions, turnover =
volume_m5 / effective_mcap. Splitting by mcap band is what made the signal visible — pooling the
chain hid it:

    solana NANO (<$50k)                     solana MICRO ($50k-250k)
      0.10-0.50  n=64  +$413.09  13.8%        0.10-0.50  n=76  +$153.49   4.2%
      0.50-1.00  n=33  +$203.87  13.0%        0.50-1.00  n=40   +$46.31   2.4%
      1.00-2.00  n=30    -$8.61  -0.7%        1.00-2.00  n=45    +$2.71   0.1%
      2.00+      n=17   -$71.83 -10.2%        2.00+      n=27   -$55.36  -3.6%

On nano the 0.50-1.00 band returns what the band we deliberately target returns (13.0% vs 13.8%)
at a higher win rate (72.7% vs 57.8%), and the losses start at 1.00 rather than 0.50. It survives
removing its two best days (+$27.93 over n=25) — the test the chain-POOLED version failed
(-$20.93), which is why that earlier recommendation was retracted.

The live cases that prompted it: Ozempig (turnover 2.04) reached TP at t=50.6s and peaked at
+115.99% — refused, and STILL refused after this change, because 2.04 is above 1.00. SPHERE
(turnover 1.80) never reached TP and ended -81.00% — also still refused. The widening is narrower
than either of those; it admits 0.50-1.00, which is the band with the evidence behind it.
"""
import pytest

from sniper.settings import memecoin as cfg

G = "MEMECOIN_REAL_INBAND_MAX_TURNOVER_M5"
SOL_NANO = "MEMECOIN_REAL_SOLANA_NANO_INBAND_MAX_TURNOVER_M5"
ENV = {G: "0.50", SOL_NANO: "1.00"}


def f(chain, band, env=ENV):
    return cfg.inband_max_turnover_m5_for(chain, band, env=env)


# --- the approved scope, and nothing wider -------------------------------------------------------

def test_solana_nano_is_widened_to_one():
    assert f("solana", "nano") == pytest.approx(1.00)


@pytest.mark.parametrize("chain,band", [
    ("solana", "micro"), ("solana", "small"), ("solana", "mid"),
    ("4663", "nano"), ("4663", "micro"), ("56", "nano"), ("8453", "nano"),
])
def test_nothing_else_moves(chain, band):
    assert f(chain, band) == pytest.approx(0.50)


def test_assumed_nano_does_not_inherit_the_widening():
    """`_coin_band` returns 'assumed_nano' when the mcap is UNKNOWN. The evidence is about coins we
    could measure as nano; widening on an assumption is the wrong direction."""
    assert f("solana", "assumed_nano") == pytest.approx(0.50)


# --- the two live cases that prompted this -------------------------------------------------------

def test_ozempig_and_sphere_are_both_still_refused():
    """Ozempig 2.04 (a winner we refused) and SPHERE 1.80 (a loser we refused) are both above 1.00.
    This change does not admit either — it admits the 0.50-1.00 band underneath them."""
    ceiling = f("solana", "nano")
    for turnover in (1.80, 2.04):
        assert turnover > ceiling, turnover


def test_the_band_with_the_evidence_is_admitted():
    ceiling = f("solana", "nano")
    for turnover in (0.51, 0.70, 0.99):
        assert turnover <= ceiling, turnover


def test_the_losing_nano_band_is_still_refused():
    """1.00-2.00 is -$8.61 over n=30 and 2.00+ is -$71.83 over n=17."""
    assert 1.01 > f("solana", "nano")


# --- fallbacks and fail-closed --------------------------------------------------------------------

def test_an_unset_chain_band_takes_the_global():
    assert f("solana", "nano", env={G: "0.50"}) == pytest.approx(0.50)


@pytest.mark.parametrize("bad", ["junk", "nan", "inf", "-1", "  "])
def test_a_malformed_override_falls_back_to_the_global_never_to_no_ceiling(bad):
    """0 here means NO CEILING. A config slip must not remove the gate."""
    got = f("solana", "nano", env={G: "0.50", SOL_NANO: bad})
    assert got == pytest.approx(0.50)


def test_a_malformed_global_still_yields_the_module_default():
    got = f("solana", "micro", env={G: "junk"})
    assert got == pytest.approx(float(cfg.MEMECOIN_REAL_INBAND_MAX_TURNOVER_M5))


def test_an_explicit_zero_override_is_honoured_as_no_ceiling():
    """Distinct from malformed: a deliberate 0 disables the ceiling for that cell."""
    assert f("solana", "nano", env={G: "0.50", SOL_NANO: "0"}) == 0.0


def test_a_missing_band_or_chain_takes_the_global():
    for chain, band in (("solana", None), ("solana", ""), (None, "nano"), ("", "nano")):
        assert f(chain, band) == pytest.approx(0.50)


# --- the gate reads the canonical band, not a re-derived cutoff ------------------------------------

def test_the_funded_gate_passes_chain_and_the_canonical_band():
    from _source_guard import code_of
    import sniper.services.memecoin_real_runner as R

    # 2026-09-12: the arguments moved into `volume_gate_args`, shared with the revival watch.
    hook = code_of(R.real_entry_hook)
    assert "network_id=identity.network_id" in hook
    assert "coin_band=getattr(classification, 'coin_band', None)" in hook
    src = code_of(R.volume_gate_args)
    assert "cfg.inband_max_turnover_m5_for(network_id, coin_band)" in src
    assert "m5_ceiling=ceiling" in src
    ceiling = next(line for line in src.splitlines() if 'ceiling =' in line)
    assert '50000' not in ceiling, "The gate must use the canonical band."

"""A route through a drained pump.fun curve is a routing gap, not a verdict on the coin.

GIGACAT (2026-09-23, ref 8c903177) graduated seven minutes after launch. Jupiter had not indexed
the new AMM pool, so its only route ran through the bonding curve -- which held 0.00141732 SOL --
and priced our $175 at 87.16% impact. The cap refused, CORRECTLY for that route. The same $175
quoted at 0.0468% through `Pump.fun Amm` once Jupiter found the pool, and that pool already existed
when we refused. So the refusal was right and the CODE was wrong: it read as a terminal liquidity
judgement on a coin that was fine.

No network.
"""
import pytest

import sniper.execution.solana_swap_executor as X


def _quote(legs, impact="0.8716"):
    return {"priceImpactPct": impact,
            "routePlan": [{"swapInfo": {"label": l, "ammKey": k}} for l, k in legs]}


GIGACAT_CURVE = "8WxTo2mxpDo2upeYNWvAavqEiMKWYbEt5dzNMjXy4GY8"
GIGACAT_AMM = "72tZoSAHAutvD7dP2fNC56aASLxbPXro9jBPohZ8aAvY"


# --- the route reader ---------------------------------------------------------------------------

def test_route_legs_reads_labels_and_amm_keys():
    """The evidence that was never kept. It was on the quote the whole time."""
    q = _quote([("Quantum", "ugobDeUHRY34mq"), ("Pump.fun Amm", GIGACAT_AMM)])
    assert X.route_legs(q) == [("Quantum", "ugobDeUHRY34mq"), ("Pump.fun Amm", GIGACAT_AMM)]


def test_route_legs_tolerates_a_venue_with_no_route_plan():
    """The Raydium fallback carries no routePlan; it must read as no legs, not raise."""
    assert X.route_legs({"priceImpactPct": "0.1"}) == []
    assert X.route_legs(None) == []


# --- the discriminator --------------------------------------------------------------------------

def test_gigacats_exact_shape_is_named(monkeypatch):
    """THE PROPERTY. A `Pump.fun` leg whose curve is drained is the GIGACAT case."""
    q = _quote([("Pump.fun", GIGACAT_CURVE)])
    got = X.completed_curve_in_route(q, lambda k: {"complete": True, "sol": 0.00141732})
    assert got == GIGACAT_CURVE


def test_a_drained_curve_is_caught_by_the_sol_floor_alone(monkeypatch):
    """`complete` may be absent on some curve layouts; the reserve still decides."""
    q = _quote([("Pump.fun", GIGACAT_CURVE)])
    assert X.completed_curve_in_route(q, lambda k: {"sol": 0.0014}) == GIGACAT_CURVE


def test_a_live_un_graduated_curve_is_untouched():
    """A pre-graduation coin routes through its curve every day and that is correct. Refusing it
    here would break the venue this fix exists to protect."""
    q = _quote([("Pump.fun", GIGACAT_CURVE)])
    assert X.completed_curve_in_route(q, lambda k: {"complete": False, "sol": 31.4}) is None


def test_the_graduated_pool_leg_is_not_the_curve():
    """`Pump.fun Amm` is the post-graduation pool -- the route we WANT. It must never match."""
    q = _quote([("Quantum", "x"), ("Pump.fun Amm", GIGACAT_AMM)], impact="0.000468")
    called = []
    assert X.completed_curve_in_route(q, lambda k: called.append(k)) is None
    assert called == [], "the AMM leg must not even be read"


# --- fail-closed --------------------------------------------------------------------------------

def test_no_reader_leaves_the_ordinary_refusal_alone():
    assert X.completed_curve_in_route(_quote([("Pump.fun", GIGACAT_CURVE)]), None) is None


def test_an_unreadable_curve_does_not_invent_a_verdict():
    def _boom(_k):
        raise RuntimeError("rpc down")
    assert X.completed_curve_in_route(_quote([("Pump.fun", GIGACAT_CURVE)]), _boom) is None
    assert X.completed_curve_in_route(_quote([("Pump.fun", GIGACAT_CURVE)]), lambda k: None) is None


def test_the_knob_off_restores_the_previous_behaviour(monkeypatch):
    from sniper.settings import memecoin as cfg
    monkeypatch.setattr(cfg, "MEMECOIN_SOL_CURVE_GRADUATION_RERESOLVE", False, raising=False)
    q = _quote([("Pump.fun", GIGACAT_CURVE)])
    assert X.completed_curve_in_route(q, lambda k: {"complete": True, "sol": 0.0}) is None


# --- the retry lane -----------------------------------------------------------------------------

def test_the_new_code_joins_the_re_quote_lane():
    """The coin becomes routable within minutes, so this must re-ask rather than die. The
    existing 6 s / 60 s cadence is deliberate: whether 60 s suffices is a measurement the route
    logging will answer, not a number picked today."""
    from sniper.services.memecoin_real_runner import _is_transient_entry_miss
    res = {"status": "aborted",
           "reason": "route_via_completed_curve: 87.16% impact priced through a drained "
                     "pump.fun curve 8WxTo2mxpDo2"}
    assert _is_transient_entry_miss(res) is True


def test_an_ordinary_impact_refusal_still_does_not_retry():
    """Re-asking a genuine impact refusal just gets the same answer and costs a quote."""
    from sniper.services.memecoin_real_runner import _is_transient_entry_miss
    assert _is_transient_entry_miss(
        {"status": "aborted", "reason": "price impact 87.16% over 30.0% cap"}) is not True

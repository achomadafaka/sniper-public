"""Tests for the rails_blind context.

A `rails_blind` row used to read identically whether a live rail sat next to the price and could
not be confirmed, or nothing was near enough for an exact price to decide anything. Position 30938
spent four days emitting the second kind and being read as the first.
"""
from __future__ import annotations

import pytest

from sniper.services.memecoin_real_runner import rails_blind_context as ctx

BASE = dict(stop_pct=60.0, tp_pct=0.0, trail_pct=0.0, trail_armed=False,
            derisk_pct=0.0, catastrophe_pct=80.0)


def test_a_live_stop_next_to_the_price_is_consequential():
    r = ctx(pnl_pct=-55.0, **BASE)
    assert r["nearest_rail"] == "stop"
    assert r["nearest_rail_distance_pct"] == pytest.approx(5.0)
    assert r["consequential"] is True


def test_a_stop_already_breached_reads_as_through_it():
    r = ctx(pnl_pct=-62.5, **BASE)
    assert r["nearest_rail"] == "stop"
    assert r["nearest_rail_distance_pct"] == pytest.approx(-2.5)
    assert r["consequential"] is True


def test_the_ETHICS_case_a_disabled_stop_is_NOT_consequential():
    """sl_pct=0 removed the only near rail. Catastrophe at -80 is 17.5 points away, so an exact
    price decides nothing and the demand gate is right to spend none."""
    r = ctx(pnl_pct=-62.5, **{**BASE, "stop_pct": 0.0})
    assert "stop" not in r["active_rails"]
    assert r["nearest_rail"] == "catastrophe"
    assert r["nearest_rail_distance_pct"] == pytest.approx(17.5)
    assert r["consequential"] is False


def test_catastrophe_becomes_consequential_when_it_is_reached():
    r = ctx(pnl_pct=-73.0, **{**BASE, "stop_pct": 0.0})
    assert r["nearest_rail"] == "catastrophe"
    assert r["consequential"] is True


def test_an_armed_trail_is_always_live():
    r = ctx(pnl_pct=+120.0, **{**BASE, "trail_pct": 30.0, "trail_armed": True})
    assert r["nearest_rail"] == "trail"
    assert r["consequential"] is True


def test_an_unarmed_trail_is_not_a_rail_yet():
    r = ctx(pnl_pct=+5.0, **{**BASE, "trail_pct": 30.0, "trail_armed": False})
    assert "trail" not in r["active_rails"]


def test_a_stall_rule_counts_as_live():
    r = ctx(pnl_pct=+40.0, **{**BASE, "stall_rule": {"activate_pct": 30.0}})
    assert "stall" in r["active_rails"] and r["consequential"] is True


def test_take_profit_and_derisk_are_measured_from_above():
    r = ctx(pnl_pct=+35.0, **{**BASE, "tp_pct": 50.0, "derisk_pct": 40.0})
    assert r["nearest_rail"] == "derisk"
    assert r["nearest_rail_distance_pct"] == pytest.approx(5.0)


def test_zero_and_missing_rails_are_excluded_not_treated_as_zero_distance():
    r = ctx(pnl_pct=-10.0, stop_pct=0.0, tp_pct=0.0, trail_pct=0.0, trail_armed=False,
            derisk_pct=0.0, catastrophe_pct=0.0)
    assert r["active_rails"] == [] and r["nearest_rail"] is None
    assert r["consequential"] is False


def test_an_unreadable_price_keeps_configured_rails_and_unknown_proximity():
    for bad in (None, float("nan"), float("inf"), "x"):
        r = ctx(pnl_pct=bad, **BASE)
        assert r["active_rails"] == ['catastrophe', 'stop']
        assert r['price_known'] is False
        assert r['nearest_rail'] is None and r['nearest_rail_distance_pct'] is None
        assert r["consequential"] is False


@pytest.mark.parametrize('pnl,consequential', [(-50., True), (-49.99, False)])
def test_known_price_keeps_the_exact_near_boundary(pnl, consequential):
    r = ctx(pnl_pct=pnl, **BASE)
    assert r['price_known'] is True
    assert r['nearest_rail'] == 'stop'
    assert r['nearest_rail_distance_pct'] == pytest.approx(pnl + 60.)
    assert r['consequential'] is consequential


def test_unknown_price_does_not_guess_distance_for_an_armed_trail_or_stall():
    r = ctx(pnl_pct=None, **{**BASE, 'tp_pct': 30., 'derisk_pct': 40.,
                           'trail_pct': 20., 'trail_armed': True}, stall_rule={'activate_pct': 30.})
    assert r['active_rails'] == ['catastrophe', 'derisk', 'stall', 'stop', 'tp', 'trail']
    assert r['nearest_rail'] is None and r['consequential'] is False

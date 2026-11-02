"""The impulse screen must measure from OUR achievable price, never the trader's fill.

Ranking wallets by their own PnL has been refuted here twice: `their_price` IS the trader's fill,
so every follow-through horizon credits the pump their own buy caused
(finding-memecoin-followthrough-vs-copy-pnl-2026-08-11), and summed PnL ranks whoever held the
biggest lottery ticket (finding-memecoin-single-wallet-roster-2026-08-11).

These pin the properties that keep this screen from repeating either mistake.
"""
from datetime import datetime, timedelta, timezone

import pytest

from scripts.research import wallet_impulse_screen as S


T0 = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def _tape(*offsets_and_prices):
    times, mids = [], []
    for offset, price in offsets_and_prices:
        times.append((T0 + timedelta(seconds=offset)).timestamp())
        mids.append(price)
    return {"MINT": (times, mids)}


def _event(mint="MINT", observed=T0):
    return {"mint": mint, "received_at_normalized": observed.isoformat(),
            "received_at": observed.isoformat()}


def test_the_baseline_is_our_first_available_price_not_their_fill():
    """their_price never enters the calculation. The event dict does not even carry it here."""
    got = S.measure(_tape((5, 100.0), (60, 130.0)), _event(), (60,), 120)
    assert got["responses"][60] == pytest.approx(30.0)
    assert got["baseline_age_s"] == pytest.approx(5.0)


def test_an_unpriceable_event_is_censored_not_scored():
    """No mark inside the budget means we could not have acted, so we do not get to claim it."""
    assert S.measure(_tape((300, 100.0), (360, 200.0)), _event(), (60,), 120) is None


def test_the_baseline_age_budget_is_enforced_at_the_edge():
    assert S.measure(_tape((120, 100.0), (180, 150.0)), _event(), (180,), 120) is not None
    assert S.measure(_tape((121, 100.0), (180, 150.0)), _event(), (180,), 120) is None


def test_a_horizon_with_only_the_baseline_is_not_reported():
    """One mark cannot show a response. Reporting 0.0% would invent a measurement."""
    got = S.measure(_tape((5, 100.0), (400, 150.0)), _event(), (60, 900), 120)
    assert 60 not in got["responses"]
    assert got["responses"][900] == pytest.approx(50.0)


def test_a_falling_market_reports_a_negative_response():
    """The screen must be able to say 'the price drops after this wallet buys' - which is what it
    says for most wallets in the live data."""
    got = S.measure(_tape((5, 100.0), (60, 70.0)), _event(), (60,), 120)
    assert got["responses"][60] == pytest.approx(-30.0)


def test_a_mint_with_no_tape_is_skipped():
    assert S.measure({}, _event(), (60,), 120) is None


def test_the_horizon_reads_the_last_mark_at_or_before_it():
    """Not the nearest, and never one after the horizon - that would look ahead."""
    got = S.measure(_tape((1, 100.0), (30, 120.0), (59, 140.0), (61, 900.0)), _event(), (60,), 120)
    assert got["responses"][60] == pytest.approx(40.0)


def test_the_bootstrap_resamples_mints_not_events():
    """Twenty events on one coin are one observation of the market's reaction, not twenty."""
    one_coin = [{"mint": "A", "responses": {60: 50.0}} for _ in range(20)]
    assert S._mint_clustered_ci(one_coin, 60, 200, 7) == (None, None)

    spread = [{"mint": m, "responses": {60: v}}
              for m, v in zip("ABCDEFGH", (10, 12, 11, 13, 9, 14, 10, 12))]
    lo, hi = S._mint_clustered_ci(spread, 60, 500, 7)
    assert lo is not None and lo < hi


def test_the_day_control_uses_other_wallets_on_the_same_days():
    """A wallet that only buys on days everything pumped must not read as skilful."""
    target = ("uid-a", "alice")
    other = ("uid-b", "bob")
    by_wallet = {
        target: [{"mint": "M1", "day": "2026-08-20", "responses": {60: 40.0}}],
        other: [{"mint": f"N{i}", "day": "2026-08-20", "responses": {60: 38.0}}
                for i in range(12)]
        + [{"mint": "OTHERDAY", "day": "2026-08-19", "responses": {60: -99.0}}],
    }
    control = S.day_control(by_wallet, target, 60)
    assert control == pytest.approx(38.0)        # the 08-19 row is not in alice's day mix


def test_the_day_control_refuses_a_thin_comparison():
    by_wallet = {("a", "a"): [{"mint": "M", "day": "2026-08-20", "responses": {60: 40.0}}],
                 ("b", "b"): [{"mint": "N", "day": "2026-08-20", "responses": {60: 5.0}}]}
    assert S.day_control(by_wallet, ("a", "a"), 60) is None

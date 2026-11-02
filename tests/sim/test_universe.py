from datetime import datetime, timezone, timedelta
from sniper.sim.universe import UniverseFilter, MarketCandidate


def _make_candidate(**overrides) -> MarketCandidate:
    near_future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    defaults = dict(
        condition_id="0xabc",
        question="US strikes Iran by March 31?",
        outcomes=["Yes", "No"],
        volume=50000.0,
        end_date=near_future,
        active=True,
        closed=False,
    )
    defaults.update(overrides)
    return MarketCandidate(**defaults)


def test_accepts_qualifying_market():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate()) is True


def test_rejects_non_binary():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(outcomes=["A", "B", "C"])) is False


def test_accepts_over_under():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(outcomes=["Over", "Under"])) is True


def test_accepts_team_matchup():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(outcomes=["Lakers", "Celtics"])) is True


def test_rejects_single_outcome():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(outcomes=["Yes"])) is False


def test_rejects_low_volume():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(volume=5000.0)) is False


def test_rejects_closed():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(closed=True)) is False


def test_rejects_inactive():
    f = UniverseFilter()
    assert f.qualifies(_make_candidate(active=False)) is False


def test_rejects_far_end_date():
    f = UniverseFilter()
    far_future = (datetime.now(timezone.utc) + timedelta(days=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f.qualifies(_make_candidate(end_date=far_future)) is False


def test_accepts_near_end_date():
    f = UniverseFilter()
    near_future = (datetime.now(timezone.utc) + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f.qualifies(_make_candidate(end_date=near_future)) is True


def test_rejects_already_ended():
    f = UniverseFilter()
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f.qualifies(_make_candidate(end_date=past)) is False


def test_rejects_90_days_plus_hours():
    f = UniverseFilter()
    barely_over = (datetime.now(timezone.utc) + timedelta(days=90, hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert f.qualifies(_make_candidate(end_date=barely_over)) is False


def test_cap_enforcement():
    f = UniverseFilter(max_active=3)
    candidates = [_make_candidate(condition_id=f"0x{i}") for i in range(5)]
    accepted = [c for c in candidates if f.qualifies(c)]
    assert len(accepted) == 5  # all qualify individually
    capped = f.apply_cap(accepted)
    assert len(capped) == 3

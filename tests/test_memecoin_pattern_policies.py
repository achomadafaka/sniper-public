from datetime import datetime, timedelta, timezone

import pytest

from sniper.research.memecoin_strategy.pattern_context import ReentrySignal
from sniper.research.memecoin_strategy.pattern_policies import (
    POLICIES,
    match_past_control,
    rank_policies,
    simulate_policy,
)


START = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _quote(index, minutes, price, quality="ok"):
    return {
        "id": index,
        "observed_at": (START + timedelta(minutes=minutes)).isoformat(),
        "price": price,
        "quality": quality,
    }


def _path(quotes, *, complete=True, notional=100):
    return {
        "path_id": "p",
        "network_id": "solana",
        "mint": "M",
        "signal_at": START.isoformat(),
        "entry_price": 1.0,
        "notional": notional,
        "observations": quotes,
        "complete": complete,
    }


def test_full_exit_policy_uses_conservative_order_stop_confirmations_and_costs():
    at = (START + timedelta(minutes=4)).isoformat()
    quotes = [
        {"id": 1, "observed_at": at, "price": 2.0, "quality": "ok"},
        _quote(2, 1, 0.50),
        _quote(3, 2, 0.49),
        _quote(4, 3, 0.48),
        {"id": 5, "observed_at": at, "price": 0.47, "quality": "ok"},
    ]
    result = simulate_policy(
        _path(quotes), "tp60_sl50x4_24h", entry_bps=100, exit_bps=100,
    )
    assert result.valid and result.reason == "stop"
    assert result.exit_price == pytest.approx(0.47)
    assert result.gross_pnl == pytest.approx(-53)
    assert result.net_pnl == pytest.approx(-54.47)
    assert result.capital_deployed == 100
    assert result.net_return == pytest.approx(-0.5447)


def test_timecap_uses_last_quote_before_deadline_and_rejects_incomplete_path():
    result = simulate_policy(
        _path([_quote(1, 30, 1.1), _quote(2, 370, 2.0)]),
        "timecap_6h", entry_bps=0, exit_bps=0,
    )
    assert result.reason == "timecap" and result.exit_price == pytest.approx(1.1)
    invalid = simulate_policy(
        _path([_quote(1, 1, 2.0)], complete=False),
        "tp60_sl50x4_24h", entry_bps=0, exit_bps=0,
    )
    assert not invalid.valid and invalid.reason == "incomplete_path"


def test_principal_runner_recovers_notional_at_actual_crossed_quote_then_exits_runner():
    result = simulate_policy(
        _path([_quote(1, 1, 2.0), _quote(2, 2, 2.6)]),
        "principal_at_60_runner_150", entry_bps=0, exit_bps=0,
    )
    assert result.valid and result.reason == "runner_tp"
    assert len(result.legs) == 2
    assert result.legs[0]["reason"] == "principal"
    assert result.legs[0]["quantity"] == pytest.approx(50)
    assert result.legs[0]["proceeds"] == pytest.approx(100)
    assert result.legs[1]["quantity"] == pytest.approx(50)
    assert result.gross_pnl == pytest.approx(130)


def test_principal_runner_charges_entry_and_each_exit_cost():
    result = simulate_policy(
        _path([_quote(1, 1, 1.6), _quote(2, 2, 1.0)]),
        "principal_at_60_runner_150", entry_bps=100, exit_bps=100,
    )
    assert result.reason == "timecap"
    assert result.net_pnl == pytest.approx(result.gross_pnl - 1 - sum(
        leg["proceeds"] * 0.01 for leg in result.legs
    ))


def test_stop_then_reentry_counts_both_notionals_and_never_fills_at_low():
    quotes = [
        _quote(1, 1, 0.50), _quote(2, 2, 0.49), _quote(3, 3, 0.48), _quote(4, 4, 0.47),
        _quote(5, 5, 0.40), _quote(6, 6, 0.49), _quote(7, 7, 1.0),
    ]
    reentry = ReentrySignal(
        confirmed_at=(START + timedelta(minutes=5)).isoformat(),
        entry_at=(START + timedelta(minutes=6)).isoformat(),
        entry_price=0.49,
        post_stop_low=0.40,
        distinct_buyers=("a", "b"),
        source_event_ids=(10, 11),
        entry_quote_id=6,
    )
    result = simulate_policy(
        _path(quotes), "stop_then_fresh_conviction_reentry",
        entry_bps=0, exit_bps=0, reentry=reentry,
    )
    assert result.valid and result.reason == "reentry_tp"
    assert result.capital_deployed == 200
    assert result.legs[0]["price"] == pytest.approx(0.47)
    assert result.legs[1]["entry_price"] == pytest.approx(0.49)
    assert result.legs[1]["entry_price"] != reentry.post_stop_low
    expected = (-53 + (1.0 / 0.49 - 1) * 100) / 200
    assert result.net_return == pytest.approx(expected)


def test_reentry_policy_does_not_reenter_after_tp_or_without_signal():
    path = _path([_quote(1, 1, 2.0)])
    no_signal = simulate_policy(
        path, "stop_then_fresh_conviction_reentry", entry_bps=0, exit_bps=0,
    )
    assert no_signal.reason == "tp" and no_signal.capital_deployed == 100


def _episode(index, *, minutes, mint=None, lane="v2", recovery=False):
    return {
        "episode_id": f"e{index}",
        "lane": lane,
        "network_id": "solana",
        "mint": mint or f"M{index}",
        "signal_at": (START + timedelta(minutes=minutes)).isoformat(),
        "market_cap_band": "<1M",
        "liquidity_band": "50-100k",
        "initial_15m_return_band": "20-40",
        "archetype": "deep_recovery" if recovery else "terminal_failure",
    }


def test_control_is_past_only_same_strata_different_mint_and_nearest():
    case = _episode(10, minutes=30, recovery=True)
    candidates = [
        _episode(1, minutes=10),
        _episode(2, minutes=25),
        _episode(3, minutes=31),
        _episode(4, minutes=29, mint=case["mint"]),
        _episode(5, minutes=28, lane="legacy"),
    ]
    assert match_past_control(case, candidates)["episode_id"] == "e2"
    assert match_past_control(case, [_episode(3, minutes=31)]) is None


def test_policy_ranking_blocks_thin_groups_and_enforces_loss_guard():
    thin = [{"policy_name": name, "mint": f"M{i}", "net_return": value}
            for i, value in enumerate((0.1, 0.2)) for name in ("base",)]
    assert rank_policies(thin, baseline_name="base")["status"] == "blocked_protocol"

    rows = []
    for index in range(20):
        rows.extend([
            {"policy_name": "base", "mint": f"M{index}", "net_return": -0.5 if index < 2 else 0.1},
            {"policy_name": "safe", "mint": f"M{index}", "net_return": -0.4 if index < 2 else 0.2},
            {"policy_name": "reckless", "mint": f"M{index}", "net_return": -0.9 if index < 3 else 0.5},
        ])
    ranked = rank_policies(rows, baseline_name="base")
    assert ranked["status"] == "blocked_protocol"
    assert ranked["winner"] is None
    assert ranked["policies"]["reckless"]["eligible"] is False
    assert set(POLICIES) >= {"tp60_sl50x4_24h", "principal_at_60_runner_150"}


import pytest as _pytest
from sniper.research.wallet_execution.publication import diagnostic_mode as _diagnostic_mode
@_pytest.fixture(autouse=True)
def _explicit_legacy_diagnostics():
    with _diagnostic_mode():
        yield

from sniper.analysis.winning_wallet_thesis.types import EdgeCluster, PositionBook, PositionEpisode
from sniper.analysis.winning_wallet_thesis.validation.power import estimate_forward_power


def _ep(i, ts):
    return PositionEpisode("0xw", f"c{i}", f"e{i}", 0, 0, buy_shares=1, buy_cost=.4,
                           resolved=True, won=1, first_buy_ts=ts)


def _cluster(eps):
    return EdgeCluster("c", "test", {}, "tag", len(eps), len(eps), 1, 5,
                       [(e.condition_id, e.outcome_index, e.episode_index) for e in eps])


def test_forward_power_requires_enough_expected_events():
    # SPARSE cadence: 10 events spread over ~270 days -> ~1 expected in a 30-day horizon
    eps = [_ep(i, 1_700_000_000 + i * 30 * 86400) for i in range(10)]
    power = estimate_forward_power(PositionBook("0xw", eps), _cluster(eps),
                                   horizon_days=30, min_events=20)
    assert power.powered is False
    assert power.expected_events < 20


def test_forward_power_powered_for_dense_cadence():
    # 40 events over ~40 days -> ~1/day -> ~30 in a 30-day horizon -> powered at min 20
    eps = [_ep(i, 1_700_000_000 + i * 86400) for i in range(40)]
    power = estimate_forward_power(PositionBook("0xw", eps), _cluster(eps),
                                   horizon_days=30, min_events=20)
    assert power.powered is True and power.expected_events >= 20

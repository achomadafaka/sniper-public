"""E2 step 1 (spec 2026-10-08-memecoin-evm-entry-gates-onchain-design.md, Gamma's 16 rows).

When the provider has no volume, our own tape's measured sum still decides what it can PROVE.
The sum is a lower bound: the source's own transaction is excluded and only known pools are counted.
- A floor it reaches is proven passed. A floor it misses is unknown, never a refusal.
- It never decides the ceiling: the ceiling was fitted on one pair's m5, the tape sums every pool.
- An unknown leg keeps today's posture: `no_volume_data`, admitted.
Example: 4663 attempt 9046798a (2026-10-08) had provider h1/m5 None and a tape sum of $1,235.64 over
11 swaps on a $21,783 coin. It logged `no_volume_data` although the tape proved its m5 floor and
its trade-count floor.
"""
from sniper.services.memecoin_real_runner import volume_alive_decision

MC = 21_783.37
FLOORS = dict(abs_floor=0.0, turnover_floor=0.0, m5_floor=0.005, m5_ceiling=2.0,
              m5_band_max_mcap=2_000_000.0, m5_txns_floor=2)


def _decide(**kw):
    args = {**FLOORS, **kw}
    return volume_alive_decision(args.pop("vol_h1", None), args.pop("mcap", MC), **args)


def test_the_9046798a_shape_is_proven_by_our_tape_not_no_volume_data():
    allow, _bar, reason = _decide(m5_floor_lower_bound=1235.64, h1_floor_lower_bound=1235.64,
                                  m5_txns_lower_bound=11)
    assert allow and reason == "own_tape_proven", reason


def test_a_tape_sum_over_the_ceiling_never_refuses():
    """The ceiling was fitted on ONE pair's m5; our tape sums every known pool of the coin, and
    arbitrage pools run up to 20x a pair's count. So the sum is no bound on what the ceiling
    measures, and a bound may admit, never refuse (test_a_lower_bound_can_admit_but_never_refuse)."""
    allow, _bar, reason = _decide(m5_floor_lower_bound=MC * 2.5, m5_txns_lower_bound=11)
    assert allow and reason == "own_tape_proven", reason


def test_a_tape_sum_under_a_floor_is_unknown_not_a_refusal():
    allow, _bar, reason = _decide(m5_floor_lower_bound=10.0, m5_txns_lower_bound=0,
                                  turnover_floor=0.05, h1_floor_lower_bound=10.0)
    assert allow and reason == "no_volume_data", reason


def test_the_hourly_turnover_floor_is_proven_by_the_tape_bound():
    allow, bar, reason = _decide(turnover_floor=0.03, h1_floor_lower_bound=MC * 0.04,
                                 m5_floor_lower_bound=MC * 0.04, m5_txns_lower_bound=11)
    assert allow and reason == "own_tape_proven" and bar == MC * 0.03


def test_the_flat_hourly_floor_without_an_mcap_is_proven_by_the_tape_bound():
    allow, _bar, reason = _decide(mcap=None, abs_floor=3000.0, h1_floor_lower_bound=3500.0,
                                  m5_txns_lower_bound=11)
    assert allow and reason == "own_tape_proven", reason


def test_the_flat_hourly_floor_under_the_bound_stays_unknown():
    allow, _bar, reason = _decide(mcap=None, abs_floor=3000.0, h1_floor_lower_bound=500.0)
    assert allow and reason == "no_volume_data", reason


def test_a_required_trade_count_is_proven_by_the_tape_count():
    allow, _bar, reason = _decide(m5_txns_required=True, m5_txns_lower_bound=3,
                                  m5_floor_lower_bound=500.0)
    assert allow and reason == "own_tape_proven", reason


def test_a_required_trade_count_the_tape_cannot_reach_stays_unreadable():
    allow, _bar, reason = _decide(m5_txns_required=True, m5_txns_lower_bound=1)
    assert not allow and reason == "m5_txns_unreadable"


def test_the_provider_number_still_decides_when_present():
    """Step 1 changes nothing where the provider answered: its m5 above the ceiling refuses, and
    the tape bound cannot lower it."""
    allow, _bar, reason = _decide(vol_h1=50_000.0, vol_m5=MC * 3, m5_txns=40,
                                  m5_floor_lower_bound=10.0)
    assert not allow and reason == "m5_turnover_over_ceiling"
    allow, _bar, reason = _decide(vol_h1=50_000.0, vol_m5=MC * 0.5, m5_txns=40,
                                  m5_floor_lower_bound=10.0)
    assert allow and reason == "ok"


# --- Gamma REFUSE on 6e08fb97a: the label claims the tape only when every active leg is proven ---------

def test_a_proven_trade_count_with_an_unknown_volume_floor_is_not_proven():
    allow, _bar, reason = _decide(m5_floor_lower_bound=10.0, m5_txns_lower_bound=5)
    assert allow and reason == "no_volume_data", reason


def test_without_an_mcap_an_unproven_flat_floor_keeps_no_volume_data_even_with_trades():
    allow, _bar, reason = _decide(mcap=None, abs_floor=3000.0, h1_floor_lower_bound=500.0,
                                  m5_txns_lower_bound=5)
    assert allow and reason == "no_volume_data", reason
    allow, bar, reason = _decide(mcap=None, abs_floor=3000.0, h1_floor_lower_bound=3500.0,
                                 m5_txns_lower_bound=5)
    assert allow and reason == "own_tape_proven" and bar == 3000.0, reason


def test_the_trade_count_bound_is_one_pools_transactions_not_the_sum():
    from sniper.services.memecoin_real_runner import onchain_m5_txns_lower_bound
    ev = {"reason": "ok", "usd": 100.0, "swaps": 9,
          "pools": [{"pool": "a", "swaps": 5, "txns": 5},
                    {"pool": "b", "swaps": 4, "txns": 4}]}
    assert onchain_m5_txns_lower_bound(ev) == 5
    assert onchain_m5_txns_lower_bound({"reason": "ok", "usd": 1.0, "swaps": 3,
                                      "txns": 3}) == 3  # BSC: one pool
    assert onchain_m5_txns_lower_bound({"reason": "feed_stale", "usd": None, "swaps": 3}) is None

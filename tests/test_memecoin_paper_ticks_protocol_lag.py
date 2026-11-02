"""A fresh chain header does not make a delayed protocol stream current (2026-10-08 review C09).

EvmTapeTicks.healthy() checked only volume_feed.received (the header socket) and open tick gaps,
so with the V4 log stream 120 s behind it carried the pool's last swap as a current tick. Health is
now bound to the position's protocol (V4 for a 32-byte pool id, V3 otherwise) with the writer's own
volume_feed_lag, by the rule the 4663 display marks already use (memecoin_evm_tape_marks): measured
within LAG_TTL_S and no more than LAG_MAX_S. A missing measurement is unknown, never fresh. A quiet
pool on a measured, current stream still carries its last swap.
"""
import sqlite3

import pytest

from sniper.services import memecoin_paper_ticks as PT
from test_memecoin_paper_ticks import ETH_USD, MINT, POOL, T0, _tape

V3_POOL = "0x" + "c3" * 20
POS = {"network_id": "4663", "mint": MINT}


def _tick(path, pool=POOL, now=T0 + 1.0):
    tape = sqlite3.connect(path)
    if pool != POOL:
        tape.execute("UPDATE pool_meta SET pool=?", (pool,))
        tape.execute("UPDATE pool_ticks SET pool=?", (pool,))
    try:
        return PT.EvmTapeTicks(lambda chain, token: ETH_USD).tick(tape, POS, pool, now)
    finally:
        tape.close()


def test_fresh_headers_with_a_delayed_v4_stream_give_no_v4_tick(tmp_path):
    tick, why = _tick(_tape(tmp_path, lag=(120.0, 1.0)))
    assert tick is None and why == "protocol_lag"


def test_a_delayed_v4_stream_does_not_stop_a_current_v3_pool(tmp_path):
    tick, why = _tick(_tape(tmp_path, lag=(120.0, 1.0)), pool=V3_POOL)
    assert why is None and tick.tick_kind == "v3_spot"


def test_a_delayed_v3_stream_stops_the_v3_pool_only(tmp_path):
    (tmp_path / "v3").mkdir()
    (tmp_path / "v4").mkdir()
    assert _tick(_tape(tmp_path / "v3", lag=(1.0, 120.0)), pool=V3_POOL)[1] == "protocol_lag"
    assert _tick(_tape(tmp_path / "v4", lag=(1.0, 120.0)))[1] is None


@pytest.mark.parametrize("lag", [(None, 1.0), (None, None)])
def test_an_unmeasured_protocol_is_unknown_never_fresh(tmp_path, lag):
    tick, why = _tick(_tape(tmp_path, lag=lag))
    assert tick is None and why == "protocol_lag_unknown"


def test_a_stale_lag_measurement_is_unknown(tmp_path):
    tick, why = _tick(_tape(tmp_path, lag_observed=T0 - 600.0))
    assert tick is None and why == "protocol_lag_unknown"


def test_a_store_from_before_the_lag_table_is_unknown_not_a_crash(tmp_path):
    tick, why = _tick(_tape(tmp_path, lag_table=False))
    assert tick is None and why == "protocol_lag_unknown"


def test_a_quiet_pool_on_a_measured_current_stream_still_carries_its_last_swap(tmp_path):
    """The pool's last swap was minutes ago, but its protocol's stream is measured and current."""
    from datetime import datetime, timezone
    path = _tape(tmp_path)
    old = datetime.fromtimestamp(T0 - 600.0, timezone.utc).isoformat()
    c = sqlite3.connect(path)
    c.execute("UPDATE pool_ticks SET observed_at=?", (old,))
    c.commit()
    c.close()
    tick, why = _tick(path, now=T0 + 1.0)
    assert why is None and tick.at == T0
    assert tick.observed_at == pytest.approx(T0 - 600.0)   # the swap itself is old: carried


def test_an_open_gap_still_wins_and_a_resolved_gap_clears(tmp_path):
    path = _tape(tmp_path, gap=True)
    assert _tick(path)[1] == "tick_gap"
    c = sqlite3.connect(path)
    c.execute("UPDATE tick_gaps SET resolved_at='2026-10-08T07:00:00+00:00'")
    c.commit()
    c.close()
    assert _tick(path)[1] is None

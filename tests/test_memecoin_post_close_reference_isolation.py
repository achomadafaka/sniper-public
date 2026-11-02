"""An old bag's observation series cannot veto a new bag's live exit price."""
import pytest

from sniper.services import memecoin_exits as exits
from sniper.services.memecoin_evm_post_close_marks import SOURCE
from test_memecoin_exits import _conn


@pytest.mark.parametrize("live_source", [None, "fastlane_rpc", "legacy_null"])
def test_post_close_observation_never_becomes_exit_guard_reference(live_source):
    conn = _conn()
    key = ("4663", "reentered-mint")
    if live_source is not None:
        exits.write_marks(conn, {key: {"price": 1.0, "source": (
            None if live_source == "legacy_null" else live_source)}},
            "2026-09-24T12:00:00+00:00", min_interval_s=0)
    exits.write_marks(conn, {key: {"price": 100.0, "source": SOURCE}},
                      "2026-09-24T12:00:01+00:00", min_interval_s=0)
    now = "2026-09-24T12:00:02+00:00"
    try:
        assert exits.last_accepted_mid(conn, *key, now) == (
            None if live_source is None else 1.0)
        live_tick = {key: {"price": 1.0, "source": "fastlane_rpc"}}
        assert exits.filter_implausible_ticks(conn, live_tick, now) == live_tick
    finally:
        conn.close()

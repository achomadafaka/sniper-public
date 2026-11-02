"""A retired venue must not produce new data."""

from __future__ import annotations

import scripts.memecoin_opportunity_worker as worker
from sniper.research.memecoin_opportunity.types import VenueId
from sniper.research.memecoin_opportunity.venue_policy import (
    active_capture_venues,
    is_active_capture_venue,
)


BAGS = VenueId.BAGS_ROBINHOOD_4663
PONS = VenueId.PONS_V1_ROBINHOOD_4663


def _bags_log(topic: str) -> dict[str, object]:
    return {
        "topics": [topic],
        "address": "0x1111111111111111111111111111111111111111",
        "blockNumber": "0x1",
        "transactionIndex": "0x0",
        "logIndex": "0x0",
        "transactionHash": "0xdead",
        "data": "0x",
    }


def test_a_retired_venue_contributes_no_topics():
    assert not is_active_capture_venue(BAGS)
    assert worker._BAGS_4663_TOPICS == ()
    # The combined capture set excludes the retired topics.
    for topic in worker._ALL_BAGS_4663_TOPICS:
        assert topic not in worker._EVM_CAPTURE_TOPICS


def test_an_active_venue_keeps_its_topics():
    assert is_active_capture_venue(PONS)
    assert worker._PONS_4663_TOPICS
    for topic in worker._PONS_4663_TOPICS:
        assert topic in worker._EVM_CAPTURE_TOPICS


def test_a_retired_venues_log_resolves_to_no_venue():
    """The log resolver rejects each retired topic."""
    for topic in worker._ALL_BAGS_4663_TOPICS:
        assert worker._evm_log_venue(_bags_log(topic), {}) is None


def test_no_get_logs_call_is_emitted_for_a_retired_venue():
    """An empty `eth_getLogs` topic array is a wildcard."""
    assert worker._BAGS_4663_TOPICS == ()
    # No request can contain an empty topic position.
    for topics in (worker._BAGS_4663_TOPICS, worker._PONS_4663_TOPICS):
        assert topics == () or all(topics)


def test_recovery_for_a_retired_venue_issues_no_rpc_call_at_all(tmp_path):
    """The RECOVERY path selected `()` and then sent `topics: [[]]` -- a wildcard.

    `eth_getLogs` reads an empty topic array as "match anything", so the retired lane
    requested EVERY log on the chain instead of none. Asserting on constants did not
    catch this: only driving the path does.
    """
    import asyncio

    from sniper.research.memecoin_opportunity.store import connect

    conn = connect(tmp_path / "op.db")
    # A frontier with a BLOCK-ADDRESSABLE cursor is required, otherwise recovery returns
    # early for an unrelated reason and the wildcard path is never reached. An earlier
    # version of this test passed against the unguarded code for exactly that reason.
    conn.execute(
        "INSERT INTO op_source_frontiers("
        "source_key,durable_cursor,resume_cursor,latest_observed_cursor,has_open_gap,"
        "version,updated_at) VALUES(?,?,?,?,1,1,?)",
        (
            f"{BAGS.value}:ws", "100:0:0", "100:0:0", "100:0:0",
            "2026-08-19T13:00:00.000000Z",
        ),
    )
    calls: list[tuple[str, object]] = []

    async def rpc_call(_gov, _priority, _url, method, params):
        calls.append((method, params))
        return "0x0"

    class _Runtime:
        def __init__(self, conn):
            self.conn = conn

        def heartbeat(self, *_a, **_k):
            pass

        def mark_recovered(self, *_a, **_k):
            pass

    original = worker._rpc_call
    worker._rpc_call = rpc_call
    try:
        asyncio.run(
            worker._recover_evm_frontier(
                conn, _Runtime(conn),
                rpc_url="https://fixture.invalid",
                max_pages=1,
                venue=BAGS,
            )
        )
    finally:
        worker._rpc_call = original

    assert calls == [], f"a retired venue must issue no RPC call, got {calls}"


def test_the_topic_map_covers_every_4663_venue():
    """A new 4663 venue must not silently default to 'no topics'."""
    mapped = set(worker._TOPICS_BY_4663_VENUE)
    assert {BAGS, PONS} <= mapped
    for venue in active_capture_venues():
        if venue in mapped:
            assert worker._active_4663_topics(venue)


def test_active_topics_follow_the_registry_not_a_hand_list():
    assert worker._active_4663_topics(BAGS) == ()
    assert worker._active_4663_topics(PONS) == worker._ALL_PONS_4663_TOPICS


def test_the_quote_status_writer_skips_a_retired_venue(tmp_path):
    from sniper.research.memecoin_opportunity.store import connect

    conn = connect(tmp_path / "op.db")
    worker._publish_quote_path_status_rows(
        conn,
        {
            BAGS.value: {"status": "healthy", "queue_depth": 0, "detail": {}},
            PONS.value: {"status": "healthy", "queue_depth": 0, "detail": {}},
        },
        now_iso="2026-08-19T13:30:00Z",
    )
    keys = {
        row[0]
        for row in conn.execute(
            "SELECT source_key FROM op_runtime_status WHERE source_key LIKE 'opportunity:quote:%'"
        )
    }
    assert keys == {f"opportunity:quote:{PONS.value}"}
    conn.close()

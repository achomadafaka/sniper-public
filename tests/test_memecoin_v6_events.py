from dataclasses import replace

import pytest

from sniper.services.memecoin_v6_events import (
    V6FeedCursor, V6FeedHealth, V6MarketEvent, event_identity,
)

NOW = "2026-07-26T12:00:00+00:00"
LATER = "2026-07-26T12:00:00.050+00:00"


def _event(**changes):
    event = V6MarketEvent(
        network_id="4663", pool="0xpool", block_height=10, block_hash="0xblock",
        tx_id="0xtx", event_index=7, source_event_at=NOW, received_at=LATER,
        kind="v2_swap", payload={"amount_in": 1},
    )
    return replace(event, **changes)


def test_evm_event_identity_includes_reorg_anchor():
    assert event_identity(_event()) != event_identity(_event(block_hash="0xreorg"))


def test_solana_event_identity_includes_slot_signature_and_index():
    event = _event(network_id="solana", block_height=123, block_hash=None,
                   tx_id="sig-1", event_index=2)
    assert event_identity(event) != event_identity(replace(event, event_index=3))


def test_event_cursor_and_health_reject_malformed_contracts():
    with pytest.raises(ValueError):
        _event(received_at="naive")
    with pytest.raises(ValueError):
        V6FeedCursor("4663", "pool", -1, "event")
    with pytest.raises(ValueError):
        V6FeedHealth("poll", True, False, NOW, -1)

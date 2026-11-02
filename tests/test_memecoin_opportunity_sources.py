from __future__ import annotations

import base64
import json
import struct
from dataclasses import replace

import pytest

from eth_abi import encode

from sniper.research.memecoin_opportunity.store import connect
from sniper.research.memecoin_opportunity.types import ConfirmationState, VenueId
from sniper.research.memecoin_opportunity.bags_solana import BAGS_DBC_PROGRAM
from sniper.research.memecoin_opportunity.decoders import base58_encode
from sniper.research.memecoin_opportunity.pons_4663 import PONS_FACTORY, TOKEN_LAUNCHED, V3_SWAP
from sniper.services.memecoin_opportunity_sources import (
    OpportunitySourceEnvelope,
    VenueIdentityMap,
    process_envelope,
)


def _pump_complete_log() -> str:
    user = bytes([3]) * 32
    mint = bytes([1]) * 32
    curve = bytes([2]) * 32
    quote = bytes([4]) * 32
    raw = bytes([95, 114, 97, 156, 212, 46, 152, 8]) + user + mint + curve + struct.pack("<q", 1_700_000_000) + quote
    return "Program data: " + base64.b64encode(raw).decode()


def _pump_create_log() -> str:
    text = lambda value: struct.pack("<I", len(value)) + value
    mint = bytes([1]) * 32
    curve = bytes([2]) * 32
    user = bytes([3]) * 32
    creator = bytes([5]) * 32
    token_program = bytes([6]) * 32
    quote_mint = bytes([4]) * 32
    raw = (
        bytes([27, 114, 169, 77, 222, 235, 99, 118])
        + text(b"name") + text(b"SYM") + text(b"uri")
        + mint + curve + user + creator
        + struct.pack("<qQQQQ", 1_700_000_000, 1_000_000, 500_000, 900_000, 1_000_000)
        + token_program + bytes([0, 0]) + quote_mint + struct.pack("<Q", 500_000)
    )
    return "Program data: " + base64.b64encode(raw).decode()


def _envelope(**overrides):
    values = dict(
        source_key="pumpfun:provider-a",
        source_provider="provider-a",
        source_cursor="100:tx:0",
        venue_id=VenueId.PUMPFUN_SOLANA,
        program_or_contract_id="6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        transaction_id="5TxSignature",
        block_or_slot=100,
        transaction_index=2,
        event_index=0,
        block_hash="5BlockHash",
        chain_timestamp="2026-07-26T12:00:00Z",
        received_at="2026-07-26T12:00:00.100000Z",
        confirmation=ConfirmationState.CONFIRMED,
        payload={"log_line": _pump_complete_log()},
        token_hint="unknown:5TxSignature",
    )
    values.update(overrides)
    return OpportunitySourceEnvelope(**values)


def test_process_envelope_persists_raw_before_canonical_and_is_idempotent(tmp_path):
    conn = connect(tmp_path / "op.db")
    try:
        result = process_envelope(conn, _envelope(), identities=VenueIdentityMap())
        assert result.raw_inserted is True
        assert result.canonical_inserted is True
        assert result.blocker is None
        assert conn.execute("SELECT COUNT(*) FROM op_raw_events").fetchone()[0] == 1
        canonical = conn.execute("SELECT event_kind,lifecycle,token_id FROM op_canonical_events").fetchone()
        assert tuple(canonical)[:2] == ("migration", "migrated")

        duplicate = process_envelope(conn, _envelope(), identities=VenueIdentityMap())
        assert duplicate.raw_inserted is False
        assert duplicate.canonical_inserted is False
    finally:
        conn.close()


def test_unknown_version_keeps_raw_fact_and_returns_blocker(tmp_path):
    conn = connect(tmp_path / "op.db")
    try:
        result = process_envelope(
            conn,
            _envelope(program_or_contract_id="foreign"),
            identities=VenueIdentityMap(),
        )
        assert result.blocker == "UNKNOWN_PROGRAM_OR_CONTRACT_VERSION"
        assert conn.execute("SELECT COUNT(*) FROM op_raw_events").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM op_canonical_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_envelope_content_identity_changes_when_payload_changes():
    left = _envelope()
    right = _envelope(payload={"log_line": _pump_complete_log(), "extra": True})
    assert left.raw_event_id != right.raw_event_id
    assert json.loads(left.raw_payload_json)["log_line"].startswith("Program data: ")


def test_recovery_and_buffered_live_observations_share_one_canonical_event(tmp_path):
    conn = connect(tmp_path / "op.db")
    identities = VenueIdentityMap()
    recovery = _envelope(
        source_provider="solana_rpc_recovery",
        received_at="2026-07-26T12:00:00.100000Z",
        confirmation=ConfirmationState.TENTATIVE,
    )
    buffered = _envelope(
        source_provider="solana_logs_subscribe",
        received_at="2026-07-26T12:00:00.300000Z",
        confirmation=ConfirmationState.TENTATIVE,
    )

    first = process_envelope(conn, recovery, identities=identities)
    second = process_envelope(conn, buffered, identities=identities)

    assert recovery.raw_event_id != buffered.raw_event_id
    assert first.canonical_event_id == second.canonical_event_id
    assert first.canonical_inserted is True
    assert second.canonical_inserted is False
    assert conn.execute("SELECT count(*) FROM op_raw_events").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM op_canonical_events").fetchone()[0] == 1


def test_process_envelope_applies_optional_point_in_time_enrichment(tmp_path):
    conn = connect(tmp_path / "op.db")
    result = process_envelope(
        conn,
        _envelope(),
        identities=VenueIdentityMap(),
        decoded_enricher=lambda _envelope, decoded: replace(decoded, progress_bps=3_700),
    )

    assert result.decoded.progress_bps == 3_700
    assert conn.execute(
        "SELECT progress_bps FROM op_canonical_events"
    ).fetchone()[0] == 3_700


def test_pons_same_transaction_swap_can_precede_launch_log(tmp_path):
    conn = connect(tmp_path / "op.db")
    identities = VenueIdentityMap()
    token = "0x1111111111111111111111111111111111111111"
    deployer = "0x2222222222222222222222222222222222222222"
    dex_factory = "0x3333333333333333333333333333333333333333"
    pair = "0x4444444444444444444444444444444444444444"
    pool = "0x5555555555555555555555555555555555555555"
    topic = lambda value: "0x" + "0" * 24 + value[2:]
    launch_log = {
        "address": PONS_FACTORY,
        "topics": [TOKEN_LAUNCHED, topic(token), topic(deployer), topic(dex_factory)],
        "data": "0x" + encode(
            ["address", "address", "uint256", "uint256", "uint256", "uint256", "uint256"],
            [pair, pool, 1, 2, 3, 4, 5],
        ).hex(),
    }
    swap_log = {
        "address": pool,
        "topics": [V3_SWAP, topic(deployer), topic(deployer)],
        "data": "0x" + encode(
            ["int256", "int256", "uint160", "uint128", "int24"],
            [-100, 20, 2**96, 1_000, 0],
        ).hex(),
    }

    swap = process_envelope(
        conn,
        _envelope(
            source_key="pons_v1_4663:ws",
            source_cursor="100:2:0",
            venue_id=VenueId.PONS_V1_ROBINHOOD_4663,
            program_or_contract_id=pool,
            transaction_id="0xaaa",
            block_hash="0xbbb",
            payload={
                "log": swap_log,
                "pons_token_id": token,
                "pons_pair_token": pair,
            },
            token_hint=pool,
        ),
        identities=identities,
    )
    launch = process_envelope(
        conn,
        _envelope(
            source_key="pons_v1_4663:ws",
            source_cursor="100:2:1",
            venue_id=VenueId.PONS_V1_ROBINHOOD_4663,
            program_or_contract_id=PONS_FACTORY,
            transaction_id="0xaaa",
            block_hash="0xbbb",
            event_index=1,
            payload={"log": launch_log},
            token_hint=token,
        ),
        identities=identities,
    )

    assert launch.blocker is None
    assert identities.pons_pool_tokens[pool] == (token, pair)
    assert swap.blocker is None
    assert swap.decoded.event_kind.value == "buy"
    assert conn.execute(
        "SELECT COUNT(*) FROM op_canonical_events WHERE venue_id='pons_v1_4663'"
    ).fetchone()[0] == 2


def test_bags_pool_scoped_token_hint_decodes_swap_without_prior_launch(tmp_path):
    conn = connect(tmp_path / "op.db")
    pool_raw, config_raw = bytes([10]) * 32, bytes([11]) * 32
    pool, mint = base58_encode(pool_raw), base58_encode(bytes([13]) * 32)
    payload = b"".join(
        (
            pool_raw,
            config_raw,
            b"\x01\x00",
            struct.pack("<QQ", 25_000, 90_000),
            struct.pack("<QQ", 25_000, 100_000),
            (123456789).to_bytes(16, "little"),
            struct.pack("<QQQ", 200, 20, 0),
            struct.pack("<QQ", 25_000, 1_700_000_001),
        )
    )
    log_line = "Program data: " + base64.b64encode(
        bytes([27, 60, 21, 213, 138, 170, 187, 147]) + payload
    ).decode()
    identities = VenueIdentityMap()

    result = process_envelope(
        conn,
        _envelope(
            source_key="bags_solana:ws",
            venue_id=VenueId.BAGS_SOLANA,
            program_or_contract_id=BAGS_DBC_PROGRAM,
            payload={"log_line": log_line, "pool_id": pool},
            token_hint=mint,
        ),
        identities=identities,
    )

    assert result.blocker is None
    assert result.decoded.token_id == mint
    assert identities.token_by_solana_pool[pool] == mint


def test_process_envelope_keeps_only_raw_cursor_after_launch_capture_window(tmp_path):
    conn = connect(tmp_path / "op.db")
    identities = VenueIdentityMap()
    first = process_envelope(
        conn,
        _envelope(payload={"log_line": _pump_create_log()}),
        identities=identities,
        capture_window_s=120,
    )
    late = process_envelope(
        conn,
        _envelope(
            source_cursor="221:tx:0",
            transaction_id="5LateSignature",
            block_or_slot=221,
            chain_timestamp="2026-07-26T12:02:01Z",
            received_at="2026-07-26T12:02:01.100000Z",
        ),
        identities=identities,
        capture_window_s=120,
    )

    assert late.blocker is None
    assert late.raw_inserted is True
    assert late.canonical_inserted is False
    assert conn.execute("SELECT COUNT(*) FROM op_raw_events").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM op_canonical_events").fetchone()[0] == 1


def test_candidate_launch_extends_capture_to_300_seconds_only(tmp_path):
    conn = connect(tmp_path / "op.db")
    identities = VenueIdentityMap()
    first = process_envelope(
        conn,
        _envelope(payload={"log_line": _pump_create_log()}),
        identities=identities,
        capture_window_s=120,
        candidate_capture_window_s=300,
    )
    launch = conn.execute(
        "SELECT launch_id,network_id,venue_id,token_id FROM op_launches"
    ).fetchone()
    conn.execute(
        "INSERT INTO op_forward_alerts "
        "(alert_id,launch_id,network_id,venue_id,token_id,snapshot_id,observed_at,"
        "clip_usd_micros,rule_version,quote_provider,round_trip_return_bps,truth_label,"
        "row_fingerprint,inserted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "candidate-alert",
            launch["launch_id"],
            launch["network_id"],
            launch["venue_id"],
            launch["token_id"],
            "snapshot-fixture",
            "2026-07-26T12:00:05Z",
            25_000_000,
            "pump-ignition-v1",
            "fixture",
            -500,
            "fixture",
            "fingerprint",
            "2026-07-26T12:00:05Z",
        ),
    )

    within = process_envelope(
        conn,
        _envelope(
            source_cursor="350:tx:0",
            transaction_id="WithinCandidateWindow",
            block_or_slot=350,
            chain_timestamp="2026-07-26T12:04:10Z",
            received_at="2026-07-26T12:04:10.100000Z",
        ),
        identities=identities,
        capture_window_s=120,
        candidate_capture_window_s=300,
    )
    beyond = process_envelope(
        conn,
        _envelope(
            source_cursor="401:tx:0",
            transaction_id="BeyondCandidateWindow",
            block_or_slot=401,
            chain_timestamp="2026-07-26T12:05:01Z",
            received_at="2026-07-26T12:05:01.100000Z",
        ),
        identities=identities,
        capture_window_s=120,
        candidate_capture_window_s=300,
    )

    assert first.canonical_inserted is True
    assert within.canonical_inserted is True
    assert beyond.canonical_inserted is False


# --- lane survivability -------------------------------------------------------
# A capture lane reports its own transport failures from inside its `except` handler.
# That handler is the one place that cannot afford to raise: an exception there escapes
# the reconnect loop and ends the coroutine, and because the Task reference is held
# until shutdown Python never logs it. Four Bags shards vanished that way for 17h on
# 2026-08-04 with a runtime row frozen at `healthy`.

def test_lane_failure_report_survives_a_broken_gap_ledger():
    """A ledger write that raises must not propagate out of the report."""
    import scripts.memecoin_opportunity_worker as worker

    heartbeats = []

    class _Runtime:
        conn = object()

        def heartbeat(self, source_key, *, status, blocker_code, detail=None):
            heartbeats.append((source_key, status, blocker_code))

    def _boom(conn, *, source_key, observed_at, detail):
        raise RuntimeError("op_data_gaps.gap_id conflicts")

    original = worker.record_source_failure
    worker.record_source_failure = _boom
    try:
        worker._report_lane_failure(_Runtime(), "bags_solana:ws", "ConnectionClosedError")
    finally:
        worker.record_source_failure = original

    # The frontier write failed, but the board still learns the source is degraded.
    assert heartbeats == [("bags_solana:ws", "degraded", "SOURCE_FAILURE")]


def test_lane_failure_report_survives_a_broken_heartbeat():
    import scripts.memecoin_opportunity_worker as worker

    class _Runtime:
        conn = object()

        def heartbeat(self, *args, **kwargs):
            raise RuntimeError("database is locked")

    recorded = []
    original = worker.record_source_failure
    worker.record_source_failure = lambda conn, **kw: recorded.append(kw["source_key"])
    try:
        worker._report_lane_failure(_Runtime(), "pumpfun_solana:ws", "boom")
    finally:
        worker.record_source_failure = original

    assert recorded == ["pumpfun_solana:ws"]


def test_supervised_lane_restarts_a_crashing_lane_and_stops_on_stop():
    """A lane that raises anything must be restarted, not left dead."""
    import asyncio

    import scripts.memecoin_opportunity_worker as worker

    stop = asyncio.Event()
    attempts = []

    async def _lane():
        attempts.append(len(attempts))
        if len(attempts) >= 3:
            stop.set()
            return
        raise RuntimeError("gap ledger conflict inside the error handler")

    async def _run():
        await asyncio.wait_for(
            worker._supervise_lane(_lane, label="bags:0", stop=stop, backoff_s=0.0),
            timeout=5,
        )

    asyncio.run(_run())
    assert len(attempts) == 3


def test_supervised_lane_restarts_a_lane_that_returns_early():
    """Returning without `stop` is a silent blackout too, not a clean exit."""
    import asyncio

    import scripts.memecoin_opportunity_worker as worker

    stop = asyncio.Event()
    attempts = []

    async def _lane():
        attempts.append(len(attempts))
        if len(attempts) >= 2:
            stop.set()

    async def _run():
        await asyncio.wait_for(
            worker._supervise_lane(_lane, label="evm", stop=stop, backoff_s=0.0),
            timeout=5,
        )

    asyncio.run(_run())
    assert len(attempts) == 2


def test_supervised_lane_propagates_cancellation():
    import asyncio

    import scripts.memecoin_opportunity_worker as worker

    stop = asyncio.Event()

    async def _lane():
        await asyncio.sleep(60)

    async def _run():
        task = asyncio.create_task(
            worker._supervise_lane(_lane, label="pump", stop=stop, backoff_s=0.0)
        )
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
